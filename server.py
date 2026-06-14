import base64
import html
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import requests
from flask import Flask, Response, request
import json

from easynews_client import EasynewsClient, EasynewsError, SearchItem


# ---------------------------------------------------------------------------
# Logging setup — plain, human-readable output for Docker logs
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Suppress noisy library logs
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

APP = Flask(__name__)
_CLIENT: Optional[EasynewsClient] = None
_CLIENT_LOCK = threading.Lock()
_CLIENT_LOGIN_TTL = 1800  # seconds between session refreshes
_CLIENT_LAST_LOGIN: float = 0.0
_CLIENT_REFRESHING = False  # guard so only one background refresh runs at a time


def _retry_request(
    fn,
    max_retries=3,
    base_delay=1.0,
    max_delay=15.0,
):
    """Exponential backoff + jitter for transient HTTP failures."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except (requests.exceptions.RequestException, EasynewsError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = min(
                    base_delay * (2 ** attempt) + random.uniform(0, base_delay),
                    max_delay,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[arg-type]


def _load_dotenv():
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
    except Exception:
        pass


_load_dotenv()

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}



API_KEY = os.environ.get("NEWZNAB_APIKEY", "testkey")
EZ_USER = os.environ.get("EASYNEWS_USER")
EZ_PASS = os.environ.get("EASYNEWS_PASS")

# When enabled, season-pack queries (a season with NO episode, e.g. "The Boys
# S05") are skipped and return no results. Easynews rarely carries real season
# packs, so this stops them from polluting season searches. Episode and movie
# searches are unaffected.
IGNORE_SEASON_PACKS = _env_bool("IGNORE_SEASON_PACKS", False)


def require_apikey() -> bool:
    key = request.args.get("apikey") or request.headers.get("X-Api-Key")
    return (API_KEY is None) or (key == API_KEY)


def _refresh_login_async() -> None:
    """
    Re-login in the background so a search is never blocked on a (sometimes slow
    or flaky) Easynews login. HTTP Basic Auth stays on the session, so searches
    keep working with the existing session while this runs.
    """
    global _CLIENT_LAST_LOGIN, _CLIENT_REFRESHING
    try:
        _CLIENT.login()  # type: ignore[union-attr]
        with _CLIENT_LOCK:
            _CLIENT_LAST_LOGIN = time.time()
        logger.info("Background session refresh succeeded.")
    except EasynewsError as e:
        # Retry sooner than a full TTL next time, but keep the existing session.
        with _CLIENT_LOCK:
            _CLIENT_LAST_LOGIN = time.time() - (_CLIENT_LOGIN_TTL - 60)
        logger.warning(
            "Background session refresh failed: %s. "
            "Keeping existing session (HTTP Basic Auth) — searches still work. "
            "Will retry in ~60s.",
            e,
        )
    finally:
        with _CLIENT_LOCK:
            _CLIENT_REFRESHING = False


def client() -> EasynewsClient:
    if not EZ_USER or not EZ_PASS:
        raise RuntimeError("Set EASYNEWS_USER and EASYNEWS_PASS environment variables")
    global _CLIENT, _CLIENT_LAST_LOGIN, _CLIENT_REFRESHING
    with _CLIENT_LOCK:
        now = time.time()
        if _CLIENT is None:
            # First-time startup — must succeed before we can serve anything.
            logger.info("Starting up: logging in to Easynews for the first time...")
            _CLIENT = EasynewsClient(EZ_USER, EZ_PASS)
            _CLIENT.login()
            _CLIENT_LAST_LOGIN = now
            logger.info("Startup login succeeded. Indexer is ready.")
            return _CLIENT

        # Periodic refresh runs in a background thread so the current request is
        # never blocked on the login round-trip. This is what was causing the
        # downstream (NZBHydra) ~4s timeouts whenever a refresh coincided with a
        # search and Easynews was slow to answer the login.
        if (
            now - _CLIENT_LAST_LOGIN > _CLIENT_LOGIN_TTL
            and not _CLIENT_REFRESHING
        ):
            age_mins = int((now - _CLIENT_LAST_LOGIN) / 60)
            logger.info(
                "Session is %d min old (TTL=%ds). Refreshing login in background...",
                age_mins, _CLIENT_LOGIN_TTL,
            )
            _CLIENT_REFRESHING = True
            # Push the timestamp forward now so we don't spawn a thread per request
            # while this refresh is in flight; the thread sets the real time on success.
            _CLIENT_LAST_LOGIN = now
            threading.Thread(
                target=_refresh_login_async, name="ez-login-refresh", daemon=True
            ).start()
        return _CLIENT


def _warm_up_login() -> None:
    """
    Log in once at startup, in the background, so the first real search after a
    container (re)start doesn't pay the blocking login cost (which previously
    pushed that first request past NZBHydra's timeout).
    """
    try:
        client()
    except Exception as e:  # noqa: BLE001 - never crash startup over a warm-up
        logger.warning(
            "Startup warm-up login failed (will retry on first request): %s", e
        )


if EZ_USER and EZ_PASS:
    threading.Thread(target=_warm_up_login, name="ez-warmup", daemon=True).start()


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def encode_id(item: dict) -> str:
    payload = {
        "hash": item.get("hash"),
        "filename": item.get("filename"),
        "ext": item.get("ext"),
        "sig": item.get("sig"),
        "title": item.get("title"),
    }
    if item.get("sample"):
        payload["sample"] = True
    raw = (
        base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode())
        .decode()
        .rstrip("=")
    )
    return raw


def decode_id(enc: str) -> dict:
    pad = "=" * (-len(enc) % 4)
    raw = base64.urlsafe_b64decode(enc + pad).decode()
    return json.loads(raw)


def to_search_item(d: dict) -> SearchItem:
    return SearchItem(
        id=None,
        hash=d["hash"],
        filename=d["filename"],
        ext=d["ext"],
        sig=d.get("sig"),
        type="VIDEO",
        raw={},
    )


_TITLE_PARENS_RE = re.compile(r"\(([^()]*)\)")


def _normalize_title(raw: str) -> str:
    text = html.unescape(raw or "").strip()
    if not text:
        return text
    matches = _TITLE_PARENS_RE.findall(text)
    for candidate in reversed(matches):
        cleaned = candidate.strip()
        if cleaned:
            return cleaned
    return text


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


_ALLOWED_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov",
    ".wmv", ".mpg", ".mpeg", ".flv", ".webm",
}

_STOPWORDS = {"the", "a", "an", "and", "of", "in", "for", "on"}

_MIN_DURATION_SECONDS = 60
_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)
_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360)\s*(p|i)?", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_SEASON_EP_RE = re.compile(
    r"(?:s(?P<season>\d{1,2})e(?P<episode>\d{1,2})|(?<!\d)(?P<season2>\d{1,2})x(?P<episode2>\d{1,2})(?!\d))",
    re.IGNORECASE,
)
# Bare season marker in a query, e.g. "From S04" / "From.S04" (no episode). This
# is NOT matched by _SEASON_EP_RE (which needs SxxExx), and the bare "s04" token
# never appears in episode filenames ("...s04e08..."), so a season-only search
# would otherwise match nothing. We parse the season here and match via metadata.
_SEASON_ONLY_RE = re.compile(
    r"(?:^|[\s\.\-_])s(?P<season>\d{1,2})(?=$|[\s\.\-_])", re.IGNORECASE
)
_SEASON_TOKEN_RE = re.compile(r"^s\d{1,2}$", re.IGNORECASE)
_ANIME_BRACKET_GROUP_RE = re.compile(r"^\[([^\]]+)\]", re.IGNORECASE)

_KNOWN_FANSUB_GROUPS = {
    "subsplease", "erai-raws", "horriblesubs", "judas", "gjm",
    "commiesubs", "commie", "animekaizoku", "anime time", "asenshi",
    "damedesuyo", "gg", "fff", "underwater", "ember", "kametsu",
    "kawaiika", "mezashite", "reinforce", "senritsu", "vivid",
    "coalgirls", "utw", "thora", "ohys-raws", "leopard-raws",
    "asw", "mtbb", "anime-time",
}
_SANITIZE_SYMBOLS_RE = re.compile(r"[\.\-_:\s]+")
_NON_ALNUM_RE = re.compile(r"[^\w\sÀ-ÿ]")

CATEGORY_MOVIES = 2000
CATEGORY_MOVIES_HD = 2030
CATEGORY_MOVIES_UHD = 2040
CATEGORY_TV = 5000
CATEGORY_TV_HD = 5030
CATEGORY_TV_UHD = 5040
CATEGORY_ANIME = 5070
CATEGORY_OTHER = 7000


def _parse_duration_seconds(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw <= 0:
            return None
        return int(raw)
    text = str(raw).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    matched = False
    for label, multiplier in (("h", 3600), ("m", 60), ("s", 1)):
        for part in re.findall(rf"(\d+)\s*{label}", text):
            total += int(part) * multiplier
            matched = True
    if matched:
        return total
    if ":" in text:
        try:
            pieces = [int(p) for p in text.split(":")]
            if len(pieces) == 3:
                h, m, s = pieces
            elif len(pieces) == 2:
                h = 0
                m, s = pieces
            else:
                return None
            return h * 3600 + m * 60 + s
        except ValueError:
            return None
    return None


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    normalized = _TOKEN_SPLIT_RE.sub(" ", text.lower())
    tokens = [
        tok for tok in normalized.split() if len(tok) > 1 and tok not in _STOPWORDS
    ]
    return tokens


def _sanitize_phrase(text: str) -> str:
    if not text:
        return ""
    working = text.replace("&", " and ")
    working = _SANITIZE_SYMBOLS_RE.sub(" ", working)
    working = _NON_ALNUM_RE.sub("", working)
    return working.lower().strip()


def _is_flagged_item(item: Any, ext: str, duration_seconds: Optional[int]) -> bool:
    passwd = False
    virus = False
    file_type = ""
    if isinstance(item, dict):
        passwd = bool(item.get("passwd") or item.get("password"))
        virus = bool(item.get("virus"))
        file_type = str(item.get("type") or item.get("file_type") or "").upper()
    if passwd or virus:
        return True
    if file_type and file_type != "VIDEO":
        return True
    if ext and ext.lower() not in _ALLOWED_VIDEO_EXTENSIONS:
        return True
    if duration_seconds is not None and duration_seconds < _MIN_DURATION_SECONDS:
        return True
    return False


def _format_duration(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds <= 0:
        return None
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _extract_quality(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if "4k" in lowered:
            return "2160p"
        match = _QUALITY_RE.search(lowered)
        if match:
            value = match.group(1)
            suffix = match.group(2) or "p"
            return f"{value}{suffix.lower()}"
        if "uhd" in lowered:
            return "2160p"
        if "fhd" in lowered:
            return "1080p"
    return None


def _build_thumbnail_url(
    base: Optional[str], hash_id: Optional[str], slug: Optional[str]
) -> Optional[str]:
    if not base or not hash_id:
        return None
    base = base.rstrip("/") + "/"
    prefix = hash_id[:3]
    safe_slug = quote((slug or hash_id).replace("/", "_"))
    return f"{base}{prefix}/pr-{hash_id}.jpg/th-{safe_slug}.jpg"


def _extract_release_markers(
    text: str, quality_hint: Optional[str] = None
) -> Dict[str, Optional[Any]]:
    info: Dict[str, Optional[Any]] = {}
    if not text:
        return info
    season_match = _SEASON_EP_RE.search(text)
    if season_match:
        season = season_match.group("season") or season_match.group("season2")
        episode = season_match.group("episode") or season_match.group("episode2")
        if season:
            info["season"] = int(season)
        if episode:
            info["episode"] = int(episode)
    year_match = _YEAR_RE.search(text)
    if year_match:
        info["year"] = int(year_match.group(0))
    quality = quality_hint or _extract_quality(text)
    if quality:
        info["quality"] = quality
    return info


def _detect_anime(title: str) -> bool:
    if _SEASON_EP_RE.search(title):
        return False
    bracket_match = _ANIME_BRACKET_GROUP_RE.search(title)
    if not bracket_match:
        return False
    group_name = bracket_match.group(1).strip().lower()
    if group_name not in _KNOWN_FANSUB_GROUPS:
        return False
    title_without_group = title[bracket_match.end():].strip()
    episode_patterns = [
        r"[\s\-_]+\d{1,4}(?:\s*v\d+)?[\s\-_\.\(\[]",
        r"[\s\-_]Ep?\.?\s*\d{1,4}",
        r"[\s\-_]Episode\s*\d{1,4}",
    ]
    return any(
        re.search(pattern, title_without_group, re.IGNORECASE)
        for pattern in episode_patterns
    )


def _detect_category(title: str, metadata: Dict[str, Optional[Any]]) -> int:
    if _detect_anime(title):
        return CATEGORY_ANIME

    season = metadata.get("season")
    episode = metadata.get("episode")
    quality = metadata.get("quality")
    year = metadata.get("year")

    quality_lower = (quality or "").lower()
    is_uhd = False
    is_hd = False

    if quality_lower:
        if "2160" in quality_lower or "4k" in quality_lower or "uhd" in quality_lower:
            is_uhd = True
        elif "720" in quality_lower or "1080" in quality_lower:
            is_hd = True

    has_tv_pattern = season is not None or episode is not None
    if not has_tv_pattern:
        if _SEASON_EP_RE.search(title):
            has_tv_pattern = True

    if has_tv_pattern:
        if is_uhd:
            return CATEGORY_TV_UHD
        elif is_hd:
            return CATEGORY_TV_HD
        else:
            return CATEGORY_TV

    if year or (not has_tv_pattern):
        if is_uhd:
            return CATEGORY_MOVIES_UHD
        elif is_hd:
            return CATEGORY_MOVIES_HD
        else:
            return CATEGORY_MOVIES

    return CATEGORY_MOVIES


def _matches_strict(title: str, strict_phrase: Optional[str]) -> bool:
    if not strict_phrase:
        return True
    candidate = _sanitize_phrase(title)
    if not candidate:
        return False
    if candidate == strict_phrase:
        return True
    candidate_tokens = candidate.split()
    phrase_tokens = strict_phrase.split()
    if not phrase_tokens:
        return True
    for idx in range(0, max(1, len(candidate_tokens) - len(phrase_tokens) + 1)):
        if candidate_tokens[idx : idx + len(phrase_tokens)] == phrase_tokens:
            return True
    return False


def filter_and_map(
    json_data: dict,
    min_bytes: int,
    query_tokens: Optional[List[str]] = None,
    query_meta: Optional[Dict[str, Optional[Any]]] = None,
    strict_phrase: Optional[str] = None,
    strict_match: bool = False,
) -> List[dict]:
    token_set: Set[str] = set(query_tokens or [])
    thumb_base = json_data.get("thumbURL") or json_data.get("thumbUrl")
    out: List[dict] = []
    for it in json_data.get("data", []):
        hash_id: Optional[str] = None
        subject: Optional[str] = None
        filename_no_ext: Optional[str] = None
        ext: Optional[str] = None
        size: Any = 0
        poster: Optional[str] = None
        posted_raw: Any = None
        sig: Optional[str] = None
        display_fn: Optional[str] = None
        extension_field: Optional[str] = None
        duration_raw: Any = None
        fullres: Optional[str] = None

        if isinstance(it, list):
            if len(it) >= 12:
                hash_id = it[0]
                subject = it[6]
                filename_no_ext = it[10]
                ext = it[11]
            if len(it) > 7:
                poster = it[7]
            if len(it) > 8:
                posted_raw = it[8]
            if len(it) > 14:
                duration_raw = it[14]
        elif isinstance(it, dict):
            hash_id = it.get("hash") or it.get("0") or it.get("id")
            subject = it.get("subject") or it.get("6")
            filename_no_ext = it.get("filename") or it.get("10")
            ext = it.get("ext") or it.get("11")
            size = it.get("size", 0)
            poster = it.get("poster") or it.get("7")
            posted_raw = it.get("timestamp") or it.get("ts") or it.get("dtime") or it.get("date") or it.get("12")
            sig = it.get("sig")
            display_fn = it.get("fn") or it.get("filename")
            extension_field = it.get("extension") or it.get("ext")
            duration_raw = it.get("14") or it.get("duration") or it.get("len")
            fullres = it.get("fullres") or it.get("resolution")

        if not hash_id or not ext:
            continue

        filename_no_ext = filename_no_ext or ""
        ext = ext or ""
        if extension_field and not ext:
            ext = extension_field

        if not isinstance(size, int):
            try:
                size = int(size)
            except Exception:
                size = 0

        if size < min_bytes:
            continue

        duration_seconds = _parse_duration_seconds(duration_raw)

        if _is_flagged_item(it, ext, duration_seconds):
            continue

        title: Optional[str] = None
        if display_fn:
            cleaned = display_fn.strip()
            if cleaned:
                normalized = cleaned.replace(" - ", "-")
                parts = [segment for segment in normalized.split(" ") if segment]
                sanitized = ".".join(parts)
                ext_component = extension_field or ext or ""
                if ext_component and not ext_component.startswith("."):
                    ext_component = f".{ext_component}"
                title = f"{sanitized}{ext_component}" if ext_component else sanitized

        if not title:
            fallback = subject or f"{filename_no_ext}{ext}"
            title = _normalize_title(fallback)

        quality = _extract_quality(title, fullres)
        title_meta = _extract_release_markers(title, quality)
        if not quality and title_meta.get("quality"):
            quality = title_meta.get("quality")

        if strict_match and not _matches_strict(title, strict_phrase):
            continue

        if query_meta:
            q_year = query_meta.get("year")
            q_season = query_meta.get("season")
            q_episode = query_meta.get("episode")
            q_quality = query_meta.get("quality")
            t_year = title_meta.get("year")
            t_season = title_meta.get("season")
            t_episode = title_meta.get("episode")
            t_quality = quality or title_meta.get("quality")
            if q_year and t_year and q_year != t_year:
                continue
            if q_season and t_season and q_season != t_season:
                continue
            if q_episode and t_episode and q_episode != t_episode:
                continue
            if q_quality and t_quality and q_quality.lower() != t_quality.lower():
                continue

        if token_set:
            title_tokens = set(_tokenize(title))
            if not title_tokens or not token_set.issubset(title_tokens):
                continue

        duration_formatted = _format_duration(duration_seconds)
        thumbnail_url = _build_thumbnail_url(thumb_base, hash_id, filename_no_ext)
        year = title_meta.get("year")

        out.append(
            {
                "hash": hash_id,
                "filename": filename_no_ext,
                "ext": ext,
                "sig": sig,
                "size": size,
                "title": title,
                "poster": poster,
                "posted": posted_raw,
                "duration": duration_seconds,
                "duration_hms": duration_formatted,
                "quality": quality,
                "thumbnail": thumbnail_url,
                "year": year,
                "season": title_meta.get("season"),
                "episode": title_meta.get("episode"),
            }
        )
    return out


@APP.route("/api")
def api():
    if not require_apikey():
        logger.warning("Rejected request with wrong API key from %s", request.remote_addr)
        return Response("Unauthorized", status=401)

    t = request.args.get("t", "caps")
    logger.info("Request: t=%s q=%r from %s", t, request.args.get("q", ""), request.remote_addr)

    if t == "caps":
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<caps>"
            '<server version="0.1" title="Easynews Bridge"/>'
            '<limits max="100" default="100"/>'
            '<registration available="no" open="no"/>'
            "<searching>"
            '<search available="yes" supportedParams="q"/>'
            '<movie-search available="yes" supportedParams="q,year"/>'
            '<tv-search available="yes" supportedParams="q,season,ep"/>'
            "</searching>"
            "<categories>"
            '<category id="2000" name="Movies">'
            '<subcat id="2030" name="Movies/HD"/>'
            '<subcat id="2040" name="Movies/UHD"/>'
            "</category>"
            '<category id="5000" name="TV">'
            '<subcat id="5030" name="TV/HD"/>'
            '<subcat id="5040" name="TV/UHD"/>'
            '<subcat id="5070" name="TV/Anime"/>'
            "</category>"
            '<category id="7000" name="Other"/>'
            "</categories>"
            "</caps>"
        )
        return Response(xml, mimetype="application/xml")

    if t in ("search", "movie", "tvsearch"):
        base_query = (request.args.get("q") or "").strip()
        cat_param = request.args.get("cat") or ""
        season_param = request.args.get("season") or request.args.get("seasonnum")
        episode_param = (
            request.args.get("ep")
            or request.args.get("epnum")
            or request.args.get("episode")
        )
        year_param = request.args.get("year") or request.args.get("yr")
        season_int = _as_int(season_param)
        episode_int = _as_int(episode_param)
        year_int = _as_int(year_param)

        search_components: List[str] = []
        if base_query:
            search_components.append(base_query)

        if t == "movie":
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))
        elif t == "tvsearch":
            if season_int is not None and episode_int is not None:
                search_components.append(f"S{season_int:02}E{episode_int:02}")
            elif season_int is not None:
                search_components.append(f"S{season_int:02}")
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))

        search_label = " ".join(part for part in search_components if part).strip()
        raw_query = search_label or base_query
        q = raw_query.strip()
        fallback_query = False
        if not q or q.lower() == "test":
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = set(cat_param.split(",")) if cat_param else set()
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories)
            if wants_anime:
                q = "one piece"
            elif wants_tv:
                q = "breaking bad"
            else:
                q = "matrix"
            fallback_query = True

        query_tokens = _tokenize(raw_query)
        # Drop bare season tokens like "s04": they never appear literally in
        # episode filenames ("...s04e08..."), so requiring them as a token would
        # filter out every real result. The season is enforced via metadata below.
        query_tokens = [tok for tok in query_tokens if not _SEASON_TOKEN_RE.match(tok)]
        query_meta = _extract_release_markers(raw_query)
        if query_meta.get("season") is None:
            season_only = _SEASON_ONLY_RE.search(raw_query)
            if season_only:
                query_meta["season"] = int(season_only.group("season"))
        if year_int:
            query_meta["year"] = year_int
        if season_int is not None:
            query_meta["season"] = season_int
        if episode_int is not None:
            query_meta["episode"] = episode_int
        strict_param = request.args.get("strict")
        strict_requested = t in {"movie", "tvsearch"}
        if strict_param is not None:
            strict_requested = strict_param.strip().lower() not in {"0", "false", "no", "off"}
        strict_phrase = _sanitize_phrase(raw_query) if strict_requested else None
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
        min_size_param = request.args.get("minsize")
        min_size_mb = 100
        if min_size_param:
            try:
                min_size_mb = max(100, int(min_size_param))
            except ValueError:
                min_size_mb = 100
        min_bytes = min_size_mb * 1024 * 1024

        if fallback_query:
        season_pack_query = (
            query_meta.get("season") is not None
            and query_meta.get("episode") is None
        )
        if IGNORE_SEASON_PACKS and season_pack_query and not fallback_query:
            logger.info(
                "Skipping season-pack query %r (IGNORE_SEASON_PACKS enabled)",
                raw_query,
            )
            items = []
        elif fallback_query:
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = set(cat_param.split(",")) if cat_param else set()
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories)

            if wants_anime:
                items = [
                    {
                        "hash": "SAMPLEHASH_ANIME123",
                        "filename": "sample.anime.series.01.720p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 350 * 1024 * 1024,
                        "title": "[SampleSubs] Sample Anime Series - 01 [720p]",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            elif wants_tv:
                items = [
                    {
                        "hash": "SAMPLEHASH_TV123456",
                        "filename": "sample.tv.show.s01e01.1080p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 800 * 1024 * 1024,
                        "title": "Sample TV Show S01E01 1080p",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            else:
                items = [
                    {
                        "hash": "SAMPLEHASH1234567890",
                        "filename": "sample.matrix.clip",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 700 * 1024 * 1024,
                        "title": "Sample Matrix Clip",
                        "sample": True,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
        else:
            c = client()
            search_start = time.time()
            # Latency-bounded, hedged search: returns the first real results and
            # is hard-capped well under NZBHydra's 4s timeout, so a slow/hung
            # Easynews response no longer surfaces as "0 results".
            data = c.search_hedged(
                query=q,
                file_type="VIDEO",
                per_page=250,
                sort_field="relevance",
                sort_dir="-",
            )
            elapsed = time.time() - search_start
            raw_count = len(data.get("data", []))
            items = filter_and_map(
                data,
                min_bytes=min_bytes,
                query_tokens=query_tokens,
                query_meta=query_meta,
                strict_phrase=strict_phrase,
                strict_match=strict_requested,
            )
            logger.info(
                "Search %r → %d raw results, %d passed filters, in %.1fs",
                q, raw_count, len(items), elapsed,
            )

        items = items[offset : offset + limit]

        display_q = raw_query if raw_query else q
        chan_title = f"Results for {display_q}"
        now_dt = datetime.now(timezone.utc)
        channel_pub = now_dt.strftime("%a, %d %b %Y %H:%M:%S %z")

        header = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            "<channel>"
            f"<title>{xml_escape(chan_title)}</title>"
            f"<description>{xml_escape(chan_title)}</description>"
            f"<link>{request.url_root.rstrip('/')}/api</link>"
            f"<pubDate>{channel_pub}</pubDate>"
        )

        body_parts: List[str] = []
        for it in items:
            enc_id = encode_id(it)
            title = xml_escape(it["title"]) if it["title"] else "Untitled"
            link = f"{request.url_root.rstrip('/')}/api?t=get&id={enc_id}&apikey={request.args.get('apikey')}"
            safe_link = xml_escape(link)
            size = it["size"]
            guid = enc_id
            poster = it.get("poster")
            posted_dt = _coerce_datetime(it.get("posted")) or now_dt
            posted_str = posted_dt.strftime("%a, %d %b %Y %H:%M:%S %z")
            posted_epoch = str(int(posted_dt.timestamp()))
            duration_hms = it.get("duration_hms")
            quality = it.get("quality")
            thumb = it.get("thumbnail")
            year = it.get("year")
            season = it.get("season")
            episode = it.get("episode")

            title_text = it.get("title", "")
            title_metadata = {
                "season": season,
                "episode": episode,
                "year": year,
                "quality": quality,
            }
            category_id = _detect_category(title_text, title_metadata)

            attr_parts = [
                f'<newznab:attr name="size" value="{size}"/>',
                f'<newznab:attr name="category" value="{category_id}"/>',
                f'<newznab:attr name="usenetdate" value="{posted_str}"/>',
                f'<newznab:attr name="posted" value="{posted_epoch}"/>',
            ]
            if poster:
                attr_parts.append(f'<newznab:attr name="poster" value="{xml_escape(poster)}"/>')
            if quality:
                attr_parts.append(f'<newznab:attr name="quality" value="{xml_escape(quality)}"/>')
            if duration_hms:
                attr_parts.append(f'<newznab:attr name="duration" value="{duration_hms}"/>')
            if thumb:
                attr_parts.append(f'<newznab:attr name="thumb" value="{xml_escape(thumb)}"/>')
            if year:
                attr_parts.append(f'<newznab:attr name="year" value="{year}"/>')
            if season:
                attr_parts.append(f'<newznab:attr name="season" value="{season}"/>')
            if episode:
                attr_parts.append(f'<newznab:attr name="episode" value="{episode}"/>')
            attr_xml = "".join(attr_parts)
            item_xml = (
                f"<item>"
                f"<title>{title}</title>"
                f'<guid isPermaLink="false">{guid}</guid>'
                f"<link>{safe_link}</link>"
                f"<category>{category_id}</category>"
                f"<pubDate>{posted_str}</pubDate>"
                f"{attr_xml}"
                f'<enclosure url="{safe_link}" length="{size}" type="application/x-nzb"/>'
                f"</item>"
            )
            body_parts.append(item_xml)

        footer = "</channel></rss>"
        xml = header + "".join(body_parts) + footer
        return Response(xml, mimetype="application/rss+xml")

    if t in ("get", "getnzb"):
        enc_id = request.args.get("id")
        if not enc_id:
            return Response("Missing id", status=400)
        d = decode_id(enc_id)
        if d.get("sample"):
            safe_title = "sample"
            nzb_content = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
                '<file subject="Sample Matrix Clip" date="0" poster="sample@example.com">'
                "<groups><group>alt.binaries.sample</group></groups>"
                '<segments><segment bytes="1024" number="1">sample</segment></segments>'
                "</file></nzb>"
            ).encode("utf-8")
            resp = Response(nzb_content, mimetype="application/x-nzb")
            resp.headers["Content-Disposition"] = f'attachment; filename="{safe_title}.nzb"'
            return resp
        si = to_search_item(d)
        try:
            c = client()
            payload = c.build_nzb_payload([si], name=d.get("title"))
            url = "https://members.easynews.com/2.0/api/dl-nzb"
            r = _retry_request(
                lambda: c.s.post(url, data=payload, timeout=60),
                max_retries=3,
                base_delay=2.0,
            )
        except EasynewsError as e:
            return Response(f"Upstream error: {e}", status=502)
        except requests.exceptions.RequestException as e:
            return Response(f"Upstream network error: {e}", status=502)
        if r.status_code != 200:
            return Response(f"Upstream error {r.status_code}", status=502)
        title = d.get("title") or (d.get("filename", "download") + d.get("ext", ""))
        safe_title = (
            "".join(ch for ch in title if ch.isalnum() or ch in (" ", "-", "_", "."))[:200].strip()
            or "download"
        )
        resp = Response(r.content, mimetype="application/x-nzb")
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_title}.nzb"'
        return resp

    return Response("Unsupported 't' parameter", status=400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    APP.run(host="0.0.0.0", port=port)
