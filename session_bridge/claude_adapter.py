from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, BinaryIO, Iterable

from .models import (
    InvalidBridgeMarker,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    decode_bridge_marker,
)


_PARSER_VERSION = 1
_HEAD_SAMPLE_BYTES = 4096
_NATIVE_ID_PROBE_BYTES = 65_536
_RECOGNIZED_RECORD_TYPES = {
    "assistant",
    "attachment",
    "custom-title",
    "last-prompt",
    "mode",
    "queue-operation",
    "system",
    "user",
}
_MARKER_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)
_NATIVE_ID_RE = re.compile(rb'"sessionId"\s*:\s*("(?:\\.|[^"\\])*")')


@dataclass(frozen=True)
class ClaudeCursor:
    offset: int
    head_length: int
    head_hash: str


@dataclass(frozen=True)
class ClaudeParseResult:
    projection: SessionProjection
    cursor: ClaudeCursor
    rebuild: bool
    malformed_lines: int
    unknown_records: int


@dataclass(frozen=True)
class _TranscriptLine:
    offset: int
    raw: bytes
    record: dict[str, Any] | None


@dataclass(frozen=True)
class _ReadSlice:
    data: bytes
    base_offset: int
    completed_length: int
    cursor: ClaudeCursor
    rebuild: bool


@dataclass(frozen=True)
class _MetadataDelta:
    native_id: str | None
    title: str | None
    cwd: str | None
    git_branch: str | None
    timestamps: tuple[float, ...]


@dataclass(frozen=True)
class _Metadata:
    native_id: str
    title: str | None
    cwd: str | None
    git_branch: str | None
    started_at: float
    last_active: float


@dataclass(frozen=True)
class _CacheEntry:
    cursor: ClaudeCursor
    metadata: _Metadata
    origin_kind: OriginKind
    origin_bridge_id: str | None


class ClaudeSourceAdapter:
    def __init__(self, projects_root: Path, *, marker_secret: bytes) -> None:
        self._projects_root = Path(projects_root)
        self._marker_secret = marker_secret
        self._cache: dict[str, _CacheEntry] = {}

    def discover(self) -> list[Path]:
        return sorted(
            self._projects_root.rglob("*.jsonl"),
            key=lambda path: str(path),
        )

    def parse(
        self, path: Path, previous: ClaudeCursor | None = None
    ) -> ClaudeParseResult:
        transcript_path = Path(path)
        read_slice = _read_for_parse(transcript_path, previous)
        lines = _parse_complete_lines(
            read_slice.data,
            read_slice.completed_length,
            base_offset=read_slice.base_offset,
        )
        records = [line.record for line in lines if line.record is not None]
        metadata_delta = _metadata_delta(records)
        cache_key = str(transcript_path.absolute())
        cached = self._cache.get(cache_key)
        warm_increment = (
            previous is not None
            and not read_slice.rebuild
            and cached is not None
            and cached.cursor == previous
        )
        if warm_increment and cached is not None:
            metadata = _merge_metadata(cached.metadata, metadata_delta)
            prior_origin_kind = cached.origin_kind
            prior_origin_bridge_id = cached.origin_bridge_id
        else:
            metadata = _materialize_metadata(metadata_delta, transcript_path)
            prior_origin_kind = OriginKind.NATIVE
            prior_origin_bridge_id = None

        malformed_lines = 0
        unknown_records = 0
        messages: list[ProjectedMessage] = []
        for line in lines:
            if line.record is None:
                malformed_lines += 1
                continue
            record_type = line.record.get("type")
            if record_type not in _RECOGNIZED_RECORD_TYPES:
                unknown_records += 1
                continue
            if record_type in {"user", "assistant"} and not line.record.get(
                "isSidechain", False
            ):
                messages.extend(_project_record(line))

        origin_kind, origin_bridge_id = _detect_origin(
            records,
            self._marker_secret,
            prior_kind=prior_origin_kind,
            prior_bridge_id=prior_origin_bridge_id,
        )
        cursor = read_slice.cursor
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=metadata.native_id,
            title=metadata.title,
            cwd=metadata.cwd,
            started_at=metadata.started_at,
            last_active=metadata.last_active,
            messages=messages,
            native_path=str(transcript_path),
            native_status="active",
            native_cursor=_serialize_cursor(cursor),
            native_hash=cursor.head_hash,
            parser_version=_PARSER_VERSION,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
            git_branch=metadata.git_branch,
        )
        self._cache[cache_key] = _CacheEntry(
            cursor=cursor,
            metadata=metadata,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
        )
        return ClaudeParseResult(
            projection=projection,
            cursor=cursor,
            rebuild=read_slice.rebuild,
            malformed_lines=malformed_lines,
            unknown_records=unknown_records,
        )

    def find_native_session(self, native_id: str) -> Path | None:
        if not isinstance(native_id, str) or not native_id.strip():
            return None
        wanted = native_id.strip()
        paths = self.discover()
        for path in paths:
            if path.stem == wanted:
                return path
        for path in paths:
            if _probe_native_id(path) == wanted:
                return path
        return None


