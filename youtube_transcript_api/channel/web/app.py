"""FastAPI application and job API for the channel transcript export web UI."""

from __future__ import annotations

import multiprocessing
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from pydantic import BaseModel, Field

from youtube_transcript_api.channel.env import load_local_env
from youtube_transcript_api.channel.proxy import proxy_config_label
from youtube_transcript_api.channel.models import (
    ChannelNotFound,
    ExportConfig,
    FilterConfig,
    ScrapeConfig,
)
from youtube_transcript_api.channel.pipeline import estimate_export_bytes
from youtube_transcript_api.channel.progress_reporting import (
    EtaSettings,
    estimate_eta_seconds,
    format_duration,
    scrape_backend_label,
)
from youtube_transcript_api.channel.urls import validate_channel_url
from youtube_transcript_api.channel.worker import run_export_job, serialize_job_config

STATIC_DIR = Path(__file__).resolve().parent / "static"
PREVIEW_MAX_BYTES = 8192
JOB_TTL_SECONDS = 3600
MAX_CONCURRENT_EXPORTS = 1
TERMINAL_LOG_INTERVAL_SECONDS = 3.0
MAX_STATUS_LOG_LINES = 40


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobState:
    job_id: str
    channel_url: str
    filter_config: FilterConfig
    export_config: ExportConfig
    scrape_config: ScrapeConfig
    fetch_max_workers: int = 2
    fetch_sleep_seconds: float = 0.5
    status: JobStatus = JobStatus.PENDING
    stage: str = "queued"
    progress: int = 0
    total: int = 0
    percent: int = 0
    message: str = "Waiting to start"
    error: Optional[str] = None
    error_report: Optional[str] = None
    output_path: Optional[Path] = None
    channel_label: str = "channel"
    kept_count: Optional[int] = None
    removed_count: Optional[int] = None
    failed_count: Optional[int] = None
    scraped_video_count: Optional[int] = None
    metadata_total: Optional[int] = None
    metadata_fetched: Optional[int] = None
    estimated_export_bytes: Optional[int] = None
    scrape_backend: str = ""
    scrape_backend_reason: str = ""
    scrape_fallback_error: Optional[str] = None
    status_log: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    elapsed_seconds: int = 0
    eta_seconds: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    worker_process: Any = field(default=None, repr=False)


class _JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()
        self._active_job_id: Optional[str] = None

    def create(
        self,
        channel_url: str,
        filter_config: FilterConfig,
        export_config: ExportConfig,
        scrape_config: ScrapeConfig,
        fetch_max_workers: int = 2,
        fetch_sleep_seconds: float = 0.5,
    ) -> JobState:
        job_id = str(uuid.uuid4())
        job = JobState(
            job_id=job_id,
            channel_url=channel_url,
            filter_config=filter_config,
            export_config=export_config,
            scrape_config=scrape_config,
            fetch_max_workers=max(1, min(fetch_max_workers, 8)),
            fetch_sleep_seconds=max(0.0, fetch_sleep_seconds),
        )
        with self._lock:
            self._evict_expired_locked()
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[JobState]:
        with self._lock:
            self._evict_expired_locked()
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if self._active_job_id == job_id:
                self._active_job_id = None
        if job is None:
            return False
        _terminate_worker_process(job.worker_process)
        _remove_output_file(job.output_path)
        return True

    def cancel_active(self) -> Optional[str]:
        with self._lock:
            job_id = self._active_job_id
            if job_id is None:
                return None
            job = self._jobs.get(job_id)
            if job is None:
                self._active_job_id = None
                return None
            job.status = JobStatus.CANCELLED
            job.stage = "error"
            job.message = "Cancelled"
            job.error = "Cancelled to start a new export"
            process = job.worker_process
            output_path = job.output_path
            self._active_job_id = None
        _terminate_worker_process(process)
        _remove_output_file(output_path)
        return job_id

    def has_active_export(self) -> bool:
        with self._lock:
            return self._active_job_id is not None

    def set_active(self, job_id: Optional[str]) -> None:
        with self._lock:
            self._active_job_id = job_id

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if now - job.created_at > JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = self._jobs.pop(job_id, None)
            if job is not None:
                _remove_output_file(job.output_path)
            if self._active_job_id == job_id:
                self._active_job_id = None


