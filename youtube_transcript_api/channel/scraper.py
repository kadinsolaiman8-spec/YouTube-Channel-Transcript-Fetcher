"""Channel video enumeration and metadata enrichment via yt-dlp."""

from __future__ import annotations

import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from youtube_transcript_api.channel.models import (
    ChannelExportException,
    ChannelNotFound,
    ChannelScrapeError,
    ProgressCallback,
    ScrapeConfig,
    VideoRecord,
    YtdlpNotInstalled,
)
from youtube_transcript_api.channel.progress import PipelineProgressTracker
from youtube_transcript_api.channel.urls import validate_channel_url
from youtube_transcript_api.channel.youtube_data_api import (
    _get_api_key,
    fetch_video_metadata_by_ids,
    scrape_channel_via_youtube_api,
    should_fallback_to_ytdlp_from_api_error,
    youtube_api_key_configured,
)

class DownloadError(Exception):
    """Raised by yt-dlp during extract; aliased to yt_dlp.utils.DownloadError when imported."""


WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
_CHANNEL_TAB_SUFFIXES = ("/videos", "/shorts", "/streams", "/playlists", "/featured", "/about")
_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MIN_ENRICH_SLEEP_SECONDS = 0.25
_ENRICH_SLEEP_BATCH_THRESHOLD = 20
_ENUMERATE_HEARTBEAT_SECONDS = 2.0
_BOT_ERROR_HINT = (
    "YouTube blocked metadata requests (bot check or rate limit). "
    "Try again later, lower concurrency, or pass browser cookies via "
    "--cookies-browser / cookies_from_browser in the web UI."
)
_UNAVAILABLE_HINT = (
    "Playlist listing failed on unavailable videos. Update yt-dlp "
    "(pip install -U yt-dlp) or set YOUTUBE_API_KEY for API fallback."
)
_API_FALLBACK_HINT = (
    "Set YOUTUBE_API_KEY to enable fallback when yt-dlp cannot list uploads."
)
_SUPPORTED_BROWSERS = frozenset(
    {"brave", "chrome", "edge", "firefox", "chromium", "opera", "vivaldi"}
)
_BRAVE_COOKIE_HINT = (
    "Could not read Brave cookies. Fully quit Brave (all windows), then retry, "
    "or export a Netscape cookie file and use --cookiefile instead."
)
_COOKIE_LOCKED_HINT = (
    "Browser cookie database is locked. Close the browser completely, then retry, "
    "or use a Netscape cookie file via --cookiefile."
)


def _import_ytdlp() -> tuple[type, type]:
    global DownloadError
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as YtdlpDownloadError
    except ImportError as exc:
        raise YtdlpNotInstalled(
            "yt-dlp is required for channel scraping. "
            "Install it with: pip install yt-dlp"
        ) from exc
    DownloadError = YtdlpDownloadError
    return YoutubeDL, DownloadError


def _ensure_ytdlp_available() -> None:
    if shutil.which("yt-dlp"):
        return
    _import_ytdlp()


def _get_youtube_dl_class() -> type:
    _ensure_ytdlp_available()
    youtube_dl_class, _ = _import_ytdlp()
    return youtube_dl_class


def _parse_cookies_from_browser(spec: str) -> tuple[str, ...]:
    """Normalize browser cookie spec to a yt-dlp cookiesfrombrowser tuple."""
    cleaned = spec.strip()
    if not cleaned:
        raise ChannelScrapeError(
            "Empty --cookies-browser value",
            details={"hint": "Example: brave, chrome, or brave:Default"},
        )

    if ":" in cleaned:
        browser_name, profile = cleaned.split(":", 1)
        browser_name = browser_name.strip().lower()
        profile = profile.strip()
    else:
        browser_name = cleaned.lower()
        profile = ""

    if browser_name not in _SUPPORTED_BROWSERS:
        supported = ", ".join(sorted(_SUPPORTED_BROWSERS))
        raise ChannelScrapeError(
            f"Unsupported browser {browser_name!r} for cookies",
            details={
                "browser": browser_name,
                "supported": supported,
                "hint": f"Use one of: {supported}",
            },
        )

    if profile:
        return (browser_name, profile)
    return (browser_name,)


