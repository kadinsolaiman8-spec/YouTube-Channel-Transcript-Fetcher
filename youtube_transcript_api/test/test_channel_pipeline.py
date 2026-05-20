from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from youtube_transcript_api.channel.models import (
    ExportConfig,
    FilterConfig,
    NoTranscriptsRetrieved,
    PipelineResult,
    ProcessedVideo,
    ScrapeConfig,
    VideoRecord,
)
from youtube_transcript_api.channel.pipeline import run_pipeline
from youtube_transcript_api.channel.models import PipelineOutput


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


class _RecordingProgress:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.progress_updates: list[tuple[int, int, str]] = []

    def on_stage(self, stage: str, message: str = "") -> None:
        self.stages.append(stage)

    def on_progress(self, current: int, total: int, message: str = "") -> None:
        self.progress_updates.append((current, total, message))


class TestRunPipeline(TestCase):
    @patch("youtube_transcript_api.channel.pipeline.AgentChannelFormatter")
    @patch("youtube_transcript_api.channel.pipeline.fetch_transcripts")
    @patch("youtube_transcript_api.channel.pipeline.filter_videos")
    @patch("youtube_transcript_api.channel.pipeline.scrape_channel")
    def test_run_pipeline_orchestrates_stages(
        self,
        mock_scrape,
        mock_filter,
        mock_fetch,
        mock_formatter_cls,
    ):
        channel_url = "https://www.youtube.com/@example"
        published = datetime(2024, 1, 1, tzinfo=timezone.utc)
        all_records = [
            _video_record("keep1", "Keep One", published, 1000),
            _video_record("keep2", "Keep Two", published, 2000),
            _video_record("removed1", "Removed", published, 10),
        ]
        kept_records = all_records[:2]
        removed_records = [all_records[2]]
        processed = [
            ProcessedVideo(record=kept_records[0], transcript_text="first"),
            ProcessedVideo(record=kept_records[1], transcript_text="second"),
        ]
        failed = [
            (
                _video_record("failed1", "Failed", published, 500),
                "No transcript found",
            )
        ]
        filter_summary = "Removed 1/3 videos below 10th percentile"

        mock_scrape.return_value = (all_records, "Example Channel", "ytdlp")
        mock_filter.return_value = (kept_records, removed_records, filter_summary)
        mock_fetch.return_value = (processed, failed)
        mock_formatter = MagicMock()
        mock_formatter.format.return_value = "formatted export"
        mock_formatter_cls.return_value = mock_formatter

        filter_config = FilterConfig(min_age_days=14, percentile_cutoff=10.0)
        export_config = ExportConfig(sort_order="asc", languages=("en",))
        progress = _RecordingProgress()

        pipeline_output = run_pipeline(
            channel_url,
            filter_config,
            export_config,
            max_workers=2,
            sleep_seconds=0.5,
            progress_callback=progress,
        )
        result = pipeline_output.result
        export_text = pipeline_output.export_text

        mock_scrape.assert_called_once_with(
            channel_url,
            max_workers=2,
            sleep_seconds=0.5,
            progress_callback=progress,
            scrape_config=ScrapeConfig(enrich_max_workers=2),
        )
        mock_filter.assert_called_once_with(all_records, filter_config)
        mock_fetch.assert_called_once()
        fetch_kwargs = mock_fetch.call_args.kwargs
        self.assertEqual(fetch_kwargs["max_workers"], 2)
        self.assertEqual(fetch_kwargs["delay"], 0.5)
        self.assertIsNotNone(fetch_kwargs["progress_callback"])
        mock_formatter.format.assert_called_once()
        format_args = mock_formatter.format.call_args
        self.assertIs(format_args[0][1], export_config)

        self.assertEqual(result.channel_label, "Example Channel")
        self.assertEqual(result.kept, processed)
        self.assertEqual(result.removed, removed_records)
        self.assertEqual(result.failed, failed)
        self.assertEqual(result.filter_summary, filter_summary)
        self.assertEqual(result.fetch_attempted, 2)
        self.assertEqual(export_text, "formatted export")

        self.assertEqual(
            progress.stages,
            ["scraping", "filtering", "fetching", "exporting"],
        )

    @patch("youtube_transcript_api.channel.pipeline.fetch_transcripts")
    @patch("youtube_transcript_api.channel.pipeline.filter_videos")
    @patch("youtube_transcript_api.channel.pipeline.scrape_channel")
    def test_run_pipeline_raises_when_all_transcripts_fail(
        self,
        mock_scrape,
        mock_filter,
        mock_fetch,
    ):
        published = datetime(2024, 1, 1, tzinfo=timezone.utc)
        kept_records = [_video_record("only", "Only Video", published, 900)]
        mock_scrape.return_value = (kept_records, "Solo Channel", "ytdlp")
        mock_filter.return_value = (kept_records, [], "Kept all 1 video(s)")
        mock_fetch.return_value = (
            [],
            [(kept_records[0], "YouTube rate limit (429)")],
        )

        with self.assertRaises(NoTranscriptsRetrieved) as exc_info:
            run_pipeline(
                "https://www.youtube.com/@solo",
                FilterConfig(),
                ExportConfig(),
            )

        details = exc_info.exception.details
        self.assertEqual(details["fetch_attempted"], 1)
        self.assertEqual(details["rate_limit_count"], 1)
        self.assertIn("429", details["hint"])

    @patch("youtube_transcript_api.channel.pipeline.AgentChannelFormatter")
    @patch("youtube_transcript_api.channel.pipeline.fetch_transcripts")
    @patch("youtube_transcript_api.channel.pipeline.filter_videos")
    @patch("youtube_transcript_api.channel.pipeline.scrape_channel")
    def test_run_pipeline_without_progress_callback(
        self,
        mock_scrape,
        mock_filter,
        mock_fetch,
        mock_formatter_cls,
    ):
        published = datetime(2024, 2, 1, tzinfo=timezone.utc)
        record = _video_record("only", "Only Video", published, 900)

        mock_scrape.return_value = ([record], "Solo Channel", "ytdlp")
        mock_filter.return_value = ([record], [], "Kept all 1 video(s)")
        mock_fetch.return_value = (
            [ProcessedVideo(record=record, transcript_text="text")],
            [],
        )
        mock_formatter_cls.return_value.format.return_value = "export body"

        pipeline_output = run_pipeline(
            "https://www.youtube.com/@solo",
            FilterConfig(),
            ExportConfig(),
        )

        self.assertIsInstance(pipeline_output, PipelineOutput)
        result = pipeline_output.result
        self.assertIsInstance(result, PipelineResult)
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(pipeline_output.export_text, "export body")
        mock_fetch.assert_called_once()
        self.assertIsNone(mock_fetch.call_args.kwargs["progress_callback"])

    @patch("youtube_transcript_api.channel.pipeline.AgentChannelFormatter")
    @patch("youtube_transcript_api.channel.pipeline.fetch_transcripts")
    @patch("youtube_transcript_api.channel.pipeline.filter_videos")
    @patch("youtube_transcript_api.channel.pipeline.scrape_channel")
    def test_run_pipeline_writes_to_output_path(
        self,
        mock_scrape,
        mock_filter,
        mock_fetch,
        mock_formatter_cls,
    ):
        published = datetime(2024, 2, 1, tzinfo=timezone.utc)
        record = _video_record("only", "Only Video", published, 900)

        mock_scrape.return_value = ([record], "Solo Channel", "ytdlp")
        mock_filter.return_value = ([record], [], "Kept all 1 video(s)")
        mock_fetch.return_value = (
            [ProcessedVideo(record=record, transcript_text="text")],
            [],
        )
        mock_formatter = MagicMock()
        mock_formatter_cls.return_value = mock_formatter

        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "export.txt"
            pipeline_output = run_pipeline(
                "https://www.youtube.com/@solo",
                FilterConfig(max_videos=1),
                ExportConfig(),
                output_path=output_path,
            )

        mock_formatter.format_to_file.assert_called_once()
        self.assertEqual(pipeline_output.output_path, output_path)
        self.assertEqual(pipeline_output.export_text, "")
