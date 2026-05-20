from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from requests.exceptions import RetryError

from youtube_transcript_api import (
    FetchedTranscript,
    FetchedTranscriptSnippet,
    IpBlocked,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.channel.fetcher import (
    RATE_LIMIT_REASON,
    RATE_LIMIT_REASON_PROXY,
    _failure_reason,
    fetch_transcripts,
)
from youtube_transcript_api.channel.models import ExportConfig, VideoRecord
from youtube_transcript_api.proxies import GenericProxyConfig


def _make_record(video_id: str) -> VideoRecord:
    return VideoRecord(
        video_id=video_id,
        title=f"Video {video_id}",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        view_count=1000,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _make_transcript(video_id: str, lines: list[str]) -> FetchedTranscript:
    snippets = [
        FetchedTranscriptSnippet(text=line, start=float(index), duration=1.0)
        for index, line in enumerate(lines)
    ]
    return FetchedTranscript(
        snippets=snippets,
        video_id=video_id,
        language="English",
        language_code="en",
        is_generated=False,
    )


class TestChannelFetcher(TestCase):
    def setUp(self):
        self.export_config = ExportConfig(languages=("en", "de"))
        self.api_patcher = patch(
            "youtube_transcript_api.channel.fetcher.YouTubeTranscriptApi"
        )
        self.mock_api_cls = self.api_patcher.start()
        self.mock_api = MagicMock()
        self.mock_api_cls.return_value = self.mock_api
        self.addCleanup(self.api_patcher.stop)

    def test_fetch_transcripts_success(self):
        self.mock_api.fetch.return_value = _make_transcript(
            "abc123",
            ["Hello", "world"],
        )

        kept, failed = fetch_transcripts([_make_record("abc123")], self.export_config)

        self.assertEqual(len(kept), 1)
        self.assertEqual(len(failed), 0)
        self.assertEqual(kept[0].record.video_id, "abc123")
        self.assertEqual(kept[0].transcript_text, "Hello\nworld")
        self.mock_api.fetch.assert_called_once_with("abc123", languages=["en", "de"])

    def test_fetch_transcripts_passes_proxy_config(self):
        proxy_config = GenericProxyConfig(http_url="http://proxy.example:8080")
        self.mock_api.fetch.return_value = _make_transcript("abc123", ["Hi"])

        fetch_transcripts(
            [_make_record("abc123")],
            self.export_config,
            proxy_config=proxy_config,
        )

        self.mock_api_cls.assert_called_once_with(proxy_config=proxy_config)

    def test_fetch_transcripts_retrievable_errors_go_to_failed(self):
        records = [
            _make_record("disabled"),
            _make_record("missing"),
            _make_record("unavailable"),
            _make_record("blocked"),
        ]
        self.mock_api.fetch.side_effect = [
            TranscriptsDisabled("disabled"),
            NoTranscriptFound("missing", ["en"], MagicMock()),
            VideoUnavailable("unavailable"),
            IpBlocked("blocked"),
        ]

        kept, failed = fetch_transcripts(records, self.export_config)

        self.assertEqual(kept, [])
        self.assertEqual(len(failed), 4)
        self.assertEqual(
            {record.video_id for record, _ in failed},
            {
                "disabled",
                "missing",
                "unavailable",
                "blocked",
            },
        )
        reasons = {reason for _, reason in failed}
        self.assertIn("Subtitles disabled", reasons)
        self.assertIn("No transcript found", reasons)
        self.assertIn("Video unavailable", reasons)
        self.assertIn(RATE_LIMIT_REASON, reasons)

    def test_fetch_transcripts_mixed_success_and_failure(self):
        records = [_make_record("ok1"), _make_record("fail"), _make_record("ok2")]
        self.mock_api.fetch.side_effect = [
            _make_transcript("ok1", ["first"]),
            TranscriptsDisabled("fail"),
            _make_transcript("ok2", ["second"]),
        ]

        kept, failed = fetch_transcripts(records, self.export_config, max_workers=1)

        self.assertEqual(len(kept), 2)
        self.assertEqual(len(failed), 1)
        self.assertEqual([video.record.video_id for video in kept], ["ok1", "ok2"])
        self.assertEqual(failed[0][0].video_id, "fail")

    def test_fetch_transcripts_empty_input(self):
        kept, failed = fetch_transcripts([], self.export_config)

        self.assertEqual(kept, [])
        self.assertEqual(failed, [])
        self.mock_api_cls.assert_not_called()

    def test_fetch_transcripts_progress_callback(self):
        self.mock_api.fetch.return_value = _make_transcript("abc123", ["Hi"])
        progress = MagicMock()

        fetch_transcripts(
            [_make_record("abc123"), _make_record("def456")],
            self.export_config,
            max_workers=1,
            progress_callback=progress,
        )

        progress.on_progress.assert_any_call(1, 2, "Fetched transcripts 1/2")
        progress.on_progress.assert_any_call(2, 2, "Fetched transcripts 2/2")
        self.assertEqual(progress.on_progress.call_count, 2)

    def test_fetch_transcripts_one_api_instance_per_thread(self):
        created_instances = []

        def create_api(**kwargs):
            instance = MagicMock()
            instance.fetch.return_value = _make_transcript("vid", ["text"])
            created_instances.append(instance)
            return instance

        self.mock_api_cls.side_effect = create_api

        fetch_transcripts(
            [_make_record("a"), _make_record("b"), _make_record("c")],
            self.export_config,
            max_workers=2,
        )

        self.assertGreaterEqual(len(created_instances), 1)
        self.assertLessEqual(len(created_instances), 2)

    def test_failure_reason_rate_limit_with_proxy(self) -> None:
        proxy_config = GenericProxyConfig(http_url="http://proxy.example:8080")
        reason = _failure_reason(IpBlocked("vid"), proxy_config)
        self.assertEqual(reason, RATE_LIMIT_REASON_PROXY)

    def test_failure_reason_rate_limit_without_proxy(self) -> None:
        reason = _failure_reason(IpBlocked("vid"), None)
        self.assertEqual(reason, RATE_LIMIT_REASON)

    def test_failure_reason_retry_error_429(self) -> None:
        exc = RetryError(
            "HTTPSConnectionPool: Max retries exceeded "
            "(Caused by ResponseError('too many 429 error responses'))"
        )
        self.assertEqual(_failure_reason(exc, None), RATE_LIMIT_REASON)

    def test_fetch_transcripts_retry_error_goes_to_failed(self):
        records = [_make_record("rate_limited")]
        self.mock_api.fetch.side_effect = RetryError(
            "too many 429 error responses",
            request=MagicMock(),
            response=MagicMock(),
        )

        kept, failed = fetch_transcripts(records, self.export_config)

        self.assertEqual(kept, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0].video_id, "rate_limited")
        self.assertEqual(failed[0][1], RATE_LIMIT_REASON)
