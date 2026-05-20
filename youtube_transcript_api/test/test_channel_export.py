import io
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from youtube_transcript_api.channel.export import (
    AgentChannelFormatter,
    join_caption_lines,
)
from youtube_transcript_api.channel.models import (
    ExportConfig,
    PipelineResult,
    ProcessedVideo,
    VideoRecord,
)

ASSETS_DIR = Path(__file__).parent / "assets" / "channel"
FIXED_EXPORT_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def _video_record(
    video_id: str,
    title: str,
    published_at: datetime,
    view_count: int,
) -> VideoRecord:
    return VideoRecord(
        video_id=video_id,
        title=title,
        published_at=published_at,
        view_count=view_count,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _pipeline_result() -> PipelineResult:
    older = datetime(2024, 1, 10, tzinfo=timezone.utc)
    newer = datetime(2024, 6, 15, tzinfo=timezone.utc)

    return PipelineResult(
        channel_label="Example Channel",
        kept=[
            ProcessedVideo(
                record=_video_record("vid_older", "Older Video", older, 12_500),
                transcript_text="Hello world.\nHello world.\nNext line.",
            ),
            ProcessedVideo(
                record=_video_record("vid_newer", "Newer Video", newer, 98_000),
                transcript_text="Only once.\nOnly once.",
            ),
        ],
        removed=[
            _video_record("vid_removed", "Removed Outlier", newer, 50),
        ],
        failed=[
            (
                _video_record(
                    "vid_failed",
                    "No Captions",
                    datetime(2024, 3, 1, tzinfo=timezone.utc),
                    1_000,
                ),
                "No transcript found",
            ),
        ],
        filter_summary=(
            "Removed 1/3 videos below 10th percentile (threshold: 500 views) "
            "after 14-day age gate"
        ),
        scraped_video_count=4,
        fetch_attempted=3,
    )


class TestAgentChannelFormatter(TestCase):
    def setUp(self):
        self.formatter = AgentChannelFormatter()
        self.result = _pipeline_result()
        self.golden_path = ASSETS_DIR / "export_fixture_asc.txt"

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_format_to_file_matches_format(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        in_memory = self.formatter.format(self.result, ExportConfig(sort_order="asc"))
        buffer = io.StringIO()
        self.formatter.format_to_file(
            self.result,
            ExportConfig(sort_order="asc"),
            buffer,
            release_transcripts=False,
        )
        self.assertEqual(buffer.getvalue(), in_memory)

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_format_matches_golden_file_asc(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        output = self.formatter.format(self.result, ExportConfig(sort_order="asc"))
        expected = self.golden_path.read_text(encoding="utf-8")

        self.assertEqual(output, expected)

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_sort_order_desc_puts_newest_first(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        output = self.formatter.format(self.result, ExportConfig(sort_order="desc"))

        newer_pos = output.index("## 1/2 Newer Video")
        older_pos = output.index("## 2/2 Older Video")
        self.assertLess(newer_pos, older_pos)

    def test_dedupe_consecutive_transcript_lines(self):
        video = ProcessedVideo(
            record=_video_record(
                "dedupe",
                "Dedupe Test",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                100,
            ),
            transcript_text="repeat\nrepeat\nunique\nunique\nunique",
        )
        result = PipelineResult(
            channel_label="Dedupe Channel",
            kept=[video],
            filter_summary="No filtering applied",
        )

        with patch(
            "youtube_transcript_api.channel.export.datetime",
            wraps=datetime,
        ) as mock_datetime:
            mock_datetime.now.return_value = FIXED_EXPORT_TIME
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )
            output = self.formatter.format(result, ExportConfig())

        self.assertIn("repeat unique", output)
        self.assertNotIn("repeat\nrepeat\n", output)

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_include_metadata_header_false_omits_document_header(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        output = self.formatter.format(
            self.result,
            ExportConfig(include_metadata_header=False),
        )

        self.assertNotIn("Example Channel | export:", output)
        self.assertIn("## 1/2", output)

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_no_failed_appendix_when_none_failed(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        result = PipelineResult(
            channel_label="No Failures",
            kept=self.result.kept,
            filter_summary="All kept",
        )
        output = self.formatter.format(result, ExportConfig())

        self.assertNotIn("# FAILED", output)

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_format_matches_golden_file_verbose(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        output = self.formatter.format(
            self.result,
            ExportConfig(sort_order="asc", export_density="verbose"),
        )
        expected = (ASSETS_DIR / "export_fixture_verbose.txt").read_text(
            encoding="utf-8"
        )
        self.assertEqual(output, expected)

    def test_join_caption_lines_merges_continuation(self):
        merged = join_caption_lines("one two\nthree four")
        self.assertEqual(merged, "one two three four")

    def test_join_caption_lines_preserves_paragraph_break(self):
        merged = join_caption_lines("Hello world.\nNext line.")
        self.assertEqual(merged, "Hello world.\n\nNext line.")

    @patch(
        "youtube_transcript_api.channel.export.datetime",
        wraps=datetime,
    )
    def test_failed_appendix_groups_reasons_when_many_failures(self, mock_datetime):
        mock_datetime.now.return_value = FIXED_EXPORT_TIME
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        published = datetime(2024, 1, 1, tzinfo=timezone.utc)
        failed = [
            (
                _video_record(f"vid_{index}", f"Title {index}", published, 100),
                "YouTube IP block" if index < 10 else "Subtitles disabled",
            )
            for index in range(11)
        ]
        result = PipelineResult(
            channel_label="Many Failures",
            filter_summary="none",
            failed=failed,
            fetch_attempted=11,
        )
        output = self.formatter.format(result, ExportConfig())

        self.assertIn("# FAILED (11 videos)", output)
        self.assertIn("YouTube IP block: 10", output)
        self.assertIn("Subtitles disabled: 1", output)
