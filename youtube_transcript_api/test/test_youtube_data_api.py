import json
from datetime import datetime, timezone

import pytest
import responses

from youtube_transcript_api.channel.models import ChannelNotFound, ChannelScrapeError
from youtube_transcript_api.channel.youtube_data_api import (
    _get_api_key,
    fetch_video_metadata_by_ids,
    is_api_not_enabled_error,
    is_invalid_api_key_error,
    should_fallback_to_ytdlp_from_api_error,
    list_channel_videos,
    resolve_channel,
    scrape_channel_via_youtube_api,
    youtube_api_key_configured,
)
from youtube_transcript_api.channel.urls import validate_channel_url

API_BASE = "https://www.googleapis.com/youtube/v3"


@responses.activate
def test_resolve_channel_by_handle():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={
            "items": [
                {
                    "id": "UCtestchannel12",
                    "snippet": {"title": "Test Channel"},
                    "contentDetails": {
                        "relatedPlaylists": {"uploads": "UUtestchannel12"}
                    },
                }
            ]
        },
    )

    channel_id, title, uploads_id = resolve_channel(
        "https://www.youtube.com/@TestChannel",
        "test-key",
    )

    assert channel_id == "UCtestchannel12"
    assert title == "Test Channel"
    assert uploads_id == "UUtestchannel12"


@responses.activate
def test_resolve_channel_by_channel_id():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={
            "items": [
                {
                    "id": "UCabcdefghijk",
                    "snippet": {"title": "By ID"},
                    "contentDetails": {
                        "relatedPlaylists": {"uploads": "UUabcdefghijk"}
                    },
                }
            ]
        },
    )

    channel_id, title, uploads_id = resolve_channel(
        "https://www.youtube.com/channel/UCabcdefghijk",
        "test-key",
    )

    assert channel_id == "UCabcdefghijk"
    assert title == "By ID"
    assert uploads_id == "UUabcdefghijk"


@responses.activate
def test_list_channel_videos_paginates_and_fetches_statistics():
    playlist_page_one = {
        "items": [
            {
                "snippet": {
                    "title": "Video One",
                    "publishedAt": "2024-01-15T12:00:00Z",
                    "resourceId": {"videoId": "vid00000001"},
                }
            }
        ],
        "nextPageToken": "page2",
    }
    playlist_page_two = {
        "items": [
            {
                "snippet": {
                    "title": "Video Two",
                    "publishedAt": "2024-02-01T08:30:00Z",
                    "resourceId": {"videoId": "vid00000002"},
                }
            }
        ],
    }

    def playlist_items_response(request):
        if "pageToken=page2" in request.url:
            payload = playlist_page_two
        else:
            payload = playlist_page_one
        return (200, {}, json.dumps(payload))

    responses.add_callback(
        responses.GET,
        f"{API_BASE}/playlistItems",
        playlist_items_response,
        content_type="application/json",
    )
    responses.add(
        responses.GET,
        f"{API_BASE}/videos",
        json={
            "items": [
                {
                    "id": "vid00000001",
                    "statistics": {"viewCount": "100"},
                    "snippet": {"title": "Video One"},
                },
                {
                    "id": "vid00000002",
                    "statistics": {"viewCount": "250"},
                    "snippet": {"title": "Video Two"},
                },
            ]
        },
    )

    records = list_channel_videos("UUuploads123", "test-key")

    assert len(records) == 2
    assert records[0].video_id == "vid00000001"
    assert records[0].view_count == 100
    assert records[0].published_at == datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert records[1].video_id == "vid00000002"
    assert records[1].view_count == 250


@responses.activate
def test_fetch_video_metadata_by_ids_batches_requests():
    responses.add(
        responses.GET,
        f"{API_BASE}/videos",
        json={
            "items": [
                {
                    "id": "vid00000001",
                    "statistics": {"viewCount": "42"},
                    "snippet": {
                        "title": "First",
                        "publishedAt": "2024-03-01T10:00:00Z",
                    },
                },
                {
                    "id": "vid00000002",
                    "statistics": {"viewCount": "99"},
                    "snippet": {
                        "title": "Second",
                        "publishedAt": "2024-04-15T08:00:00Z",
                    },
                },
            ]
        },
    )

    metadata = fetch_video_metadata_by_ids(
        ["vid00000001", "vid00000002"],
        "test-key",
    )

    assert metadata["vid00000001"]["view_count"] == 42
    assert metadata["vid00000001"]["title"] == "First"
    assert metadata["vid00000001"]["published_at"] == datetime(
        2024, 3, 1, 10, 0, tzinfo=timezone.utc
    )
    assert metadata["vid00000002"]["view_count"] == 99


