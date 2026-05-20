"""Command-line entry point for channel transcript export."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from youtube_transcript_api.channel.env import load_local_env
from youtube_transcript_api.channel.proxy import resolve_proxy_config
from youtube_transcript_api.channel.models import (
    ChannelNotFound,
    ChannelExportException,
    ExportConfig,
    FilterConfig,
    PipelineResult,
    ScrapeConfig,
)
from youtube_transcript_api.channel.pipeline import run_pipeline
from youtube_transcript_api.channel.urls import validate_channel_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export YouTube channel transcripts to LLM-ready plain text.",
    )
    parser.add_argument(
        "channel_url",
        help="YouTube channel URL (@handle, /channel/UC..., /c/...)",
    )
    parser.add_argument(
        "--percentile",
        type=float,
        default=10.0,
        dest="percentile_cutoff",
        help="Drop bottom N percent by view count (default: 10)",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=14,
        help="Exclude videos newer than this many days (default: 14)",
    )
    parser.add_argument(
        "--min-views-floor",
        type=int,
        default=0,
        help="Optional hard minimum view count (default: 0, disabled)",
    )
    parser.add_argument(
        "--sort",
        choices=["asc", "desc"],
        default="asc",
        help="Sort order by publish date (default: asc)",
    )
    parser.add_argument(
        "--languages",
        default="en",
        help="Comma-separated transcript language codes (default: en)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="channel_export.txt",
        help="Output file path (default: channel_export.txt)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=2,
        help="Concurrent workers for scraping enrichment and transcript fetch (default: 2)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        dest="sleep_seconds",
        help="Delay in seconds between requests (default: 0.5)",
    )
    parser.add_argument(
        "--cookies-browser",
        default=None,
        help=(
            "Browser for yt-dlp cookies (brave, chrome, edge, firefox, chromium, "
            "opera, vivaldi). Use browser:profile for a named profile, e.g. "
            "brave:Default. Fully quit the browser before scraping."
        ),
    )
    parser.add_argument(
        "--cookiefile",
        default=None,
        help="Path to Netscape cookie file for yt-dlp",
    )
    parser.add_argument(
        "--scrape-workers",
        type=int,
        default=2,
        help="Concurrent workers for metadata enrichment during scrape (default: 2)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=0,
        help="Cap videos to export after filtering; 0 = no limit (default: 0)",
    )
    parser.add_argument(
        "--export-density",
        choices=["compact", "verbose"],
        default="compact",
        help="Export layout: compact (fewer tokens, default) or verbose",
    )
    parser.add_argument(
        "--webshare-proxy-username",
        default=None,
        type=str,
        help='Webshare "Proxy Username" (overrides env; see README IP bans section)',
    )
    parser.add_argument(
        "--webshare-proxy-password",
        default=None,
        type=str,
        help='Webshare "Proxy Password" (overrides env)',
    )
    parser.add_argument(
        "--http-proxy",
        default="",
        metavar="URL",
        help="HTTP proxy URL (overrides env)",
    )
    parser.add_argument(
        "--https-proxy",
        default="",
        metavar="URL",
        help="HTTPS proxy URL (overrides env)",
    )
    return parser


def _print_summary(result: PipelineResult, file=sys.stderr) -> None:
    lines = [
        f"Channel: {result.channel_label}",
        f"Kept: {len(result.kept)}",
        f"Removed: {len(result.removed)}",
        f"Failed: {len(result.failed)}",
        f"Filter: {result.filter_summary}",
    ]
    for line in lines:
        print(line, file=file)


def main(argv: Optional[List[str]] = None) -> int:
    load_local_env()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        validate_channel_url(args.channel_url)
    except ChannelNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        return _run_export(args)
    except ChannelExportException as exc:
        print(str(exc), file=sys.stderr)
        details = getattr(exc, "details", None)
        if details and details.get("hint"):
            print(details["hint"], file=sys.stderr)
        return 1


def _run_export(args: argparse.Namespace) -> int:

    filter_config = FilterConfig(
        min_age_days=args.min_age_days,
        percentile_cutoff=args.percentile_cutoff,
        min_views_floor=args.min_views_floor,
        max_videos=max(0, args.max_videos),
    )
    languages = tuple(
        language.strip() for language in args.languages.split(",") if language.strip()
    )
    export_config = ExportConfig(
        sort_order=args.sort,
        languages=languages or ("en",),
        export_density=args.export_density,
    )

    scrape_config = ScrapeConfig(
        enrich_max_workers=args.scrape_workers,
        cookies_from_browser=args.cookies_browser,
        cookiefile=args.cookiefile,
    )
    proxy_config = resolve_proxy_config(
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        webshare_proxy_username=args.webshare_proxy_username,
        webshare_proxy_password=args.webshare_proxy_password,
    )
    output_path = Path(args.output)
    pipeline_output = run_pipeline(
        args.channel_url,
        filter_config,
        export_config,
        max_workers=args.max_workers,
        sleep_seconds=args.sleep_seconds,
        proxy_config=proxy_config,
        scrape_config=scrape_config,
        output_path=output_path,
    )

    _print_summary(pipeline_output.result)
    print(f"Wrote export to {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
