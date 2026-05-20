"""Concurrent batch transcript fetching with per-video error isolation."""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import List, Optional, Tuple, Union

from requests.exceptions import RetryError

from .._api import YouTubeTranscriptApi
from .._errors import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)
from ..formatters import TextFormatter
from ..proxies import ProxyConfig
from .models import ExportConfig, ProcessedVideo, ProgressCallback, VideoRecord

RATE_LIMIT_REASON = "YouTube rate limit (429)"
RATE_LIMIT_REASON_PROXY = "YouTube rate limit (429) (proxy configured)"
IP_BLOCK_REASON = "YouTube IP block"
IP_BLOCK_REASON_PROXY = "YouTube IP block (proxy configured)"

# Extra pause after a 429 so the next video (especially with max_workers=1) can rotate IP.
_RATE_LIMIT_BACKOFF_SECONDS = 5.0

_RETRIEVABLE_ERRORS = (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    IpBlocked,
    RequestBlocked,
    RetryError,
)

_thread_local = threading.local()


def _is_rate_limit_exception(exc: Exception) -> bool:
    if isinstance(exc, IpBlocked):
        return True
    message = str(exc).lower()
    return "429" in message or "too many 429" in message


def _failure_reason(
    exc: Exception,
    proxy_config: Optional[ProxyConfig] = None,
) -> str:
    if _is_rate_limit_exception(exc):
        if proxy_config is not None:
            return RATE_LIMIT_REASON_PROXY
        return RATE_LIMIT_REASON
    if isinstance(exc, RequestBlocked):
        if proxy_config is not None:
            return IP_BLOCK_REASON_PROXY
        return IP_BLOCK_REASON
    if isinstance(exc, TranscriptsDisabled):
        return "Subtitles disabled"
    if isinstance(exc, NoTranscriptFound):
        return "No transcript found"
    if isinstance(exc, VideoUnavailable):
        return "Video unavailable"
    first_line = str(exc).splitlines()[0].strip()
    return first_line or type(exc).__name__


def _reset_thread_api() -> None:
    if hasattr(_thread_local, "api"):
        delattr(_thread_local, "api")


def _get_thread_api(proxy_config: Optional[ProxyConfig]) -> YouTubeTranscriptApi:
    api = getattr(_thread_local, "api", None)
    if api is None:
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        _thread_local.api = api
    return api


def _fetch_single(
    record: VideoRecord,
    export_config: ExportConfig,
    proxy_config: Optional[ProxyConfig],
    delay: float,
) -> Union[ProcessedVideo, Tuple[VideoRecord, str]]:
    if delay > 0:
        time.sleep(delay)

    try:
        api = _get_thread_api(proxy_config)
        transcript = api.fetch(
            record.video_id,
            languages=list(export_config.languages),
        )
        text = TextFormatter().format_transcript(transcript)
        return ProcessedVideo(record=record, transcript_text=text)
    except _RETRIEVABLE_ERRORS as exc:
        if _is_rate_limit_exception(exc):
            _reset_thread_api()
            if proxy_config is not None and _RATE_LIMIT_BACKOFF_SECONDS > 0:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
        return (record, _failure_reason(exc, proxy_config))


def fetch_transcripts(
    records: List[VideoRecord],
    export_config: ExportConfig,
    max_workers: int = 2,
    delay: float = 0.5,
    proxy_config: Optional[ProxyConfig] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Tuple[List[ProcessedVideo], List[Tuple[VideoRecord, str]]]:
    if not records:
        return [], []

    total = len(records)
    kept: List[ProcessedVideo] = []
    failed: List[Tuple[VideoRecord, str]] = []
    ordered_results: List[Optional[Union[ProcessedVideo, Tuple[VideoRecord, str]]]] = [
        None
    ] * total
    worker_count = max(1, max_workers)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        record_iter = iter(enumerate(records))
        pending: dict = {}
        completed = 0

        def _submit_next() -> None:
            try:
                index, record = next(record_iter)
            except StopIteration:
                return
            future = executor.submit(
                _fetch_single,
                record,
                export_config,
                proxy_config,
                delay,
            )
            pending[future] = index

        for _ in range(min(worker_count, total)):
            _submit_next()

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                ordered_results[index] = future.result()
                completed += 1
                if progress_callback is not None:
                    progress_callback.on_progress(
                        completed,
                        total,
                        f"Fetched transcripts {completed}/{total}",
                    )
                _submit_next()

    for result in ordered_results:
        if result is None:
            continue
        if isinstance(result, ProcessedVideo):
            kept.append(result)
        else:
            failed.append(result)

    return kept, failed
