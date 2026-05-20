"""YouTube Data API v3 fallback when yt-dlp channel enumeration fails."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional
import requests
from requests.exceptions import RequestException, Timeout

from youtube_transcript_api.channel.models import (
    ChannelNotFound,
    ChannelScrapeError,
    VideoRecord,
)
from youtube_transcript_api.channel.progress import PipelineProgressTracker
from youtube_transcript_api.channel.urls import parse_channel_url

WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
_API_BASE = "https://www.googleapis.com/youtube/v3"
_INVALID_API_KEY_HINT = (
    "Check YOUTUBE_API_KEY in .env or .env.local, enable YouTube Data API v3 "
    "in Google Cloud Console, restrict the key to that API, and restart the server."
)
_API_NOT_ENABLED_HINT = (
    "Enable YouTube Data API v3 for your Google Cloud project at "
    "https://console.cloud.google.com/apis/library/youtube.googleapis.com "
    "then restart the server, or use browser cookies in Advanced to scrape via yt-dlp."
)
_PLAYLIST_PAGE_SIZE = 50
_VIDEOS_BATCH_SIZE = 50


def _get_api_key() -> Optional[str]:
    value = os.environ.get("YOUTUBE_API_KEY", "").strip()
    return value or None


def youtube_api_key_configured() -> bool:
    return _get_api_key() is not None


_API_TIMEOUT_SECONDS = 30


def is_invalid_api_key_error(exc: BaseException) -> bool:
    """Return True when exc is a ChannelScrapeError caused by an invalid API key."""
    if not isinstance(exc, ChannelScrapeError):
        return False
    if exc.details.get("error_kind") == "invalid_api_key":
        return True
    message = str(exc)
    return "API key not valid" in message or "API key expired" in message


def is_api_not_enabled_error(exc: BaseException) -> bool:
    """Return True when YouTube Data API v3 is disabled or blocked for the project."""
    if not isinstance(exc, ChannelScrapeError):
        return False
    if exc.details.get("error_kind") == "api_not_enabled":
        return True
    message = str(exc).lower()
    return (
        "are blocked" in message
        or "has not been used in project" in message
        or "it is disabled" in message
        or "accessnotconfigured" in message.replace(" ", "")
    )


def should_fallback_to_ytdlp_from_api_error(exc: BaseException) -> bool:
    """Return True when yt-dlp should be tried after a YouTube Data API failure."""
    return is_invalid_api_key_error(exc) or is_api_not_enabled_error(exc)


def _is_api_not_enabled_response(
    status_code: int,
    message: str,
    payload: dict[str, Any],
) -> bool:
    if status_code != 403:
        return False
    lowered = message.lower()
    if "are blocked" in lowered:
        return True
    if "has not been used in project" in lowered or "it is disabled" in lowered:
        return True
    errors = payload.get("error", {}).get("errors") or []
    return any(err.get("reason") == "accessNotConfigured" for err in errors)


def _is_invalid_api_key_response(
    status_code: int,
    message: str,
    payload: dict[str, Any],
) -> bool:
    if status_code != 400:
        return False
    if "API key not valid" in message or "API key expired" in message:
        return True
    errors = payload.get("error", {}).get("errors") or []
    return any(err.get("reason") in ("keyInvalid", "API_KEY_INVALID") for err in errors)


def _api_get(
    path: str,
    api_key: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    query = {**params, "key": api_key}
    try:
        response = requests.get(
            f"{_API_BASE}/{path}",
            params=query,
            timeout=_API_TIMEOUT_SECONDS,
        )
    except Timeout as exc:
        raise ChannelScrapeError(
            f"YouTube Data API timed out after {_API_TIMEOUT_SECONDS}s ({path})",
            details={
                "path": path,
                "error_type": "Timeout",
                "hint": "Retry later or check network connectivity.",
            },
        ) from exc
    except RequestException as exc:
        raise ChannelScrapeError(
            f"YouTube Data API network error ({path}): {exc}",
            details={
                "path": path,
                "error_type": type(exc).__name__,
                "hint": "Check network connectivity and firewall settings.",
            },
        ) from exc
    if response.status_code != 200:
        payload: dict[str, Any] = {}
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        details: dict[str, Any] = {
            "status_code": response.status_code,
            "path": path,
            "hint": "Verify YOUTUBE_API_KEY and API quota.",
        }
        if _is_invalid_api_key_response(response.status_code, message, payload):
            details["error_kind"] = "invalid_api_key"
            details["hint"] = _INVALID_API_KEY_HINT
        elif _is_api_not_enabled_response(response.status_code, message, payload):
            details["error_kind"] = "api_not_enabled"
            details["hint"] = _API_NOT_ENABLED_HINT
        raise ChannelScrapeError(
            f"YouTube Data API request failed: {message}",
            details=details,
        )
    return response.json()


def _parse_channel_url(channel_url: str) -> tuple[str, str]:
    """Return (kind, value) where kind is handle, channel_id, or uploads_playlist."""
    return parse_channel_url(channel_url)


def resolve_channel(
    channel_url: str,
    api_key: str,
) -> tuple[str, str, str]:
    """Resolve channel URL to (channel_id, channel_title, uploads_playlist_id)."""
    kind, value = _parse_channel_url(channel_url)

    if kind == "uploads_playlist":
        playlist_id = value
        playlist = _api_get(
            "playlists",
            api_key,
            {"part": "snippet", "id": playlist_id},
        )
        items = playlist.get("items") or []
        if not items:
            raise ChannelNotFound(
                f"Uploads playlist not found: {playlist_id}",
                details={"input_url": channel_url, "playlist_id": playlist_id},
            )
        snippet = items[0].get("snippet") or {}
        channel_title = str(
            snippet.get("channelTitle") or snippet.get("title") or "unknown"
        )
        channel_id = str(snippet.get("channelId") or "")
        if not channel_id:
            raise ChannelScrapeError(
                "Playlist response missing channelId",
                details={"playlist_id": playlist_id},
            )
        return channel_id, channel_title, playlist_id

    if kind == "handle":
        payload = _api_get(
            "channels",
            api_key,
            {"part": "contentDetails,snippet", "forHandle": value},
        )
    else:
        payload = _api_get(
            "channels",
            api_key,
            {"part": "contentDetails,snippet", "id": value},
        )

    items = payload.get("items") or []
    if not items:
        raise ChannelNotFound(
            f"Channel not found for {channel_url}",
            details={"input_url": channel_url, "lookup": kind},
        )

    item = items[0]
    channel_id = str(item["id"])
    snippet = item.get("snippet") or {}
    channel_title = str(snippet.get("title") or "unknown")
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        raise ChannelScrapeError(
            "Channel has no uploads playlist in API response",
            details={"channel_id": channel_id},
        )
    return channel_id, channel_title, str(uploads)


def _parse_api_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _append_records_for_page(
    records: list[VideoRecord],
    page_video_ids: list[str],
    stubs: dict[str, dict[str, Any]],
    view_counts: dict[str, int],
) -> None:
    for video_id in page_video_ids:
        stub = stubs[video_id]
        records.append(
            VideoRecord(
                video_id=video_id,
                title=stub.get("title") or "",
                published_at=stub.get("published_at")
                or datetime.fromtimestamp(0, tz=timezone.utc),
                view_count=view_counts.get(video_id, 0),
                url=WATCH_URL_TEMPLATE.format(video_id=video_id),
            )
        )


def fetch_video_metadata_by_ids(
    video_ids: list[str],
    api_key: str,
) -> dict[str, dict[str, Any]]:
    """Batch-fetch title, published_at, and view_count via videos.list."""
    metadata: dict[str, dict[str, Any]] = {}
    if not video_ids:
        return metadata

    for batch_start in range(0, len(video_ids), _VIDEOS_BATCH_SIZE):
        batch = video_ids[batch_start : batch_start + _VIDEOS_BATCH_SIZE]
        payload = _api_get(
            "videos",
            api_key,
            {"part": "statistics,snippet", "id": ",".join(batch)},
        )
        for item in payload.get("items") or []:
            vid = str(item["id"])
            statistics = item.get("statistics") or {}
            raw_views = statistics.get("viewCount")
            view_count = int(raw_views) if raw_views is not None else 0
            snippet = item.get("snippet") or {}
            published_at: Optional[datetime] = None
            if snippet.get("publishedAt"):
                published_at = _parse_api_datetime(snippet["publishedAt"])
            title = snippet.get("title")
            metadata[vid] = {
                "title": str(title) if title else None,
                "published_at": published_at,
                "view_count": view_count,
            }
    return metadata


def _fetch_view_counts_for_ids(
    video_ids: list[str],
    api_key: str,
    stubs: dict[str, dict[str, Any]],
) -> dict[str, int]:
    view_counts: dict[str, int] = {}
    metadata = fetch_video_metadata_by_ids(video_ids, api_key)
    for vid, fields in metadata.items():
        stubs.setdefault(vid, {})
        if fields.get("title"):
            stubs[vid]["title"] = fields["title"]
        published_at = fields.get("published_at")
        if published_at is not None and published_at.timestamp() != 0:
            stubs[vid]["published_at"] = published_at
        view_counts[vid] = int(fields.get("view_count") or 0)
    return view_counts


def list_channel_videos(
    uploads_playlist_id: str,
    api_key: str,
    progress: Optional[PipelineProgressTracker] = None,
) -> list[VideoRecord]:
    """List all videos in an uploads playlist via playlistItems + videos.list."""
    page_token: Optional[str] = None
    page_index = 0
    records: list[VideoRecord] = []

    while True:
        params: dict[str, Any] = {
            "part": "snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": _PLAYLIST_PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = _api_get("playlistItems", api_key, params)
        items = payload.get("items") or []
        page_index += 1

        page_video_ids: list[str] = []
        stubs: dict[str, dict[str, Any]] = {}
        for item in items:
            snippet = item.get("snippet") or {}
            resource = snippet.get("resourceId") or {}
            video_id = resource.get("videoId")
            if not video_id:
                continue
            video_id = str(video_id)
            page_video_ids.append(video_id)
            stubs[video_id] = {
                "title": str(snippet.get("title") or ""),
                "published_at": _parse_api_datetime(snippet.get("publishedAt")),
            }

        if page_video_ids:
            view_counts = _fetch_view_counts_for_ids(page_video_ids, api_key, stubs)
            _append_records_for_page(records, page_video_ids, stubs, view_counts)

        if progress is not None:
            progress.report_substage(
                "enumerate",
                page_index,
                page_index + 1,
                f"YouTube API: listed {len(records)} playlist items…",
            )
            progress.report_log(
                f"YouTube Data API page {page_index}: {len(records)} videos listed so far"
            )

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return records


def scrape_channel_via_youtube_api(
    channel_url: str,
    api_key: str,
    progress: Optional[PipelineProgressTracker] = None,
) -> tuple[list[VideoRecord], str]:
    """Enumerate channel videos using YouTube Data API v3."""
    if progress is not None:
        progress.report_meta(scrape_backend="youtube_data_api")
        progress.report_log("YouTube Data API: resolving channel…")
        progress.report_substage(
            "resolve",
            0,
            1,
            "Resolving channel via YouTube Data API…",
        )

    _channel_id, channel_title, uploads_playlist_id = resolve_channel(
        channel_url, api_key
    )

    if progress is not None:
        progress.report_substage(
            "resolve",
            1,
            1,
            f"Resolved {channel_title} via YouTube Data API",
        )
        progress.report_substage(
            "enumerate",
            0,
            1,
            "Listing videos via YouTube Data API…",
        )

    records = list_channel_videos(uploads_playlist_id, api_key, progress)

    if not records:
        raise ChannelNotFound(
            f"No videos found for channel {channel_url}",
            details={
                "input_url": channel_url,
                "backend": "youtube_data_api",
                "uploads_playlist_id": uploads_playlist_id,
            },
        )

    if progress is not None:
        progress.report_substage(
            "enumerate",
            1,
            1,
            f"Found {len(records)} videos via YouTube Data API",
        )
        progress.report_meta(
            scraped_video_count=len(records),
            metadata_total=0,
            metadata_fetched=0,
        )
        progress.report(40, f"Scraped {len(records)} videos via YouTube Data API")

    return records, channel_title
