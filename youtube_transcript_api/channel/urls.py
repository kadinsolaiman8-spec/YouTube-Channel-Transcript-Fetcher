"""Channel URL validation and parsing."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api.channel.models import ChannelNotFound

_HANDLE_PATTERN = re.compile(r"^@[\w.-]+$")
_HANDLE_VALUE_PATTERN = re.compile(r"^[\w.-]+$")

_INVALID_HANDLE_HINT = (
    "Include a channel handle after @, for example "
    "https://www.youtube.com/@ChannelName"
)
_UNSUPPORTED_HINT = (
    "Use @handle, /channel/UC..., or an uploads playlist URL (list=UU...)."
)


def validate_channel_url(channel_url: str) -> None:
    """Raise ChannelNotFound if the URL cannot identify a channel."""
    parse_channel_url(channel_url)


def parse_channel_url(channel_url: str) -> tuple[str, str]:
    """Return (kind, value) where kind is handle, channel_id, or uploads_playlist."""
    url = channel_url.strip()
    if not url:
        raise ChannelNotFound(
            "Empty channel URL",
            details={"input_url": channel_url},
        )

    if _HANDLE_PATTERN.match(url):
        return "handle", url[1:]

    parsed = urlparse(
        url if "://" in url else f"https://www.youtube.com/{url.lstrip('/')}"
    )
    path = parsed.path.rstrip("/")

    if path == "/@" or path.startswith("/@"):
        handle = path[2:].split("/", 1)[0] if len(path) > 2 else ""
        if not handle or not _HANDLE_VALUE_PATTERN.match(handle):
            raise ChannelNotFound(
                "Channel URL is missing a handle after @",
                details={
                    "input_url": channel_url,
                    "hint": _INVALID_HANDLE_HINT,
                },
            )
        return "handle", handle

    if path.startswith("/channel/"):
        channel_id = path.split("/channel/", 1)[1].split("/", 1)[0]
        if channel_id.startswith("UC") and len(channel_id) > 2:
            return "channel_id", channel_id
        raise ChannelNotFound(
            "Channel URL is missing a valid channel ID (expected /channel/UC...)",
            details={
                "input_url": channel_url,
                "hint": _UNSUPPORTED_HINT,
            },
        )

    query = parse_qs(parsed.query)
    list_ids = query.get("list", [])
    if list_ids and str(list_ids[0]).startswith("UU"):
        return "uploads_playlist", str(list_ids[0])

    raise ChannelNotFound(
        f"Unsupported channel URL: {channel_url}",
        details={
            "input_url": channel_url,
            "hint": _UNSUPPORTED_HINT,
        },
    )
