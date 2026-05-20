"""Weighted progress tracking for the channel export pipeline."""

from __future__ import annotations

from typing import Any, Optional

from .models import ProgressCallback


def _emit_meta(callback: Optional[ProgressCallback], **fields: Any) -> None:
    if callback is None:
        return
    on_meta = getattr(callback, "on_meta", None)
    if callable(on_meta):
        on_meta(**fields)


def _emit_log(callback: Optional[ProgressCallback], message: str) -> None:
    if callback is None:
        return
    on_log = getattr(callback, "on_log", None)
    if callable(on_log):
        on_log(message)


# Stage ranges on a 0–100 scale (start inclusive, end exclusive).
STAGE_RANGES: dict[str, tuple[int, int]] = {
    "scraping": (0, 40),
    "filtering": (40, 45),
    "fetching": (45, 95),
    "exporting": (95, 100),
}

SCRAPE_SUBSTAGES: dict[str, tuple[int, int]] = {
    "resolve": (0, 8),
    "enumerate": (8, 18),
    "enrich": (18, 40),
}


class PipelineProgressTracker:
    """Maps substage progress into a single 0–100 overall percent."""

    def __init__(self, callback: Optional[ProgressCallback] = None) -> None:
        self._callback = callback
        self._stage = ""
        self._stage_start = 0
        self._stage_end = 100

    def on_stage(self, stage: str, message: str = "") -> None:
        self._stage = stage
        self._stage_start, self._stage_end = STAGE_RANGES.get(stage, (0, 100))
        if self._callback is not None:
            self._callback.on_stage(stage, message)
        if message:
            self.report(self._stage_start, message)

    def report(self, percent: int, message: str) -> None:
        percent = max(0, min(100, int(percent)))
        if self._callback is not None:
            self._callback.on_progress(percent, 100, message)

    def report_substage(
        self,
        substage: str,
        current: int,
        total: int,
        message: str,
    ) -> None:
        sub_start, sub_end = SCRAPE_SUBSTAGES.get(substage, (0, 40))
        if total <= 0:
            fraction = 0.0
        else:
            fraction = min(1.0, current / total)
        percent = sub_start + int((sub_end - sub_start) * fraction)
        self.report(percent, message)

    def report_within_stage(
        self,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if total <= 0:
            fraction = 0.0
        else:
            fraction = min(1.0, current / total)
        span = self._stage_end - self._stage_start
        percent = self._stage_start + int(span * fraction)
        self.report(percent, message)

    def report_meta(self, **fields: Any) -> None:
        _emit_meta(self._callback, **fields)

    def report_log(self, message: str) -> None:
        _emit_log(self._callback, message)
