#!/usr/bin/env python3
"""Measure RSS at channel export checkpoints (idle import, HTTP GET /, optional export)."""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Callable, Optional


def _rss_mb() -> float:
    try:
        import psutil
    except ImportError:
        print(
            "Install psutil for RSS reporting: pip install psutil",
            file=sys.stderr,
        )
        return 0.0
    process = psutil.Process()
    return process.memory_info().rss / (1024 * 1024)


def _report(label: str) -> None:
    print(f"{label}: {_rss_mb():.1f} MB RSS")


def _optional_tracemalloc_top() -> None:
    import tracemalloc

    if not tracemalloc.is_tracing():
        tracemalloc.start()
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")[:10]
    print("Top allocations (tracemalloc):")
    for stat in stats:
        print(f"  {stat}")


def _run_get_root() -> None:
    from fastapi.testclient import TestClient

    from youtube_transcript_api.channel.web.app import app

    client = TestClient(app)
    response = client.get("/")
    response.raise_for_status()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-url",
        default=None,
        help="Optional channel URL to run one full export after baseline",
    )
    parser.add_argument(
        "--tracemalloc",
        action="store_true",
        help="Print top tracemalloc allocators after each step",
    )
    args = parser.parse_args(argv)

    steps: list[tuple[str, Callable[[], None]]] = [
        ("baseline (interpreter)", lambda: None),
        (
            "after import channel.web.app",
            lambda: importlib.import_module("youtube_transcript_api.channel.web.app"),
        ),
        ("after GET /", _run_get_root),
    ]

    for label, action in steps:
        action()
        _report(label)
        if args.tracemalloc:
            _optional_tracemalloc_top()
        print()

    if args.export_url:
        from pathlib import Path
        import tempfile

        from youtube_transcript_api.channel.models import ExportConfig, FilterConfig
        from youtube_transcript_api.channel.pipeline import run_pipeline

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            output_path = Path(tmp.name)
        _report("before export")
        run_pipeline(
            args.export_url,
            FilterConfig(),
            ExportConfig(),
            output_path=output_path,
        )
        _report("after export")
        output_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
