import base64
import html
import logging
import os
import random
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote
from query_replace import parse_rules as _parse_query_replace, apply_rules as _apply_query_replace

import requests
from flask import Flask, Response, request
import json

from easynews_client import (
    EasynewsClient,
    EasynewsError,
    SearchItem,
    _active_endpoint_label,
    paginate_enabled,
    max_pages,
    _SEARCH_ATTEMPT_TIMEOUT,
)


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
_CLIENT_LOGIN_TTL = 86400  # 24 h between background session refreshes (searches
                           # ride Basic Auth regardless, so this is not critical)
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

# When enabled, skip the title-matching filters (strict phrase, season/episode/
# year/quality, and query-token subset). Validity checks (hash/ext, min size,
# virus/password/duration) still apply. Useful if a downstream client (Sonarr/
# Radarr) does its own matching and you'd rather hand it everything Easynews
# returns. Default off.
DISABLE_RESULT_FILTERS = _env_bool("EASYNEWS_DISABLE_FILTERS", False)

# When de-duplicating identical-hash entries, keep the one with the newest post
# date instead of the first (which is by relevance). Helps when an old post has
# gone dead and the exact same file was re-posted later. Note: re-packed/re-
# encoded re-uploads have a *different* hash and are never deduped anyway.
# Default off = keep the first (relevance-ranked) entry.
DEDUP_KEEP_NEWEST = _env_bool("EASYNEWS_DEDUP_KEEP_NEWEST", False)

# Keep results Easynews flags as password-protected. The flag is a heuristic and
# often a false positive — especially on VIDEO results, where Easynews returns
# full metadata (runtime/codecs) it could only read if the file weren't actually
# locked — and when something is genuinely passworded the password is usually
# posted alongside in an NFO/TXT. stremthru and AIOStreams keep these; this lets
# you match that. Virus-flagged items are still always dropped. Default off =
# password-flagged results are dropped.
ALLOW_PASSWORD = _env_bool("EASYNEWS_ALLOW_PASSWORD", False)

# Drop connector stopwords (and/of/the/…) from the query sent to Easynews.
# Easynews AND-matches every query word, so a word absent from the release name
# (e.g. clients expand "Escha & Logy" → "Escha and Logy", but releases write
# "Escha..Logy") zeroes out the whole search. Filtering still uses the full
# token set, so precision is unchanged. Default on (it's a recall fix).
STRIP_STOPWORDS = _env_bool("EASYNEWS_STRIP_STOPWORDS", True)

# Optionally add a direct Easynews HTTPS download URL to each search result.
# URL format:
# https://members.easynews.com/dl/auto/443/{hash}{id}{ext}/{filename}{ext}?sid={sid}&sig={sig}
#
# When enabled, the URL is exposed as the item's <comments> field so clients
# that support it (such as Prowlarr) can use it as the release's information
# link or clickable title.
INCLUDE_DIRECT_URL = _env_bool("EASYNEWS_INCLUDE_DIRECT_URL", True)


def _encode_uri(uri: str) -> str:
    return quote(uri, safe="")


def _build_direct_url(
    hash_id: Optional[str],
    item_id: Optional[str],
    filename_no_ext: Optional[str],
    ext: Optional[str],
    sig: Optional[str],
    sid: Optional[str],
) -> Optional[str]:
    """Direct HTTPS download URL, From a 3.0 API response.
    hash/id/ext/sig/sid all come from the same search
    response — no separate lookup needed."""
    if not (hash_id and item_id and sig and sid):
        return None
    ext_part = ext or ""
    if ext_part and not ext_part.startswith("."):
        ext_part = f".{ext_part}"
    file_seg = _encode_uri(f"{hash_id}{item_id}{ext_part}")
    name_seg = _encode_uri(f"{filename_no_ext or hash_id}{ext_part}")
    url = f"https://members.easynews.com/dl/auto/443/{file_seg}/{name_seg}?sid={_encode_uri(sid)}&sig={_encode_uri(sig)}"

    return url

# Per-title query overrides. Rewrites a specific incoming TITLE to the form a
# release is actually posted under (e.g. Norsemen -> Vikingane, or fixing an
# upstream-mangled "ikke lov a le pa hytta" -> "...aa...paa..."). Applied to the
# title only and used for BOTH the outbound query and the title filters, so a
# rename doesn't then reject every result. See query_replace.py. Default off.
QUERY_REPLACE = _parse_query_replace(os.getenv("EASYNEWS_QUERY_REPLACE", ""))

# Transliterate Norwegian letters (ø→oe, å→aa, æ→ae, and uppercase) everywhere
# the query is matched. Scene/Usenet releases routinely ASCII-fold these — a film
# titled "Trøst" is posted as "Troest" — so a client searching the accented form
# AND-matches nothing on Easynews and zeroes out. We fold the OUTBOUND query so
# Easynews finds the ASCII-named release, and fold both sides of the title filters
# (so a "Troest" hit isn't then dropped for not containing "trøst"). Folding is
# symmetric and only touches æ/ø/å, so an already-ASCII release named "Trost"
# still won't collide — non-Norwegian queries are untouched. Default off (opt-in).
TRANSLITERATE_NORWEGIAN = _env_bool("EASYNEWS_TRANSLITERATE_NORWEGIAN", False)

# ø→oe, å→aa, æ→ae (str.translate maps a code point to a replacement string).
_NORWEGIAN_TRANSLITERATION = {
    ord("æ"): "ae", ord("Æ"): "Ae",
    ord("ø"): "oe", ord("Ø"): "Oe",
    ord("å"): "aa", ord("Å"): "Aa",
}


def _transliterate_norwegian(text: str) -> str:
    """Fold Norwegian æ/ø/å to their conventional ASCII digraphs (ae/oe/aa).
    No-op on text without those letters, so it's safe to call unconditionally
    once the feature flag is on."""
    if not text:
        return text
    return text.translate(_NORWEGIAN_TRANSLITERATION)

