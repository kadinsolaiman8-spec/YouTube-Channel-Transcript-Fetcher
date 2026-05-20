"""Tests for channel URL validation."""

import pytest

from youtube_transcript_api.channel.models import ChannelNotFound
from youtube_transcript_api.channel.urls import parse_channel_url, validate_channel_url


def test_validate_channel_url_accepts_handle_shorthand():
    validate_channel_url("@TestChannel")
    kind, value = parse_channel_url("@TestChannel")
    assert kind == "handle"
    assert value == "TestChannel"


def test_validate_channel_url_accepts_full_handle_url():
    validate_channel_url("https://www.youtube.com/@TestChannel")
    kind, value = parse_channel_url("https://www.youtube.com/@TestChannel")
    assert kind == "handle"
    assert value == "TestChannel"


def test_validate_channel_url_rejects_empty_handle_url():
    with pytest.raises(ChannelNotFound, match="missing a handle"):
        validate_channel_url("https://www.youtube.com/@")


def test_validate_channel_url_rejects_empty_input():
    with pytest.raises(ChannelNotFound, match="Empty channel URL"):
        validate_channel_url("   ")


def test_validate_channel_url_accepts_channel_id_url():
    url = "https://www.youtube.com/channel/UCabcdefghijk"
    validate_channel_url(url)
    kind, value = parse_channel_url(url)
    assert kind == "channel_id"
    assert value == "UCabcdefghijk"


def test_validate_channel_url_accepts_uploads_playlist():
    url = "https://www.youtube.com/playlist?list=UUuploads123"
    validate_channel_url(url)
    kind, value = parse_channel_url(url)
    assert kind == "uploads_playlist"
    assert value == "UUuploads123"
