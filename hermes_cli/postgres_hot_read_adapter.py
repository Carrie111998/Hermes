"""Inert, dependency-injected PostgreSQL hot-message reader and shadow comparator."""

from __future__ import annotations

import math
import json
import copy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, AsyncContextManager, Callable, Mapping, Protocol, Sequence


MAX_SESSION_ID_BYTES = 1024
MAX_LIMIT = 100
MAX_OFFSET = 10_000
HOT_WINDOW_SECONDS = 86_400
FETCH_TIMEOUT_SECONDS = 2.0
MAX_ROW_BYTES = 256 * 1024
MAX_PAGE_BYTES = 1024 * 1024
METADATA_KEYS = frozenset({
    "scope", "reason", "sqlite_row_count", "postgres_row_count",
    "first_difference_index", "field", "hot_status", "outcome",
})
MESSAGE_COLUMNS = (
    "id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
    "timestamp", "token_count", "finish_reason", "reasoning", "reasoning_content",
    "reasoning_details", "codex_reasoning_items", "codex_message_items",
    "platform_message_id", "observed", "active", "compacted", "effect_disposition",
    "api_content", "display_kind", "display_metadata",
)
MESSAGE_QUERY = (
    f"SELECT {', '.join(MESSAGE_COLUMNS)} FROM hermes_hot.messages "
    "WHERE session_id=$1 AND timestamp >= $2 AND ($3::boolean OR active=1) "
    "ORDER BY id ASC LIMIT $4 OFFSET $5"
)


class HotReadStatus(str, Enum):
    OK = "ok"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    MALFORMED_ROW = "malformed_row"


@dataclass(frozen=True)
class HotReadRequest:
    session_id: str
    cutoff_epoch_s: float
    limit: int
    offset: int
    include_inactive: bool


@dataclass(frozen=True)
class HotReadResult:
    status: HotReadStatus
    rows: tuple[dict[str, Any], ...] = ()


