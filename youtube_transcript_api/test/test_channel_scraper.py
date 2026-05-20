import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from youtube_transcript_api.channel import scraper
from youtube_transcript_api.channel.models import (
    ChannelNotFound,
    ChannelScrapeError,
    ScrapeConfig,
    VideoRecord,
    YtdlpNotInstalled,
)
from youtube_transcript_api.channel.scraper import (
    _parse_cookies_from_browser,
    scrape_channel,
)

ASSETS_DIR = Path(__file__).parent / "assets" / "channel"

CHANNEL_INFO = {
    "channel": "Test Channel",
    "channel_id": "UCtestchannel12",
    "id": "UCtestchannel12",
}

FLAT_ALL_NEED_ENRICH = {
    "channel": "Needs API Enrich",
    "entries": [
        {
            "id": "vid00000001",
            "title": "Video One",
            "url": "https://www.youtube.com/watch?v=vid00000001",
        },
        {
            "id": "vid00000002",
            "title": "Video Two",
            "url": "https://www.youtube.com/watch?v=vid00000002",
        },
        {
            "id": "vid00000003",
            "title": "Video Three",
            "url": "https://www.youtube.com/watch?v=vid00000003",
        },
    ],
}


def load_fixture(name: str) -> dict:
    with open(ASSETS_DIR / name, encoding="utf-8") as file:
        return json.load(file)


@pytest.fixture
def ytdlp_on_path(monkeypatch):
    monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: False)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd: "/usr/bin/yt-dlp" if cmd == "yt-dlp" else None,
    )


def _make_mock_youtube_dl(flat_info, enrich_info):
    class MockYoutubeDL:
        instances = []

        def __init__(self, opts):
            self.opts = opts
            MockYoutubeDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            if self.opts.get("extract_flat") is True:
                if "list=" in url:
                    return flat_info
                return CHANNEL_INFO
            if "xyz987uvw65" in url or "nodatevid01" in url:
                return enrich_info
            return enrich_info

    return MockYoutubeDL


def _patch_ytdlp(monkeypatch, mock_class: type) -> None:
    monkeypatch.setattr(
        scraper,
        "_import_ytdlp",
        lambda: (mock_class, scraper.DownloadError),
    )


