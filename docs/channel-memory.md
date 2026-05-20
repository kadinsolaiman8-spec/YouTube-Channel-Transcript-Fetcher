# Channel export memory profiling

Use this when investigating high RAM usage from the channel web UI or CLI.

## Quick profile

```bash
pip install psutil
python scripts/channel_mem_profile.py
```

Optional full export (network + yt-dlp):

```bash
python scripts/channel_mem_profile.py --export-url "https://www.youtube.com/@CHANNEL"
```

Add `--tracemalloc` for top allocation sites after each step.

## What to expect

| Checkpoint | Typical RSS (guide) |
|------------|---------------------|
| After `import ...channel.web.app` | Well under ~300 MB on Windows |
| After `GET /` | Same as import (no scrape/transcripts) |
| After one ~150-video export | Tens of MB growth, not GB |

If idle import is already multi-GB, check for:

- Another Python process in Task Manager
- A leftover `youtube_channel_web` server with an old in-memory job (restart the server)
- Antivirus or browser tooling attached to Python

## Server commands

```bash
youtube_channel_web
# or
uvicorn youtube_transcript_api.channel.web.app:app --host 127.0.0.1 --port 8080
```

Exports run in a **child process**; the uvicorn process should stay lightweight. Peak memory during export appears on a short-lived worker process, not necessarily the main Python PID you started first.

## Architecture notes

- Export text is written to a **temp file**; the web job store keeps metadata and counts only.
- Completed jobs expire after **1 hour** (temp files removed).
- Only **one concurrent export** is allowed per server instance.

## Transcript fetch: proxies and rate limits

YouTube often blocks transcript requests by IP. Channel export reads proxy settings from the environment (web worker and CLI after `load_local_env`):

| Variable | Purpose |
|----------|---------|
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | Residential Webshare pool (recommended for IP bans) |
| `WEBSHARE_PROXY_RETRIES` | Channel export only: retries per transcript on 429 (default **2**; core library default is 10) |
| `HTTP_PROXY` / `HTTPS_PROXY` | Generic HTTP/HTTPS proxy |

Copy [`.env.example`](../.env.example) to `.env.local` and fill in values. Restart the web server after changes.

CLI overrides (same as core `youtube_transcript_api`):

```bash
youtube_channel_export "https://www.youtube.com/@CHANNEL" \
  --webshare-proxy-username USER --webshare-proxy-password PASS
# or
youtube_channel_export "https://www.youtube.com/@CHANNEL" \
  --http-proxy http://user:pass@host:port
```

Safer defaults for large channels: **2 fetch workers**, **0.5s delay** between transcript requests. Tune in the web UI **Advanced** sheet or via `--max-workers` and `--sleep` on the CLI.

When `WEBSHARE_PROXY_*` or `HTTP_PROXY` is set, prefer **1 fetch worker** and **delay ≥ 1.0** (or **2.0+** after 429 errors) to reduce rate limiting.

See the main README [Working around IP bans](https://github.com/jdepoix/youtube-transcript-api?tab=readme-ov-file#working-around-ip-bans-requestblocked-or-ipblocked-exception) section.

## HTTP 429 / RetryError during fetch

If the error report shows `RetryError` or `too many 429 error responses` on `/api/timedtext`, YouTube is **rate-limiting** caption downloads. This often appears when a residential proxy is configured (the library retries 429s automatically) but concurrency is still too high.

| What to do | Why |
|------------|-----|
| Advanced → fetch workers **1**, delay **2.0+** | Fewer parallel timedtext requests |
| Max videos **3** for a smoke test | Confirm `transcripts:3/3` before a full export |
| Restart server after `.env.local` proxy changes | Worker loads env at process start |
| Check error report `proxy_configured: true` | Confirms the worker saw your proxy env vars |

Per-video failures are recorded as `YouTube rate limit (429)` in `# FAILED` instead of crashing the whole job.

## Export header fields

Compact exports use a one-line header, for example:

```text
# Channel Name | export:2026-05-20T12:00:00+00:00 | transcripts:2/105 | scraped:119 filtered_out:14 | sort:oldest | filter:...
```

| Field | Meaning |
|-------|---------|
| `transcripts:X/Y` | `X` transcripts retrieved, `Y` videos attempted after filtering |
| `scraped` | Videos found on the channel before filtering |
| `filtered_out` | Videos removed by age gate / percentile / view floor |

Failed videos appear in a `# FAILED` appendix with short one-line reasons (and a grouped summary when there are more than 10 failures).

## Scrape failures: `enrich_failures` equals video count

If the error report shows `raw_entry_count` > 0 but `enrich_failures` matches that count (e.g. 119/119), yt-dlp listed the channel but per-video metadata fetches failed. With `YOUTUBE_API_KEY` set, the scraper now batch-enriches via **YouTube Data API `videos.list`** before falling back to per-video yt-dlp.

| Symptom | What to try |
|---------|-------------|
| Listing works, all enrich fails, API key set | Confirm `videos.list` is enabled and has quota; check status log for `YouTube API enriched N/M` |
| `api_metadata_enrich_attempted: true` in error details | API batch was tried; if still zero records, use browser cookies in **Advanced** or update yt-dlp |
| `bot_blocked_count` > 0 | YouTube bot check on metadata; add cookies or retry later with lower scrape workers |

Cookies fix both channel listing (when `channels.list` is blocked) and metadata when both API and yt-dlp fail.

## Export only shows video titles (`# FAILED`)

If the header says `transcripts:0/105` and the file is a list of titles under `# FAILED`, scrape/filter worked but **no caption text was downloaded** (usually YouTube IP block on transcript requests, not missing metadata). The job should now **fail in the UI** instead of offering a misleading download.

Fix transcript fetching (not scrape):

1. Add `WEBSHARE_PROXY_*` or `HTTP_PROXY` to `.env.local` and restart the server
2. In **Advanced**, use fetch workers `1` and delay `1.0` or higher
3. Wait if your IP was rate-limited, then retry a small `Max videos` test (e.g. 3) first
