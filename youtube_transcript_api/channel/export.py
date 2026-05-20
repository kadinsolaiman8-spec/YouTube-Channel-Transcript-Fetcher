"""LLM-structured plain-text export formatting for channel transcript pipelines."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import IO, List, Tuple

from .models import ExportConfig, ExportDensity, PipelineResult, ProcessedVideo

_HEADER_RULE = "=" * 80
_VIDEO_RULE = "-" * 80
_SENTENCE_ENDINGS = (".", "?", "!")
_FAILED_SUMMARY_MIN = 10


class AgentChannelFormatter:
    """Formats a channel pipeline result as a single LLM-oriented plain-text document."""

    def format(self, result: PipelineResult, config: ExportConfig) -> str:
        videos = _sort_videos(list(result.kept), config.sort_order)
        sections: List[str] = []

        if config.include_metadata_header:
            sections.append(_format_document_header(result, config, len(videos)))

        for index, video in enumerate(videos, start=1):
            sections.append(_format_video_block(video, index, len(videos), config))

        if result.failed:
            sections.append(_format_failed_appendix(result.failed, config))

        return "\n\n".join(sections) + "\n"

    def format_to_file(
        self,
        result: PipelineResult,
        config: ExportConfig,
        file_handle: IO[str],
        *,
        release_transcripts: bool = True,
    ) -> None:
        """Write export incrementally; optionally clear transcript_text after each video."""
        videos = _sort_videos(list(result.kept), config.sort_order)
        total = len(videos)
        blocks_written = 0

        if config.include_metadata_header:
            file_handle.write(_format_document_header(result, config, total))
            blocks_written += 1

        for index, video in enumerate(videos, start=1):
            if blocks_written:
                file_handle.write("\n\n")
            file_handle.write(_format_video_block(video, index, total, config))
            blocks_written += 1
            if release_transcripts:
                video.transcript_text = ""

        if result.failed:
            if blocks_written:
                file_handle.write("\n\n")
            file_handle.write(_format_failed_appendix(result.failed, config))

        file_handle.write("\n")


def join_caption_lines(text: str) -> str:
    """Merge caption lines into paragraphs to reduce newline tokens."""
    if not text:
        return text

    lines = [line.strip() for line in text.splitlines()]
    if not lines:
        return text

    paragraphs: List[str] = []
    current = lines[0]
    for line in lines[1:]:
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            continue
        if not current:
            current = line
            continue
        if _line_ends_sentence(current):
            paragraphs.append(current)
            current = line
        else:
            current = f"{current.rstrip()} {line.lstrip()}"
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def _line_ends_sentence(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped:
        return False
    return stripped[-1] in _SENTENCE_ENDINGS


def _prepare_transcript_text(text: str) -> str:
    return join_caption_lines(_dedupe_consecutive_lines(text))


def _short_video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _sort_videos(
    videos: List[ProcessedVideo], sort_order: str
) -> List[ProcessedVideo]:
    reverse = sort_order == "desc"
    return sorted(videos, key=lambda video: video.record.published_at, reverse=reverse)


def _sort_label(sort_order: str) -> str:
    return "newest" if sort_order == "desc" else "oldest"


def _format_document_header(
    result: PipelineResult, config: ExportConfig, kept_count: int
) -> str:
    if config.export_density == "verbose":
        return _format_document_header_verbose(result, config, kept_count)
    return _format_document_header_compact(result, config, kept_count)


def _format_document_header_compact(
    result: PipelineResult, config: ExportConfig, success_count: int
) -> str:
    attempted = result.fetch_attempted or (
        success_count + len(result.failed)
    )
    removed_count = len(result.removed)
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return (
        f"# {result.channel_label} | export:{exported_at} | "
        f"transcripts:{success_count}/{attempted} | "
        f"scraped:{result.scraped_video_count} filtered_out:{removed_count} | "
        f"sort:{_sort_label(config.sort_order)} | filter:{result.filter_summary}"
    )


def _format_document_header_verbose(
    result: PipelineResult, config: ExportConfig, success_count: int
) -> str:
    attempted = result.fetch_attempted or (
        success_count + len(result.failed)
    )
    removed_count = len(result.removed)
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines = [
        _HEADER_RULE,
        "CHANNEL EXPORT",
        _HEADER_RULE,
        f"Channel: {result.channel_label}",
        f"Exported: {exported_at}",
        f"Transcripts: {success_count}/{attempted}",
        (
            f"Scraped: {result.scraped_video_count} "
            f"(filtered out: {removed_count})"
        ),
        f"Filter: {result.filter_summary}",
        f"Sort: {_sort_label(config.sort_order)} first",
        _HEADER_RULE,
    ]
    return "\n".join(lines)


def _format_video_block(
    video: ProcessedVideo, index: int, total: int, config: ExportConfig
) -> str:
    if config.export_density == "verbose":
        return _format_video_block_verbose(video, index, total)
    return _format_video_block_compact(video, index, total)


def _format_video_block_compact(
    video: ProcessedVideo, index: int, total: int
) -> str:
    record = video.record
    published = record.published_at.strftime("%Y-%m-%d")
    transcript = _prepare_transcript_text(video.transcript_text)
    header = (
        f"## {index}/{total} {record.title} | {published} | "
        f"{record.video_id} | views:{record.view_count}"
    )
    return "\n".join([header, _short_video_url(record.video_id), "", transcript])


def _format_video_block_verbose(
    video: ProcessedVideo, index: int, total: int
) -> str:
    record = video.record
    published = record.published_at.strftime("%Y-%m-%d")
    transcript = _prepare_transcript_text(video.transcript_text)

    lines = [
        _VIDEO_RULE,
        f"VIDEO {index} / {total}",
        _VIDEO_RULE,
        f"Title: {record.title}",
        f"Video ID: {record.video_id}",
        f"Published: {published}",
        f"Views: {record.view_count:,}",
        f"URL: {record.url}",
        "",
        "--- TRANSCRIPT ---",
        "",
        transcript,
        "",
        _HEADER_RULE,
        f"END VIDEO {index}",
        _HEADER_RULE,
    ]
    return "\n".join(lines)


def _format_failed_appendix(
    failed: List[Tuple],
    config: ExportConfig,
) -> str:
    if config.export_density == "verbose":
        return _format_failed_appendix_verbose(failed)
    return _format_failed_appendix_compact(failed)


def _format_failed_appendix_compact(failed: List[Tuple]) -> str:
    lines = [f"# FAILED ({len(failed)} videos)"]
    if len(failed) > _FAILED_SUMMARY_MIN:
        reason_counts = Counter(reason for _, reason in failed)
        for reason, count in sorted(
            reason_counts.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            lines.append(f"{reason}: {count}")
        lines.append("")
    for record, reason in failed:
        lines.append(f"{record.video_id} | {record.title} | {reason}")
    return "\n".join(lines)


def _format_failed_appendix_verbose(failed: List[Tuple]) -> str:
    lines = [
        _HEADER_RULE,
        "FAILED VIDEOS",
        _HEADER_RULE,
    ]
    for record, reason in failed:
        lines.extend(
            [
                f"Video ID: {record.video_id}",
                f"Title: {record.title}",
                f"URL: {record.url}",
                f"Reason: {reason}",
                "",
            ]
        )
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _dedupe_consecutive_lines(text: str) -> str:
    if not text:
        return text

    lines = text.splitlines()
    if not lines:
        return text

    deduped = [lines[0]]
    for line in lines[1:]:
        if line != deduped[-1]:
            deduped.append(line)
    return "\n".join(deduped)
