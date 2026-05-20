"""Tests for progress ETA helpers."""

from __future__ import annotations

from unittest import TestCase

from youtube_transcript_api.channel.progress_reporting import (
    EtaSettings,
    estimate_eta_seconds,
    metadata_past_halfway,
    project_pipeline_seconds,
)


class TestProgressReporting(TestCase):
    def test_metadata_past_halfway_when_no_enrichment_needed(self) -> None:
        self.assertTrue(metadata_past_halfway(0, 0))

    def test_metadata_past_halfway_before_enrich_complete(self) -> None:
        self.assertFalse(metadata_past_halfway(10, 4))
        self.assertTrue(metadata_past_halfway(10, 5))

    def test_estimate_eta_inflates_before_metadata_halfway(self) -> None:
        baseline = estimate_eta_seconds(
            60.0,
            20,
            scraped_video_count=100,
            metadata_total=10,
            metadata_fetched=2,
        )
        settled = estimate_eta_seconds(
            60.0,
            20,
            scraped_video_count=100,
            metadata_total=10,
            metadata_fetched=5,
        )
        self.assertIsNotNone(baseline)
        self.assertIsNotNone(settled)
        assert baseline is not None
        assert settled is not None
        self.assertGreater(baseline, settled)

    def test_estimate_eta_inflates_before_video_count_known(self) -> None:
        unknown = estimate_eta_seconds(60.0, 20)
        known = estimate_eta_seconds(
            60.0,
            20,
            scraped_video_count=50,
            metadata_total=0,
            metadata_fetched=0,
        )
        self.assertIsNotNone(unknown)
        self.assertIsNotNone(known)
        assert unknown is not None
        assert known is not None
        self.assertGreater(unknown, known)

    def test_fetch_sleep_increases_projected_total(self) -> None:
        fast = project_pipeline_seconds(
            EtaSettings(fetch_sleep_seconds=0.0, fetch_max_workers=4),
            scraped_video_count=100,
            metadata_total=0,
            metadata_fetched=0,
            known_kept_count=80,
        )
        slow = project_pipeline_seconds(
            EtaSettings(fetch_sleep_seconds=2.0, fetch_max_workers=1),
            scraped_video_count=100,
            metadata_total=0,
            metadata_fetched=0,
            known_kept_count=80,
        )
        self.assertGreater(slow, fast)

    def test_max_videos_caps_projection(self) -> None:
        uncapped = project_pipeline_seconds(
            EtaSettings(max_videos=0),
            scraped_video_count=500,
            metadata_total=0,
            metadata_fetched=0,
        )
        capped = project_pipeline_seconds(
            EtaSettings(max_videos=50),
            scraped_video_count=500,
            metadata_total=0,
            metadata_fetched=0,
        )
        self.assertGreater(uncapped, capped)

    def test_fetch_progress_refines_eta(self) -> None:
        settings = EtaSettings(fetch_max_workers=2, fetch_sleep_seconds=0.5)
        early = estimate_eta_seconds(
            120.0,
            55,
            settings=settings,
            message="Fetching transcripts for 100 videos…",
            scraped_video_count=100,
            metadata_total=0,
            metadata_fetched=0,
            known_kept_count=80,
        )
        mid_fetch = estimate_eta_seconds(
            120.0,
            55,
            settings=settings,
            message="Fetched transcripts 40/80",
            scraped_video_count=100,
            metadata_total=0,
            metadata_fetched=0,
            known_kept_count=80,
        )
        self.assertIsNotNone(early)
        self.assertIsNotNone(mid_fetch)
        assert early is not None
        assert mid_fetch is not None
        self.assertLess(mid_fetch, early)
