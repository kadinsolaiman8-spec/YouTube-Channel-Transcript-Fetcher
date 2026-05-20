from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Protocol, Tuple

SortOrder = Literal["asc", "desc"]
ExportDensity = Literal["compact", "verbose"]


class ChannelExportException(Exception):
    """Base exception for channel transcript export operations."""


class ChannelNotFound(ChannelExportException):
    """Raised when a channel URL cannot be resolved or has no videos."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ChannelScrapeError(ChannelExportException):
    """Raised when yt-dlp fails during channel resolve or playlist enumeration."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


class YtdlpNotInstalled(ChannelExportException):
    """Raised when yt-dlp is not available on the system or as an import."""


class NoTranscriptsRetrieved(ChannelExportException):
    """Raised when every transcript fetch failed (export would be titles/errors only)."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    title: str
    published_at: datetime  # UTC
    view_count: int
    url: str


@dataclass
class FilterConfig:
    min_age_days: int = 14
    percentile_cutoff: float = 10.0  # drop bottom N percent by view count
    min_views_floor: int = 0  # optional hard floor; 0 disables
    max_videos: int = 0  # cap videos to fetch/export; 0 = no limit


@dataclass
class ExportConfig:
    sort_order: SortOrder = "asc"
    languages: Tuple[str, ...] = ("en",)
    include_metadata_header: bool = True
    export_density: ExportDensity = "compact"


@dataclass
class ScrapeConfig:
    """yt-dlp scraping options (enumeration + per-video metadata enrichment)."""

    enrich_max_workers: int = 2
    cookies_from_browser: Optional[str] = None
    cookiefile: Optional[str] = None


@dataclass
class ProcessedVideo:
    record: VideoRecord
    transcript_text: str  # timestamp-free plain text


@dataclass
class PipelineResult:
    channel_label: str
    kept: List[ProcessedVideo] = field(default_factory=list)
    removed: List[VideoRecord] = field(default_factory=list)
    failed: List[Tuple[VideoRecord, str]] = field(default_factory=list)
    filter_summary: str = ""
    scraped_video_count: int = 0
    fetch_attempted: int = 0
    scrape_backend: str = "unknown"


@dataclass
class PipelineOutput:
    """Result of run_pipeline; export body is either in memory or on disk."""

    result: PipelineResult
    export_text: str = ""
    output_path: Optional[Path] = None


class ProgressCallback(Protocol):
    """Optional hook for pipeline stages and per-item progress (CLI / web UI)."""

    def on_stage(self, stage: str, message: str = "") -> None:
        """Called when the pipeline enters a named stage (e.g. scrape, filter)."""
        ...

    def on_progress(self, current: int, total: int, message: str = "") -> None:
        """Called with progress within the current stage; total is often 100 overall."""
        ...