_jobs = _JobStore()
_mp_context = multiprocessing.get_context("spawn")


def _remove_output_file(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _terminate_worker_process(process: Any) -> None:
    if process is None:
        return
    try:
        if not process.is_alive():
            return
        process.terminate()
        process.join(timeout=5.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=2.0)
    except (AttributeError, OSError, ValueError):
        pass


class FilterConfigPayload(BaseModel):
    min_age_days: int = 14
    percentile_cutoff: float = 10.0
    min_views_floor: int = 0
    max_videos: int = 0


class ExportConfigPayload(BaseModel):
    sort_order: str = "asc"
    languages: List[str] = Field(default_factory=lambda: ["en"])
    include_metadata_header: bool = True
    export_density: str = "compact"


class ScrapeConfigPayload(BaseModel):
    cookies_from_browser: Optional[str] = None
    cookiefile: Optional[str] = None
    enrich_max_workers: int = 2


class FetchConfigPayload(BaseModel):
    max_workers: int = 2
    sleep_seconds: float = 0.5


class CreateJobRequest(BaseModel):
    channel_url: str
    filter_config: FilterConfigPayload = Field(default_factory=FilterConfigPayload)
    export_config: ExportConfigPayload = Field(default_factory=ExportConfigPayload)
    scrape_config: ScrapeConfigPayload = Field(default_factory=ScrapeConfigPayload)
    fetch_config: FetchConfigPayload = Field(default_factory=FetchConfigPayload)


class CreateJobResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    stage: str
    progress: int
    total: int
    percent: int = 0
    message: str
    error: Optional[str] = None
    error_report: Optional[str] = None
    kept_count: Optional[int] = None
    removed_count: Optional[int] = None
    failed_count: Optional[int] = None
    scraped_video_count: Optional[int] = None
    estimated_export_bytes: Optional[int] = None
    scrape_backend: Optional[str] = None
    scrape_backend_label: Optional[str] = None
    scrape_backend_reason: Optional[str] = None
    scrape_fallback_error: Optional[str] = None
    status_log: List[str] = Field(default_factory=list)
    elapsed_seconds: Optional[int] = None
    eta_seconds: Optional[int] = None
    eta_label: Optional[str] = None


STAGE_LABELS = {
    "starting": "Starting",
    "scraping": "Scraping channel",
    "filtering": "Filtering outliers",
    "fetching": "Fetching transcripts",
    "exporting": "Building export",
    "done": "Complete",
    "error": "Failed",
    "queued": "Queued",
}


def _payload_to_filter_config(payload: FilterConfigPayload) -> FilterConfig:
    return FilterConfig(
        min_age_days=payload.min_age_days,
        percentile_cutoff=payload.percentile_cutoff,
        min_views_floor=payload.min_views_floor,
        max_videos=max(0, payload.max_videos),
    )


def _payload_to_scrape_config(payload: ScrapeConfigPayload) -> ScrapeConfig:
    return ScrapeConfig(
        enrich_max_workers=max(1, min(payload.enrich_max_workers, 8)),
        cookies_from_browser=payload.cookies_from_browser,
        cookiefile=payload.cookiefile,
    )


def _payload_to_export_config(payload: ExportConfigPayload) -> ExportConfig:
    sort_order = payload.sort_order if payload.sort_order in ("asc", "desc") else "asc"
    languages = tuple(payload.languages) if payload.languages else ("en",)
    density = (
        payload.export_density
        if payload.export_density in ("compact", "verbose")
        else "compact"
    )
    return ExportConfig(
        sort_order=sort_order,  # type: ignore[arg-type]
        languages=languages,
        include_metadata_header=payload.include_metadata_header,
        export_density=density,  # type: ignore[arg-type]
    )


SCRAPE_BACKEND_REASON_LABELS = {
    "no_cookies_api_key_set": "No cookies — using API key",
    "ytdlp_fallback": "yt-dlp failed — fell back to API",
    "primary": "yt-dlp",
}


def _build_eta_settings(job: JobState) -> EtaSettings:
    uses_cookies = bool(
        job.scrape_config.cookies_from_browser or job.scrape_config.cookiefile
    )
    return EtaSettings(
        max_videos=job.filter_config.max_videos,
        percentile_cutoff=job.filter_config.percentile_cutoff,
        min_age_days=job.filter_config.min_age_days,
        min_views_floor=job.filter_config.min_views_floor,
        fetch_max_workers=job.fetch_max_workers,
        fetch_sleep_seconds=job.fetch_sleep_seconds,
        enrich_max_workers=job.scrape_config.enrich_max_workers,
        scrape_backend=job.scrape_backend,
        language_count=max(1, len(job.export_config.languages)),
        export_density=job.export_config.export_density,
        uses_cookies=uses_cookies,
    )


def _estimate_job_eta(job: JobState, elapsed: int) -> Optional[int]:
    return estimate_eta_seconds(
        elapsed,
        job.percent,
        settings=_build_eta_settings(job),
        message=job.message,
        scraped_video_count=job.scraped_video_count,
        metadata_total=job.metadata_total,
        metadata_fetched=job.metadata_fetched,
        known_kept_count=job.kept_count,
    )


def _append_job_log(job_id: str, line: str) -> None:
    text = line.strip()
    if not text:
        return
    job = _jobs.get(job_id)
    if job is None:
        return
    log = list(job.status_log)
    log.append(text)
    if len(log) > MAX_STATUS_LOG_LINES:
        log = log[-MAX_STATUS_LOG_LINES:]
    _jobs.update(job_id, status_log=log)


def _refresh_job_timing(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    elapsed = max(0, int(time.time() - job.started_at))
    eta = _estimate_job_eta(job, elapsed)
    _jobs.update(job_id, elapsed_seconds=elapsed, eta_seconds=eta)


def _format_terminal_status(job: JobState) -> str:
    elapsed = max(0, int(time.time() - job.started_at))
    eta = _estimate_job_eta(job, elapsed)
    eta_text = format_duration(eta) if eta is not None else "—"
    backend = scrape_backend_label(job.scrape_backend) if job.scrape_backend else "—"
    return (
        f"[channel-export] {job.percent:3d}% | {STAGE_LABELS.get(job.stage, job.stage)} | "
        f"elapsed {format_duration(elapsed)} | ETA ~{eta_text} | backend {backend} | "
        f"{job.message}"
    )


def _apply_progress_message(job_id: str, message: Dict[str, Any]) -> None:
    kind = message.get("kind")
    if kind == "log":
        _append_job_log(job_id, str(message.get("message", "")))
        return

    if kind == "meta":
        fields = {
            key: value
            for key, value in message.items()
            if key != "kind" and hasattr(JobState, key)
        }
        if fields:
            _jobs.update(job_id, **fields)
        return

    if kind == "stage":
        stage = str(message.get("stage", "running"))
        label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
        text = str(message.get("message") or label)
        _jobs.update(job_id, stage=stage, message=text)
        _refresh_job_timing(job_id)
        if stage == "filtering" and "Kept" in text:
            kept = _parse_kept_count_from_message(text)
            _jobs.update(
                job_id,
                kept_count=kept,
                estimated_export_bytes=estimate_export_bytes(kept),
            )
        return

    if kind != "progress":
        return

    current = int(message.get("current", 0))
    total = int(message.get("total", 0))
    text = str(message.get("message") or "")
    if total == 100:
        percent = max(0, min(100, current))
    elif total > 0:
        percent = max(0, min(100, round((current / total) * 100)))
    else:
        percent = 0
    job = _jobs.get(job_id)
    stage_message = text or (job.message if job is not None else "")
    _jobs.update(
        job_id,
        progress=current,
        total=total,
        percent=percent,
        message=stage_message or f"{percent}% complete",
    )
    _refresh_job_timing(job_id)


def _parse_kept_count_from_message(message: str) -> int:
    match = re.search(r"Kept\s+(\d+)\s+videos", message)
    if match:
        return int(match.group(1))
    return 0


def _supervise_export_process(
    job_id: str,
    process: multiprocessing.Process,
    progress_queue: Any,
    result_queue: Any,
) -> None:
    stop_logging = threading.Event()

    def _drain_progress() -> None:
        while True:
            message = progress_queue.get()
            if message is None:
                break
            _apply_progress_message(job_id, message)

    def _terminal_logger() -> None:
        while not stop_logging.wait(TERMINAL_LOG_INTERVAL_SECONDS):
            job = _jobs.get(job_id)
            if job is None:
                continue
            if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                break
            _refresh_job_timing(job_id)
            job = _jobs.get(job_id)
            if job is not None:
                print(_format_terminal_status(job), flush=True)

    drain_thread = threading.Thread(target=_drain_progress, daemon=True)
    log_thread = threading.Thread(target=_terminal_logger, daemon=True)
    drain_thread.start()
    log_thread.start()
    process.join()
    stop_logging.set()
    drain_thread.join(timeout=5.0)
    log_thread.join(timeout=TERMINAL_LOG_INTERVAL_SECONDS + 1)

    job = _jobs.get(job_id)
    if job is None or job.status == JobStatus.CANCELLED:
        return

    try:
        result = result_queue.get(timeout=1.0)
    except Exception:
        job = _jobs.get(job_id)
        if job is not None and job.status == JobStatus.CANCELLED:
            return
        _jobs.update(
            job_id,
            status=JobStatus.FAILED,
            stage="error",
            error="Worker process ended without a result",
            message="Pipeline failed",
        )
        _jobs.set_active(None)
        return

    job = _jobs.get(job_id)
    if job is None or job.status == JobStatus.CANCELLED:
        return

    if result.get("status") == "completed":
        output_path = Path(result["output_path"])
        _jobs.update(
            job_id,
            status=JobStatus.COMPLETED,
            stage="done",
            progress=100,
            total=100,
            percent=100,
            message="Export complete",
            output_path=output_path,
            channel_label=str(result.get("channel_label") or "channel"),
            kept_count=int(result.get("kept_count", 0)),
            removed_count=int(result.get("removed_count", 0)),
            failed_count=int(result.get("failed_count", 0)),
            scraped_video_count=int(result.get("scraped_video_count", 0)),
            estimated_export_bytes=int(result.get("estimated_export_bytes", 0)),
            scrape_backend=str(result.get("scrape_backend") or ""),
        )
    else:
        _jobs.update(
            job_id,
            status=JobStatus.FAILED,
            stage="error",
            error=str(result.get("error") or "Unknown error"),
            error_report=result.get("error_report"),
            message="Pipeline failed",
        )
        output_path = _jobs.get(job_id)
        if output_path is not None:
            _remove_output_file(output_path.output_path)

    _jobs.set_active(None)


def _start_export_worker(job: JobState) -> None:
    filter_payload, export_payload, scrape_payload = serialize_job_config(
        job.filter_config,
        job.export_config,
        job.scrape_config,
    )
    fd, output_name = tempfile.mkstemp(suffix=".txt", prefix="channel_export_")
    os.close(fd)
    output_path = Path(output_name)

    progress_queue = _mp_context.Queue()
    result_queue = _mp_context.Queue()
    process = _mp_context.Process(
        target=run_export_job,
        args=(
            job.channel_url,
            filter_payload,
            export_payload,
            scrape_payload,
            str(output_path),
            progress_queue,
            result_queue,
            job.fetch_max_workers,
            job.fetch_sleep_seconds,
        ),
        daemon=True,
    )

    _jobs.update(
        job.job_id,
        status=JobStatus.RUNNING,
        stage="starting",
        message="Starting pipeline",
        output_path=output_path,
        started_at=time.time(),
    )
    print(f"[channel-export] Job {job.job_id} started", flush=True)
    _jobs.set_active(job.job_id)
    process.start()
    _jobs.update(job.job_id, worker_process=process)
    threading.Thread(
        target=_supervise_export_process,
        args=(job.job_id, process, progress_queue, result_queue),
        daemon=True,
    ).start()


def _job_to_response(job: JobState) -> JobStatusResponse:
    percent = job.percent
    if percent == 0 and job.total > 0 and job.progress > 0:
        percent = max(0, min(100, round((job.progress / job.total) * 100)))

    elapsed = max(0, int(time.time() - job.started_at))
    eta = job.eta_seconds
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        eta = _estimate_job_eta(job, elapsed)

    eta_label = None
    if eta is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        eta_label = f"~{format_duration(eta)} remaining"

    backend_label = (
        scrape_backend_label(job.scrape_backend) if job.scrape_backend else None
    )
    reason_key = job.scrape_backend_reason
    reason_label = SCRAPE_BACKEND_REASON_LABELS.get(reason_key, reason_key or None)

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        stage=job.stage,
        progress=job.progress,
        total=job.total,
        percent=percent,
        message=job.message,
        error=job.error,
        error_report=job.error_report,
        kept_count=job.kept_count,
        removed_count=job.removed_count,
        failed_count=job.failed_count,
        scraped_video_count=job.scraped_video_count,
        estimated_export_bytes=job.estimated_export_bytes,
        scrape_backend=job.scrape_backend or None,
        scrape_backend_label=backend_label,
        scrape_backend_reason=reason_label,
        scrape_fallback_error=job.scrape_fallback_error,
        status_log=list(job.status_log),
        elapsed_seconds=elapsed,
        eta_seconds=eta,
        eta_label=eta_label,
    )


def _require_completed_job(job_id: str) -> JobState:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Job is not complete yet")
    if job.output_path is None or not job.output_path.is_file():
        raise HTTPException(status_code=404, detail="Export output not available")
    return job


def _download_filename(job: JobState) -> str:
    slug = (
        re.sub(r"[^a-zA-Z0-9]+", "_", job.channel_label.lower()).strip("_") or "channel"
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{slug}_{date_str}.txt"


def _read_preview(path: Path) -> str:
    with path.open("rb") as export_file:
        raw = export_file.read(PREVIEW_MAX_BYTES + 1)
    preview = raw[:PREVIEW_MAX_BYTES].decode("utf-8", errors="ignore")
    if len(raw) > PREVIEW_MAX_BYTES:
        preview += "\n\n[Preview truncated — download for full export]"
    return preview


app = FastAPI(title="Channel Transcript Export", version="0.1.0")


@app.get("/")
def serve_index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="index.html not found")
    return FileResponse(index_path, media_type="text/html")


@app.post("/api/jobs", response_model=CreateJobResponse)
def create_job(body: CreateJobRequest) -> CreateJobResponse:
    channel_url = body.channel_url.strip()
    if not channel_url:
        raise HTTPException(status_code=400, detail="channel_url is required")
    try:
        validate_channel_url(channel_url)
    except ChannelNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _jobs.has_active_export():
        cancelled_id = _jobs.cancel_active()
        if cancelled_id is not None:
            print(
                f"[channel-export] Cancelled job {cancelled_id} for new export",
                flush=True,
            )

    filter_config = _payload_to_filter_config(body.filter_config)
    export_config = _payload_to_export_config(body.export_config)
    scrape_config = _payload_to_scrape_config(body.scrape_config)
    fetch_max_workers = max(1, min(body.fetch_config.max_workers, 8))
    fetch_sleep_seconds = max(0.0, body.fetch_config.sleep_seconds)
    job = _jobs.create(
        channel_url,
        filter_config,
        export_config,
        scrape_config,
        fetch_max_workers=fetch_max_workers,
        fetch_sleep_seconds=fetch_sleep_seconds,
    )
    proxy_label = proxy_config_label()
    if proxy_label:
        _append_job_log(job.job_id, proxy_label)
    _start_export_worker(job)

    return CreateJobResponse(job_id=job.job_id)


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    if not _jobs.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "deleted"}


@app.get("/api/jobs/{job_id}/preview")
def preview_job(job_id: str) -> PlainTextResponse:
    job = _require_completed_job(job_id)
    return PlainTextResponse(_read_preview(job.output_path))


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = _require_completed_job(job_id)
    return FileResponse(
        path=job.output_path,
        media_type="text/plain; charset=utf-8",
        filename=_download_filename(job),
    )


def create_app() -> FastAPI:
    return app


def main() -> None:
    import uvicorn

    load_local_env()
    uvicorn.run(
        "youtube_transcript_api.channel.web.app:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )


if __name__ == "__main__":
    main()