class TestScrapeChannel:
    def test_scrape_channel_normalizes_records_and_enriches_views(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("flat_playlist.json")
        enrich_info = load_fixture("video_enrich.json")
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel",
            max_workers=2,
        )

        assert channel_label == "Test Channel"
        assert len(records) == 3

        by_id = {record.video_id: record for record in records}

        assert by_id["abc123def45"].view_count == 50000
        assert by_id["abc123def45"].title == "Video With Views"
        assert by_id["abc123def45"].url == "https://www.youtube.com/watch?v=abc123def45"
        assert by_id["abc123def45"].published_at == datetime(
            2024, 1, 15, tzinfo=timezone.utc
        )

        assert by_id["xyz987uvw65"].view_count == 1200
        assert by_id["xyz987uvw65"].title == "Video Needs Enrich"

        assert by_id["ts111222333"].view_count == 900
        assert by_id["ts111222333"].published_at == datetime.fromtimestamp(
            1709251200, tz=timezone.utc
        )

        enrich_instances = [
            instance
            for instance in mock_class.instances
            if instance.opts.get("extract_flat") is not True
        ]
        assert enrich_instances

    def test_scrape_channel_enriches_missing_publish_dates(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = {
            "channel": "Flat Dates",
            "entries": [
                {
                    "id": "nodatevid01",
                    "title": "Needs Date Enrich",
                    "url": "https://www.youtube.com/watch?v=nodatevid01",
                }
            ],
        }
        enrich_info = {
            "id": "nodatevid01",
            "title": "Needs Date Enrich",
            "upload_date": "20240120",
            "view_count": 500,
        }
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@FlatDates"
        )

        assert channel_label == "Flat Dates"
        assert len(records) == 1
        assert records[0].published_at == datetime(2024, 1, 20, tzinfo=timezone.utc)
        assert records[0].view_count == 500

    def test_scrape_channel_skips_enrichment_when_views_present(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = {
            "channel": "All Views",
            "entries": [
                {
                    "id": "vid00000001",
                    "title": "Complete",
                    "upload_date": "20240301",
                    "view_count": 42,
                }
            ],
        }

        class MockYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.opts.get("extract_flat") is True:
                    if "list=" in url:
                        return flat_info
                    return CHANNEL_INFO
                return flat_info

        _patch_ytdlp(monkeypatch, MockYoutubeDL)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@AllViews"
        )

        assert channel_label == "All Views"
        assert len(records) == 1
        assert records[0].view_count == 42
        assert records[0].url == "https://www.youtube.com/watch?v=vid00000001"

    def test_scrape_channel_raises_channel_not_found_for_empty_playlist(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("empty_playlist.json")

        class MockYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.opts.get("extract_flat") is True and "list=" not in url:
                    return CHANNEL_INFO
                return flat_info

        _patch_ytdlp(monkeypatch, MockYoutubeDL)

        with pytest.raises(ChannelNotFound, match="No videos found") as exc_info:
            scrape_channel("https://www.youtube.com/@Empty")

        assert "input_url" in exc_info.value.details

    def test_scrape_channel_raises_scrape_error_on_extract_error(
        self, ytdlp_on_path, monkeypatch
    ):
        class MockYoutubeDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                raise RuntimeError("network down")

        _patch_ytdlp(monkeypatch, MockYoutubeDL)
        monkeypatch.setattr(scraper, "_get_api_key", lambda: None)

        with pytest.raises(
            ChannelScrapeError, match="Could not scrape channel"
        ) as exc_info:
            scrape_channel("https://www.youtube.com/@Missing")

        assert exc_info.value.details["error_type"] == "RuntimeError"

    def test_enumerate_opts_use_flat_extract(self, ytdlp_on_path):
        opts = scraper._enumerate_opts()
        assert opts["extract_flat"] is True
        assert opts["ignoreerrors"] is True
        assert opts["lazy_playlist"] is True

    def test_scrape_channel_returns_partial_playlist_on_download_error(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("flat_playlist.json")
        captured: dict = {}

        class MockYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.opts.get("extract_flat") is True and "list=" not in url:
                    return CHANNEL_INFO
                if self.opts.get("extract_flat") is True and "list=" in url:
                    captured["info"] = flat_info
                    raise scraper.DownloadError("unavailable video in playlist")
                return flat_info

        def enumerate_playlist(
            list_url, youtube_dl_class, scrape_config, progress=None
        ):
            info: dict = {}
            try:
                with youtube_dl_class(scraper._enumerate_opts(scrape_config)) as ydl:
                    try:
                        extracted = ydl.extract_info(list_url, download=False)
                        if extracted:
                            info = extracted
                    except scraper.DownloadError:
                        if captured.get("info"):
                            info = captured["info"]
                        else:
                            raise
            finally:
                pass
            entries, skipped = scraper._extract_entries(info)
            return info, entries, skipped

        _patch_ytdlp(monkeypatch, MockYoutubeDL)
        monkeypatch.setattr(scraper, "_enumerate_playlist", enumerate_playlist)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel"
        )

        assert channel_label == "Test Channel"
        assert len(records) == 3

    def test_scrape_channel_skips_unavailable_flat_entries(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = {
            "channel": "Test Channel",
            "entries": [
                {
                    "id": "abc123def45",
                    "title": "Good Video",
                    "upload_date": "20240115",
                    "view_count": 100,
                },
                None,
                {
                    "title": "No Id Entry",
                    "url": "https://www.youtube.com/watch?v=",
                },
                {
                    "id": "xyz987uvw65",
                    "title": "Second Good",
                    "upload_date": "20240201",
                    "view_count": 200,
                },
            ],
        }
        mock_class = _make_mock_youtube_dl(flat_info, load_fixture("video_enrich.json"))
        _patch_ytdlp(monkeypatch, mock_class)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel"
        )

        assert channel_label == "Test Channel"
        assert len(records) == 2
        assert {record.video_id for record in records} == {"abc123def45", "xyz987uvw65"}

    def test_scrape_channel_falls_back_to_youtube_api(self, ytdlp_on_path, monkeypatch):
        api_records = [
            VideoRecord(
                video_id="api00000001",
                title="API Video",
                published_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
                view_count=99,
                url="https://www.youtube.com/watch?v=api00000001",
            )
        ]

        class MockYoutubeDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                raise scraper.DownloadError("playlist blocked")

        _patch_ytdlp(monkeypatch, MockYoutubeDL)
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-api-key")
        monkeypatch.setattr(
            scraper,
            "scrape_channel_via_youtube_api",
            lambda channel_url, api_key, progress=None: (api_records, "API Channel"),
        )

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@ApiFallback"
        )

        assert channel_label == "API Channel"
        assert scrape_backend == "youtube_data_api"
        assert len(records) == 1
        assert records[0].video_id == "api00000001"

    def test_scrape_channel_prefers_youtube_api_without_cookies(self, monkeypatch):
        api_records = [
            VideoRecord(
                video_id="api00000001",
                title="API Video",
                published_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
                view_count=99,
                url="https://www.youtube.com/watch?v=api00000001",
            )
        ]
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-api-key")
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: True)
        monkeypatch.setattr(
            scraper,
            "scrape_channel_via_youtube_api",
            lambda channel_url, api_key, progress=None: (api_records, "API Direct"),
        )

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@Direct",
            scrape_config=ScrapeConfig(),
        )

        assert channel_label == "API Direct"
        assert scrape_backend == "youtube_data_api"
        assert len(records) == 1

    def test_scrape_channel_falls_back_to_ytdlp_on_blocked_api(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("flat_playlist.json")
        enrich_info = load_fixture("video_enrich.json")
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)
        monkeypatch.setenv("YOUTUBE_API_KEY", "blocked-key")
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: True)

        def _raise_blocked_api(channel_url, api_key, progress=None):
            raise ChannelScrapeError(
                "YouTube Data API request failed: Requests to this API youtube method "
                "youtube.api.v3.V3DataChannelService.List are blocked.",
                details={"error_kind": "api_not_enabled", "status_code": 403},
            )

        monkeypatch.setattr(
            scraper,
            "scrape_channel_via_youtube_api",
            _raise_blocked_api,
        )

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel",
            scrape_config=ScrapeConfig(),
        )

        assert scrape_backend == "ytdlp"
        assert channel_label == "Test Channel"
        assert len(records) == 3

    def test_scrape_channel_falls_back_to_ytdlp_on_invalid_api_key(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("flat_playlist.json")
        enrich_info = load_fixture("video_enrich.json")
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)
        monkeypatch.setenv("YOUTUBE_API_KEY", "bad-key")
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: True)

        def _raise_invalid_api_key(channel_url, api_key, progress=None):
            raise ChannelScrapeError(
                "YouTube Data API request failed: API key not valid. "
                "Please pass a valid API key.",
                details={"error_kind": "invalid_api_key"},
            )

        monkeypatch.setattr(
            scraper,
            "scrape_channel_via_youtube_api",
            _raise_invalid_api_key,
        )

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel",
            scrape_config=ScrapeConfig(),
        )

        assert scrape_backend == "ytdlp"
        assert channel_label == "Test Channel"
        assert len(records) == 3

    def _setup_ytdlp_api_fallback(self, monkeypatch, flat_info, enrich_info):
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-api-key")
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: True)

        def _raise_blocked_api(channel_url, api_key, progress=None):
            raise ChannelScrapeError(
                "YouTube Data API request failed: API blocked",
                details={"error_kind": "api_not_enabled", "status_code": 403},
            )

        monkeypatch.setattr(
            scraper,
            "scrape_channel_via_youtube_api",
            _raise_blocked_api,
        )

    def test_scrape_channel_api_metadata_enrich_when_ytdlp_enrich_empty(
        self, ytdlp_on_path, monkeypatch
    ):
        class EmptyEnrichMockYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.opts.get("extract_flat") is True:
                    if "list=" in url:
                        return FLAT_ALL_NEED_ENRICH
                    return CHANNEL_INFO
                return {}

        _patch_ytdlp(monkeypatch, EmptyEnrichMockYoutubeDL)
        self._setup_ytdlp_api_fallback(monkeypatch, FLAT_ALL_NEED_ENRICH, {})

        def _api_metadata(video_ids, api_key):
            return {
                video_id: {
                    "title": f"API {video_id}",
                    "published_at": datetime(2024, 5, 1, tzinfo=timezone.utc),
                    "view_count": 1000,
                }
                for video_id in video_ids
            }

        monkeypatch.setattr(scraper, "fetch_video_metadata_by_ids", _api_metadata)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@NeedsApiEnrich",
            scrape_config=ScrapeConfig(),
        )

        assert scrape_backend == "ytdlp"
        assert len(records) == 3
        assert all(record.published_at.year == 2024 for record in records)
        assert all(record.view_count == 1000 for record in records)

    def test_scrape_channel_ytdlp_enrich_only_for_api_misses(
        self, ytdlp_on_path, monkeypatch
    ):
        enrich_info = {
            "id": "vid00000003",
            "title": "Video Three Enriched",
            "upload_date": "20240601",
            "view_count": 77,
        }
        self._setup_ytdlp_api_fallback(
            monkeypatch,
            FLAT_ALL_NEED_ENRICH,
            enrich_info,
        )

        def _partial_api_metadata(video_ids, api_key):
            metadata = {}
            for video_id in video_ids:
                if video_id != "vid00000003":
                    metadata[video_id] = {
                        "title": f"API {video_id}",
                        "published_at": datetime(2024, 5, 1, tzinfo=timezone.utc),
                        "view_count": 500,
                    }
            return metadata

        monkeypatch.setattr(
            scraper, "fetch_video_metadata_by_ids", _partial_api_metadata
        )

        records, _, scrape_backend = scrape_channel(
            "https://www.youtube.com/@PartialApi",
            scrape_config=ScrapeConfig(),
            max_workers=1,
        )

        assert scrape_backend == "ytdlp"
        assert len(records) == 3
        by_id = {record.video_id: record for record in records}
        assert by_id["vid00000003"].view_count == 77
        assert by_id["vid00000003"].published_at == datetime(
            2024, 6, 1, tzinfo=timezone.utc
        )

    def test_scrape_channel_raises_with_metadata_failure_details(
        self, ytdlp_on_path, monkeypatch
    ):
        class EmptyEnrichMockYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                if self.opts.get("extract_flat") is True:
                    if "list=" in url:
                        return FLAT_ALL_NEED_ENRICH
                    return CHANNEL_INFO
                return {"error": "ytdlp enrich failed"}

        _patch_ytdlp(monkeypatch, EmptyEnrichMockYoutubeDL)
        self._setup_ytdlp_api_fallback(monkeypatch, FLAT_ALL_NEED_ENRICH, {})
        monkeypatch.setattr(
            scraper,
            "fetch_video_metadata_by_ids",
            lambda video_ids, api_key: {},
        )

        with pytest.raises(ChannelScrapeError, match="No video metadata") as exc_info:
            scrape_channel(
                "https://www.youtube.com/@AllFail",
                scrape_config=ScrapeConfig(),
            )

        details = exc_info.value.details
        assert details["enrich_failures"] == 3
        assert details["api_metadata_enrich_attempted"] is True
        assert details["scrape_backend"] == "ytdlp"
        assert "videos.list" in details["hint"]

    def test_scrape_channel_raises_when_api_fallback_unavailable(
        self, ytdlp_on_path, monkeypatch
    ):
        class MockYoutubeDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                raise scraper.DownloadError("playlist blocked")

        _patch_ytdlp(monkeypatch, MockYoutubeDL)
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

        with pytest.raises(ChannelScrapeError, match="Could not scrape channel"):
            scrape_channel("https://www.youtube.com/@NoApiKey")

    def test_parse_cookies_from_browser_normalizes_specs(self):
        assert _parse_cookies_from_browser("brave") == ("brave",)
        assert _parse_cookies_from_browser("Brave:Default") == ("brave", "Default")
        assert _parse_cookies_from_browser(
            r"brave:C:\Users\me\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default"
        ) == (
            "brave",
            r"C:\Users\me\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default",
        )

    def test_scrape_channel_raises_when_cookiefile_missing(
        self, ytdlp_on_path, monkeypatch, tmp_path
    ):
        missing = tmp_path / "missing_cookies.txt"
        config = ScrapeConfig(cookiefile=str(missing))

        with pytest.raises(ChannelScrapeError, match="Cookie file not found"):
            scrape_channel(
                "https://www.youtube.com/@TestChannel",
                scrape_config=config,
            )

    def test_scrape_channel_raises_ytdlp_not_installed(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: False)

        def _raise_not_installed() -> tuple[type, type]:
            raise YtdlpNotInstalled("yt-dlp is required")

        monkeypatch.setattr(scraper, "_import_ytdlp", _raise_not_installed)

        with pytest.raises(YtdlpNotInstalled, match="yt-dlp is required"):
            scrape_channel("https://www.youtube.com/@TestChannel")

    def test_scrape_channel_uses_import_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        monkeypatch.setattr(scraper, "youtube_api_key_configured", lambda: False)
        flat_info = load_fixture("flat_playlist.json")

        mock_class = _make_mock_youtube_dl(flat_info, load_fixture("video_enrich.json"))
        _patch_ytdlp(monkeypatch, mock_class)

        records, channel_label, scrape_backend = scrape_channel(
            "https://www.youtube.com/@TestChannel"
        )

        assert channel_label == "Test Channel"
        assert len(records) == 3

    def test_scrape_channel_applies_sleep_between_enrichment_requests(
        self, ytdlp_on_path, monkeypatch
    ):
        flat_info = load_fixture("flat_playlist.json")
        enrich_info = load_fixture("video_enrich.json")
        mock_class = _make_mock_youtube_dl(flat_info, enrich_info)
        _patch_ytdlp(monkeypatch, mock_class)

        with patch.object(scraper.time, "sleep") as sleep_mock:
            scrape_channel(
                "https://www.youtube.com/@TestChannel",
                max_workers=1,
                sleep_seconds=0.5,
            )

        sleep_mock.assert_called_with(0.5)
