# [☕ Please support my work on Buy Me a Coffee](https://buymeacoffee.com/gaikwadsank)

# Easynews Newznab-like server

Flask server that bridges Easynews search to a Newznab-like API so you can add it to Prowlarr as a custom indexer and download NZBs. Video-only, sorts by relevance, returns as many results as possible, and filters files smaller than 100 MB.

## Setup (Local)

1. Create and activate a Python 3.11+ virtual environment:

```
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Linux / macOS (bash/zsh)
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```
pip install -r requirements.txt
```

3. Configure credentials and API key. Create a `.env` file in the repo root:

```
EASYNEWS_USER=your_easynews_username
EASYNEWS_PASS=your_easynews_password
NEWZNAB_APIKEY=testkey
```

4. Run the server:

```
python server.py
```

It starts on `http://127.0.0.1:8081`.

## Setup (Docker)


### Pull from GitHub Container Registry

```
docker pull ghcr.io/sanket9225/easynews_as_indexer:latest
```

Run the published image (Linux/macOS shells):

```
docker run --rm -d -p 8081:8081 \
	-e EASYNEWS_USER=your_easynews_username \
	-e EASYNEWS_PASS=your_easynews_password \
	-e NEWZNAB_APIKEY=testkey \
	-e PORT=8081 \
	-e STRICT_MATCHING=1 \
	ghcr.io/sanket9225/easynews_as_indexer:latest
```

> The published image currently includes `linux/amd64` and `linux/arm64` manifests.

Windows PowerShell equivalent:

```
docker run --rm -d -p 8081:8081 ^
	-e EASYNEWS_USER=your_easynews_username ^
	-e EASYNEWS_PASS=your_easynews_password ^
	-e NEWZNAB_APIKEY=testkey ^
	-e PORT=8081 ^
	-e STRICT_MATCHING=1 ^
	ghcr.io/sanket9225/easynews_as_indexer:latest
```

To tail logs from the detached container run `docker logs -f <container-id>`.

## Endpoints

- Caps: `GET /api?t=caps&apikey=<key>`
- Search (video-only): `GET /api?t=search&q=<query>&apikey=<key>&limit=<n>&minsize=<MB>`
	- Default `limit=100`, `minsize=100` (MB)
	- Also supports `t=movie` and `t=tvsearch`
	- **Strict matching** is enabled by default for `t=movie` and `t=tvsearch` (requires title to contain all query words); disabled for plain `t=search`
	- Optional `strict=0|1` overrides title matching strictness per request
	- Movie search accepts `year=<YYYY>` to bias results; TV search accepts `season=<NN>` and `ep=<NN>` (automatically appended as `SxxEyy` in the Easynews query)
- Download NZB: `GET /api?t=get&id=<encoded>&apikey=<key>`
	- Filename equals the item title

## Optional configuration

All optional and read at startup — set them in `.env` (or `-e` flags) and restart. See `.env.example` for the fully annotated list.

**Search endpoint (A/B testing speed).** Switch which Easynews endpoint is used:

- `EASYNEWS_SEARCH_API` — `2.0` (default, `/2.0/search/solr-search/`) or `3.0` (`/3.0/api/search/`, newer JSON API)
- `EASYNEWS_SEARCH_URL_TEMPLATE` — full URL override if the 3.0 params differ (placeholders `{base}` `{query}` `{page}` `{per_page}`); wins over `EASYNEWS_SEARCH_API`
- `EASYNEWS_BASE_URL`, `EASYNEWS_RESULTS_KEY`, `EASYNEWS_LOG_LATENCY` — host override, alternate JSON results key, and per-request latency logging

`easynews_endpoint_benchmark.sh` curls both endpoints and times them so you can compare. The NZB download path always uses `/2.0/api/dl-nzb`.

**Language / codec metadata.** Emitted as `newznab:attr` so downstream tools (e.g. AIOStreams, which reads `subs` for subtitle languages and `language` for audio) can filter by language — handy for, say, Norwegian (`nor`) subtitles. Default on:

- `EASYNEWS_META_SUBS`, `EASYNEWS_META_AUDIO`, `EASYNEWS_META_CODECS` — set any to `false` to drop that attribute

These codes come from Easynews's JSON and are present on the 3.0 API / 2.0 dict responses. If you route through NZBHydra2, it must be set to pass these attributes through.

**Other:**

- `EASYNEWS_DISABLE_FILTERS=true` — skip title-matching filters (validity, size, virus/duration checks still apply)
- `EASYNEWS_PAGINATE=true` + `EASYNEWS_MAX_PAGES=<n>` — fetch extra result pages (deduped). Off by default; adds latency, so raise `SEARCH_BUDGET_SECONDS` and your indexer timeout if you enable it.
- `IGNORE_SEASON_PACKS=true` — skip season-only queries Easynews rarely fulfils

## Prowlarr integration

Add a Newznab (generic) indexer in Prowlarr:
- URL: `http://127.0.0.1:8081`
- API Key: the same key in your `.env` (e.g., `testkey`)

---

## [☕ If this project helps you, consider buying me a coffee](https://buymeacoffee.com/gaikwadsank)
