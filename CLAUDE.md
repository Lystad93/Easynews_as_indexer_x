# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo. This file is the
source of truth for *how this service works and ships* — things not obvious from
the code alone. For *what changed and why*, read `git log` (commit messages are
kept descriptive on purpose).

## What this is

A small **Newznab-API bridge** that lets Usenet automation tools (NZBHydra2,
Prowlarr, Sonarr, Radarr) search **Easynews** as if it were a Newznab indexer.
There is no build step beyond the Docker image — the repo *is* the artifact.

- `server.py` — Flask app exposing `/api` (Newznab: `t=caps|search|movie|tvsearch|get`).
  Translates client queries → Easynews search, maps/filters results → Newznab RSS,
  and serves `.nzb` files for downloads.
- `easynews_client.py` — `EasynewsClient`: login, search, NZB build/download against
  `members.easynews.com`.

## Data flow

```
NZBHydra/Sonarr/Radarr ──GET /api?t=search&q=…&apikey=… ──▶ server.py
        server.py ──▶ EasynewsClient.search_hedged() ──▶ members.easynews.com/2.0/search/solr-search
                                                          (endpoint configurable: 2.0 | 3.0, see Configuration)
        server.py ◀── filter_and_map() → Newznab RSS
download: client ──GET /api?t=get&id=… ──▶ decode id ──▶ POST …/2.0/api/dl-nzb ──▶ .nzb
```

- **Auth to Easynews is HTTP Basic Auth** set on the requests session
  (`self.s.auth`). `login()` primes/validates the session; **searches keep working
  on Basic Auth even if the cookie refresh is stale**, so a refresh failure is not
  fatal.
- `server.py` holds a single process-global `EasynewsClient` (`_CLIENT`) shared
  across threads.

## Runtime & deploy

- Image: `python:3.11-slim`, run via
  `gunicorn --workers 1 --threads 4 --timeout 90 server:APP` on `${PORT}` (default 8081).
- **1 worker / 4 threads is intentional** — the shared `_CLIENT` (and its logged-in
  session + background refresh) lives in one process. More workers ⇒ each worker
  logs in separately and the keepalive/session-sharing assumptions break.
- CI: pushing to `main` triggers the **"Build and Push Docker Image"** GitHub
  Action → `ghcr.io/lystad93/easynews_as_indexer_x:latest`.
- Deploy/update on the VPS (compose service `easynews-indexer`, profiles
  `easynews-indexer` / `all`):
  ```bash
  docker compose --profile easynews-indexer pull
  docker compose --profile easynews-indexer up -d
  ```
- **This repo is the only source of truth.** Don't trust or edit stray local copies.

## Configuration (`.env`, injected via compose `env_file`)

See `.env.example` for the full annotated list. Summary:

| Variable | Default | Purpose |
|---|---|---|
| `EASYNEWS_USER` / `EASYNEWS_PASS` | — | Easynews account (required) |
| `NEWZNAB_APIKEY` | `testkey` | Key clients use to call this bridge — change it |
| `PORT` | `8081` | Listen port |
| `SEARCH_BUDGET_SECONDS` | `3.3` | Hard cap on total time per search |
| `SEARCH_HEDGE_AFTER_SECONDS` | `1.2` | Fire a parallel "hedge" request if Easynews is slower than this |
| `SEARCH_ATTEMPT_TIMEOUT_SECONDS` | `2.5` | Per-request read timeout |
| `IGNORE_SEASON_PACKS` | `false` | Skip season-only queries (season, no episode) — Easynews rarely has real packs |
| `EASYNEWS_SEARCH_API` | `2.0` | Search endpoint: `2.0` (`/2.0/search/solr-search/`) or `3.0` (`/3.0/api/search`, no trailing slash) |
| `EASYNEWS_BASE_URL` | `https://members.easynews.com` | Override the Easynews host |
| `EASYNEWS_SEARCH_URL_TEMPLATE` | — | Full search-URL override (wins over `SEARCH_API`); placeholders `{base}{query}{page}{per_page}` |
| `EASYNEWS_RESULTS_KEY` | `data` | Top-level JSON key holding result rows |
| `EASYNEWS_LOG_LATENCY` | `false` | Log active endpoint + per-request latency at INFO |
| `EASYNEWS_DISABLE_FILTERS` | `false` | Skip title-matching filters (validity/size/virus/duration still apply) |
| `EASYNEWS_META_SUBS` | `true` | Emit subtitle langs as `newznab:attr name="subs"` |
| `EASYNEWS_META_AUDIO` | `true` | Emit audio langs as `newznab:attr name="language"` |
| `EASYNEWS_META_CODECS` | `true` | Emit video/audio codecs as `newznab:attr name="video"`/`"audio"` |
| `EASYNEWS_PAGINATE` | `false` | Fetch extra search pages (concurrent, hash-deduped) — adds latency |
| `EASYNEWS_MAX_PAGES` | `1` | Pages to fetch when `EASYNEWS_PAGINATE` is on |