class ShadowOutcome(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ShadowComparison:
    outcome: ShadowOutcome
    metadata: Mapping[str, object]


class HotTierConnection(Protocol):
    async def fetch(self, query: str, *args: object, timeout: float) -> Any: ...


Acquire = Callable[[], AsyncContextManager[HotTierConnection]]

_TEXT_FIELDS = frozenset({
    "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
    "finish_reason", "reasoning", "reasoning_content", "reasoning_details",
    "codex_reasoning_items", "codex_message_items", "platform_message_id",
    "effect_disposition", "api_content", "display_kind", "display_metadata",
})
_INT_FIELDS = frozenset({"id", "token_count", "observed", "active", "compacted"})


def _decode_text(value: object) -> object:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return value


def _normalize_row(source: object) -> tuple[dict[str, Any], int]:
    if not hasattr(source, "keys"):
        raise ValueError
    keys = tuple(source.keys())  # type: ignore[union-attr]
    if set(keys) != set(MESSAGE_COLUMNS) or len(keys) != len(MESSAGE_COLUMNS):
        raise ValueError
    row: dict[str, Any] = {}
    size = 0
    for field in MESSAGE_COLUMNS:
        value = source[field]  # type: ignore[index]
        if isinstance(value, memoryview):
            size += value.nbytes
        elif isinstance(value, bytes):
            size += len(value)
        elif isinstance(value, str):
            size += len(value.encode("utf-8", errors="strict"))
        elif value is not None:
            size += 8
        if size > MAX_ROW_BYTES:
            raise ValueError
        value = _decode_text(value)
        if field in _TEXT_FIELDS and value is not None and not isinstance(value, str):
            raise ValueError
        if field in _INT_FIELDS and value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError
        if field == "timestamp" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError
        row[field] = value
    content = row["content"]
    if isinstance(content, str) and content.startswith("\x00json:"):
        try:
            row["content"] = json.loads(content[6:])
        except (json.JSONDecodeError, TypeError):
            pass
    if row["tool_calls"]:
        try:
            row["tool_calls"] = json.loads(row["tool_calls"])
        except (json.JSONDecodeError, TypeError):
            row["tool_calls"] = []
    return row, size


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def make_24h_request(
    session_id: str,
    *,
    now_epoch_s: float,
    limit: int = MAX_LIMIT,
    offset: int = 0,
    include_inactive: bool = False,
) -> HotReadRequest:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be nonempty")
    try:
        encoded = session_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("session_id must be valid UTF-8") from exc
    if len(encoded) > MAX_SESSION_ID_BYTES:
        raise ValueError("session_id is too long")
    now = _number(now_epoch_s, "now_epoch_s")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit is out of range")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError("offset is out of range")
    if not isinstance(include_inactive, bool):
        raise ValueError("include_inactive must be boolean")
    return HotReadRequest(session_id, now - HOT_WINDOW_SECONDS, limit, offset, include_inactive)


async def read_hot_messages(request: HotReadRequest | None, acquire: Acquire) -> HotReadResult:
    if not isinstance(request, HotReadRequest) or not _valid_request(request):
        return HotReadResult(HotReadStatus.INVALID_REQUEST)
    try:
        async with acquire() as connection:
            rows = await connection.fetch(
                MESSAGE_QUERY,
                request.session_id,
                request.cutoff_epoch_s,
                request.include_inactive,
                request.limit,
                request.offset,
                timeout=FETCH_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        return HotReadResult(HotReadStatus.TIMEOUT)
    except Exception:
        return HotReadResult(HotReadStatus.UNAVAILABLE)
    normalized = []
    page_bytes = 0
    last_id: int | None = None
    try:
        for index, row in enumerate(rows):
            if index >= request.limit:
                raise ValueError
            item, row_bytes = _normalize_row(row)
            message_id = item["id"]
            timestamp = item["timestamp"]
            active = item["active"]
            if (
                not isinstance(message_id, int)
                or message_id < 1
                or (last_id is not None and message_id <= last_id)
                or item["session_id"] != request.session_id
                or not isinstance(timestamp, (int, float))
                or timestamp < request.cutoff_epoch_s
                or (not request.include_inactive and active != 1)
            ):
                raise ValueError
            last_id = message_id
            page_bytes += row_bytes
            if page_bytes > MAX_PAGE_BYTES:
                raise ValueError
            normalized.append(item)
    except (ValueError, TypeError, UnicodeError, KeyError, OverflowError):
        return HotReadResult(HotReadStatus.MALFORMED_ROW)
    return HotReadResult(HotReadStatus.OK, tuple(normalized))


def _valid_request(request: HotReadRequest) -> bool:
    try:
        session_bytes = request.session_id.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeError):
        return False
    return (
        bool(session_bytes)
        and len(session_bytes) <= MAX_SESSION_ID_BYTES
        and not isinstance(request.cutoff_epoch_s, bool)
        and isinstance(request.cutoff_epoch_s, (int, float))
        and math.isfinite(request.cutoff_epoch_s)
        and not isinstance(request.limit, bool)
        and isinstance(request.limit, int)
        and 1 <= request.limit <= MAX_LIMIT
        and not isinstance(request.offset, bool)
        and isinstance(request.offset, int)
        and 0 <= request.offset <= MAX_OFFSET
        and isinstance(request.include_inactive, bool)
    )


def _metadata(outcome: ShadowOutcome, **values: object) -> Mapping[str, object]:
    metadata = {"scope": "messages_24h", **values, "outcome": outcome.value}
    if not metadata.keys() <= METADATA_KEYS:
        raise AssertionError("unsafe shadow metadata key")
    return MappingProxyType(metadata)


def _bounded_value_size(
    value: object,
    *,
    seen: set[int],
    depth: int = 0,
) -> int:
    if depth > 32:
        raise ValueError
    if value is None or isinstance(value, (bool, int)):
        return 8
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError
        return 8
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="strict"))
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError
        seen.add(identity)
        try:
            total = 0
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError
                total += len(key.encode("utf-8", errors="strict"))
                total += _bounded_value_size(nested, seen=seen, depth=depth + 1)
                if total > MAX_ROW_BYTES:
                    raise ValueError
            return total
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise ValueError
        seen.add(identity)
        try:
            total = 0
            for nested in value:
                total += _bounded_value_size(nested, seen=seen, depth=depth + 1)
                if total > MAX_ROW_BYTES:
                    raise ValueError
            return total
        finally:
            seen.remove(identity)
    raise ValueError


