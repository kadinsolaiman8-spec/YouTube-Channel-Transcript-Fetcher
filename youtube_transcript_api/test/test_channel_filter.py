from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest import TestCase
from unittest.mock import patch

from youtube_transcript_api.channel.filter import filter_videos
from youtube_transcript_api.channel.models import FilterConfig, VideoRecord

UTC = timezone.utc
FIXED_NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _record(
    video_id: str,
    view_count: int,
    *,
    days_ago: int = 30,
    title: Optional[str] = None,
) -> VideoRecord:
    published_at = FIXED_NOW - timedelta(days=days_ago)
    return VideoRecord(
        video_id=video_id,
        title=title or f"Video {video_id}",
        published_at=published_at,
        view_count=view_count,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


@patch("youtube_transcript_api.channel.filter._utc_now", return_value=FIXED_NOW)
class TestChannelFilter(TestCase):
    def test_empty_input(self, _mock_now):
        kept, removed, summary = filter_videos([], FilterConfig())
        self.assertEqual(kept, [])
        self.assertEqual(removed, [])
        self.assertEqual(summary, "No videos to filter.")

    def test_keeps_all_when_views_uniform_and_old_enough(self, _mock_now):
        records = [_record(str(i), 1000, days_ago=20) for i in range(10)]
        kept, removed, summary = filter_videos(
            records, FilterConfig(min_age_days=14, percentile_cutoff=10.0)
        )
        self.assertEqual(len(kept), 10)
        self.assertEqual(removed, [])
        self.assertIn("No videos removed below 10th percentile", summary)

    def test_percentile_removes_lowest_views(self, _mock_now):
        records = [
            _record("low", 100),
            _record("mid1", 500),
            _record("mid2", 600),
            _record("mid3", 700),
            _record("mid4", 800),
            _record("mid5", 900),
            _record("mid6", 1000),
            _record("mid7", 1100),
            _record("mid8", 1200),
            _record("high", 5000),
        ]
        kept, removed, summary = filter_videos(
            records, FilterConfig(min_age_days=0, percentile_cutoff=10.0)
        )
        removed_ids = {record.video_id for record in removed}
        self.assertIn("low", removed_ids)
        self.assertEqual(len(kept), 9)
        self.assertIn("10th percentile", summary)
        self.assertIn("threshold:", summary)

    def test_age_gate_removes_recent_videos(self, _mock_now):
        records = [
            _record("old", 1000, days_ago=30),
            _record("new", 5000, days_ago=3),
        ]
        kept, removed, summary = filter_videos(
            records, FilterConfig(min_age_days=14, percentile_cutoff=0.0)
        )
        self.assertEqual([record.video_id for record in kept], ["old"])
        self.assertEqual([record.video_id for record in removed], ["new"])
        self.assertIn("14-day age gate removed 1 video(s)", summary)

    def test_all_videos_too_new(self, _mock_now):
        records = [_record(str(i), 1000, days_ago=5) for i in range(5)]
        kept, removed, summary = filter_videos(records, FilterConfig(min_age_days=14))
        self.assertEqual(kept, [])
        self.assertEqual(len(removed), 5)
        self.assertIn("14-day age gate removed 5 video(s)", summary)

    def test_min_views_floor(self, _mock_now):
        records = [
            _record("tiny", 50, days_ago=30),
            _record("ok", 5000, days_ago=30),
        ]
        kept, removed, summary = filter_videos(
            records,
            FilterConfig(
                min_age_days=0,
                percentile_cutoff=0.0,
                min_views_floor=100,
            ),
        )
        self.assertEqual([record.video_id for record in kept], ["ok"])
        self.assertEqual([record.video_id for record in removed], ["tiny"])
        self.assertIn("hard floor removed 1 video(s) below 100 views", summary)

    def test_single_video_passes(self, _mock_now):
        records = [_record("only", 42, days_ago=60)]
        kept, removed, summary = filter_videos(records, FilterConfig())
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, [])
        self.assertIn("No videos removed below 10th percentile", summary)

    def test_naive_datetime_treated_as_utc(self, _mock_now):
        naive_published = datetime(2026, 4, 1, 0, 0, 0)
        record = VideoRecord(
            video_id="naive",
            title="Naive date",
            published_at=naive_published,
            view_count=1000,
            url="https://www.youtube.com/watch?v=naive",
        )
        kept, removed, summary = filter_videos([record], FilterConfig(min_age_days=14))
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, [])
        self.assertIn("No videos removed", summary)

    def test_percentile_and_age_gate_combined(self, _mock_now):
        records = [
            _record("old_low", 100, days_ago=40),
            _record("old_high", 10000, days_ago=40),
            _record("new_high", 10000, days_ago=2),
        ]
        kept, removed, summary = filter_videos(
            records, FilterConfig(min_age_days=14, percentile_cutoff=10.0)
        )
        kept_ids = {record.video_id for record in kept}
        removed_ids = {record.video_id for record in removed}
        self.assertEqual(kept_ids, {"old_high"})
        self.assertEqual(removed_ids, {"old_low", "new_high"})
        self.assertIn("age gate", summary)
        self.assertIn("percentile", summary)
