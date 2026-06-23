# Easynews → Newznab indexer (`_x` fork)

A Flask server that bridges **Easynews** search to a **Newznab-like API**, so you can add Easynews as a custom indexer in **Prowlarr / NZBHydra2** and pull NZBs from it (and feed it into **AIOStreams**, Stremio, etc.). Video-only, sorted by relevance.

This is a fork of [**Sanket9225/Easynews_as_indexer**](https://github.com/Sanket9225/Easynews_as_indexer). It keeps the same `/api` Newznab surface but is re-tuned for **reliability behind a strict downstream timeout** (NZBHydra2's ~4 s) and for **surfacing language-tagged releases** (Nordic/Norwegian-subbed in current compose change to what you like. Anime focused search very limited testing done here as i dont watch anime). All new behaviour is opt-in or matches the original's defaults, so it's a drop-in replacement.

Including passworded releases "idea" was taken from munif/stremthru dev's research, read him talking about it in either his discord chat or aiostreams, if I understood correctly most of the password tagged releases werent in fact password protected, I havent encountered any issues for the time being. 
Utilizing the API 3.0 endpoint was taken from  "nanzepanze" user in munif's discord chat. 

Probably some other things are from either virens aiostream easynews builtin addon code or munif stremthru easynews tag, as I asked claude to review both to look for anything useful. But base code is from sankets easynews-indexer repo. 

---

## Key differences from the original

| Area | Original | This fork (`_x`) |
|---|---|---|
| Login on the request path | Re-logs in **inline/synchronously** when its 600 s TTL lapses — that search blocks on the login | **Background** login refresh (30 min TTL) + startup warm-up — searches never wait on a re-login |
| Slow / hung response | Single request, 30 s read timeout, one attempt | **Latency-bounded hedged search** — short per-attempt timeout, fires a parallel "hedge" if Easynews is slow, whole call capped by a budget |
| Empty results | Single request — an empty (or timed-out) response returns as 0 results | **Trust-empty fast path** (default on) — a valid "0 results" returns immediately instead of firing the remaining hedged attempts |
| Easynews endpoint | 2.0 solr search only | **2.0 or 3.0 JSON API** (`EASYNEWS_SEARCH_API`), with full URL override + a benchmark script |
| Language metadata | — | **Audio/subtitle/codec `newznab:attr`** so downstream tools can filter by language |
| Language targeting | — | **Subtitle filter** (`EASYNEWS_REQUIRE_SUBS`) + **extra search terms** (`EASYNEWS_EXTRA_TERMS=nordic`) to surface buried subbed releases |
| Docker image | `ghcr.io/sanket9225/easynews_as_indexer` | `ghcr.io/lystad93/easynews_as_indexer_x:latest` |
| Compose | Minimal (`ports`, one env var) | **Profile-gated** (`easynews-indexer` / `all`), `env_file`, `restart`, `expose`, multi-instance examples |
| Gunicorn | `--workers 4` | `--workers 1 --threads 4 --timeout 90` (threaded, so the background refresh/hedging share one process) |

### What's new, grouped

**Reliability (the main reason for the fork)**
- **Background login refresh + warm-up** — the original re-logs in **inline on the request path** when its session TTL (600 s) lapses (`client()` in `server.py`), so that one search blocks on the login — and if the login fails it builds a fresh client and logs in *again*, still inline. The fork moves the refresh to a background thread (TTL 30 min) and warms the session up at startup, so a search never waits on a login.
- **Hedged, time-budgeted search** — *new in this fork; the original issues a single request with a 30 s read timeout and no retry.* Each search is capped by `SEARCH_BUDGET_SECONDS` (default `3.3`). If Easynews hasn't answered within `SEARCH_HEDGE_AFTER_SECONDS` (default `1.2`), a parallel request is fired and the first good answer wins; `SEARCH_ATTEMPT_TIMEOUT_SECONDS` (default `2.5`) kills a single hung connection. The budget is meant to sit **under your downstream indexer's timeout** (e.g. NZBHydra2's, which I run at ~4 s) so a slow Easynews response can't read as 0 results downstream.
- **Trust-empty fast path** (`SEARCH_TRUST_EMPTY`, default on) — a valid HTTP-200 "0 results" is returned immediately instead of firing the remaining hedged attempts (up to 3), which drops no-result queries from ~3.3 s to ~0.3 s. Set it `false` to restore the interim 3-try-on-empty behaviour. A genuine hang surfaces as an error and is still retried/hedged.

**Search endpoint flexibility**
- **Easynews 3.0 JSON API support** — `EASYNEWS_SEARCH_API=2.0|3.0`. The 3.0 endpoint returns a leaner payload (~3× smaller). `EASYNEWS_SEARCH_URL_TEMPLATE`, `EASYNEWS_BASE_URL`, `EASYNEWS_RESULTS_KEY`, and `EASYNEWS_LOG_LATENCY` let you override host/URL/JSON shape and log per-request latency.
- **`easynews_endpoint_benchmark.sh`** — curls both endpoints and times them so you can A/B which is faster for you.
- **Optional multi-page search** — `EASYNEWS_PAGINATE=true` + `EASYNEWS_MAX_PAGES=<n>` fetch extra pages concurrently (deduped, respects `numPages` so single-page queries cost nothing extra).

**Result quality & language targeting** — the original's title-matching and validity filters (strict phrase, season/episode/year/quality, query-token subset, min size, 60 s min duration, VIDEO type/extension, virus) are **unchanged**. The fork adds toggles around them, plus hash dedup and language handling:
- **Language/codec metadata** as `newznab:attr` (`EASYNEWS_META_SUBS` → `subs`, `EASYNEWS_META_AUDIO` → `language`, `EASYNEWS_META_CODECS` → `video`/`audio`; all on by default). AIOStreams can then filter by, e.g., Norwegian (`nor`) subtitles.
- **Subtitle-language filter** — `EASYNEWS_REQUIRE_SUBS=nor` (or per-request `&subs=nor,swe`) keeps only releases Easynews reports as having matching subtitle tracks.
- **Extra search terms** — `EASYNEWS_EXTRA_TERMS=nordic,norsk` runs `"<query> <term>"` in parallel and merges, pulling language-tagged releases that relevance ranking would otherwise bury past page 1.
- **Stopword stripping** (`EASYNEWS_STRIP_STOPWORDS`, default on) — drops connectors (`and`, `of`, `the`…) from the query, since Easynews AND-matches every word and a missing word zeroes out the search. Filtering still uses the full token set, so precision is unchanged.
- **Password-protected handling** (`EASYNEWS_ALLOW_PASSWORD`, default off) — by default the fork drops password-flagged items exactly like the original; turn it on to keep them (the flag is often a false positive on video). Virus-flagged items are **always** dropped, in both.
- **Hash dedup** — the original returns duplicate hashes as-is; the fork drops repeats (keep-first by default), or keeps the newest post of an identical hash with `EASYNEWS_DEDUP_KEEP_NEWEST` (older identical posts are often dead).
- **Season-pack control** (`IGNORE_SEASON_PACKS`) — skip season-only queries (`Show S05` with no episode) that Easynews rarely fulfils, plus a fix for season-only searches returning nothing.
- **Disable filters** (`EASYNEWS_DISABLE_FILTERS`) — pass through everything Easynews returns (validity/size/virus/duration checks still apply) if your downstream client does its own matching.

**Packaging / deployment**
- Image published to `ghcr.io/lystad93/easynews_as_indexer_x:latest` (`linux/amd64` + `linux/arm64`).
- `docker-compose.yaml` is profile-gated (`easynews-indexer` / `all`), uses `env_file: .env`, `restart: unless-stopped`, and `expose` (built to sit behind a reverse proxy), with commented-out **Nordic** and **anime** tuned instances you can run alongside the default.
- Gunicorn runs `--workers 1 --threads 4 --timeout 90` so the background refresh and hedging work within a single process.
- CI action versions pinned (Node 24-compatible).

> Every new variable defaults to the original's behaviour (or off). See **[`.env.example`](.env.example)** for the fully annotated list — that file is the source of truth for defaults.

---

## Setup (Docker Compose — recommended)

1. Copy and fill in the env file:

   ```bash
   cp .env.example .env
   # edit .env: set EASYNEWS_USER, EASYNEWS_PASS, and change NEWZNAB_APIKEY
   ```

2. Start it:

   ```bash
   docker compose --profile easynews-indexer up -d
   ```

The shipped `docker-compose.yaml` uses `expose: ["8081"]` (not published to the host) because it's designed to sit behind a reverse proxy. If you're running it standalone, either add a `ports: ["8081:8081"]` mapping or use the `docker run` form below.

To apply changes to `.env` afterwards, just re-run the `up -d` — no rebuild needed:

```bash
docker compose --profile easynews-indexer pull   # grab the latest image
docker compose --profile easynews-indexer up -d
```

## Setup (Docker, standalone)

```bash
docker run --rm -d -p 8081:8081 \
    -e EASYNEWS_USER=your_easynews_username \
    -e EASYNEWS_PASS=your_easynews_password \
    -e NEWZNAB_APIKEY=testkey \
    ghcr.io/lystad93/easynews_as_indexer_x:latest
```

Tail logs with `docker logs -f <container-id>`.

## Setup (Local)

1. Create and activate a Python 3.11+ virtual environment:

   ```bash
   # Linux / macOS
   python3 -m venv .venv && source .venv/bin/activate
   # Windows (PowerShell)
   python -m venv .venv; .venv\Scripts\Activate.ps1
   ```

2. Install dependencies and configure `.env`:

   ```bash
   pip install -r requirements.txt
   cp .env.example .env   # then edit credentials
   ```

3. Run the server:

   ```bash
   python server.py
   ```

   It starts on `http://127.0.0.1:8081`.

## Endpoints

- **Caps:** `GET /api?t=caps&apikey=<key>`
- **Search (video-only):** `GET /api?t=search&q=<query>&apikey=<key>&limit=<n>&minsize=<MB>`
  - Defaults: `limit=100`, `minsize=100` (MB)
  - Also supports `t=movie` and `t=tvsearch`
  - **Strict matching** is on by default for `t=movie`/`t=tvsearch` (title must contain all query words), off for plain `t=search`; override per request with `strict=0|1`
  - `t=movie` accepts `year=<YYYY>`; `t=tvsearch` accepts `season=<NN>` and `ep=<NN>` (appended as `SxxEyy`)
  - Per-request subtitle filter: `&subs=nor` (or `&subs=nor,swe`; `&subs=` disables it for that request)
- **Download NZB:** `GET /api?t=get&id=<encoded>&apikey=<key>` — filename equals the item title

## Configuration

Credentials are required; everything else is optional and read at startup. Set them in `.env` (or `-e` flags) and restart. See **[`.env.example`](.env.example)** for the full annotated reference.

| Variable | Default | Purpose |
|---|---|---|
| `EASYNEWS_USER` / `EASYNEWS_PASS` | — (required) | Easynews account |
| `NEWZNAB_APIKEY` | `testkey` | Key clients use to authenticate to this server — change it |
| `PORT` | `8081` | Listen port |
| `SEARCH_BUDGET_SECONDS` | `3.3` | Hard cap per search — keep under your indexer's timeout |
| `SEARCH_HEDGE_AFTER_SECONDS` | `1.2` | Fire a parallel hedge request after this delay |
| `SEARCH_ATTEMPT_TIMEOUT_SECONDS` | `2.5` | Per-request read timeout |
| `SEARCH_TRUST_EMPTY` | `true` | Return a fast valid "0 results" instead of retrying |
| `EASYNEWS_KEEPALIVE` | `true` | Keep a warm TLS connection during idle gaps; `false` disables the background ping |
| `EASYNEWS_KEEPALIVE_INTERVAL_SECONDS` / `_IDLE_SECONDS` | `45` / `40` | Keepalive ping cadence and idle threshold |
| `EASYNEWS_SEARCH_API` | `2.0` | `2.0` (solr) or `3.0` (newer JSON API) |
| `EASYNEWS_SEARCH_URL_TEMPLATE` | — | Full search-URL override (`{base}`/`{query}`/`{page}`/`{per_page}`) |
| `EASYNEWS_META_SUBS` / `_AUDIO` / `_CODECS` | `true` | Emit subtitle/audio/codec `newznab:attr` |
| `EASYNEWS_REQUIRE_SUBS` | — | Keep only releases with matching subtitle tracks (e.g. `nor`) |
| `EASYNEWS_EXTRA_TERMS` | — | Extra parallel `"<query> <term>"` searches (e.g. `nordic`) |
| `EASYNEWS_STRIP_STOPWORDS` | `true` | Drop connector words from the Easynews query |
| `EASYNEWS_ALLOW_PASSWORD` | `false` | Keep password-flagged results (virus-flagged always dropped) |
| `EASYNEWS_DEDUP_KEEP_NEWEST` | `false` | On identical-hash dupes, keep the newest post |
| `IGNORE_SEASON_PACKS` | `false` | Skip season-only queries Easynews rarely has |
| `EASYNEWS_DISABLE_FILTERS` | `false` | Skip title-matching filters (validity checks stay on) |
| `EASYNEWS_PAGINATE` / `EASYNEWS_MAX_PAGES` | `false` / `1` | Fetch extra result pages (deduped) |

## Integration

**Prowlarr / NZBHydra2** — add a Newznab (generic) indexer:
- URL: `http://<host>:8081` (or the container name on your Docker network, e.g. `http://easynews-indexer:8081` or `http://easynews-indexer-anime:8081` etc.. )
- API Key: the `NEWZNAB_APIKEY` from your `.env`

> If you route through **NZBHydra2**,  Keep Hydra's own indexer timeout at ~4 s, comfortably above `SEARCH_BUDGET_SECONDS`.

**AIOStreams** — reads the `subs` attr as subtitle languages and `language` as audio languages, so the language metadata this fork emits lets you filter (e.g. to Norwegian subs).

## Credits

Forked from [**Sanket9225/Easynews_as_indexer**](https://github.com/Sanket9225/Easynews_as_indexer) — all credit for the original bridge goes to the upstream author. If the original project helps you, consider [buying them a coffee ☕](https://buymeacoffee.com/gaikwadsank).
