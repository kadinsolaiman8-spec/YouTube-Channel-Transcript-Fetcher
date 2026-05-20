"""Shared helpers for progress ETA formatting and scrape backend labels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

SCRAPE_BACKEND_LABELS = {
    "ytdlp": "yt-dlp",
    "youtube_data_api": "YouTube Data API",
    "unknown": "Unknown",
}

# Rough per-stage timing constants (seconds) used before observed rates dominate.
_BASE_RESOLVE_SECONDS = 8.0
_BASE_ENUMERATE_SECONDS_YTDLP = 12.0
_BASE_ENUMERATE_PER_VIDEO_YTDLP = 0.025
_BASE_ENUMERATE_SECONDS_API = 6.0
_BASE_ENUMERATE_PER_VIDEO_API = 0.012
_BASE_ENRICH_PER_VIDEO_YTDLP = 1.75
_BASE_ENRICH_PER_VIDEO_API = 0.04
_BASE_FILTER_SECONDS = 2.5
_BASE_FETCH_SECONDS_PER_VIDEO = 2.2
_BASE_EXPORT_SECONDS = 4.0
_BASE_EXPORT_PER_VIDEO = 0.018
_UNKNOWN_CHANNEL_VIDEO_GUESS = 40

# Inflate ETA before enough metadata is known — early progress % is misleading.
_PRE_VIDEO_COUNT_ETA_FACTOR = 2.5
_PRE_METADATA_HALF_ETA_FACTOR = 1.85


@dataclass(frozen=True)
class EtaSettings:
    """User-configurable knobs that shift expected pipeline duration."""

    max_videos: int = 0
    percentile_cutoff: float = 10.0
    min_age_days: int = 14
    min_views_floor: int = 0
    fetch_max_workers: int = 2
    fetch_sleep_seconds: float = 0.5
    enrich_max_workers: int = 2
    scrape_backend: str = ""
    language_count: int = 1
    export_density: str = "compact"
    uses_cookies: bool = False


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def metadata_past_halfway(
    metadata_total: Optional[int],
    metadata_fetched: Optional[int],
) -> bool:
    """True once metadata enrichment is done or past half of pending fetches."""
    if metadata_total is None:
        return False
    if metadata_total <= 0:
        return True
    fetched = metadata_fetched or 0
    return fetched >= metadata_total / 2


def parse_fetch_progress(message: str) -> tuple[Optional[int], Optional[int]]:
    match = re.search(r"Fetched transcripts (\d+)/(\d+)", message or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def effective_scraped_count(
    scraped_video_count: Optional[int],
    max_videos: int,
) -> int:
    if scraped_video_count is None or scraped_video_count <= 0:
        return _UNKNOWN_CHANNEL_VIDEO_GUESS
    if max_videos > 0:
        return min(scraped_video_count, max_videos)
    return scraped_video_count


def estimate_kept_fraction(settings: EtaSettings) -> float:
    kept_from_percentile = max(0.05, 1.0 - settings.percentile_cutoff / 100.0)
    age_factor = 1.0
    if settings.min_age_days > 0:
        age_factor = max(0.55, 1.0 - min(settings.min_age_days, 120) / 400.0)
    views_factor = 0.9 if settings.min_views_floor > 0 else 1.0
    return kept_from_percentile * age_factor * views_factor


def estimate_kept_count(
    scraped_video_count: Optional[int],
    settings: EtaSettings,
    *,
    known_kept_count: Optional[int] = None,
) -> int:
    if known_kept_count is not None and known_kept_count > 0:
        return known_kept_count
    videos = effective_scraped_count(scraped_video_count, settings.max_videos)
    return max(1, int(videos * estimate_kept_fraction(settings)))


def _scrape_seconds(
    settings: EtaSettings,
    scraped_video_count: Optional[int],
    metadata_total: Optional[int],
    metadata_fetched: Optional[int],
) -> float:
    videos = effective_scraped_count(scraped_video_count, settings.max_videos)
    backend = settings.scrape_backend or "ytdlp"

    if backend == "youtube_data_api":
        resolve = _BASE_RESOLVE_SECONDS * 0.7
        enumerate = _BASE_ENUMERATE_SECONDS_API + videos * _BASE_ENUMERATE_PER_VIDEO_API
        pending = metadata_total if metadata_total is not None else 0
        if metadata_total is None and scraped_video_count is None:
            pending = int(videos * 0.15)
        fetched = metadata_fetched or 0
        remaining = max(0, pending - fetched)
        enrich = (
            remaining * _BASE_ENRICH_PER_VIDEO_API / max(1, settings.enrich_max_workers)
        )
    else:
        resolve = _BASE_RESOLVE_SECONDS
        if settings.uses_cookies:
            resolve *= 1.15
        enumerate = (
            _BASE_ENUMERATE_SECONDS_YTDLP + videos * _BASE_ENUMERATE_PER_VIDEO_YTDLP
        )
        pending = metadata_total if metadata_total is not None else 0
        if metadata_total is None and scraped_video_count is None:
            pending = int(videos * 0.35)
        fetched = metadata_fetched or 0
        remaining = max(0, pending - fetched)
        enrich = (
            remaining
            * _BASE_ENRICH_PER_VIDEO_YTDLP
            / max(1, settings.enrich_max_workers)
        )

    return resolve + enumerate + enrich


def _fetch_seconds(settings: EtaSettings, kept_count: int) -> float:
    if kept_count <= 0:
        return 0.0
    per_video = _BASE_FETCH_SECONDS_PER_VIDEO + settings.fetch_sleep_seconds
    per_video += max(0, settings.language_count - 1) * 0.15
    return kept_count * per_video / max(1, settings.fetch_max_workers)


def _export_seconds(settings: EtaSettings, kept_count: int) -> float:
    density_factor = 1.45 if settings.export_density == "verbose" else 1.0
    return _BASE_EXPORT_SECONDS + kept_count * _BASE_EXPORT_PER_VIDEO * density_factor


def _fetch_remaining_seconds(
    settings: EtaSettings,
    fetch_completed: int,
    fetch_total: int,
) -> float:
    remaining_videos = max(0, fetch_total - fetch_completed)
    if remaining_videos <= 0:
        return _export_seconds(settings, fetch_total)
    per_video = _BASE_FETCH_SECONDS_PER_VIDEO + settings.fetch_sleep_seconds
    per_video += max(0, settings.language_count - 1) * 0.15
    fetch_remaining = remaining_videos * per_video / max(1, settings.fetch_max_workers)
    return fetch_remaining + _export_seconds(settings, fetch_total)


def project_pipeline_seconds(
    settings: EtaSettings,
    *,
    scraped_video_count: Optional[int] = None,
    metadata_total: Optional[int] = None,
    metadata_fetched: Optional[int] = None,
    known_kept_count: Optional[int] = None,
    fetch_completed: Optional[int] = None,
    fetch_total: Optional[int] = None,
) -> float:
    """Estimate total pipeline duration from settings and known progress."""
    kept_count = estimate_kept_count(
        scraped_video_count,
        settings,
        known_kept_count=known_kept_count,
    )
    scrape = _scrape_seconds(
        settings,
        scraped_video_count,
        metadata_total,
        metadata_fetched,
    )
    filter_seconds = (
        _BASE_FILTER_SECONDS
        + effective_scraped_count(scraped_video_count, settings.max_videos) * 0.002
    )
    fetch = _fetch_seconds(settings, kept_count)
    if fetch_total and fetch_completed is not None and fetch_total > 0:
        kept_count = fetch_total
        fetch = _fetch_seconds(settings, fetch_total)
    export = _export_seconds(settings, kept_count)
    return scrape + filter_seconds + fetch + export


def _early_inflation_factor(
    scraped_video_count: Optional[int],
    metadata_total: Optional[int],
    metadata_fetched: Optional[int],
) -> float:
    factor = 1.0
    if not metadata_past_halfway(metadata_total, metadata_fetched):
        factor = max(factor, _PRE_METADATA_HALF_ETA_FACTOR)
    if scraped_video_count is None:
        factor = max(factor, _PRE_VIDEO_COUNT_ETA_FACTOR)
    return factor


def estimate_eta_seconds(
    elapsed_seconds: float,
    percent: int,
    *,
    settings: Optional[EtaSettings] = None,
    message: str = "",
    scraped_video_count: Optional[int] = None,
    metadata_total: Optional[int] = None,
    metadata_fetched: Optional[int] = None,
    known_kept_count: Optional[int] = None,
) -> Optional[int]:
    if percent <= 0 or percent >= 100:
        return None
    if elapsed_seconds < 3:
        return None

    remaining_fraction = (100 - percent) / percent
    linear_eta = int(elapsed_seconds * remaining_fraction)
    linear_eta = int(
        linear_eta
        * _early_inflation_factor(scraped_video_count, metadata_total, metadata_fetched)
    )

    if settings is None:
        return linear_eta

    fetch_completed, fetch_total = parse_fetch_progress(message)
    if (
        fetch_completed is not None
        and fetch_total
        and fetch_total > 0
        and fetch_completed < fetch_total
    ):
        model_remaining = _fetch_remaining_seconds(
            settings, fetch_completed, fetch_total
        )
    else:
        projected_total = project_pipeline_seconds(
            settings,
            scraped_video_count=scraped_video_count,
            metadata_total=metadata_total,
            metadata_fetched=metadata_fetched,
            known_kept_count=known_kept_count,
            fetch_completed=fetch_completed,
            fetch_total=fetch_total,
        )
        model_remaining = max(0.0, projected_total - elapsed_seconds)

        if percent >= 8:
            observed_total = elapsed_seconds / (percent / 100.0)
            observed_weight = min(0.8, (percent - 5) / 100.0)
            blended_total = (
                observed_weight * observed_total
                + (1.0 - observed_weight) * projected_total
            )
            model_remaining = max(0.0, blended_total - elapsed_seconds)

    if percent < 12:
        eta = max(float(linear_eta), model_remaining)
    elif fetch_completed is not None and fetch_total and fetch_completed < fetch_total:
        eta = max(float(linear_eta) * 0.2, model_remaining)
    elif percent < 45:
        eta = 0.35 * linear_eta + 0.65 * model_remaining
    else:
        eta = 0.55 * linear_eta + 0.45 * model_remaining

    return max(1, int(eta))


def scrape_backend_label(backend: str) -> str:
    return SCRAPE_BACKEND_LABELS.get(backend, backend)
