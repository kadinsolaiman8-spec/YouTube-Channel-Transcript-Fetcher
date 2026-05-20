"""Subprocess worker for channel export jobs (keeps web server memory isolated)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from youtube_transcript_api.channel.env import load_local_env
from youtube_transcript_api.channel.errors import format_error_report
from youtube_transcript_api.channel.proxy import proxy_config_from_env
from youtube_transcript_api.channel.models import (
    ExportConfig,
    FilterConfig,
    ScrapeConfig,
)


def _config_from_payloads(
    filter_payload: Dict[str, Any],
    export_payload: Dict[str, Any],
    scrape_payload: Dict[str, Any],
) -> tuple[FilterConfig, ExportConfig, ScrapeConfig]:
    filter_config = FilterConfig(**filter_payload)
    density = export_payload.get("export_density", "compact")
    if density not in ("compact", "verbose"):
        density = "compact"
    export_config = ExportConfig(
        sort_order=export_payload.get("sort_order", "asc"),
        languages=tuple(export_payload.get("languages") or ("en",)),
        include_metadata_header=bool(
            export_payload.get("include_metadata_header", True)
        ),
        export_density=density,
    )
    scrape_config = ScrapeConfig(**scrape_payload)
    return filter_config, export_config, scrape_config


class _QueueProgress:
    def __init__(self, progress_queue: Any) -> None:
        self._queue = progress_queue

    def on_stage(self, stage: str, message: str = "") -> None:
        self._queue.put(
            {
                "kind": "stage",
                "stage": stage,
                "message": message,
            }
        )

    def on_progress(self, current: int, total: int, message: str = "") -> None:
        self._queue.put(
            {
                "kind": "progress",
                "current": current,
                "total": total,
                "message": message,
            }
        )

    def on_meta(self, **fields: Any) -> None:
        self._queue.put({"kind": "meta", **fields})

    def on_log(self, message: str) -> None:
        self._queue.put({"kind": "log", "message": message})


def run_export_job(
    channel_url: str,
    filter_payload: Dict[str, Any],
    export_payload: Dict[str, Any],
    scrape_payload: Dict[str, Any],
    output_path: str,
    progress_queue: Any,
    result_queue: Any,
    max_workers: int = 2,
    sleep_seconds: float = 0.5,
) -> None:
    """Entry point for multiprocessing.Process (import pipeline inside child)."""
    from youtube_transcript_api.channel.pipeline import (
        estimate_export_bytes,
        run_pipeline,
    )

    load_local_env()
    proxy_config = proxy_config_from_env()

    filter_config, export_config, scrape_config = _config_from_payloads(
        filter_payload,
        export_payload,
        scrape_payload,
    )
    progress = _QueueProgress(progress_queue)
    try:
        output = run_pipeline(
            channel_url,
            filter_config,
            export_config,
            max_workers=max_workers,
            sleep_seconds=sleep_seconds,
            proxy_config=proxy_config,
            progress_callback=progress,
            scrape_config=scrape_config,
            output_path=Path(output_path),
        )
        result = output.result
        result_queue.put(
            {
                "status": "completed",
                "output_path": output_path,
                "kept_count": len(result.kept),
                "removed_count": len(result.removed),
                "failed_count": len(result.failed),
                "channel_label": result.channel_label,
                "scraped_video_count": result.scraped_video_count,
                "scrape_backend": result.scrape_backend,
                "estimated_export_bytes": estimate_export_bytes(len(result.kept)),
            }
        )
    except Exception as exc:
        stage = (
            "fetching"
            if exc.__class__.__name__ == "NoTranscriptsRetrieved"
            else "error"
        )
        report = format_error_report(
            exc,
            channel_url=channel_url,
            filter_config=filter_config,
            export_config=export_config,
            scrape_config=scrape_config,
            stage=stage,
            proxy_configured=proxy_config is not None,
            fetch_max_workers=max_workers,
            fetch_sleep_seconds=sleep_seconds,
        )
        result_queue.put(
            {
                "status": "failed",
                "error": str(exc),
                "error_report": report,
            }
        )
    finally:
        progress_queue.put(None)


def serialize_job_config(
    filter_config: FilterConfig,
    export_config: ExportConfig,
    scrape_config: ScrapeConfig,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    export_payload = {
        "sort_order": export_config.sort_order,
        "languages": list(export_config.languages),
        "include_metadata_header": export_config.include_metadata_header,
        "export_density": export_config.export_density,
    }
    return (
        asdict(filter_config),
        export_payload,
        asdict(scrape_config),
    )