def _read_for_parse(path: Path, previous: ClaudeCursor | None) -> _ReadSlice:
    with path.open("rb") as stream:
        if previous is None:
            return _read_full(stream, rebuild=False)

        stream.seek(0, 2)
        file_size = stream.tell()
        stream.seek(0)
        previous_head = stream.read(max(previous.head_length, 0))
        boundary = b"\n"
        if previous.offset > 0 and file_size >= previous.offset:
            stream.seek(previous.offset - 1)
            boundary = stream.read(1)
        rebuild = (
            previous.offset < 0
            or previous.head_length < 0
            or file_size < previous.offset
            or len(previous_head) != previous.head_length
            or _sha256(previous_head) != previous.head_hash
            or boundary != b"\n"
        )
        if rebuild:
            stream.seek(0)
            return _read_full(stream, rebuild=True)

        stream.seek(previous.offset)
        tail = stream.read()
        completed_length = _completed_byte_length(tail)
        return _ReadSlice(
            data=tail,
            base_offset=previous.offset,
            completed_length=completed_length,
            cursor=ClaudeCursor(
                offset=previous.offset + completed_length,
                head_length=previous.head_length,
                head_hash=previous.head_hash,
            ),
            rebuild=False,
        )


def _read_full(stream: BinaryIO, *, rebuild: bool) -> _ReadSlice:
    data = stream.read()
    head_length = min(len(data), _HEAD_SAMPLE_BYTES)
    completed_length = _completed_byte_length(data)
    return _ReadSlice(
        data=data,
        base_offset=0,
        completed_length=completed_length,
        cursor=ClaudeCursor(
            offset=completed_length,
            head_length=head_length,
            head_hash=_sha256(data[:head_length]),
        ),
        rebuild=rebuild,
    )


def _probe_native_id(path: Path) -> str | None:
    with path.open("rb") as stream:
        prefix = stream.read(_NATIVE_ID_PROBE_BYTES)
    match = _NATIVE_ID_RE.search(prefix)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _nonempty_string(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _completed_byte_length(data: bytes) -> int:
    last_newline = data.rfind(b"\n")
    return last_newline + 1 if last_newline >= 0 else 0


def _parse_complete_lines(
    data: bytes, completed_length: int, *, base_offset: int
) -> list[_TranscriptLine]:
    lines: list[_TranscriptLine] = []
    offset = base_offset
    for raw in data[:completed_length].splitlines(keepends=True):
        try:
            decoded = json.loads(raw)
            record = decoded if isinstance(decoded, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            record = None
        lines.append(_TranscriptLine(offset=offset, raw=raw, record=record))
        offset += len(raw)
    return lines


def _metadata_delta(records: list[dict[str, Any]]) -> _MetadataDelta:
    native_id = _first_native_id(records)
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    timestamps: list[float] = []

    for record in records:
        if record.get("type") not in _RECOGNIZED_RECORD_TYPES:
            continue
        if record.get("type") == "custom-title":
            candidate_title = _nonempty_string(record.get("customTitle"))
            if candidate_title is not None:
                title = candidate_title
        if not record.get("isSidechain", False):
            candidate_cwd = _nonempty_string(record.get("cwd"))
            if candidate_cwd is not None:
                cwd = candidate_cwd
            candidate_branch = _nonempty_string(record.get("gitBranch"))
            if candidate_branch is not None:
                git_branch = candidate_branch
            timestamp = _parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)

    return _MetadataDelta(
        native_id=native_id,
        title=title,
        cwd=cwd,
        git_branch=git_branch,
        timestamps=tuple(timestamps),
    )


def _materialize_metadata(delta: _MetadataDelta, path: Path) -> _Metadata:
    if delta.timestamps:
        started_at = min(delta.timestamps)
        last_active = max(delta.timestamps)
    else:
        started_at = path.stat().st_mtime
        last_active = started_at
    return _Metadata(
        native_id=delta.native_id or path.stem,
        title=delta.title,
        cwd=delta.cwd,
        git_branch=delta.git_branch,
        started_at=started_at,
        last_active=last_active,
    )


def _merge_metadata(baseline: _Metadata, delta: _MetadataDelta) -> _Metadata:
    started_at = baseline.started_at
    last_active = baseline.last_active
    if delta.timestamps:
        started_at = min(started_at, min(delta.timestamps))
        last_active = max(last_active, max(delta.timestamps))
    return _Metadata(
        native_id=delta.native_id or baseline.native_id,
        title=delta.title or baseline.title,
        cwd=delta.cwd or baseline.cwd,
        git_branch=delta.git_branch or baseline.git_branch,
        started_at=started_at,
        last_active=last_active,
    )


def _first_native_id(records: Iterable[dict[str, Any]]) -> str | None:
    for record in records:
        if record.get("type") not in _RECOGNIZED_RECORD_TYPES:
            continue
        native_id = _nonempty_string(record.get("sessionId"))
        if native_id is not None:
            return native_id
    return None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            timestamp = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None

    if abs(timestamp) >= 100_000_000_000:
        timestamp /= 1000.0
    return timestamp if math.isfinite(timestamp) else None


def _project_record(line: _TranscriptLine) -> list[ProjectedMessage]:
    record = line.record
    assert record is not None
    record_type = record.get("type")
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    timestamp = _parse_timestamp(record.get("timestamp")) or 0.0
    event_id = _nonempty_string(record.get("uuid")) or (
        f"offset:{line.offset}:{_sha256(line.raw)}"
    )

    if isinstance(content, str):
        return [
            ProjectedMessage(
                native_event_id=event_id,
                ordinal=0,
                role="assistant" if record_type == "assistant" else "user",
                content=content,
                timestamp=timestamp,
            )
        ]
    if not isinstance(content, list):
        return []

    projected: list[ProjectedMessage] = []
    for ordinal, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                projected.append(
                    ProjectedMessage(
                        native_event_id=event_id,
                        ordinal=ordinal,
                        role="assistant" if record_type == "assistant" else "user",
                        content=text,
                        timestamp=timestamp,
                    )
                )
        elif record_type == "assistant" and block_type == "tool_use":
            projected.append(_project_tool_use(event_id, ordinal, timestamp, block))
        elif record_type == "user" and block_type == "tool_result":
            projected.append(_project_tool_result(event_id, ordinal, timestamp, block))
    return projected


def _project_tool_use(
    event_id: str, ordinal: int, timestamp: float, block: dict[str, Any]
) -> ProjectedMessage:
    name = _nonempty_string(block.get("name")) or "unknown_tool"
    tool_call_id = _nonempty_string(block.get("id"))
    tool_call = {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": _canonical_json(block.get("input", {})),
        },
    }
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=ordinal,
        role="assistant",
        content=None,
        timestamp=timestamp,
        tool_name=name,
        tool_calls=[tool_call],
    )


