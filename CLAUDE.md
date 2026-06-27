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
| `SEARCH_TRUST_EMPTY` | `true` | Treat a fast successful "0 results" as final (no-result queries ~0.3s vs ~3.3s); `false` = old 3-try-on-empty |
| `EASYNEWS_KEEPALIVE` | `true` | Background thread keeps a warm TLS connection during idle gaps; `false` disables it (search pays the handshake after an idle gap) |
| `EASYNEWS_KEEPALIVE_INTERVAL_SECONDS` | `45` | How often the keepalive thread wakes to maybe ping |
| `EASYNEWS_KEEPALIVE_IDLE_SECONDS` | `40` | Only ping after this many seconds of no real search traffic |
| `IGNORE_SEASON_PACKS` | `false` | Skip season-only queries (season, no episode) — Easynews rarely has real packs |
| `EASYNEWS_SEARCH_API` | `2.0` | Search endpoint: `2.0` (`/2.0/search/solr-search/`) or `3.0` (`/3.0/api/search`, no trailing slash) |
| `EASYNEWS_BASE_URL` | `https://members.easynews.com` | Override the Easynews host |
| `EASYNEWS_SEARCH_URL_TEMPLATE` | — | Full search-URL override (wins over `SEARCH_API`); placeholders `{base}{query}{page}{per_page}` |
| `EASYNEWS_RESULTS_KEY` | `data` | Top-level JSON key holding result rows |
| `EASYNEWS_LOG_LATENCY` | `false` | Log active endpoint + per-request latency at INFO |
| `EASYNEWS_DISABLE_FILTERS` | `false` | Skip title-matching filters (validity/size/virus/duration still apply) |
| `EASYNEWS_DEDUP_KEEP_NEWEST` | `false` | On identical-hash duplicates, keep the newest post instead of the first (re-packed re-uploads differ in hash, so aren't merged anyway) |
| `EASYNEWS_ALLOW_PASSWORD` | `false` | Keep results Easynews flags password-protected (often a false positive; stremthru/AIOStreams keep them). Virus-flagged always dropped |
| `EASYNEWS_STRIP_STOPWORDS` | `true` | Drop connector words (and/of/the/…) from the query sent to Easynews — it AND-matches every word, so an expanded title ("Escha and Logy") otherwise zeroes out against releases named "Escha..Logy" |
| `EASYNEWS_TRANSLITERATE_NORWEGIAN` | `false` | Fold Norwegian ø→oe, å→aa, æ→ae (uppercase too) on the outbound query *and* both sides of the title filters, so an accented search ("Trøst") matches the ASCII-folded release ("Troest") releases are posted under. Symmetric fold, only touches æ/ø/å — non-Norwegian queries unaffected |
| `EASYNEWS_META_SUBS` | `true` | Emit subtitle langs as `newznab:attr name="subs"` |
| `EASYNEWS_META_AUDIO` | `true` | Emit audio langs as `newznab:attr name="language"` |
| `EASYNEWS_META_CODECS` | `true` | Emit video/audio codecs as `newznab:attr name="video"`/`"audio"` |
| `EASYNEWS_META_BITRATE` | `true` | Emit video bitrate as `newznab:attr name="bitrate"` (e.g. `12.72 Mbps`) from raw `bps` (2 dp; more precise than `prettyBps`) |
| `EASYNEWS_META_GROUP` | `true` | Emit Usenet group(s) as `newznab:attr name="group"` |
| `EASYNEWS_META_PASSWORD` | `true` | Emit password flag as `newznab:attr name="password"` (`1`/`0`) |
| `EASYNEWS_EXTRA_TERMS` | — | Comma-separated; also runs `<query> <term>` per term and merges (e.g. `nordic, danish` surfaces deep-ranked language-tagged releases on page 1) |
| `EASYNEWS_REQUIRE_SUBS` | — | Comma-separated lang codes (e.g. `nor`); keep only releases whose subtitle tracks include one. Per-request override: `&subs=nor` on the `/api` URL |
| `EASYNEWS_PAGINATE` | `false` | Fetch extra search pages (concurrent, hash-deduped) — adds latency |
| `EASYNEWS_MAX_PAGES` | `1` | Pages to fetch when `EASYNEWS_PAGINATE` is on |
| `EASYNEWS_ADVANCED_SEARCH` | `false` | Use the solr `/advanced` endpoint (`st=adv`) so Easynews filters spam (`spamf`) + non-whitelisted extensions (`fex`) server-side. Proven on 2.0; on 3.0 the params are sent but may be ignored (A/B test — log shows `[api 3.0+adv]`) |
| `EASYNEWS_SPAM_FILTER` | follows advanced | (advanced only) Send `spamf=1` to drop Easynews-flagged spam |
| `EASYNEWS_FILE_EXTENSIONS` | `m4v,3gp,…,webm` | (advanced only) Video-container whitelist sent as `fex=`; empty = don't send |
| `EASYNEWS_SORT_1` / `_DIR` | `relevance` / `-` | Primary sort key + direction (`-` desc, `+` asc). Fields: `relevance`, `dsize`, `dtime`, `dsubject` |
| `EASYNEWS_SORT_2` / `_DIR` | — / `-` | Secondary sort tie-breaker (empty = off) |
| `EASYNEWS_SORT_3` / `_DIR` | — / `-` | Tertiary sort tie-breaker (empty = off) |
| `EASYNEWS_MIN_DURATION_SECONDS` | `60` | Drop videos shorter than this (kills 2–5 min sample clips; try `360`) |
| `EASYNEWS_CACHE_TTL_SECONDS` | `0` | Cache final result list per query for N s (repeat polls served from memory). `0` = off. Keep short (60–180) so new posts aren't hidden |
| `EASYNEWS_CACHE_MAX_ENTRIES` | `512` | Max cached queries; oldest evicted first (LRU) |
| `EASYNEWS_FALLBACK_SEARCH` | `false` | On a 0-result query only, run backup searches from alternate spellings + alternate titles |
| `EASYNEWS_FALLBACK_TRANSLITERATE` | `true` | (fallback) include alternate ASCII spellings (Nordic+German digraph/bare-vowel + accent strip) |
| `EASYNEWS_FALLBACK_ALT_TITLES` | `true` | (fallback) include curated aliases from the custom-titles file |
| `EASYNEWS_CUSTOM_TITLES_FILE` | `custom-titles.json` | Path to the canonical→aliases JSON map |
| `EASYNEWS_FALLBACK_MAX_QUERIES` | `6` | Cap on concurrent backup queries |
| `EASYNEWS_FALLBACK_BUDGET_SECONDS` | `4.0` | Wall-clock budget for the backup stage (runs after the primary) |
| `EASYNEWS_EXTRA_TERMS_FALLBACK_ONLY` | `false` | Run `EXTRA_TERMS` only on a 0-result query, not on every search |

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

**Advanced search + sorting.** `EASYNEWS_ADVANCED_SEARCH=true` flips the 2.0
endpoint to `/2.0/search/solr-search/advanced` with `st=adv`, enabling
server-side spam filtering (`spamf`) and a file-extension whitelist (`fex`,
`EASYNEWS_FILE_EXTENSIONS`) so junk is dropped before our own filters run —
the same trick the upstream easynews-plus-plus Stremio addon uses. On the 3.0
api the path is unchanged and the params ride along experimentally (Easynews
may ignore them); the log line reads `[api 3.0+adv]` so you can A/B test whether
it actually helps. Sorting is now multi-level and env-driven via
`EASYNEWS_SORT_1/2/3` (+ `_DIR`); unset = the previous single relevance sort, so
defaults are unchanged. Both knobs are built entirely in `easynews_client.py`'s
URL builders, so `server.py` is untouched. Recommended order for an indexer:
`SORT_1=dsize`, `SORT_2=relevance`, `SORT_3=dtime`.

**Result cache.** `EASYNEWS_CACHE_TTL_SECONDS>0` turns on a process-global,
TTL-bounded LRU cache (`_SEARCH_CACHE` in `server.py`) of the final mapped+filtered
item list, keyed by the effective query + min-size + strict flag + `subs`. Repeat
polls (Prowlarr/Sonarr/Radarr RSS sync, re-runs, multiple apps) are served from
memory; `offset`/`limit` are sliced *after* the cache so paging one search reuses
one entry. The single-worker / shared-threads gunicorn setup means a plain dict is
shared across all request threads. Keep the TTL short — it caches empties too, so a
long TTL hides a just-posted release until expiry. Default `0` = off.

**0-result fallback (alternate spellings + titles).** `EASYNEWS_FALLBACK_SEARCH`
adds a backup stage that runs *only when the primary search returns nothing*, so
it never spends extra Easynews requests on a query that already worked. It builds
candidate queries from `_spelling_variants()` (multi-convention transliteration —
Nordic+German digraph, bare-vowel, and a full NFKD accent strip; empty for an
already-ASCII title, so no wasted query) and `_alternative_titles()` (curated
aliases from `custom-titles.json`, direct + substring match, aliases also spelling-
folded). Each candidate keeps the original season/episode/year suffix and is matched
against *itself* (its own tokens/strict-phrase), then results merge hash-deduped.
Candidates run concurrently single-shot, capped by `EASYNEWS_FALLBACK_MAX_QUERIES`
and `EASYNEWS_FALLBACK_BUDGET_SECONDS`. Because the backup runs *after* the primary,
a 0-result query's total time can approach `SEARCH_BUDGET_SECONDS` + the fallback
budget — keep NZBHydra's timeout above the sum if you enable it. Separately,
`EASYNEWS_EXTRA_TERMS_FALLBACK_ONLY` defers `EXTRA_TERMS` into this same 0-result
stage instead of firing them on every search.

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
  `SEARCH_BUDGET_SECONDS`; if Easynews is slow (or errors) it races a fresh
  parallel request instead of returning a spurious "0 results". A *hang/timeout
  is an error* (it retries + hedges), whereas a successful HTTP 200 with no rows
  is a genuine "0 results" — so with `SEARCH_TRUST_EMPTY=true` (default) that
  empty returns immediately rather than retrying 3× and idling out the budget
  (no-result queries drop from ~3.3s to ~0.3s). Set `SEARCH_TRUST_EMPTY=false`
  to restore the old 3-try-on-empty behaviour.
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