def _cookie_opts(scrape_config: Optional[ScrapeConfig]) -> dict[str, Any]:
    if scrape_config is None:
        return {}
    opts: dict[str, Any] = {}
    if scrape_config.cookies_from_browser:
        opts["cookiesfrombrowser"] = _parse_cookies_from_browser(
            scrape_config.cookies_from_browser
        )
    if scrape_config.cookiefile:
        opts["cookiefile"] = scrape_config.cookiefile
    return opts


def _validate_scrape_config(scrape_config: ScrapeConfig) -> None:
    if scrape_config.cookiefile:
        cookie_path = Path(scrape_config.cookiefile)
        if not cookie_path.is_file():
            raise ChannelScrapeError(
                f"Cookie file not found: {scrape_config.cookiefile}",
                details={
                    "cookiefile": scrape_config.cookiefile,
                    "hint": (
                        "Check the path exists and is readable. "
                        "Export youtube.com cookies to a Netscape .txt file."
                    ),
                },
            )


def _is_cookie_extraction_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "cookie" in lowered
        or "database is locked" in lowered
        or "could not copy" in lowered
    )


def _cookie_error_hint(scrape_config: ScrapeConfig, message: str) -> str:
    browser = (scrape_config.cookies_from_browser or "").lower()
    if "brave" in browser or "brave" in message.lower():
        return _BRAVE_COOKIE_HINT
    if "database is locked" in message.lower() or "could not copy" in message.lower():
        return _COOKIE_LOCKED_HINT
    return (
        "Browser cookie extraction failed. Close the browser completely or "
        "use --cookiefile with a Netscape cookie export."
    )


def _scrape_error_hint(exc: BaseException, scrape_config: ScrapeConfig) -> str:
    message = str(exc)
    if _is_bot_or_rate_limit_error(message):
        return _BOT_ERROR_HINT
    if _is_cookie_extraction_error(message):
        return _cookie_error_hint(scrape_config, message)
    lowered = message.lower()
    if "unavailable" in lowered or "private" in lowered or "removed" in lowered:
        return _UNAVAILABLE_HINT
    return _API_FALLBACK_HINT


def _base_opts(scrape_config: Optional[ScrapeConfig] = None) -> dict[str, Any]:
    return {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
        "ignoreerrors": "only_download",
        **_cookie_opts(scrape_config),
    }


def _enumerate_opts(scrape_config: Optional[ScrapeConfig] = None) -> dict[str, Any]:
    # Flat-list playlists; skip bad entries instead of aborting the whole list.
    return {
        **_base_opts(scrape_config),
        "extract_flat": True,
        "ignoreerrors": True,
        "lazy_playlist": True,
    }


def _enrich_opts(scrape_config: Optional[ScrapeConfig] = None) -> dict[str, Any]:
    return _base_opts(scrape_config)


def _effective_enrich_sleep(sleep_seconds: float, pending_count: int) -> float:
    if pending_count > _ENRICH_SLEEP_BATCH_THRESHOLD:
        return max(sleep_seconds, _MIN_ENRICH_SLEEP_SECONDS)
    return sleep_seconds


def _is_bot_or_rate_limit_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "not a bot" in lowered
        or "sign in to confirm" in lowered
        or "429" in lowered
        or "too many requests" in lowered
    )


def _entry_video_id(entry: dict[str, Any]) -> Optional[str]:
    video_id = entry.get("id") or entry.get("video_id")
    if not video_id:
        return None
    video_id = str(video_id)
    if _VIDEO_ID_PATTERN.match(video_id):
        return video_id
    if "v=" in video_id:
        candidate = video_id.split("v=", 1)[1].split("&", 1)[0]
        if _VIDEO_ID_PATTERN.match(candidate):
            return candidate
    return None


def _parse_published_at(entry: dict[str, Any]) -> Optional[datetime]:
    timestamp = entry.get("timestamp")
    if timestamp is not None:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    upload_date = entry.get("upload_date")
    if upload_date:
        return datetime.strptime(str(upload_date), "%Y%m%d").replace(
            tzinfo=timezone.utc
        )
    return None


def _parse_view_count(entry: dict[str, Any]) -> Optional[int]:
    view_count = entry.get("view_count")
    if view_count is None:
        return None
    return int(view_count)