def _project_tool_result(
    event_id: str, ordinal: int, timestamp: float, block: dict[str, Any]
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=ordinal,
        role="tool",
        content=_visible_tool_result(block.get("content")),
        timestamp=timestamp,
        tool_call_id=_nonempty_string(block.get("tool_use_id")),
    )


def _visible_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = [
            block["text"]
            for block in value
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if text_parts and len(text_parts) == len(value):
            return "\n".join(text_parts)
    return _canonical_json(value)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return str(value)


def _detect_origin(
    records: list[dict[str, Any]],
    marker_secret: bytes,
    *,
    prior_kind: OriginKind,
    prior_bridge_id: str | None,
) -> tuple[OriginKind, str | None]:
    if prior_bridge_id is not None:
        if prior_kind is OriginKind.BRIDGE_PLACEHOLDER and any(
            _is_human_user(record) for record in records
        ):
            return OriginKind.BRIDGE_CONTINUATION, prior_bridge_id
        return prior_kind, prior_bridge_id

    for index, record in enumerate(records):
        if record.get("type") not in {"user", "assistant"} or record.get(
            "isSidechain", False
        ):
            continue
        for text in _record_text_blocks(record):
            for match in _MARKER_CANDIDATE_RE.finditer(text):
                try:
                    payload = decode_bridge_marker(match.group(0), marker_secret)
                except InvalidBridgeMarker:
                    continue
                if payload.target_provider is Provider.CLAUDE:
                    continued = any(
                        _is_human_user(later_record)
                        for later_record in records[index + 1 :]
                    )
                    kind = (
                        OriginKind.BRIDGE_CONTINUATION
                        if continued
                        else OriginKind.BRIDGE_PLACEHOLDER
                    )
                    return kind, payload.bridge_id
    return OriginKind.NATIVE, None


def _is_human_user(record: dict[str, Any]) -> bool:
    if record.get("type") != "user" or record.get("isSidechain", False):
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in content
    )


def _record_text_blocks(record: dict[str, Any]) -> list[str]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def _serialize_cursor(cursor: ClaudeCursor) -> str:
    return json.dumps(
        {
            "head_hash": cursor.head_hash,
            "head_length": cursor.head_length,
            "offset": cursor.offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
