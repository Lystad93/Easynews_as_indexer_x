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

The `SEARCH_*` knobs are read at **container start** (change `.env` → `up -d`, no
rebuild). Keep this invariant true:
`HEDGE_AFTER < ATTEMPT_TIMEOUT ≤ BUDGET < client's indexer timeout`.

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