# Extra metadata emitted as newznab:attr so downstream tools can use it. The
# audio/subtitle language codes come from Easynews's named JSON fields
# (subtitle_tracks/slangs, audio_tracks/alangs) and only exist when the endpoint
# returns them (the 3.0 api and the 2.0 dict form do). AIOStreams reads the
# "subs" attr as subtitle languages and "language" as audio languages — handy
# for e.g. filtering to Norwegian subtitles. Default on; disable individually.
# NOTE: if you go through NZBHydra2, it must pass these attrs through to the
# downstream client (they originate here regardless).
META_SUBS = _env_bool("EASYNEWS_META_SUBS", True)
META_AUDIO = _env_bool("EASYNEWS_META_AUDIO", True)
META_CODECS = _env_bool("EASYNEWS_META_CODECS", True)
# Emit the video bitrate (from Easynews's raw `bps`, formatted as "12.72 Mbps").
# More precise than Easynews's own `prettyBps`, which rounds to whole Mbps.
META_BITRATE = _env_bool("EASYNEWS_META_BITRATE", True)
# Emit the Usenet group(s) (newznab "group" attr) and the password flag
# (newznab "password" attr, "1"/"0"). Both come straight from the JSON.
META_GROUP = _env_bool("EASYNEWS_META_GROUP", True)
META_PASSWORD = _env_bool("EASYNEWS_META_PASSWORD", True)


def _format_bitrate_mbps(raw: Any) -> Optional[str]:
    """Easynews's `bps` (raw bits/sec) → "12.72 Mbps" (2 decimals). Returns None
    for missing/zero values (some posts report bps=0)."""
    try:
        bps = int(raw)
    except (TypeError, ValueError):
        return None
    if bps <= 0:
        return None
    return f"{bps / 1_000_000:.2f} Mbps"

# Extra search terms (comma-separated). For each one, the bridge also runs
# "<query> <term>" alongside the bare query and merges the results. Easynews
# AND-matches the term, so a language tag like "nordic" surfaces releases that
# the bare relevance ranking buries deep (e.g. "From S01E04 nordic" returns the
# NORDiC release on page 1). Example: EASYNEWS_EXTRA_TERMS=nordic, danish, norsk
# Each term adds one (concurrent) request per search. Empty = feature off.
EXTRA_TERMS = [
    term.strip()
    for term in os.environ.get("EASYNEWS_EXTRA_TERMS", "").split(",")
    if term.strip()
]


def _parse_langs_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip().lower() for v in value.split(",") if v.strip()]