@responses.activate
def test_scrape_channel_via_youtube_api_end_to_end():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={
            "items": [
                {
                    "id": "UCapiend2end01",
                    "snippet": {"title": "API End To End"},
                    "contentDetails": {
                        "relatedPlaylists": {"uploads": "UUapiend2end01"}
                    },
                }
            ]
        },
    )
    responses.add(
        responses.GET,
        f"{API_BASE}/playlistItems",
        json={
            "items": [
                {
                    "snippet": {
                        "title": "Only Video",
                        "publishedAt": "2024-03-10T00:00:00Z",
                        "resourceId": {"videoId": "onlyvideo01"},
                    }
                }
            ]
        },
    )
    responses.add(
        responses.GET,
        f"{API_BASE}/videos",
        json={
            "items": [
                {
                    "id": "onlyvideo01",
                    "statistics": {"viewCount": "42"},
                    "snippet": {"title": "Only Video"},
                }
            ]
        },
    )

    records, label = scrape_channel_via_youtube_api(
        "https://www.youtube.com/@ApiEndToEnd",
        "test-key",
    )

    assert label == "API End To End"
    assert len(records) == 1
    assert records[0].video_id == "onlyvideo01"
    assert records[0].view_count == 42


@responses.activate
def test_resolve_channel_raises_when_not_found():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={"items": []},
    )

    with pytest.raises(ChannelNotFound, match="Channel not found"):
        resolve_channel("https://www.youtube.com/@Missing", "test-key")


@responses.activate
def test_api_get_raises_scrape_error_on_http_failure():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={"error": {"message": "quota exceeded"}},
        status=403,
    )

    with pytest.raises(ChannelScrapeError, match="YouTube Data API request failed"):
        resolve_channel("https://www.youtube.com/@Quota", "test-key")


def test_validate_channel_url_rejects_empty_handle_before_api():
    with pytest.raises(ChannelNotFound, match="missing a handle"):
        validate_channel_url("https://www.youtube.com/@")


@responses.activate
@responses.activate
def test_api_get_blocked_api_sets_error_kind():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={
            "error": {
                "message": (
                    "Requests to this API youtube method "
                    "youtube.api.v3.V3DataChannelService.List are blocked."
                ),
                "errors": [{"reason": "accessNotConfigured"}],
            }
        },
        status=403,
    )

    with pytest.raises(ChannelScrapeError) as exc_info:
        resolve_channel("https://www.youtube.com/@Test", "blocked-key")

    assert exc_info.value.details.get("error_kind") == "api_not_enabled"
    assert "YouTube Data API v3" in exc_info.value.details.get("hint", "")
    assert is_api_not_enabled_error(exc_info.value)
    assert should_fallback_to_ytdlp_from_api_error(exc_info.value)


def test_api_get_invalid_api_key_sets_error_kind():
    responses.add(
        responses.GET,
        f"{API_BASE}/channels",
        json={
            "error": {
                "message": "API key not valid. Please pass a valid API key.",
                "errors": [{"reason": "keyInvalid"}],
            }
        },
        status=400,
    )

    with pytest.raises(ChannelScrapeError) as exc_info:
        resolve_channel("https://www.youtube.com/@Test", "bad-key")

    assert exc_info.value.details.get("error_kind") == "invalid_api_key"
    assert "YouTube Data API v3" in exc_info.value.details.get("hint", "")
    assert is_invalid_api_key_error(exc_info.value)


def test_get_api_key_reads_environment(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert _get_api_key() is None
    assert youtube_api_key_configured() is False

    monkeypatch.setenv("YOUTUBE_API_KEY", "  secret-key  ")
    assert _get_api_key() == "secret-key"
    assert youtube_api_key_configured() is True
