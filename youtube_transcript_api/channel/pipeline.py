"""Orchestrates scrape → filter → fetch → export for channel transcript export."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..proxies import ProxyConfig
from .export import AgentChannelFormatter
from .fetcher import fetch_transcripts
from .filter import filter_videos
from .errors import build_no_transcripts_details
from .models import (
    ExportConfig,
    FilterConfig,
    NoTranscriptsRetrieved,
    PipelineOutput,
    PipelineResult,
    ProgressCallback,
    ScrapeConfig,
)
from .progress import PipelineProgressTracker
from .scraper import scrape_channel

# Rough bytes per video for pre-fetch UI hints (metadata + average transcript).
ESTIMATED_BYTES_PER_VIDEO = 3_500


def _apply_max_videos(
    kept: list,
    filter_config: FilterConfig,
) -> list:
    if filter_config.max_videos <= 0:
        return kept
    return kept[: filter_config.max_videos]


def estimate_export_bytes(video_count: int) -> int:
    return max(0, video_count) * ESTIMATED_BYTES_PER_VIDEO


def run_pipeline(
    channel_url: str,
    filter_config: FilterConfig,
    export_config: ExportConfig,
    max_workers: int = 2,
    sleep_seconds: float = 0.5,
    proxy_config: Optional[ProxyConfig] = None,
    progress_callback: Optional[ProgressCallback] = None,
    scrape_config: Optional[ScrapeConfig] = None,
    output_path: Optional[Path] = None,
) -> PipelineOutput:
    progress = PipelineProgressTracker(progress_callback)
    scrape_settings = scrape_config or ScrapeConfig()

    progress.on_stage("scraping", "Scraping channel video list…")
    records, channel_label, scrape_backend = scrape_channel(
        channel_url,
        max_workers=scrape_settings.enrich_max_workers,
        sleep_seconds=sleep_seconds,
        progress_callback=progress_callback,
        scrape_config=scrape_settings,
    )
    scraped_video_count = len(records)
    progress.report_meta(scrape_backend=scrape_backend)

    progress.on_stage("filtering", "Filtering low-view outliers…")
    kept, removed, filter_summary = filter_videos(records, filter_config)
    del records

    kept = _apply_max_videos(kept, filter_config)
    progress.report_within_stage(
        1,
        1,
        f"Kept {len(kept)} videos, removed {len(removed)} outliers",
    )

    fetch_attempted = len(kept)
    progress.on_stage("fetching", f"Fetching transcripts for {fetch_attempted} videos…")

    def _fetch_progress(current: int, total: int, message: str = "") -> None:
        label = message or f"Fetched transcripts {current}/{total}"
        progress.report_within_stage(current, total, label)

    class _FetchProgressAdapter:
        def on_stage(self, stage: str, message: str = "") -> None:
            pass

        def on_progress(self, current: int, total: int, message: str = "") -> None:
            _fetch_progress(current, total, message)

    fetch_progress = _FetchProgressAdapter() if progress_callback is not None else None

    processed, failed = fetch_transcripts(
        kept,
        export_config,
        max_workers=max_workers,
        delay=sleep_seconds,
        proxy_config=proxy_config,
        progress_callback=fetch_progress,
    )
    del kept

    if not processed and failed:
        raise NoTranscriptsRetrieved(
            (
                f"No transcripts retrieved for channel "
                f"({len(failed)}/{fetch_attempted} fetches failed)"
            ),
            details=build_no_transcripts_details(
                failed,
                fetch_attempted=fetch_attempted,
                proxy_configured=proxy_config is not None,
            ),
        )

    result = PipelineResult(
        channel_label=channel_label,
        kept=processed,
        removed=removed,
        failed=failed,
        filter_summary=filter_summary,
        scraped_video_count=scraped_video_count,
        fetch_attempted=fetch_attempted,
        scrape_backend=scrape_backend,
    )

    progress.on_stage("exporting", "Formatting LLM-ready export file…")
    progress.report_within_stage(1, 1, "Writing export document")
    formatter = AgentChannelFormatter()

    if output_path is not None:
        with output_path.open("w", encoding="utf-8") as export_file:
            formatter.format_to_file(result, export_config, export_file)
        progress.report(100, "Export complete")
        return PipelineOutput(result=result, output_path=output_path)

    export_text = formatter.format(result, export_config)
    progress.report(100, "Export complete")
    return PipelineOutput(result=result, export_text=export_text)