# Restrict results to releases whose subtitle tracks include at least one of
# these language codes (e.g. "nor" for Norwegian, "nor,swe" for either). Global
# default; can be overridden per request with &subs= on the /api URL. Empty =
# no restriction. NOTE: only keeps items where Easynews actually reports a
# matching subtitle track, so it's strict — releases whose subs Easynews didn't
# index are dropped. Pairs well with EASYNEWS_EXTRA_TERMS=nordic.
REQUIRE_SUBS = _parse_langs_csv(os.environ.get("EASYNEWS_REQUIRE_SUBS"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


# ── Search-result cache ─────────────────────────────────────────────────────
# Prowlarr/Sonarr/Radarr poll the indexer with the same queries constantly (RSS
# sync, re-runs, multiple apps). This caches the final mapped+filtered item
# list per distinct query for a short TTL, so repeats return instantly and
# don't spend an Easynews request. Single worker / shared threads (gunicorn -w1 --threads N), so
# a plain module-level dict is shared across all request threads.
# TTL is deliberately SHORT for an indexer: too long and a just-posted release is
# invisible until it expires. 0 = caching off.
CACHE_TTL_SECONDS = _env_int("EASYNEWS_CACHE_TTL_SECONDS", 0)
CACHE_MAX_ENTRIES = _env_int("EASYNEWS_CACHE_MAX_ENTRIES", 512)
_SEARCH_CACHE: "OrderedDict[str, tuple[float, List[dict]]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Optional[List[dict]]:
    if CACHE_TTL_SECONDS <= 0:
        return None
    with _CACHE_LOCK:
        hit = _SEARCH_CACHE.get(key)
        if hit is None:
            return None
        ts, items = hit
        if time.time() - ts > CACHE_TTL_SECONDS:
            _SEARCH_CACHE.pop(key, None)
            return None
        _SEARCH_CACHE.move_to_end(key)  # mark most-recently-used
        return items


def _cache_put(key: str, items: List[dict]) -> None:
    if CACHE_TTL_SECONDS <= 0:
        return
    with _CACHE_LOCK:
        _SEARCH_CACHE[key] = (time.time(), items)
        _SEARCH_CACHE.move_to_end(key)
        while len(_SEARCH_CACHE) > CACHE_MAX_ENTRIES:
            _SEARCH_CACHE.popitem(last=False)  # evict oldest (LRU)


# ── 0-result fallback: spelling variants + alternate titles ─────────────────
# When the primary search returns NOTHING, optionally run extra "backup"
# searches built from alternate ASCII spellings of the title and curated
# alternate titles. Gated so the extra Easynews requests only fire when
# they could actually help (a 0-result query), never on a query that already
# found something — no wasted requests in the common case.
FALLBACK_SEARCH = _env_bool("EASYNEWS_FALLBACK_SEARCH", False)
# Include alternate ASCII spellings (Tromsø→Tromsoe/Tromso, Köln→Koeln/Koln/Cologne…).
FALLBACK_TRANSLITERATE = _env_bool("EASYNEWS_FALLBACK_TRANSLITERATE", True)
# Include curated aliases from the custom-titles file (Walking Dead→The Walking Dead…).
FALLBACK_ALT_TITLES = _env_bool("EASYNEWS_FALLBACK_ALT_TITLES", True)
# Hard cap on how many backup queries fire (resource guard), and the wall-clock
# budget for the whole backup stage. Backups run concurrently, single-shot.
FALLBACK_MAX_QUERIES = _env_int("EASYNEWS_FALLBACK_MAX_QUERIES", 6)
FALLBACK_BUDGET_SECONDS = _env_float("EASYNEWS_FALLBACK_BUDGET_SECONDS", 4.0)
# Run EASYNEWS_EXTRA_TERMS only as part of the 0-result fallback, instead of
# alongside every search. Saves the per-search extra requests when the bare
# query already returns results. Default off = extra terms run on every search.
EXTRA_TERMS_FALLBACK_ONLY = _env_bool("EASYNEWS_EXTRA_TERMS_FALLBACK_ONLY", False)

# Curated alternate-title map (canonical title -> [aliases]). JSON file, same
# format as the upstream easynews-plus-plus custom-titles.json. Loaded once at
# start; missing/empty file = feature simply contributes nothing.
CUSTOM_TITLES_FILE = os.environ.get("EASYNEWS_CUSTOM_TITLES_FILE", "custom-titles.json").strip()


def _load_custom_titles(path: str) -> Dict[str, List[str]]:
    if not path:
        return {}
    try:
        candidate = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
        if not os.path.exists(candidate):
            return {}
        with open(candidate, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out: Dict[str, List[str]] = {}
        for key, vals in (raw or {}).items():
            if str(key).startswith("_"):
                continue  # allow "_comment" style notes in the file
            if isinstance(vals, str):
                vals = [vals]
            if isinstance(vals, list):
                out[str(key)] = [str(v) for v in vals if str(v).strip()]
        if out:
            logger.info("Loaded %d custom-title mapping(s) from %s", len(out), candidate)
        return out
    except Exception as exc:  # noqa: BLE001 — bad file shouldn't crash the server
        logger.warning("Could not load custom titles from %r: %s", path, exc)
        return {}


CUSTOM_TITLES = _load_custom_titles(CUSTOM_TITLES_FILE)

# Multi-convention transliteration maps for the spelling-variant generator. Each
# whole-string convention yields at most one variant, so a foreign title gives a
# small, bounded set (≤3) and an already-ASCII title gives ZERO (no wasted query).
_VAR_DIGRAPH = {
    ord("æ"): "ae", ord("Æ"): "Ae", ord("ø"): "oe", ord("Ø"): "Oe",
    ord("å"): "aa", ord("Å"): "Aa", ord("ä"): "ae", ord("Ä"): "Ae",
    ord("ö"): "oe", ord("Ö"): "Oe", ord("ü"): "ue", ord("Ü"): "Ue",
    ord("ß"): "ss",
}
_VAR_BAREVOWEL = {
    ord("æ"): "ae", ord("Æ"): "Ae", ord("ø"): "o", ord("Ø"): "O",
    ord("å"): "a", ord("Å"): "A", ord("ä"): "a", ord("Ä"): "A",
    ord("ö"): "o", ord("Ö"): "O", ord("ü"): "u", ord("Ü"): "U",
    ord("ß"): "ss",
}
# Applied before the NFKD accent strip: ø/æ/ß/đ/ł never NFKD-decompose so they
# need explicit maps (å/Å would decompose on their own; included for uniformity).
_VAR_PRESTRIP = {
    ord("ø"): "o", ord("Ø"): "O", ord("å"): "a", ord("Å"): "A",
    ord("æ"): "ae", ord("Æ"): "Ae", ord("ß"): "ss", ord("đ"): "d", ord("Đ"): "D",
    ord("ł"): "l", ord("Ł"): "L",
}


def _strip_accents(text: str) -> str:
    """Fold any accented/diacritic title to plain ASCII (José→Jose, Köln→Koln,
    Tromsø→Tromso). Handles the non-decomposing Nordic/Slavic letters explicitly,
    then NFKD-strips the rest."""
    pre = text.translate(_VAR_PRESTRIP)
    nfkd = unicodedata.normalize("NFKD", pre)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _spelling_variants(title: str) -> List[str]:
    """Up to three alternate ASCII spellings of a title, in the conventions
    releases actually use: digraph (ø→oe, ü→ue), bare-vowel (ø→o, ü→u), and a
    full accent-strip. Empty for an already-ASCII title (so no backup query is
    wasted on titles that don't need one)."""
    if not title:
        return []
    out: List[str] = []
    for conv in (title.translate(_VAR_DIGRAPH), title.translate(_VAR_BAREVOWEL), _strip_accents(title)):
        conv = conv.strip()
        if conv and conv != title and conv not in out:
            out.append(conv)
    return out


def _alternative_titles(title: str) -> List[str]:
    """Curated aliases for a title from CUSTOM_TITLES — direct key match plus
    substring match in either direction (so 'Walking Dead' picks up 'The Walking
    Dead'). Returns aliases only (never the input itself)."""
    if not title or not CUSTOM_TITLES:
        return []
    out: List[str] = []
    low = title.lower()
    for key, vals in CUSTOM_TITLES.items():
        kl = key.lower()
        if kl == low or kl in low or low in kl:
            for v in vals:
                if v and v.lower() != low and v not in out:
                    out.append(v)
    return out


def _join_langs(value: Any) -> Optional[str]:
    """Normalise a language field (list like ['nor','eng'] or a comma string)
    into a clean, de-duplicated, lowercase comma-joined string of ISO codes."""
    if not value:
        return None
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip().lower() for p in value]
    else:
        return None
    seen: List[str] = []
    for p in parts:
        if p:
            # Look up the code in our mapping (e.g., turn 'nob' into 'nor')
            # if it's not in the map, keep the original code.
            canon = _LANG_CANON.get(p, p)
            if canon not in seen:
                seen.append(canon)
    return ",".join(sorted(seen)) if seen else None
    
# ── Subtitle/audio language canonicalisation ────────────────────────────────
# Easynews tags tracks with ISO 639-2 codes, sometimes the bibliographic (/B)
# variant (ger/fre/dut/chi/cze/gre/rum/slo/alb/baq/geo/mac/may), and tags
# Norwegian as the specific "nob" (Bokmål) rather than the macrolanguage "nor".
# Clients filter with whatever code they have — "nor", the 2-letter "no", "nb",
# etc. We fold every known variant of a language to one token so a &subs= /
# EASYNEWS_REQUIRE_SUBS filter matches regardless of which code is used on either
# side. Matching only — the emitted attrs keep Easynews's raw codes.
_LANG_GROUPS = (
    ("nor", ("no", "nb", "nn", "nob", "nno", "nor")),  # Norwegian (+Bokmål/Nynorsk)
    ("eng", ("en", "eng")), ("swe", ("sv", "swe")), ("dan", ("da", "dan")),
    ("fin", ("fi", "fin")), ("isl", ("is", "ice", "isl")), ("ara", ("ar", "ara")),
    ("ger", ("de", "ger", "deu")), ("fre", ("fr", "fre", "fra")),
    ("spa", ("es", "spa")), ("ita", ("it", "ita")), ("dut", ("nl", "dut", "nld")),
    ("por", ("pt", "por")), ("rus", ("ru", "rus")), ("pol", ("pl", "pol")),
    ("cze", ("cs", "cze", "ces")), ("gre", ("el", "gre", "ell")),
    ("rum", ("ro", "rum", "ron")), ("slo", ("sk", "slo", "slk")),
    ("alb", ("sq", "alb", "sqi")), ("arm", ("hy", "arm", "hye")),
    ("baq", ("eu", "baq", "eus")), ("geo", ("ka", "geo", "kat")),
    ("mac", ("mk", "mac", "mkd")), ("may", ("ms", "may", "msa")),
    ("chi", ("zh", "chi", "zho")), ("hrv", ("hr", "hrv")), ("hun", ("hu", "hun")),
    ("est", ("et", "est")), ("cat", ("ca", "cat")), ("heb", ("he", "heb")),
    ("hin", ("hi", "hin")), ("ind", ("id", "ind")), ("jpn", ("ja", "jpn")),
    ("kor", ("ko", "kor")), ("tur", ("tr", "tur")), ("ukr", ("uk", "ukr")),
    ("vie", ("vi", "vie")), ("tha", ("th", "tha")), ("srp", ("sr", "srp")),
    ("slv", ("sl", "slv")), ("lit", ("lt", "lit")), ("lav", ("lv", "lav")),
    ("bul", ("bg", "bul")), ("aze", ("az", "aze")), ("kaz", ("kk", "kaz")),
    ("kan", ("kn", "kan")), ("khm", ("km", "khm")), ("kir", ("ky", "kir")),
    ("mal", ("ml", "mal")), ("tam", ("ta", "tam")), ("tel", ("te", "tel")),
    ("glg", ("gl", "glg")),
)
_LANG_CANON: Dict[str, str] = {
    alias: canon for canon, aliases in _LANG_GROUPS for alias in aliases
}


def _canon_langs(codes: List[str]) -> Set[str]:
    """Fold a list of language codes to canonical tokens (see _LANG_GROUPS) so
    equivalent ISO 639 variants (no/nb/nob/nor, ger/de/deu, …) compare equal."""
    out: Set[str] = set()
    for c in codes:
        c = (c or "").strip().lower()
        if c:
            out.add(_LANG_CANON.get(c, c))
    return out

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
            _CLIENT.start_keepalive()
            logger.info("Startup login succeeded. Indexer is ready.")
            return _CLIENT

        # Periodic refresh runs in a background thread so the current request is
        # never blocked on the login round-trip — a blocking refresh on the
        # request path pushes the response past NZBHydra's ~4s timeout whenever
        # Easynews answers the login slowly.
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
    container (re)start doesn't pay a blocking login that can push the response
    past NZBHydra's timeout.
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

# Minimum real-video duration. Easynews is full of short preview/sample
# clips — many run 2–5 minutes and sail past a 60s floor, showing up as junk
# "releases" a downstream client may grab and then fail on. Raise this (e.g. 360
# = 6 minutes, matching the upstream addon) to cut clip noise. Lower it only if
# you legitimately index very short content (music videos, shorts). (default: 60)
_MIN_DURATION_SECONDS = _env_int("EASYNEWS_MIN_DURATION_SECONDS", 60)
# Split on underscores too ([\W_] = anything that isn't a letter/digit). Fansub
# groups (HorribleSubs, Coalgirls, Erai-raws, SubsPlease) join words with "_",
# e.g. "[Coalgirls]_Occult_Academy_03_..."; without this the whole name becomes
# one glued token and the release is wrongly dropped.
_TOKEN_SPLIT_RE = re.compile(r"[\W_]+", re.UNICODE)
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
_ANIME_EPISODE_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[\s\-_]+\d{1,4}(?:\s*v\d+)?[\s\-_\.\(\[]",
        r"[\s\-_]Ep?\.?\s*\d{1,4}",
        r"[\s\-_]Episode\s*\d{1,4}",
    )
)

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


# Precompiled h/m/s unit patterns — _parse_duration_seconds runs once per raw
# result row, so these are deliberately not built per call.
_DURATION_UNIT_RES = (
    (re.compile(r"(\d+)\s*h"), 3600),
    (re.compile(r"(\d+)\s*m"), 60),
    (re.compile(r"(\d+)\s*s"), 1),
)


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
    for pattern, multiplier in _DURATION_UNIT_RES:
        for part in pattern.findall(text):
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
    if TRANSLITERATE_NORWEGIAN:
        # Fold here so the query and the title (both routed through _tokenize)
        # compare in the same ASCII space — "trøst" and "troest" both become
        # "troest", so a folded query still subset-matches an ASCII release.
        normalized = _transliterate_norwegian(normalized)
    tokens = [
        tok for tok in normalized.split() if len(tok) > 1 and tok not in _STOPWORDS
    ]
    return tokens


_MULTIDASH_RE = re.compile(r"-{2,}")
# A leading operator (- ! +) that targets a NUMBER, e.g. "!1080p", "-720p".
# NZBHydra's capability probes look like this; real film titles never do, so
# this is safe for titles that *contain* "!" (Airplane!, Mamma Mia!, Mother!,
# Tora! Tora! Tora!) and even ones that *start* with it (!Women Art Revolution).
_LEADING_OP_NUM_RE = re.compile(r"^[!+\-](?=\d)")


def _clean_search_query(text: str) -> str:
    """Neutralise client query operators before sending to Easynews. Clients
    (notably NZBHydra's capability probes) send things like "Avengers --1080p"
    or "Avengers !1080p" to test exclusion support; Easynews treats those as
    broken queries and answers slowly/empty. We collapse dash runs, strip stray
    quotes, and drop a leading operator only when it targets a number (so it's
    a quality exclusion, not punctuation in a title). Title "!"/"-" punctuation
    is preserved. Downstream filtering still uses the raw query, so this only
    changes what we ask Easynews."""
    if not text:
        return text
    cleaned = _MULTIDASH_RE.sub(" ", text).replace('"', " ")
    tokens = []
    for tok in cleaned.split():
        tok = _LEADING_OP_NUM_RE.sub("", tok)
        if tok:
            tokens.append(tok)
    return " ".join(tokens).strip()


def _strip_search_stopwords(text: str) -> str:
    """Drop connector stopwords (and/of/the/…) from the query sent to Easynews.
    Easynews AND-matches every query word, so a word absent from the release
    name (e.g. clients expand "Escha & Logy" → "Escha and Logy", but releases
    write "Escha..Logy") zeroes out the whole search. Returns the original text
    if every token is a stopword."""
    if not text:
        return text
    toks = [t for t in text.split() if t.lower() not in _STOPWORDS]
    return " ".join(toks) if toks else text


def _sanitize_phrase(text: str) -> str:
    if not text:
        return ""
    working = text.replace("&", " and ")
    working = _SANITIZE_SYMBOLS_RE.sub(" ", working)
    working = _NON_ALNUM_RE.sub("", working)
    if TRANSLITERATE_NORWEGIAN:
        # Same symmetric fold as _tokenize, so the strict-phrase match (query vs
        # title both pass through here) lines up in ASCII space.
        working = _transliterate_norwegian(working)
    return working.lower().strip()


def _is_flagged_item(item: Any, ext: str, duration_seconds: Optional[int]) -> bool:
    passwd = False
    virus = False
    file_type = ""
    if isinstance(item, dict):
        passwd = bool(item.get("passwd") or item.get("password"))
        virus = bool(item.get("virus"))
        file_type = str(item.get("type") or item.get("file_type") or "").upper()
    if virus:
        return True
    if passwd and not ALLOW_PASSWORD:
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
    return any(p.search(title_without_group) for p in _ANIME_EPISODE_RES)


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
    phrase_tokens = strict_phrase.split()
    if not phrase_tokens:
        return True
    # Contiguous token-run match, done as a space-padded substring test (tokens
    # are whitespace-split so they can't contain spaces themselves).
    return f" {' '.join(phrase_tokens)} " in f" {' '.join(candidate.split())} "


def _posted_epoch(value: Any) -> float:
    """Best-effort sortable epoch for a result's post date, for the dedup
    tie-break. Handles unix timestamps directly and parses date strings."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    dt = _coerce_datetime(value)
    return dt.timestamp() if dt else 0.0


def filter_and_map(
    json_data: dict,
    min_bytes: int,
    query_tokens: Optional[List[str]] = None,
    query_meta: Optional[Dict[str, Optional[Any]]] = None,
    strict_phrase: Optional[str] = None,
    strict_match: bool = False,
    require_subs: Optional[List[str]] = None,
) -> List[dict]:
    token_set: Set[str] = set(query_tokens or [])
    thumb_base = json_data.get("thumbURL") or json_data.get("thumbUrl")
    sid = json_data.get("sid")
    # Loop-invariant: fold the requested subtitle langs once, not per row.
    require_canon: Optional[Set[str]] = _canon_langs(require_subs) if require_subs else None
    out: List[dict] = []
    seen_hashes: Set[str] = set()
    seen_index: Dict[str, int] = {}  # hash -> index in out (for keep-newest dedup)
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
        sub_langs: Any = None
        audio_langs: Any = None
        vcodec: Any = None
        acodec: Any = None
        bitrate_raw: Any = None
        group_raw: Any = None
        passwd_flag: bool = False
        item_id: Any = None

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
            item_id = it.get("id")
            subject = it.get("subject") or it.get("6")
            # `fn` / `extension` / `rawSize` / `runtime` are the named fields the
            # newer 3.0 JSON (and 2.0 dict form) use; the "10"/"11"/"4"/"14"
            # fallbacks cover the positional 2.0 form. All harmless if absent.
            filename_no_ext = it.get("filename") or it.get("fn") or it.get("10")
            ext = it.get("ext") or it.get("extension") or it.get("11")
            size = it.get("size") or it.get("rawSize") or it.get("4") or 0
            poster = it.get("poster") or it.get("7")
            posted_raw = it.get("timestamp") or it.get("ts") or it.get("dtime") or it.get("date") or it.get("12")
            sig = it.get("sig")
            display_fn = it.get("fn") or it.get("filename")
            extension_field = it.get("extension") or it.get("ext")
            duration_raw = it.get("runtime") or it.get("14") or it.get("duration") or it.get("len")
            fullres = it.get("fullres") or it.get("resolution")
            sub_langs = (
                it.get("subtitle_tracks") or it.get("slang") or it.get("slangs")
                or it.get("subs")
            )
            audio_langs = (
                it.get("audio_tracks") or it.get("alang") or it.get("alangs")
                or it.get("language")
            )
            vcodec = it.get("vcodec") or it.get("video_codec")
            acodec = it.get("acodec") or it.get("audio_codec")
            bitrate_raw = it.get("bps")
            group_raw = it.get("groups")
            passwd_flag = bool(it.get("password") or it.get("passwd"))
            # `yres` is Easynews's probed vertical resolution (e.g. 1080) — a
            # reliable height even when the filename omits "1080p". Feed it as the
            # quality fallback so quality/category detection isn't title-only.
            if not fullres:
                yres = it.get("yres") or it.get("height")
                try:
                    if yres and int(yres) > 0:
                        fullres = f"{int(yres)}p"
                except (TypeError, ValueError):
                    pass

        if not hash_id or not ext:
            continue

        # Fast path: drop repeat hashes (keep first). When keep-newest is on we
        # defer the decision to the append site so we can compare post dates.
        if not DEDUP_KEEP_NEWEST:
            if hash_id in seen_hashes:
                continue
            seen_hashes.add(hash_id)

        # macOS AppleDouble sidecar ("._name") — a metadata stub, not real media.
        # Always dropped (even with EASYNEWS_DISABLE_FILTERS); never useful.
        if (filename_no_ext or "").lstrip().startswith("._"):
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

        # Quality: prefer what the title says, else fall back to the probed
        # vertical resolution (fullres / yres).
        title_meta = _extract_release_markers(title)
        quality = title_meta.get("quality") or _extract_quality(fullres)

        if not DISABLE_RESULT_FILTERS and strict_match and not _matches_strict(title, strict_phrase):
            continue

        if not DISABLE_RESULT_FILTERS and query_meta:
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

        if not DISABLE_RESULT_FILTERS and token_set:
            title_tokens = set(_tokenize(title))
            if not title_tokens or not token_set.issubset(title_tokens):
                continue

        # Subtitle-language requirement (global EASYNEWS_REQUIRE_SUBS or per
        # request &subs=). Keep only releases whose reported subtitle tracks
        # include at least one requested language. Not gated by
        # DISABLE_RESULT_FILTERS — it's an explicit content requirement.
        subs_joined = _join_langs(sub_langs)
        if require_canon is not None:
            # Canonicalise both sides so &subs=nor (or no, or nob) matches
            # however Easynews tagged the track — it uses ISO 639-2 and tags
            # Norwegian as the specific "nob" (Bokmål), not the macro "nor".
            item_subs = [s for s in (subs_joined or "").split(",") if s]
            if not (_canon_langs(item_subs) & require_canon):
                continue

        duration_formatted = _format_duration(duration_seconds)
        thumbnail_url = _build_thumbnail_url(thumb_base, hash_id, filename_no_ext)
        direct_url = (
            _build_direct_url(hash_id, item_id, filename_no_ext, ext, sig, sid)
            if INCLUDE_DIRECT_URL
            else None
        )
        year = title_meta.get("year")

        item = {
            "hash": hash_id,
            "filename": filename_no_ext,
            "ext": ext,
            "sig": sig,
            "direct_url": direct_url,
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
            "subs": subs_joined,
            "audio_langs": _join_langs(audio_langs),
            "vcodec": (str(vcodec).strip() or None) if vcodec else None,
            "acodec": (str(acodec).strip() or None) if acodec else None,
            "bitrate": _format_bitrate_mbps(bitrate_raw),
            "group": (str(group_raw).strip() or None) if group_raw else None,
            "password": passwd_flag,
        }

        if DEDUP_KEEP_NEWEST:
            prev = seen_index.get(hash_id)
            if prev is not None:
                # Same file seen already — keep whichever was posted more recently.
                if _posted_epoch(posted_raw) > _posted_epoch(out[prev].get("posted")):
                    out[prev] = item
                continue
            seen_index[hash_id] = len(out)

        out.append(item)
    return out


def _search_and_filter(
    c: EasynewsClient,
    raw: str,
    *,
    query_meta: Dict[str, Optional[Any]],
    min_bytes: int,
    require_subs: Optional[List[str]],
    strict_requested: bool,
    single_shot: bool = False,
    status: Optional[Dict[str, bool]] = None,
) -> List[dict]:
    """Run one Easynews search for *raw* and return mapped+filtered items.

    The token/strict-phrase filter context is built FROM *raw* itself, so an
    alternate spelling/title is matched against the thing we actually searched
    for (not the user's original query). season/episode/year still come from the
    shared *query_meta*. ``single_shot`` uses one bounded request (for the
    concurrent fallback fan-out) instead of the full hedged search. If *status*
    is given, a degraded single-shot request sets ``status['degraded']`` so the
    caller can refuse to cache a resulting false empty.
    """
    search_q = _clean_search_query(raw)
    if STRIP_STOPWORDS:
        search_q = _strip_search_stopwords(search_q)
    if TRANSLITERATE_NORWEGIAN:
        search_q = _transliterate_norwegian(search_q)
    if not search_q:
        return []
    tokens = [t for t in _tokenize(raw) if not _SEASON_TOKEN_RE.match(t)]
    phrase = _sanitize_phrase(raw) if strict_requested else None
    if single_shot:
        try:
            data = c._search_once(
                search_q, "VIDEO", 1, 250, "relevance", "-",
                0, timeout=FALLBACK_BUDGET_SECONDS, nonce=random.random(),
            )
        except Exception as exc:  # noqa: BLE001 — a failed backup is harmless
            logger.debug("Fallback query %r failed: %s", search_q, exc)
            if status is not None:
                status["degraded"] = True
            return []
    else:
        data = c.search_hedged(
            query=search_q, file_type="VIDEO", per_page=250,
            sort_field="relevance", sort_dir="-",
        )
    return filter_and_map(
        data, min_bytes=min_bytes, query_tokens=tokens, query_meta=query_meta,
        strict_phrase=phrase, strict_match=strict_requested, require_subs=require_subs,
    )


def _run_fallback_search(
    c: EasynewsClient,
    title: str,
    suffix: str,
    *,
    query_meta: Dict[str, Optional[Any]],
    min_bytes: int,
    require_subs: Optional[List[str]],
    strict_requested: bool,
    extra_terms: Optional[List[str]] = None,
) -> "tuple[List[dict], bool]":
    """0-result backup stage. Builds candidate queries from alternate
    spellings + curated alternate titles (+ optional extra terms), runs them
    concurrently (bounded), and returns the merged, hash-deduped items plus a
    `degraded` flag (True if any backup query errored/429'd, so the caller won't
    cache a resulting empty). Only called when the primary search found nothing,
    so it costs zero in the common case."""
    candidates: List[str] = []

    def _add(t: str) -> None:
        cand = " ".join(p for p in (t, suffix) if p).strip()
        if cand and cand not in candidates:
            candidates.append(cand)

    # Spelling/alias candidates only when the full fallback is enabled. When only
    # EXTRA_TERMS_FALLBACK_ONLY is on, the fan-out is the extra terms alone.
    if FALLBACK_SEARCH and FALLBACK_TRANSLITERATE:
        for v in _spelling_variants(title):
            _add(v)
    if FALLBACK_SEARCH and FALLBACK_ALT_TITLES:
        for alt in _alternative_titles(title):
            _add(alt)
            for v in _spelling_variants(alt):  # comprehensive: also fold the aliases
                _add(v)
    for term in (extra_terms or []):
        base = " ".join(p for p in (title, suffix) if p).strip()
        if base:
            cand = f"{base} {term}".strip()
            if cand not in candidates:
                candidates.append(cand)

    if not candidates:
        return [], False
    candidates = candidates[:FALLBACK_MAX_QUERIES]
    logger.info("Fallback search: 0 primary results, trying %d backup query/ies: %s",
                len(candidates), "; ".join(candidates))

    merged: List[dict] = []
    seen: Set[str] = set()
    lock = threading.Lock()
    status: Dict[str, bool] = {"degraded": False}

    def _run(cq: str) -> None:
        items = _search_and_filter(
            c, cq, query_meta=query_meta, min_bytes=min_bytes,
            require_subs=require_subs, strict_requested=strict_requested,
            single_shot=True, status=status,
        )
        if not items:
            return
        with lock:
            for it in items:
                h = it.get("hash")
                if h and h not in seen:
                    seen.add(h)
                    merged.append(it)

    threads = [
        threading.Thread(target=_run, args=(cq,), name=f"ez-fallback-{i}", daemon=True)
        for i, cq in enumerate(candidates)
    ]
    for th in threads:
        th.start()
    # One shared deadline for the whole backup stage — a per-thread join timeout
    # would accumulate (N hung backups ⇒ N× the budget of request wall-clock).
    join_deadline = time.monotonic() + FALLBACK_BUDGET_SECONDS + 1.0
    for th in threads:
        th.join(timeout=max(0.0, join_deadline - time.monotonic()))
    if merged:
        logger.info("Fallback search recovered %d result(s).", len(merged))
    return merged, status["degraded"]


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
        if QUERY_REPLACE and base_query:
            rewritten = _apply_query_replace(base_query, QUERY_REPLACE)
            if rewritten != base_query:
                logger.info("Query override: %r -> %r", base_query, rewritten)
                base_query = rewritten
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
        # Hard floor: 100 MB minimum file size. A request's &minsize=<MB> can
        # only RAISE it — lowering below 100 requires a code change here.
        min_size_param = request.args.get("minsize")
        min_size_mb = 100
        if min_size_param:
            try:
                min_size_mb = max(100, int(min_size_param))
            except ValueError:
                min_size_mb = 100
        min_bytes = min_size_mb * 1024 * 1024

        # Per-request subtitle-language requirement (&subs=nor or &subs=nor,swe).
        # Overrides the global EASYNEWS_REQUIRE_SUBS; &subs= (empty) disables it
        # for this request.
        subs_param = request.args.get("subs")
        if subs_param is None:
            subs_param = request.args.get("requiresubs")
        require_subs = (
            _parse_langs_csv(subs_param) if subs_param is not None else REQUIRE_SUBS
        )

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
            # Serve repeated queries from the short-TTL result cache —
            # Prowlarr/Sonarr/Radarr poll constantly. Key on everything that
            # changes the output; offset/limit are sliced AFTER, so paging the
            # same search reuses one entry.
            cache_key = json.dumps(
                {"q": q, "min": min_bytes, "strict": strict_requested,
                 "subs": sorted(require_subs or [])},
                sort_keys=True, ensure_ascii=False,
            )
            cached = _cache_get(cache_key)
            if cached is not None:
                items = cached
                logger.info("Search %r served from cache → %d item(s)", q, len(items))
            else:
                # Only a cache miss needs the Easynews client — a hit must never
                # block on _CLIENT_LOCK (held for the whole login on cold start).
                c = client()
                search_start = time.time()
                # Sanitise the outbound query (a client quirk like "Avengers
                # --1080p" shouldn't become a broken Easynews query) and drop
                # connector stopwords; filtering still uses the raw query.
                search_q = _clean_search_query(q)
                if STRIP_STOPWORDS:
                    search_q = _strip_search_stopwords(search_q)
                # Fold Norwegian letters so we ask Easynews for the ASCII form
                # releases are posted under (a "Trøst" search → "Troest").
                if TRANSLITERATE_NORWEGIAN:
                    search_q = _transliterate_norwegian(search_q)

                # Extra-term searches (EASYNEWS_EXTRA_TERMS) run CONCURRENTLY with
                # the bare query — unless deferred to the 0-result fallback via
                # EASYNEWS_EXTRA_TERMS_FALLBACK_ONLY.
                aug_holder: Dict[str, Any] = {"rows": [], "degraded": False}
                aug_thread: Optional[threading.Thread] = None
                aug_started = 0.0
                if EXTRA_TERMS and search_q and not EXTRA_TERMS_FALLBACK_ONLY:
                    aug_queries = [f"{search_q} {term}".strip() for term in EXTRA_TERMS]

                    def _run_aug() -> None:
                        rows, degraded = c.search_queries(
                            aug_queries, file_type="VIDEO", per_page=250,
                            sort_field="relevance", sort_dir="-",
                        )
                        aug_holder["rows"] = rows
                        aug_holder["degraded"] = degraded

                    aug_thread = threading.Thread(
                        target=_run_aug, name="ez-extra-terms", daemon=True
                    )
                    aug_started = time.monotonic()
                    aug_thread.start()

                # Latency-bounded, hedged search: first real results, hard-capped
                # under NZBHydra's timeout so a slow Easynews != "0 results".
                data = c.search_hedged(
                    query=search_q, file_type="VIDEO", per_page=250,
                    sort_field="relevance", sort_dir="-",
                )
                # Optional extra pages (EASYNEWS_PAGINATE); respects numPages so a
                # single-page query costs zero extra calls.
                if paginate_enabled() and data.get("data"):
                    try:
                        total_pages = int(data.get("numPages") or 1)
                    except (TypeError, ValueError):
                        total_pages = 1
                    pages_wanted = min(max_pages(), max(total_pages, 1))
                    if pages_wanted > 1:
                        extra_rows = c.fetch_more_pages(
                            search_q, file_type="VIDEO", per_page=250,
                            sort_field="relevance", sort_dir="-",
                            start_page=2, max_pages=pages_wanted,
                        )
                        if extra_rows:
                            data = {**data, "data": list(data.get("data") or []) + extra_rows}
                # Merge the concurrent extra-term rows (prepended so they survive
                # the limit cut; same filtering still drops off-target shows).
                if aug_thread is not None:
                    # Grace measured from when the aug queries STARTED (they run
                    # concurrently with the hedged search) — anchoring it here
                    # would stack it on top of the search budget and could push
                    # the response past NZBHydra's timeout.
                    aug_thread.join(timeout=max(
                        0.0, aug_started + _SEARCH_ATTEMPT_TIMEOUT + 1.0 - time.monotonic()
                    ))
                    aug_rows = aug_holder.get("rows") or []
                    if aug_rows:
                        data = {**data, "data": list(aug_rows) + list(data.get("data") or [])}
                        logger.info(
                            "Extra-term search added %d row(s) from term(s): %s",
                            len(aug_rows), ", ".join(EXTRA_TERMS),
                        )
                raw_count = len(data.get("data", []))
                # A degraded search (budget hit / all attempts errored / throttled)
                # is NOT a confirmed "0 results" — caching its empty would hide real
                # results for the whole TTL (the bug where a slow/429'd query poisons
                # the cache with a false negative). Only genuine, complete responses
                # (including a fast genuine empty) are cacheable.
                # Degraded if the main hedged search degraded OR any extra-term
                # query errored/429'd (those often surface the subtitle-filtered
                # releases, so a failed one can fake a 0 once require_subs prunes
                # the rest). Either way, don't persist the result.
                search_incomplete = bool(data.get("_incomplete")) or bool(
                    aug_holder.get("degraded")
                )
                items = filter_and_map(
                    data, min_bytes=min_bytes, query_tokens=query_tokens,
                    query_meta=query_meta, strict_phrase=strict_phrase,
                    strict_match=strict_requested, require_subs=require_subs,
                )
                subs_note = f", subs={'+'.join(require_subs)}" if require_subs else ""
                logger.info(
                    "Search %r [%s%s] → %d raw results, %d passed filters, in %.1fs",
                    q, _active_endpoint_label(), subs_note, raw_count, len(items),
                    time.time() - search_start,
                )

                # 0-result fallback — only when the primary found nothing,
                # so the extra Easynews requests never fire on a query that already
                # returned results.
                want_fallback = FALLBACK_SEARCH or (EXTRA_TERMS_FALLBACK_ONLY and bool(EXTRA_TERMS))
                fb_title = base_query
                fb_suffix = (
                    " ".join(search_components[1:]).strip()
                    if len(search_components) > 1 else ""
                )
                if not items and want_fallback and fb_title:
                    fb_extra = EXTRA_TERMS if (EXTRA_TERMS_FALLBACK_ONLY and EXTRA_TERMS) else None
                    fb_items, fb_degraded = _run_fallback_search(
                        c, fb_title, fb_suffix, query_meta=query_meta,
                        min_bytes=min_bytes, require_subs=require_subs,
                        strict_requested=strict_requested, extra_terms=fb_extra,
                    )
                    if fb_items:
                        items = fb_items
                    # A degraded backup that recovered nothing is also a false 0.
                    if fb_degraded and not fb_items:
                        search_incomplete = True

                # Never cache a degraded/best-effort result (it may be a false
                # empty from a slow or throttled Easynews). A clean response —
                # whether it has rows or is a genuine empty — is cached as normal.
                if not search_incomplete:
                    _cache_put(cache_key, items)
                elif items:
                    # Degraded but we still scraped something back — serve it, just
                    # don't persist it, so the next poll re-checks Easynews.
                    logger.info("Search %r degraded; serving %d item(s) uncached.", q, len(items))
                else:
                    logger.info("Search %r degraded with no data; not caching (will retry).", q)

        items = items[offset : offset + limit]

        display_q = raw_query if raw_query else q
        chan_title = f"Results for {display_q}"
        now_dt = datetime.now(timezone.utc)
        channel_pub = now_dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        # Request-invariant pieces of the per-item links, hoisted out of the loop.
        url_base = request.url_root.rstrip("/")
        apikey = request.args.get("apikey")

        header = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            "<channel>"
            f"<title>{xml_escape(chan_title)}</title>"
            f"<description>{xml_escape(chan_title)}</description>"
            f"<link>{url_base}/api</link>"
            f"<pubDate>{channel_pub}</pubDate>"
        )

        body_parts: List[str] = []
        for it in items:
            enc_id = encode_id(it)
            title = xml_escape(it["title"]) if it["title"] else "Untitled"
            link = f"{url_base}/api?t=get&id={enc_id}&apikey={apikey}"
            safe_link = xml_escape(link)
            size = it["size"]
            direct_url = it.get("direct_url")
            guid = enc_id
            comments_xml = f"<comments>{xml_escape(direct_url)}</comments>" if direct_url else ""
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
            # Language/codec metadata. AIOStreams reads "subs" as subtitle
            # languages and "language" as audio languages; emit one comma-joined
            # value per attr (duplicate attr names get overwritten downstream).
            if META_SUBS and it.get("subs"):
                attr_parts.append(f'<newznab:attr name="subs" value="{xml_escape(it["subs"])}"/>')
            if META_AUDIO and it.get("audio_langs"):
                attr_parts.append(f'<newznab:attr name="language" value="{xml_escape(it["audio_langs"])}"/>')
            if META_CODECS and it.get("vcodec"):
                attr_parts.append(f'<newznab:attr name="video" value="{xml_escape(it["vcodec"])}"/>')
            if META_CODECS and it.get("acodec"):
                attr_parts.append(f'<newznab:attr name="audio" value="{xml_escape(it["acodec"])}"/>')
            if META_BITRATE and it.get("bitrate"):
                attr_parts.append(f'<newznab:attr name="bitrate" value="{xml_escape(it["bitrate"])}"/>')
            if META_GROUP and it.get("group"):
                attr_parts.append(f'<newznab:attr name="group" value="{xml_escape(it["group"])}"/>')
            if META_PASSWORD:
                attr_parts.append(f'<newznab:attr name="password" value="{"1" if it.get("password") else "0"}"/>')
            attr_xml = "".join(attr_parts)
            item_xml = (
                f"<item>"
                f"<title>{title}</title>"
                f'<guid isPermaLink="false">{guid}</guid>'
                f"<link>{safe_link}</link>"
                f"{comments_xml}"
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
