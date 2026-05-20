"""Statistical outlier filtering for channel video records (pure functions, no I/O)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from youtube_transcript_api.channel.models import FilterConfig, VideoRecord


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _passes_age_gate(record: VideoRecord, min_age_days: int, now: datetime) -> bool:
    published = _as_utc(record.published_at)
    return (now - published) >= timedelta(days=min_age_days)


def _percentile(values: list[int], percentile: float) -> float:
    """Linear-interpolation percentile (numpy default method)."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * (percentile / 100.0)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = rank - lower_index
    return (
        sorted_values[lower_index] * (1.0 - weight)
        + sorted_values[upper_index] * weight
    )


def _format_views(view_count: float) -> str:
    return f"{int(round(view_count)):,}"


def _build_filter_summary(
    total_in: int,
    age_gate_removed: int,
    percentile_removed: int,
    floor_removed: int,
    min_age_days: int,
    percentile_cutoff: float,
    threshold: float,
    min_views_floor: int,
    eligible_after_age_gate: int,
) -> str:
    parts: list[str] = []

    if age_gate_removed:
        parts.append(f"{min_age_days}-day age gate removed {age_gate_removed} video(s)")

    if eligible_after_age_gate == 0:
        if not parts:
            return f"No videos eligible for filtering ({total_in} input)."
        return ". ".join(parts) + "."

    if percentile_removed:
        parts.append(
            f"Removed {percentile_removed}/{total_in} videos below "
            f"{percentile_cutoff:g}th percentile "
            f"(threshold: {_format_views(threshold)} views)"
        )
    elif percentile_cutoff > 0:
        parts.append(
            f"No videos removed below {percentile_cutoff:g}th percentile "
            f"(threshold: {_format_views(threshold)} views)"
        )

    if min_views_floor > 0 and floor_removed:
        parts.append(
            f"hard floor removed {floor_removed} video(s) below "
            f"{min_views_floor:,} views"
        )

    if not parts:
        return f"Kept all {total_in} video(s); no filtering applied."

    summary = ". ".join(parts)
    if age_gate_removed and percentile_removed:
        summary = f"{summary} (after {min_age_days}-day age gate)"
    return summary


def filter_videos(
    records: list[VideoRecord],
    config: FilterConfig,
) -> tuple[list[VideoRecord], list[VideoRecord], str]:
    """
    Apply age gate, percentile cutoff, and optional view floor.

    Returns (kept, removed, filter_summary).
    """
    total_in = len(records)
    if total_in == 0:
        return [], [], "No videos to filter."

    now = _utc_now()
    age_eligible: list[VideoRecord] = []
    removed: list[VideoRecord] = []

    for record in records:
        if _passes_age_gate(record, config.min_age_days, now):
            age_eligible.append(record)
        else:
            removed.append(record)

    age_gate_removed = total_in - len(age_eligible)
    view_counts = [record.view_count for record in age_eligible]
    threshold = _percentile(view_counts, config.percentile_cutoff)

    kept: list[VideoRecord] = []
    percentile_removed = 0
    floor_removed = 0

    for record in age_eligible:
        below_percentile = record.view_count < threshold
        below_floor = (
            config.min_views_floor > 0 and record.view_count < config.min_views_floor
        )
        if below_percentile or below_floor:
            removed.append(record)
            if below_percentile:
                percentile_removed += 1
            if below_floor:
                floor_removed += 1
        else:
            kept.append(record)

    summary = _build_filter_summary(
        total_in=total_in,
        age_gate_removed=age_gate_removed,
        percentile_removed=percentile_removed,
        floor_removed=floor_removed,
        min_age_days=config.min_age_days,
        percentile_cutoff=config.percentile_cutoff,
        threshold=threshold,
        min_views_floor=config.min_views_floor,
        eligible_after_age_gate=len(age_eligible),
    )
    return kept, removed, summary
