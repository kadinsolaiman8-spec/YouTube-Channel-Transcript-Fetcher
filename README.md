# YouTube Channel Transcript Fetcher

Export **LLM-ready plain-text transcripts** for an entire YouTube channel — with filtering, a local web UI, and CLI tooling.

Built on top of [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) (MIT). This repo adds channel scraping (yt-dlp), optional YouTube Data API enrichment, transcript fetching with proxy support, and export formatting.

## Features

- **Web UI** — start exports from the browser, watch progress, preview, and download
- **CLI** — scriptable batch exports for automation
- **Smart filtering** — age gate, view-count percentile cutoff, optional view floor, max video cap
- **Scrape fallbacks** — yt-dlp listing with optional `YOUTUBE_API_KEY` batch metadata enrichment
- **Proxy support** — Webshare residential or generic HTTP proxies for YouTube IP blocks
- **Compact export format** — token-efficient output with grouped failure summaries

## Requirements

- **Python 3.8+** (3.11+ recommended)
- **ffmpeg** — required by yt-dlp for some metadata paths ([install guide](https://github.com/yt-dlp/yt-dlp#dependencies))
- Optional: **YouTube Data API v3 key** — helps when yt-dlp cannot enrich video metadata
- Optional: **Residential proxy** — recommended when transcript requests hit YouTube IP blocks

## Installation

### Option A — Poetry (recommended)

```bash
git clone https://github.com/kadinsolaiman8-spec/YouTube-Channel-Transcript-Fetcher.git
cd YouTube-Channel-Transcript-Fetcher

poetry install --with channel,test
```

### Option B — pip (editable)

```bash
git clone https://github.com/kadinsolaiman8-spec/YouTube-Channel-Transcript-Fetcher.git
cd YouTube-Channel-Transcript-Fetcher

pip install -e ".[channel]"
```

For development and tests:

```bash
pip install -e ".[channel,test,dev]"
```

## Environment setup

Copy the example env file and fill in values:

```bash
cp .env.example .env.local
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `YOUTUBE_API_KEY` | Optional | YouTube Data API v3 — used when yt-dlp cannot list or enrich channel uploads. Create at [Google Cloud Console](https://console.cloud.google.com/apis/credentials) and enable **YouTube Data API v3**. |
| `WEBSHARE_PROXY_USERNAME` | Optional | Webshare residential proxy username (server-side only) |
| `WEBSHARE_PROXY_PASSWORD` | Optional | Webshare residential proxy password |
| `WEBSHARE_PROXY_RETRIES` | Optional | Retries per transcript on HTTP 429 (default **2**; core library default is 10) |
| `HTTP_PROXY` / `HTTPS_PROXY` | Optional | Generic proxy URLs for transcript fetches |

**Load order:** shell environment wins → `.env` → `.env.local` (overrides `.env`).

Both `.env` and `.env.local` are gitignored. **Never commit API keys or proxy credentials.**

Restart the web server after changing env files — the worker loads env at process start.

## Web UI

Start the local server (binds to **127.0.0.1:8080** only):

```bash
youtube_channel_web
```

Or directly with uvicorn:

```bash
uvicorn youtube_transcript_api.channel.web.app:app --host 127.0.0.1 --port 8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

### Web workflow

1. Paste a channel URL (`@handle`, `/channel/UC...`, or uploads playlist `list=UU...`)
2. Adjust filters (percentile cutoff, min age, sort order, languages)
3. Open **Advanced** for scrape cookies, fetch workers, delay, and max videos
4. Start export — progress, ETA, and status log update live
5. Preview or download the `.txt` export when complete

### Advanced settings

| Setting | Default | Notes |
|---------|---------|-------|
| Fetch workers | 2 | Use **1** when proxies are configured or after 429 errors |
| Delay between fetches | 0.5s | Use **1.0–2.0+** with proxies or rate limits |
| Scrape workers | 2 | Parallel metadata enrichment during scrape |
| Max videos | 0 (unlimited) | Set to **3** for a smoke test before full export |
| Cookies from browser | — | e.g. `brave:Default` — **fully quit the browser** first |
| Cookie file | — | Netscape-format cookies.txt path on the **server machine** |

Proxy credentials are read from `.env.local` on the server — they are **not** sent from the browser.

## CLI

```bash
youtube_channel_export "https://www.youtube.com/@ChannelName" -o export.txt
```

### Common flags

| Flag | Default | Description |
|------|---------|-------------|
| `--percentile` | 10 | Drop bottom N% of videos by view count |
| `--min-age-days` | 14 | Exclude videos newer than N days |
| `--min-views-floor` | 0 | Hard minimum view count (0 = off) |
| `--sort` | asc | `asc` or `desc` by publish date |
| `--languages` | en | Comma-separated transcript language codes |
| `--output`, `-o` | channel_export.txt | Output file path |
| `--max-workers` | 2 | Concurrent transcript fetch workers |
| `--sleep` | 0.5 | Seconds between transcript requests |
| `--scrape-workers` | 2 | Metadata enrichment workers during scrape |
| `--max-videos` | 0 | Cap videos after filtering (0 = no limit) |
| `--export-density` | compact | `compact` or `verbose` |
| `--cookies-browser` | — | yt-dlp browser cookies (e.g. `chrome:Default`) |
| `--cookiefile` | — | Path to Netscape cookie file |
| `--webshare-proxy-username` / `--webshare-proxy-password` | — | Override Webshare env vars |
| `--http-proxy` / `--https-proxy` | — | Override generic proxy env vars |

### Example commands

```bash
# Smoke test — 3 videos, slower pacing
youtube_channel_export "https://www.youtube.com/@ChannelName" \
  --max-videos 3 --max-workers 1 --sleep 2.0 -o smoke.txt

# With browser cookies for bot-blocked metadata
youtube_channel_export "https://www.youtube.com/@ChannelName" \
  --cookies-browser "brave:Default" -o export.txt

# With explicit Webshare proxy
youtube_channel_export "https://www.youtube.com/@ChannelName" \
  --webshare-proxy-username USER --webshare-proxy-password PASS \
  --max-workers 1 --sleep 1.5 -o export.txt
```

## Export format

Compact exports start with a one-line header:

```text
# Channel Name | export:2026-05-20T12:00:00+00:00 | transcripts:2/105 | scraped:119 filtered_out:14 | sort:oldest | filter:...
```

| Field | Meaning |
|-------|---------|
| `transcripts:X/Y` | X transcripts retrieved, Y videos attempted after filtering |
| `scraped` | Videos found before filtering |
| `filtered_out` | Videos removed by filters |

Failed videos appear under `# FAILED` with short one-line reasons. Exports with 10+ failures include a grouped summary.

## Troubleshooting

### Transcript IP blocks (`IpBlocked`, `transcripts:0/N`)

YouTube is blocking caption downloads from your IP.

1. Add `WEBSHARE_PROXY_*` or `HTTP_PROXY` to `.env.local` and restart the server
2. Set fetch workers to **1** and delay to **1.0+** (Advanced sheet or `--max-workers 1 --sleep 2.0`)
3. Run a small `--max-videos 3` test before a full export

See the upstream [Working around IP bans](https://github.com/jdepoix/youtube-transcript-api?tab=readme-ov-file#working-around-ip-bans-requestblocked-or-ipblocked-exception) guide.

### HTTP 429 / rate limits

Reduce concurrency and increase delay. Check the error report for `proxy_configured: true` to confirm the worker loaded proxy env vars.

### Scrape works but all metadata fails (`enrich_failures` = video count)

- Confirm `YOUTUBE_API_KEY` is set and **YouTube Data API v3** is enabled with quota
- Try browser cookies in Advanced (`cookies_from_browser` or cookie file)
- Update yt-dlp: `pip install -U yt-dlp`

### Bot check during scrape

Use browser cookies — fully quit the browser before auto-read, or export a Netscape cookie file.

### High memory usage

Exports run in a **child process**; the web server stays lightweight. See [docs/channel-memory.md](docs/channel-memory.md) for profiling.

## Testing

```bash
# All tests
pytest youtube_transcript_api

# Channel feature tests only
pytest youtube_transcript_api/test/test_channel_export.py \
  youtube_transcript_api/test/test_channel_web.py \
  youtube_transcript_api/test/test_channel_pipeline.py
```

## Project layout

```
youtube_transcript_api/
  channel/
    cli.py              # youtube_channel_export entry point
    web/app.py          # FastAPI web UI (youtube_channel_web)
    pipeline.py         # Scrape → filter → fetch → export
    scraper.py          # yt-dlp + YouTube Data API
    fetcher.py          # Transcript download with proxy support
    export.py           # Plain-text formatting
    proxy.py            # Env/CLI proxy configuration
    worker.py           # Isolated subprocess for web exports
docs/
  channel-memory.md     # Memory profiling and advanced troubleshooting
```

## Security notes

- The web UI listens on **127.0.0.1** only — intended for local use
- API keys and proxy passwords stay in `.env.local` on the server, never in the browser
- Cookie files grant session access — treat them like passwords
- Do not expose the web server to the public internet without adding authentication

## License

MIT — see [LICENSE](LICENSE).

This project extends [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) by Jonas Depoix (MIT).
