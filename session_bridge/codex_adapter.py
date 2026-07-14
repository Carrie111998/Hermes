from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Protocol

from agent.transports.codex_event_projector import CodexEventProjector

from .models import (
    InvalidBridgeMarker,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    decode_bridge_marker,
)


_PARSER_VERSION = 1
_REQUEST_TIMEOUT = 30.0
_SUPPORTED_ITEM_TYPES = frozenset({
    "agentMessage",
    "commandExecution",
    "dynamicToolCall",
    "fileChange",
    "mcpToolCall",
    "reasoning",
    "userMessage",
})
_MARKER_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)


class _RequestClient(Protocol):
    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CodexThreadSummary:
    native_id: str
    title: str | None
    cwd: str | None
    started_at: float
    last_active: float
    archived: bool
    revision: str


class CodexSourceAdapter:
    def __init__(self, client: _RequestClient, *, marker_secret: bytes) -> None:
        self._client = client
        self._marker_secret = marker_secret
        self._initialized = False
        self._initialization_failed = False
        self._seen_inventory: dict[str, CodexThreadSummary] = {}
        self._inventory_cache: dict[str, CodexThreadSummary] = {}

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self._ensure_initialized()
        summaries = self._fetch_inventory(archived=archived)

        changed = [
            summary
            for summary in summaries
            if self._seen_inventory.get(summary.native_id) != summary
        ]
        next_seen = dict(self._seen_inventory)
        next_cache = dict(self._inventory_cache)
        for summary in summaries:
            next_seen[summary.native_id] = summary
            next_cache[summary.native_id] = summary
        self._seen_inventory = next_seen
        self._inventory_cache = next_cache
        return changed

    def project_thread(self, summary: CodexThreadSummary) -> SessionProjection:
        self._ensure_initialized()
        response = self._client.request(
            "thread/read",
            {"threadId": summary.native_id, "includeTurns": True},
            timeout=_REQUEST_TIMEOUT,
        )
        thread = _thread_from_response(response)
        response_native_id = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if response_native_id is None:
            raise ValueError("Codex thread/read response has no thread identity")
        if response_native_id != summary.native_id:
            raise ValueError("Codex thread/read returned a different thread identity")

        summary_started_at, summary_last_active = _normalized_activity(
            summary.started_at, summary.last_active, context="Codex thread summary"
        )

        projector = CodexEventProjector()
        projected: list[ProjectedMessage] = []
        fallback_occurrences: dict[str, int] = {}
        turns = thread["turns"]
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("Codex thread/read turn must be an object")
            turn_timestamp = _timestamp_from(turn)
            if "items" not in turn:
                raise ValueError("Codex thread/read turn has no items list")
            items = turn["items"]
            if not isinstance(items, list):
                raise ValueError("Codex thread/read turn items must be a list")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = _nonempty_string(item.get("type"))
                if item_type not in _SUPPORTED_ITEM_TYPES:
                    continue
                candidate = deepcopy(projector)
                try:
                    result = candidate.project_item(item)
                except (AttributeError, TypeError, ValueError):
                    continue
                if not _valid_reasoning_item(item):
                    continue
                item_timestamp = _timestamp_from(item)
                timestamp = (
                    item_timestamp
                    if item_timestamp is not None
                    else turn_timestamp
                    if turn_timestamp is not None
                    else summary_started_at
                )
                try:
                    fallback_identity, fallback_digest = _fallback_identity(
                        item,
                        result.messages,
                        timestamp=timestamp,
                        occurrences=fallback_occurrences,
                    )
                    item_messages = _project_messages(
                        item,
                        result.messages,
                        timestamp=timestamp,
                        fallback_identity=fallback_identity,
                    )
                except (TypeError, ValueError):
                    continue
                projector = candidate
                projected.extend(item_messages)
                if fallback_digest is not None:
                    fallback_occurrences[fallback_digest] = (
                        fallback_occurrences.get(fallback_digest, 0) + 1
                    )

        origin_kind, origin_bridge_id = _detect_origin(
            projected, marker_secret=self._marker_secret
        )
        native_path = _nonempty_string(
            _first(thread, "rolloutPath", "rollout_path")
        ) or _nonempty_string(_first(response, "rolloutPath", "rollout_path"))
        message_timestamps = [message.timestamp for message in projected]
        started_at = min([summary_started_at, *message_timestamps])
        last_active = max([summary_last_active, *message_timestamps])
        normalized_summary = CodexThreadSummary(
            native_id=summary.native_id,
            title=summary.title,
            cwd=summary.cwd,
            started_at=started_at,
            last_active=last_active,
            archived=summary.archived,
            revision=summary.revision,
        )
        native_hash = _projection_hash(normalized_summary, projected)
        return SessionProjection(
            provider=Provider.CODEX,
            native_id=summary.native_id,
            title=summary.title,
            cwd=summary.cwd,
            started_at=started_at,
            last_active=last_active,
            messages=projected,
            native_path=native_path,
            native_status="archived" if summary.archived else "active",
            native_cursor=summary.revision,
            native_hash=native_hash,
            parser_version=_PARSER_VERSION,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
        )

    def find_native_thread(self, native_id: str) -> CodexThreadSummary | None:
        if not isinstance(native_id, str) or not native_id.strip():
            return None
        self._ensure_initialized()
        wanted = native_id.strip()

        active = self._fetch_inventory(archived=False)
        found = next(
            (summary for summary in active if summary.native_id == wanted), None
        )
        if found is not None:
            self._inventory_cache[wanted] = found
            return found

        archived = self._fetch_inventory(archived=True)
        found = next(
            (summary for summary in archived if summary.native_id == wanted), None
        )
        if found is not None:
            self._inventory_cache[wanted] = found
        return found

    def _ensure_initialized(self) -> None:
        if self._initialization_failed:
            raise RuntimeError(
                "Codex app-server initialization outcome is unknown; replace the "
                "client before retrying"
            )
        if self._initialized:
            return
        if getattr(self._client, "_initialized", False) is True:
            self._initialized = True
            return
        initialize = getattr(self._client, "initialize", None)
        if not callable(initialize):
            self._initialized = True
            return
        try:
            initialize()
        except Exception as exc:
            self._initialization_failed = True
            raise RuntimeError(
                "Codex app-server initialization outcome is unknown; replace the "
                "client before retrying"
            ) from exc
        if hasattr(self._client, "_initialized") and not getattr(
            self._client, "_initialized"
        ):
            self._initialization_failed = True
            raise RuntimeError(
                "Codex app-server initialization did not complete; replace the client"
            )
        self._initialized = True

    def _fetch_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        cursor: Any = None
        seen_cursors: set[str] = set()
        normalized: dict[str, CodexThreadSummary] = {}
        conflicts: set[str] = set()
        raw_entry_count = 0
        while True:
            params: dict[str, Any] = {"archived": archived}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._client.request(
                "thread/list", params, timeout=_REQUEST_TIMEOUT
            )
            if not isinstance(response, dict):
                raise ValueError("Codex thread/list response must be an object")
            entries = _first(response, "data", "threads")
            if entries is None:
                raise ValueError("Codex thread/list response has no entries list")
            if not isinstance(entries, list):
                raise ValueError("Codex thread/list entries must be a list")
            raw_entry_count += len(entries)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    summary = _normalize_summary(entry, archived=archived)
                except (TypeError, ValueError):
                    continue
                prior = normalized.get(summary.native_id)
                if prior is None and summary.native_id not in conflicts:
                    normalized[summary.native_id] = summary
                elif prior != summary:
                    normalized.pop(summary.native_id, None)
                    conflicts.add(summary.native_id)

            next_cursor = _first(response, "nextCursor", "next_cursor")
            if next_cursor in (None, ""):
                break
            cursor_key = _canonical_json(next_cursor)
            if cursor_key in seen_cursors:
                raise ValueError("Codex thread/list returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        if raw_entry_count and not normalized:
            raise ValueError("Codex thread/list contained no valid inventory entries")
        return [normalized[native_id] for native_id in sorted(normalized)]


def _thread_from_response(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Codex thread/read response must be an object")
    nested = response.get("thread")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ValueError("Codex thread/read thread must be an object")
        thread = nested
    else:
        thread = response
    if "turns" not in thread or not isinstance(thread["turns"], list):
        raise ValueError("Codex thread/read response must include a turns list")
    return thread


def _normalize_summary(entry: dict[str, Any], *, archived: bool) -> CodexThreadSummary:
    native_id = _nonempty_string(
        _first(entry, "id", "threadId", "thread_id", "sessionId", "session_id")
    )
    if native_id is None:
        raise ValueError("Codex inventory entry has no thread ID")

    title = _optional_string(_first(entry, "title", "name", "preview"))
    cwd = _optional_string(
        _first(entry, "cwd", "workingDirectory", "working_directory")
    )
    started_at = _inventory_timestamp(
        entry,
        aliases=(
            ("createdAt", False),
            ("created_at", False),
            ("startedAt", False),
            ("started_at", False),
            ("createdAtMs", True),
            ("startedAtMs", True),
        ),
    )
    last_active = _inventory_timestamp(
        entry,
        aliases=(
            ("updatedAt", False),
            ("updated_at", False),
            ("lastActive", False),
            ("last_active", False),
            ("updatedAtMs", True),
            ("lastActiveMs", True),
        ),
    )
    if started_at is None and last_active is None:
        raise ValueError("Codex inventory entry has no valid timestamps")
    if started_at is None:
        started_at = last_active
    if last_active is None:
        last_active = started_at
    assert started_at is not None and last_active is not None
    started_at, last_active = _normalized_activity(
        started_at, last_active, context="Codex inventory"
    )

    archived_value = entry.get("archived", archived)
    if not isinstance(archived_value, bool):
        raise ValueError("Codex inventory archived state must be boolean")
    normalized: dict[str, Any] = {
        "native_id": native_id,
        "title": title,
        "cwd": cwd,
        "started_at": started_at,
        "last_active": last_active,
        "archived": archived_value,
    }
    revision_value = _first(entry, "revision", "version", "updatedVersion")
    revision = _normalize_revision(revision_value)
    if revision is None:
        revision = hashlib.sha256(
            _canonical_json(normalized).encode("utf-8")
        ).hexdigest()
    return CodexThreadSummary(
        native_id=native_id,
        title=title,
        cwd=cwd,
        started_at=started_at,
        last_active=last_active,
        archived=archived_value,
        revision=revision,
    )


def _normalize_revision(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Codex inventory revision must not be boolean")
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("Codex inventory revision must not be empty")
        return normalized
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    raise ValueError("Codex inventory revision has an unsupported type")


def _project_messages(
    item: dict[str, Any],
    messages: list[dict],
    *,
    timestamp: float,
    fallback_identity: str | None,
) -> list[ProjectedMessage]:
    item_id = _nonempty_string(item.get("id"))
    base_identity = item_id or fallback_identity
    if base_identity is None:
        return []
    multiple = len(messages) > 1
    projected: list[ProjectedMessage] = []
    for ordinal, message in enumerate(messages):
        native_event_id = f"{base_identity}:{ordinal}" if multiple else base_identity
        tool_calls = message.get("tool_calls")
        tool_name = None
        if isinstance(tool_calls, list) and tool_calls:
            function = tool_calls[0].get("function")
            if isinstance(function, dict):
                tool_name = _nonempty_string(function.get("name"))
        projected.append(
            ProjectedMessage(
                native_event_id=native_event_id,
                ordinal=ordinal if multiple else 0,
                role=str(message.get("role") or "assistant"),
                content=_message_content(message.get("content")),
                timestamp=timestamp,
                tool_name=tool_name,
                tool_calls=deepcopy(tool_calls)
                if isinstance(tool_calls, list)
                else None,
                tool_call_id=_nonempty_string(message.get("tool_call_id")),
                reasoning=_nonempty_string(message.get("reasoning")),
            )
        )
    return projected


def _fallback_identity(
    item: dict[str, Any],
    messages: list[dict],
    *,
    timestamp: float,
    occurrences: dict[str, int],
) -> tuple[str | None, str | None]:
    if _nonempty_string(item.get("id")) is not None or not messages:
        return None, None
    digest = hashlib.sha256(
        _canonical_json({
            "type": item.get("type"),
            "messages": messages,
            "timestamp": timestamp,
        }).encode("utf-8")
    ).hexdigest()
    occurrence = occurrences.get(digest, 0)
    identity = digest if occurrence == 0 else f"{digest}:occ:{occurrence}"
    return identity, digest


def _message_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _valid_reasoning_item(item: dict[str, Any]) -> bool:
    if item.get("type") != "reasoning":
        return True
    for key in ("summary", "content"):
        fragments = item.get(key)
        if fragments is None:
            continue
        if not isinstance(fragments, list) or not all(
            isinstance(fragment, str) for fragment in fragments
        ):
            return False
    return True


def _detect_origin(
    messages: list[ProjectedMessage], *, marker_secret: bytes
) -> tuple[OriginKind, str | None]:
    marker_message_indexes: set[int] = set()
    marker_occurrences: list[tuple[int, str]] = []
    for index, message in enumerate(messages):
        if message.role != "user" or not message.content:
            continue
        for match in _MARKER_CANDIDATE_RE.finditer(message.content):
            try:
                payload = decode_bridge_marker(match.group(0), marker_secret)
            except InvalidBridgeMarker:
                continue
            if payload.target_provider is Provider.CODEX:
                marker_message_indexes.add(index)
                marker_occurrences.append((index, payload.bridge_id))

    marker_ids = {bridge_id for _, bridge_id in marker_occurrences}
    if len(marker_ids) > 1:
        raise ValueError("Codex thread has conflicting bridge markers")
    if not marker_ids:
        return OriginKind.NATIVE, None

    bridge_id = next(iter(marker_ids))
    first_marker_index = min(index for index, _ in marker_occurrences)
    continued = any(
        index > first_marker_index
        and index not in marker_message_indexes
        and message.role == "user"
        and bool((message.content or "").strip())
        for index, message in enumerate(messages)
    )
    return (
        OriginKind.BRIDGE_CONTINUATION if continued else OriginKind.BRIDGE_PLACEHOLDER,
        bridge_id,
    )


def _projection_hash(
    summary: CodexThreadSummary, messages: list[ProjectedMessage]
) -> str:
    supported = {
        "summary": asdict(summary),
        "messages": [asdict(message) for message in messages],
    }
    return hashlib.sha256(_canonical_json(supported).encode("utf-8")).hexdigest()


def _timestamp_from(
    value: dict[str, Any],
    *,
    aliases: tuple[tuple[str, bool], ...] = (
        ("timestamp", False),
        ("createdAt", False),
        ("created_at", False),
        ("completedAt", False),
        ("completed_at", False),
        ("createdAtMs", True),
        ("completedAtMs", True),
    ),
) -> float | None:
    for key, milliseconds in aliases:
        if key not in value:
            continue
        parsed = _parse_timestamp(value[key], milliseconds=milliseconds)
        if parsed is not None:
            return parsed
    return None


def _inventory_timestamp(
    value: dict[str, Any], *, aliases: tuple[tuple[str, bool], ...]
) -> float | None:
    for key, milliseconds in aliases:
        if key not in value:
            continue
        parsed = _parse_timestamp(value[key], milliseconds=milliseconds)
        if parsed is None:
            raise ValueError(f"Codex inventory timestamp {key!r} is invalid")
        return parsed
    return None


def _normalized_activity(
    started_at: Any, last_active: Any, *, context: str
) -> tuple[float, float]:
    if (
        isinstance(started_at, bool)
        or isinstance(last_active, bool)
        or not isinstance(started_at, (int, float))
        or not isinstance(last_active, (int, float))
    ):
        raise ValueError(f"{context} activity timestamps must be numeric")
    normalized_start = float(started_at)
    normalized_last = float(last_active)
    if not math.isfinite(normalized_start) or not math.isfinite(normalized_last):
        raise ValueError(f"{context} activity timestamps must be finite")
    return min(normalized_start, normalized_last), max(
        normalized_start, normalized_last
    )


def _parse_timestamp(value: Any, *, milliseconds: bool) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if milliseconds:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed_datetime = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
        parsed = parsed_datetime.timestamp()
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Codex inventory text field must be a string")
    normalized = value.strip()
    return normalized or None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