def _watch_url(entry: dict[str, Any], video_id: str) -> str:
    url = entry.get("url") or entry.get("webpage_url")
    if url:
        return str(url)
    return WATCH_URL_TEMPLATE.format(video_id=video_id)


def _channel_label(info: dict[str, Any]) -> str:
    for key in ("channel", "uploader", "title"):
        value = info.get(key)
        if value:
            return str(value)
    return str(info.get("id") or "unknown")


def _extract_entries(info: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    entries = info.get("entries")
    if not entries:
        return [], 0
    kept: list[dict[str, Any]] = []
    skipped = 0
    for entry in entries:
        if not entry:
            skipped += 1
            continue
        if not _entry_video_id(entry):
            skipped += 1
            continue
        kept.append(entry)
    return kept, skipped


def _looks_like_video_entries(entries: list[dict[str, Any]]) -> bool:
    for entry in entries[:10]:
        if _entry_video_id(entry):
            return True
    return False


def _strip_channel_tab_suffix(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    for suffix in _CHANNEL_TAB_SUFFIXES:
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


def _uploads_playlist_url(channel_id: str) -> str:
    if channel_id.startswith("UC") and len(channel_id) > 2:
        return f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}"
    raise ValueError(f"Invalid YouTube channel id: {channel_id!r}")


def _resolve_list_url(
    channel_url: str,
    youtube_dl_class: type,
    scrape_config: Optional[ScrapeConfig],
) -> tuple[str, str]:
    """Resolve a channel URL to the uploads playlist (or existing list URL)."""
    url = channel_url.strip()
    if "list=" in url:
        with youtube_dl_class(_base_opts(scrape_config)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ChannelNotFound(
                f"No data returned for playlist {url}",
                details={"input_url": channel_url, "resolved_url": url},
            )
        return url, _channel_label(info)

    channel_base = _strip_channel_tab_suffix(url)
    resolve_opts = {**_base_opts(scrape_config), "extract_flat": True}
    with youtube_dl_class(resolve_opts) as ydl:
        channel_info = ydl.extract_info(channel_base, download=False)

    if not channel_info:
        raise ChannelNotFound(
            f"No data returned for channel {channel_url}",
            details={"input_url": channel_url, "resolved_url": channel_base},
        )

    channel_label = _channel_label(channel_info)
    channel_id = channel_info.get("channel_id") or channel_info.get("id")
    if channel_id and str(channel_id).startswith("UC"):
        return _uploads_playlist_url(str(channel_id)), channel_label

    videos_url = f"{channel_base}/videos"
    return videos_url, channel_label


def _enrichment_entry_from_api_fields(api_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "view_count": api_fields.get("view_count"),
        "published_at": api_fields.get("published_at"),
        "title": api_fields.get("title"),
    }


def _first_enrich_error(enrichments: dict[int, dict[str, Any]]) -> Optional[str]:
    for enriched in enrichments.values():
        error = enriched.get("error")
        if error:
            return str(error)[:500]
    return None


def _metadata_enrich_failure_hint(
    bot_blocked_count: int,
    api_metadata_enrich_attempted: bool,
) -> str:
    if bot_blocked_count:
        return _BOT_ERROR_HINT
    if youtube_api_key_configured():
        suffix = (
            " YouTube Data API batch enrich was attempted; verify videos.list "
            "quota and key restrictions."
            if api_metadata_enrich_attempted
            else ""
        )
        return (
            "Videos were listed but metadata could not be fetched. "
            "Per-video yt-dlp metadata failed."
            f"{suffix} Try browser cookies in Advanced or update yt-dlp: "
            "pip install -U yt-dlp"
        )
    return (
        "Videos were listed but metadata could not be fetched "
        "(deleted/private/rate-limited). Update yt-dlp: pip install -U yt-dlp"
    )


def _apply_enrichments_to_records(
    records: list[VideoRecord],
    enrichments: dict[int, dict[str, Any]],
) -> tuple[list[VideoRecord], int, list[int]]:
    kept_records: list[VideoRecord] = []
    enrich_failures = 0
    failed_indices: list[int] = []

    for record_index, record in enumerate(records):
        enriched = enrichments.get(record_index, {})
        published_at = enriched.get("published_at") or record.published_at
        if published_at.timestamp() == 0:
            enrich_failures += 1
            failed_indices.append(record_index)
            continue

        view_count = enriched.get("view_count")
        if view_count is None:
            view_count = record.view_count
        title = enriched.get("title") or record.title
        kept_records.append(
            VideoRecord(
                video_id=record.video_id,
                title=str(title),
                published_at=published_at,
                view_count=int(view_count),
                url=record.url,
            )
        )

    return kept_records, enrich_failures, failed_indices


def _try_api_metadata_enrich(
    pending_enrich: list[tuple[int, str]],
    records: list[VideoRecord],
    api_key: str,
    progress: PipelineProgressTracker,
) -> tuple[dict[int, dict[str, Any]], list[tuple[int, str]]]:
    enrichments: dict[int, dict[str, Any]] = {}
    still_pending: list[tuple[int, str]] = []
    video_ids = [records[index].video_id for index, _ in pending_enrich]

    progress.report_log("Enriching metadata via YouTube Data API (videos.list)…")
    progress.report_meta(scrape_backend_reason="api_metadata_enrich")
    progress.report_substage(
        "enrich",
        0,
        len(pending_enrich),
        f"YouTube API: enriching metadata for {len(pending_enrich)} videos…",
    )

    try:
        metadata_by_id = fetch_video_metadata_by_ids(video_ids, api_key)
    except ChannelExportException as exc:
        progress.report_log(f"YouTube API metadata enrich failed: {exc}")
        return enrichments, list(pending_enrich)
    except Exception as exc:
        progress.report_log(f"YouTube API metadata enrich failed: {exc}")
        return enrichments, list(pending_enrich)

    api_enriched = 0
    for index, url in pending_enrich:
        api_fields = metadata_by_id.get(records[index].video_id, {})
        published_at = api_fields.get("published_at")
        if published_at is not None and published_at.timestamp() != 0:
            enrichments[index] = _enrichment_entry_from_api_fields(api_fields)
            api_enriched += 1
        else:
            still_pending.append((index, url))

    progress.report_log(
        f"YouTube API enriched {api_enriched}/{len(pending_enrich)} videos"
    )
    progress.report_meta(metadata_fetched=api_enriched)
    progress.report_substage(
        "enrich",
        api_enriched,
        len(pending_enrich),
        f"YouTube API enriched {api_enriched}/{len(pending_enrich)} videos",
    )
    return enrichments, still_pending


def _run_ytdlp_metadata_enrich(
    still_pending: list[tuple[int, str]],
    enrichments: dict[int, dict[str, Any]],
    *,
    youtube_dl_class: type,
    enrich_sleep: float,
    config: ScrapeConfig,
    enrich_workers: int,
    progress: PipelineProgressTracker,
    enrich_total: int,
    api_enriched_count: int,
) -> int:
    if not still_pending:
        return 0

    bot_blocked_count = 0
    ytdlp_total = len(still_pending)
    completed = 0

    with ThreadPoolExecutor(max_workers=enrich_workers) as executor:
        future_to_index = {
            executor.submit(
                _enrich_metadata,
                url,
                youtube_dl_class,
                enrich_sleep,
                config,
            ): index
            for index, url in still_pending
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                enrichments[index] = future.result()
            except Exception as exc:
                enrichments[index] = {"error": str(exc)}
            if enrichments[index].get("bot_blocked"):
                bot_blocked_count += 1
            completed += 1
            fetched = api_enriched_count + completed
            progress.report_meta(metadata_fetched=fetched)
            progress.report_substage(
                "enrich",
                fetched,
                enrich_total,
                f"Fetched metadata {fetched}/{enrich_total}",
            )

    return bot_blocked_count


def _enrich_metadata(
    video_url: str,
    youtube_dl_class: type,
    sleep_seconds: float,
    scrape_config: Optional[ScrapeConfig],
) -> dict[str, Any]:
    if sleep_seconds:
        time.sleep(sleep_seconds)
    try:
        with youtube_dl_class(_enrich_opts(scrape_config)) as ydl:
            detail = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        error_text = str(exc)
        result: dict[str, Any] = {"error": error_text}
        if _is_bot_or_rate_limit_error(error_text):
            result["bot_blocked"] = True
        return result
    if not detail:
        return {}
    return {
        "view_count": _parse_view_count(detail),
        "published_at": _parse_published_at(detail),
        "title": detail.get("title"),
    }


def _enumerate_playlist(
    list_url: str,
    youtube_dl_class: type,
    scrape_config: Optional[ScrapeConfig],
    progress: Optional[PipelineProgressTracker] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    stop_heartbeat = threading.Event()
    elapsed_seconds = 0

    def _heartbeat() -> None:
        nonlocal elapsed_seconds
        while not stop_heartbeat.wait(_ENUMERATE_HEARTBEAT_SECONDS):
            elapsed_seconds += int(_ENUMERATE_HEARTBEAT_SECONDS)
            if progress is not None:
                progress.report_substage(
                    "enumerate",
                    0,
                    1,
                    f"Listing channel videos… ({elapsed_seconds}s)",
                )

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    info: dict[str, Any] = {}
    _, ytdlp_download_error = _import_ytdlp()
    try:
        with youtube_dl_class(_enumerate_opts(scrape_config)) as ydl:
            try:
                extracted = ydl.extract_info(list_url, download=False)
                if extracted:
                    info = extracted
            except ytdlp_download_error as exc:
                if info:
                    entries, skipped = _extract_entries(info)
                    return info, entries, skipped
                raise ChannelScrapeError(
                    f"Could not list playlist {list_url}: {exc}",
                    details={
                        "resolved_url": list_url,
                        "error_type": type(exc).__name__,
                        "hint": _UNAVAILABLE_HINT,
                    },
                ) from exc
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=0.5)

    if not info:
        return {}, [], 0

    entries, skipped = _extract_entries(info)
    return info, entries, skipped


def _cookies_configured(config: ScrapeConfig) -> bool:
    return bool(
        (config.cookies_from_browser or "").strip()
        or (config.cookiefile or "").strip()
    )


def _prefer_youtube_data_api(config: ScrapeConfig) -> bool:
    """Use API directly when no cookies are set but YOUTUBE_API_KEY is available."""
    return not _cookies_configured(config) and youtube_api_key_configured()


def _try_youtube_api_fallback(
    channel_url: str,
    progress: PipelineProgressTracker,
    original_exc: BaseException,
) -> tuple[list[VideoRecord], str, str]:
    api_key = _get_api_key()
    if not api_key:
        progress.report_log(
            "YouTube Data API fallback skipped (YOUTUBE_API_KEY not set)."
        )
        raise original_exc

    error_name = type(original_exc).__name__
    progress.report_log(
        f"yt-dlp failed ({error_name}); switching to YouTube Data API…"
    )
    progress.report_meta(
        scrape_backend="youtube_data_api",
        scrape_backend_reason="ytdlp_fallback",
        scrape_fallback_error=str(original_exc)[:240],
    )
    progress.report_substage(
        "enumerate",
        0,
        1,
        "yt-dlp failed; trying YouTube Data API…",
    )
    try:
        records, channel_label = scrape_channel_via_youtube_api(
            channel_url, api_key, progress
        )
        progress.report_log(
            f"YouTube Data API listed {len(records)} videos for {channel_label}."
        )
        return records, channel_label, "youtube_data_api"
    except ChannelExportException as exc:
        progress.report_log(f"YouTube Data API failed: {exc}")
        raise
    except Exception as exc:
        progress.report_log(f"YouTube Data API failed: {exc}")
        raise ChannelScrapeError(
            f"YouTube Data API fallback failed for {channel_url}: {exc}",
            details={
                "input_url": channel_url,
                "error_type": type(exc).__name__,
                "original_error": type(original_exc).__name__,
                "hint": "Verify YOUTUBE_API_KEY and API quota.",
            },
        ) from exc


def scrape_channel(
    channel_url: str,
    max_workers: int = 2,
    sleep_seconds: float = 0,
    progress_callback: Optional[ProgressCallback] = None,
    scrape_config: Optional[ScrapeConfig] = None,
) -> tuple[list[VideoRecord], str, str]:
    """Enumerate channel videos and enrich missing metadata via yt-dlp or YouTube Data API."""
    progress = PipelineProgressTracker(progress_callback)
    config = scrape_config or ScrapeConfig()
    _validate_scrape_config(config)
    validate_channel_url(channel_url)

    if _prefer_youtube_data_api(config):
        progress.report_log(
            "No browser cookies configured; listing channel via YouTube Data API."
        )
        progress.report_meta(
            scrape_backend="youtube_data_api",
            scrape_backend_reason="no_cookies_api_key_set",
        )
        api_key = _get_api_key()
        if not api_key:
            raise ChannelScrapeError(
                "YOUTUBE_API_KEY is not set",
                details={"hint": "Add YOUTUBE_API_KEY to .env or use browser cookies."},
            )
        try:
            records, channel_label = scrape_channel_via_youtube_api(
                channel_url, api_key, progress
            )
            return records, channel_label, "youtube_data_api"
        except ChannelScrapeError as exc:
            if not should_fallback_to_ytdlp_from_api_error(exc):
                raise
            progress.report_log(
                "YouTube Data API unavailable for this key; falling back to yt-dlp."
            )
            progress.report_meta(
                scrape_backend_reason="youtube_api_ytdlp_fallback",
            )

    youtube_dl_class = _get_youtube_dl_class()
    progress.report_log("Listing channel via yt-dlp.")
    progress.report_meta(scrape_backend="ytdlp", scrape_backend_reason="primary")
    enrich_workers = max(1, min(max_workers, config.enrich_max_workers))
    list_url = channel_url
    channel_label = "unknown"
    skipped_unavailable = 0

    progress.report_substage(
        "resolve",
        0,
        1,
        "Resolving channel uploads playlist…",
    )

    try:
        list_url, channel_label = _resolve_list_url(
            channel_url, youtube_dl_class, config
        )
        progress.report_substage(
            "resolve",
            1,
            1,
            f"Resolved playlist for {channel_label}",
        )
        progress.report_substage(
            "enumerate",
            0,
            1,
            "Listing channel videos…",
        )
        info, entries, skipped_unavailable = _enumerate_playlist(
            list_url,
            youtube_dl_class,
            config,
            progress=progress,
        )
    except (ChannelScrapeError, ChannelNotFound) as exc:
        return _try_youtube_api_fallback(channel_url, progress, exc)
    except Exception as exc:
        if _is_cookie_extraction_error(str(exc)):
            raise ChannelScrapeError(
                f"Could not read browser cookies: {exc}",
                details={
                    "input_url": channel_url,
                    "error_type": type(exc).__name__,
                    "hint": _cookie_error_hint(config, str(exc)),
                },
            ) from exc
        wrapped = ChannelScrapeError(
            f"Could not scrape channel {channel_url}: {exc}",
            details={
                "input_url": channel_url,
                "resolved_url": list_url,
                "error_type": type(exc).__name__,
                "hint": _scrape_error_hint(exc, config),
            },
        )
        return _try_youtube_api_fallback(channel_url, progress, wrapped)

    if not info:
        raise ChannelNotFound(
            f"No data returned for channel {channel_url}",
            details={"input_url": channel_url, "resolved_url": list_url},
        )

    if not entries or not _looks_like_video_entries(entries):
        raise ChannelNotFound(
            f"No videos found for channel {channel_url}",
            details={
                "input_url": channel_url,
                "resolved_url": list_url,
                "raw_entry_count": len(entries),
                "hint": (
                    "Channel pages without /videos return tab links, not videos. "
                    "The scraper resolves the uploads playlist automatically; "
                    "if this persists, try https://www.youtube.com/@Handle/videos "
                    "or update yt-dlp: pip install -U yt-dlp"
                ),
            },
        )

    enumerate_message = f"Found {len(entries)} videos on channel"
    if skipped_unavailable:
        enumerate_message += f" ({skipped_unavailable} unavailable skipped)"
    progress.report_substage(
        "enumerate",
        1,
        1,
        enumerate_message,
    )

    raw_entry_count = len(entries)
    records: list[VideoRecord] = []
    pending_enrich: list[tuple[int, str]] = []
    skipped_no_video_id = 0

    for entry in entries:
        video_id = _entry_video_id(entry)
        if not video_id:
            skipped_no_video_id += 1
            continue

        published_at = _parse_published_at(entry)
        view_count = _parse_view_count(entry)
        url = _watch_url(entry, video_id)
        title = str(entry.get("title") or "")

        if published_at is None or view_count is None:
            pending_enrich.append((len(records), url))

        records.append(
            VideoRecord(
                video_id=video_id,
                title=title,
                published_at=published_at or datetime.fromtimestamp(0, tz=timezone.utc),
                view_count=int(view_count) if view_count is not None else 0,
                url=url,
            )
        )

    del entries

    if not records:
        raise ChannelNotFound(
            f"No videos found for channel {channel_url}",
            details={
                "input_url": channel_url,
                "resolved_url": list_url,
                "raw_entry_count": raw_entry_count,
                "skipped_no_video_id": skipped_no_video_id,
                "hint": (
                    "Entries were returned but none could be parsed as videos. "
                    "Update yt-dlp: pip install -U yt-dlp"
                ),
            },
        )

    progress.report_meta(scraped_video_count=len(records))

    if pending_enrich:
        enrich_total = len(pending_enrich)
        enrich_sleep = _effective_enrich_sleep(sleep_seconds, enrich_total)
        progress.report_meta(metadata_total=enrich_total, metadata_fetched=0)
        progress.report_substage(
            "enrich",
            0,
            enrich_total,
            f"Fetching metadata for {enrich_total} videos…",
        )
        enrichments: dict[int, dict[str, Any]] = {}
        api_metadata_enrich_attempted = False
        still_pending = list(pending_enrich)
        api_enriched_count = 0

        api_key = _get_api_key()
        if api_key:
            api_metadata_enrich_attempted = True
            enrichments, still_pending = _try_api_metadata_enrich(
                pending_enrich,
                records,
                api_key,
                progress,
            )
            api_enriched_count = len(pending_enrich) - len(still_pending)

        bot_blocked_count = _run_ytdlp_metadata_enrich(
            still_pending,
            enrichments,
            youtube_dl_class=youtube_dl_class,
            enrich_sleep=enrich_sleep,
            config=config,
            enrich_workers=enrich_workers,
            progress=progress,
            enrich_total=enrich_total,
            api_enriched_count=api_enriched_count,
        )

        all_records = records
        records, enrich_failures, failed_indices = _apply_enrichments_to_records(
            all_records,
            enrichments,
        )

        if not records and api_key and failed_indices:
            progress.report_log(
                "Retrying metadata enrich via YouTube Data API for failed videos…"
            )
            retry_pending = [
                (index, all_records[index].url) for index in failed_indices
            ]
            retry_enrichments, still_after_retry = _try_api_metadata_enrich(
                retry_pending,
                all_records,
                api_key,
                progress,
            )
            enrichments.update(retry_enrichments)
            if still_after_retry:
                bot_blocked_count += _run_ytdlp_metadata_enrich(
                    still_after_retry,
                    enrichments,
                    youtube_dl_class=youtube_dl_class,
                    enrich_sleep=enrich_sleep,
                    config=config,
                    enrich_workers=enrich_workers,
                    progress=progress,
                    enrich_total=enrich_total,
                    api_enriched_count=api_enriched_count,
                )
            records, enrich_failures, failed_indices = _apply_enrichments_to_records(
                all_records,
                enrichments,
            )

        if not records:
            raise ChannelScrapeError(
                f"No video metadata could be fetched for channel {channel_url}",
                details={
                    "input_url": channel_url,
                    "resolved_url": list_url,
                    "raw_entry_count": raw_entry_count,
                    "enrich_failures": enrich_failures,
                    "bot_blocked_count": bot_blocked_count,
                    "scrape_backend": "ytdlp",
                    "api_metadata_enrich_attempted": api_metadata_enrich_attempted,
                    "sample_enrich_error": _first_enrich_error(enrichments),
                    "hint": _metadata_enrich_failure_hint(
                        bot_blocked_count,
                        api_metadata_enrich_attempted,
                    ),
                },
            )

    else:
        progress.report_meta(metadata_total=0, metadata_fetched=0)

    progress.report(40, f"Scraped {len(records)} videos with metadata")

    playlist_label = _channel_label(info)
    if playlist_label and playlist_label != "unknown":
        channel_label = playlist_label

    progress.report_meta(scrape_backend="ytdlp")
    return records, channel_label, "ytdlp"
