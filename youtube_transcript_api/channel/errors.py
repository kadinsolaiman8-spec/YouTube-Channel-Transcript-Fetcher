"""Error formatting helpers for channel export failures."""

from __future__ import annotations

import os
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from youtube_transcript_api.channel.fetcher import (
    IP_BLOCK_REASON,
    IP_BLOCK_REASON_PROXY,
    RATE_LIMIT_REASON,
    RATE_LIMIT_REASON_PROXY,
)
from youtube_transcript_api.channel.models import (
    ExportConfig,
    FilterConfig,
    ScrapeConfig,
)


def build_no_transcripts_details(
    failed: List[Tuple[Any, str]],
    *,
    fetch_attempted: int,
    proxy_configured: bool,
) -> Dict[str, Any]:
    reason_counts = Counter(reason for _, reason in failed)
    ip_blocks = reason_counts.get(IP_BLOCK_REASON, 0) + reason_counts.get(
        IP_BLOCK_REASON_PROXY, 0
    )
    rate_limits = reason_counts.get(RATE_LIMIT_REASON, 0) + reason_counts.get(
        RATE_LIMIT_REASON_PROXY, 0
    )
    details: Dict[str, Any] = {
        "fetch_attempted": fetch_attempted,
        "failed_count": len(failed),
        "failure_summary": dict(reason_counts),
        "proxy_configured": proxy_configured,
    }
    if ip_blocks:
        details["ip_block_count"] = ip_blocks
    if rate_limits:
        details["rate_limit_count"] = rate_limits
    if reason_counts:
        details["sample_failure"] = failed[0][1]

    majority = max(1, len(failed) // 2)
    if rate_limits >= majority:
        details["hint"] = (
            "YouTube rate-limited transcript requests (HTTP 429). In Advanced, use "
            "fetch workers 1 and delay 2.0 or higher, then retry a small Max videos "
            "batch (e.g. 3) before a full channel export."
        )
        if proxy_configured:
            details["hint"] = (
                "Transcript fetches were rate-limited (429) even with a proxy. "
                "Residential proxies can still 429 under load. Use fetch workers 1, "
                "delay 2.0+, and retry a small batch first (Advanced)."
            )
    elif ip_blocks >= majority:
        details["hint"] = (
            "YouTube blocked transcript requests from your IP. The export file would "
            "only list video titles under # FAILED, not caption text. Set "
            "WEBSHARE_PROXY_USERNAME/PASSWORD or HTTP_PROXY in .env.local, restart the "
            "server, and retry. See README: Working around IP bans."
        )
        if proxy_configured:
            details["hint"] = (
                "Transcript fetches failed even with a proxy configured. Try residential "
                "rotating proxies, lower fetch workers, and a longer delay between "
                "fetches (Advanced). The export would only list titles under # FAILED."
            )
    else:
        details["hint"] = (
            "No caption text was retrieved. Check failure_summary in this report; "
            "common causes are disabled subtitles or unavailable videos."
        )
    return details


def _fetch_config_lines(
    *,
    proxy_configured: bool,
    fetch_max_workers: Optional[int] = None,
    fetch_sleep_seconds: Optional[float] = None,
) -> list[str]:
    lines = [
        "Fetch configuration:",
        f"  proxy_configured: {proxy_configured}",
    ]
    if fetch_max_workers is not None:
        lines.append(f"  fetch_max_workers: {fetch_max_workers}")
    if fetch_sleep_seconds is not None:
        lines.append(f"  fetch_sleep_seconds: {fetch_sleep_seconds}")
    lines.append("")
    return lines


def _scrape_config_lines(scrape_config: Optional[ScrapeConfig]) -> list[str]:
    if scrape_config is None:
        scrape_config = ScrapeConfig()

    cookiefile_display = "(none)"
    if scrape_config.cookiefile:
        cookiefile_display = Path(scrape_config.cookiefile).name

    cookies_browser_display = scrape_config.cookies_from_browser or "(none)"
    api_key_set = bool(os.environ.get("YOUTUBE_API_KEY", "").strip())

    return [
        "Scrape configuration:",
        f"  cookies_from_browser: {cookies_browser_display}",
        f"  cookiefile: {cookiefile_display}",
        f"  enrich_max_workers: {scrape_config.enrich_max_workers}",
        f"  youtube_api_key_set: {api_key_set}",
        "",
    ]


def format_error_report(
    exc: BaseException,
    *,
    channel_url: str,
    filter_config: FilterConfig,
    export_config: ExportConfig,
    stage: str,
    scrape_config: Optional[ScrapeConfig] = None,
    proxy_configured: bool = False,
    fetch_max_workers: Optional[int] = None,
    fetch_sleep_seconds: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a copy-pasteable diagnostic report for chat / debugging."""
    lines = [
        "=== Channel Transcript Export — Error Report ===",
        f"Time (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"Stage: {stage}",
        f"Channel URL: {channel_url}",
        "",
        "Configuration:",
        f"  percentile_cutoff: {filter_config.percentile_cutoff}",
        f"  min_age_days: {filter_config.min_age_days}",
        f"  min_views_floor: {filter_config.min_views_floor}",
        f"  sort_order: {export_config.sort_order}",
        f"  languages: {', '.join(export_config.languages)}",
        f"  include_metadata_header: {export_config.include_metadata_header}",
        f"  export_density: {export_config.export_density}",
        "",
        *_fetch_config_lines(
            proxy_configured=proxy_configured,
            fetch_max_workers=fetch_max_workers,
            fetch_sleep_seconds=fetch_sleep_seconds,
        ),
        *_scrape_config_lines(scrape_config),
        "Error:",
        f"  {type(exc).__name__}: {exc}",
    ]

    details = getattr(exc, "details", None)
    if details:
        lines.append("")
        lines.append("Details:")
        for key, value in details.items():
            lines.append(f"  {key}: {value}")

    if extra:
        lines.append("")
        lines.append("Context:")
        for key, value in extra.items():
            lines.append(f"  {key}: {value}")

    lines.extend(
        [
            "",
            "Traceback:",
            traceback.format_exc(),
            "=== End Error Report ===",
        ]
    )
    return "\n".join(lines)