All of the above are read at **container start** (change `.env` → `up -d`, no
rebuild). Keep this invariant true:
`HEDGE_AFTER < ATTEMPT_TIMEOUT ≤ BUDGET < client's indexer timeout`.

**Search endpoint (2.0 vs 3.0).** `EASYNEWS_SEARCH_API` switches between the proven
`/2.0/search/solr-search/` and the newer JSON `/3.0/api/search`. Default is `2.0`,
so behaviour is unchanged unless you opt in. Confirmed against a live account:
the 3.0 path takes **no trailing slash** (a trailing slash 404s to the web-app
HTML), accepts the same params as 2.0 (we keep `vv=1`, which is what returns the
runtime/codec/language metadata), and returns a leaner JSON payload (~3x smaller)
with the same `data` rows. If a future param change breaks it, grab the real
request from the 3.0 web UI's DevTools and set `EASYNEWS_SEARCH_URL_TEMPLATE` (no
code change). The NZB **download** path stays on `/2.0/api/dl-nzb` regardless.
`EASYNEWS_LOG_LATENCY` and the per-search log line (`Search 'x' [api 3.0] → …`)
are there to compare speed; `easynews_endpoint_benchmark.sh` does an A/B curl test.

**Language/codec metadata.** `subs`/`language`/`video`/`audio` attrs are populated
from Easynews's named JSON fields (`subtitle_tracks`/`slangs`, `audio_tracks`/`alangs`,
`vcodec`, `acodec`), which only exist on the 3.0 api and the 2.0 *dict* response form
(not the positional array form). AIOStreams reads `subs` (subtitles) and `language`
(audio). **Caveat:** if results flow through NZBHydra2, it must be configured to pass
these attrs through to the downstream client.

Still hardcoded (change in code if needed): `_CLIENT_LOGIN_TTL=1800` (server.py),
min file size `100` MB (also per-request via `&minsize=`), `_MIN_DURATION_SECONDS=60`,
`_LOGIN_TIMEOUT=15`, `_DOWNLOAD_TIMEOUT=60`, `_SEARCH_TIMEOUT=30` (legacy/NZB path only).

## NZBHydra2 integration

- NZBHydra's per-indexer **Timeout (4 s) must stay above `SEARCH_BUDGET_SECONDS`**.
  If the bridge can exceed it you get `EasyH: timeout. Code: 0` → 0 results.
- An external `easynews-keepalive` container queries NZBHydra ~every 20 min
  (query "bridge to terabithia 2007") to keep the stack warm.

## Why the search path looks the way it does

- **Login refresh is non-blocking** (background thread) + a **startup warm-up
  login**, so a search never blocks on a slow/flaky Easynews login. Inline login
  on the request path was the original cause of downstream ~4 s timeouts.
- **`search_hedged()`** returns the first *real* results and is hard-capped under
  `SEARCH_BUDGET_SECONDS`; if Easynews is slow it races a fresh parallel request
  instead of returning a spurious "0 results". Genuinely-empty results can't be
  invented.
- **Season-only searches** (e.g. `From S04`) parse a bare `Sxx` into metadata and
  do *not* require an `s04` literal token (it never appears in `…s04e08…` names).

## Local dev / verify

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python3 -m py_compile server.py easynews_client.py
# smoke test (Flask dev server):
EASYNEWS_USER=… EASYNEWS_PASS=… NEWZNAB_APIKEY=key .venv/bin/python server.py
curl 'http://localhost:8081/api?t=caps'
curl 'http://localhost:8081/api?t=search&q=matrix&apikey=key'
```

## Conventions

- Keep changes minimal and in the surrounding style.
- Write descriptive commit messages — they are this project's change log.
- End commit messages with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