def _sqlite_snapshot(
    rows: Sequence[Mapping[str, Any]],
    request: HotReadRequest,
) -> tuple[dict[str, Any], ...]:
    if isinstance(rows, (str, bytes)) or len(rows) > request.limit:
        raise ValueError
    size = 0
    last_id: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        message_id = row.get("id")
        timestamp = row.get("timestamp")
        active = row.get("active")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id < 1
            or (last_id is not None and message_id <= last_id)
            or row.get("session_id") != request.session_id
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
            or timestamp < request.cutoff_epoch_s
            or isinstance(active, bool)
            or not isinstance(active, int)
            or active not in (0, 1)
            or (not request.include_inactive and active != 1)
        ):
            raise ValueError
        last_id = message_id
        row_size = sum(_bounded_value_size(value, seen=set()) for value in row.values())
        size += row_size
        if row_size > MAX_ROW_BYTES or size > MAX_PAGE_BYTES:
            raise ValueError
    snapshot = copy.deepcopy(rows)
    return tuple(dict(row) for row in snapshot)


async def compare_shadow_messages(
    request: HotReadRequest | None,
    sqlite_rows: Sequence[Mapping[str, Any]],
    acquire: Acquire,
) -> ShadowComparison:
    if not isinstance(request, HotReadRequest) or not _valid_request(request):
        outcome = ShadowOutcome.SKIPPED
        return ShadowComparison(
            outcome,
            _metadata(outcome, reason="invalid_request", hot_status=HotReadStatus.INVALID_REQUEST.value),
        )
    try:
        sqlite = _sqlite_snapshot(sqlite_rows, request)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        outcome = ShadowOutcome.SKIPPED
        return ShadowComparison(outcome, _metadata(outcome, reason="sqlite_out_of_bounds"))
    hot = await read_hot_messages(request, acquire)
    if hot.status is not HotReadStatus.OK:
        reason = {
            HotReadStatus.INVALID_REQUEST: "invalid_request",
            HotReadStatus.TIMEOUT: "hot_timeout",
            HotReadStatus.UNAVAILABLE: "hot_unavailable",
            HotReadStatus.MALFORMED_ROW: "hot_malformed_row",
        }[hot.status]
        outcome = ShadowOutcome.SKIPPED
        return ShadowComparison(outcome, _metadata(outcome, reason=reason, hot_status=hot.status.value))
    base = {
        "sqlite_row_count": len(sqlite),
        "postgres_row_count": len(hot.rows),
        "hot_status": hot.status.value,
    }
    if len(sqlite) != len(hot.rows):
        outcome = ShadowOutcome.MISMATCH
        return ShadowComparison(outcome, _metadata(outcome, reason="row_count", **base))
    for index, (left, right) in enumerate(zip(sqlite, hot.rows)):
        if set(left) != set(right):
            outcome = ShadowOutcome.MISMATCH
            return ShadowComparison(outcome, _metadata(outcome, reason="row_shape", first_difference_index=index, **base))
        for field in MESSAGE_COLUMNS:
            if left[field] != right[field]:
                outcome = ShadowOutcome.MISMATCH
                return ShadowComparison(outcome, _metadata(outcome, reason="field_value", first_difference_index=index, field=field, **base))
    outcome = ShadowOutcome.MATCH
    return ShadowComparison(outcome, _metadata(outcome, **base))
