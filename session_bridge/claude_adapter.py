from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, BinaryIO, Callable, Protocol
import uuid

from .models import (
    InvalidBridgeMarker,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    BridgeMarkerPayload,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
)


_PARSER_VERSION = 1
_HEAD_SAMPLE_BYTES = 65_536
_NATIVE_ID_PROBE_BYTES = 65_536
_RECOGNIZED_RECORD_TYPES = {
    "agent-name",
    "assistant",
    "attachment",
    "custom-title",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
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
_RECORD_TYPE_RE = re.compile(rb'"type"\s*:\s*("(?:\\.|[^"\\])*")')
_CURSOR_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SUBAGENT_STEM_RE = re.compile(r"^agent-([A-Za-z0-9_-]{1,128})$")
_DESCRIPTOR_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._/+;-]+")
CLAUDE_PLACEHOLDER_MAX_BUDGET_USD = "0.50"


def claude_project_directory_name(cwd: str) -> str:
    """Return Claude Code's project-directory encoding for an exact cwd."""

    if not isinstance(cwd, str) or not cwd:
        raise ValueError("Claude project cwd must be nonempty text")
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


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
    entrypoint: str | None = None


class ClaudeReadableSource(Protocol):
    def find_native_session(self, native_id: str) -> Path | None: ...

    def find_native_sessions(self, native_id: str) -> list[Path]: ...

    def find_native_sessions_by_stem(self, native_id: str) -> list[Path]: ...

    def find_native_sessions_by_stem_fresh(self, native_id: str) -> list[Path]: ...

    def parse(self, path: Path) -> ClaudeParseResult: ...


class ClaudeMarkerSource(ClaudeReadableSource, Protocol):
    def projection_has_exact_marker(
        self, projection: SessionProjection, marker: str
    ) -> bool: ...

    def projection_has_marker_payload(
        self, projection: SessionProjection, payload: BridgeMarkerPayload
    ) -> bool: ...


@dataclass(frozen=True)
class PlaceholderResult:
    native_id: str
    canonical_session_id: str
    used_registration_turn: bool
    verified_at: float


class PlaceholderCreationError(RuntimeError):
    """A sanitized provider placeholder creation failure."""

    def __init__(
        self,
        code: str,
        *,
        native_id: str | None = None,
        observed_cost_usd: int | float | None = None,
        duration_ms: int | float | None = None,
        num_turns: int | None = None,
    ) -> None:
        self.code = code
        self.native_id = native_id
        self.observed_cost_usd = _optional_nonnegative_float(observed_cost_usd)
        self.duration_ms = _optional_nonnegative_float(duration_ms)
        self.num_turns = _optional_nonnegative_int(num_turns)
        super().__init__(code)


class AmbiguousPlaceholderCreation(PlaceholderCreationError):
    """Creation may have happened; callers must reconcile before retrying."""


@dataclass(frozen=True)
class _TranscriptLine:
    offset: int
    raw: bytes
    record: dict[str, Any] | None


@dataclass(frozen=True)
class _ReadSlice:
    data: bytes
    head_data: bytes
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
    entrypoint: str | None


# How long a discovery listing may be reused before the tree is re-walked.
# Bounds how late a brand-new transcript can be picked up; see discover().
_DISCOVER_TTL_SECONDS = 60.0


class ClaudeSourceAdapter:
    def __init__(self, projects_root: Path, *, marker_secret: bytes) -> None:
        self._projects_root = Path(projects_root)
        self._marker_secret = marker_secret
        self._cache: dict[str, _CacheEntry] = {}
        self._discover_cache: list[Path] | None = None
        self._discover_at: float = 0.0

    def discover(self) -> list[Path]:
        """List Claude transcripts, reusing the last walk for up to the TTL.

        2026-08-13: this re-ran ``rglob`` over the whole projects tree on EVERY
        scan cycle (425 project dirs / 3,641 transcripts here). py-spy caught it
        mid-walk on the wedged service::

            _select_from (pathlib.py:205) -> rglob -> discover
            (claude_adapter.py:189)

        Provider scans share a small pool (``provider_calls_inflight`` was 2), so
        this walk monopolised the capacity and STARVED the codex scan: codex sat
        at indexed_total=1000 / remaining=1722 with zero progress and zero errors
        for 20+ minutes, while claude kept indexing. Its catalog freshness then
        aged past the 33s limit and pinned session-bridge-catalog to 'unknown'.

        A directory-mtime signature was considered and rejected: transcripts live
        at BOTH depth 1 and depth 3 under the root, and creating a file in a
        nested subdirectory does not bump the immediate project dir's mtime, so
        such a signature would silently miss new sessions. A TTL cannot miss
        anything -- it only delays discovery by at most the TTL, which for a
        mirror is well inside tolerance.
        """
        now = time.monotonic()
        cached = self._discover_cache
        if cached is not None and (now - self._discover_at) < _DISCOVER_TTL_SECONDS:
            return list(cached)
        discovered = sorted(
            self._projects_root.rglob("*.jsonl"),
            key=lambda path: str(path),
        )
        self._discover_cache = discovered
        self._discover_at = now
        return list(discovered)

    def parse(
        self, path: Path, previous: ClaudeCursor | None = None
    ) -> ClaudeParseResult:
        transcript_path = Path(path)
        cache_key = str(transcript_path.absolute())
        cached = self._cache.get(cache_key)
        read_slice = _read_for_parse(
            transcript_path,
            previous,
            probe_head=previous is not None and cached is None,
        )
        lines = _parse_complete_lines(
            read_slice.data,
            read_slice.completed_length,
            base_offset=read_slice.base_offset,
        )
        records = [line.record for line in lines if line.record is not None]
        head_entrypoint = _entrypoint_from_head(read_slice.head_data)
        delta_entrypoint = _entrypoint_from_records(records)
        delta_native_id = _validated_record_native_id(
            records,
            transcript_path=transcript_path,
        )
        record_scope = (
            delta_native_id
            if len(_record_native_ids(records)) > 1
            else None
        )
        if record_scope is not None:
            records = [
                record
                for record in records
                if _nonempty_string(record.get("sessionId")) == record_scope
            ]
        metadata_delta = _metadata_delta(records, native_id=delta_native_id)
        warm_increment = (
            previous is not None
            and not read_slice.rebuild
            and cached is not None
            and cached.cursor == previous
        )
        if warm_increment and cached is not None:
            if (
                delta_native_id is not None
                and delta_native_id != cached.metadata.native_id
            ):
                raise ValueError("Claude transcript native identity changed")
            metadata = _merge_metadata(cached.metadata, metadata_delta)
            prior_origin_kind = cached.origin_kind
            prior_origin_bridge_id = cached.origin_bridge_id
            entrypoint = _merge_entrypoints(
                cached.entrypoint,
                head_entrypoint,
                delta_entrypoint,
            )
        else:
            if previous is not None and not read_slice.rebuild:
                baseline_native_id = _native_id_from_bytes(read_slice.head_data)
                if _subagent_native_id(transcript_path) is not None:
                    baseline_native_id = transcript_path.stem
                baseline_native_id = baseline_native_id or transcript_path.stem
                if (
                    delta_native_id is not None
                    and delta_native_id != baseline_native_id
                ):
                    raise ValueError("Claude transcript native identity changed")
            else:
                baseline_native_id = delta_native_id or transcript_path.stem
            metadata = _materialize_metadata(
                metadata_delta,
                transcript_path,
                native_id=baseline_native_id,
            )
            prior_origin_kind = OriginKind.NATIVE
            prior_origin_bridge_id = None
            entrypoint = _merge_entrypoints(
                head_entrypoint,
                delta_entrypoint,
            )

        malformed_lines = 0
        unknown_records = 0
        messages: list[ProjectedMessage] = []
        projected_by_identity: dict[tuple[str, int], ProjectedMessage] = {}
        for line in lines:
            if line.record is None:
                malformed_lines += 1
                continue
            if (
                record_scope is not None
                and _nonempty_string(line.record.get("sessionId")) != record_scope
            ):
                continue
            record_type = line.record.get("type")
            if record_type not in _RECOGNIZED_RECORD_TYPES:
                unknown_records += 1
                continue
            if record_type in {"user", "assistant"} and _is_eligible_record(
                line.record
            ):
                for message in _project_record(line):
                    identity = (message.native_event_id, message.ordinal)
                    existing_message = projected_by_identity.get(identity)
                    if existing_message == message:
                        continue
                    projected_by_identity[identity] = message
                    messages.append(message)

        # Claude Code does not guarantee that a user record is written before
        # the assistant records it caused; measured 2026-08-25, a registration
        # transcript carried the prompt with the EARLIER timestamp but appended
        # it LAST.  Consumers read this list as an ordered turn sequence, so
        # file position is the wrong order to hand them.
        #
        # Sort only when every message carries a usable timestamp.  An absent
        # or unparseable one projects as 0.0, so sorting on it would hoist that
        # record ahead of the prompt -- the same malformed shape this ordering
        # exists to prevent.  The sort is stable, so messages sharing a
        # timestamp -- notably the content blocks of one record, appended in
        # ordinal order -- keep the order they were projected in.
        if messages and all(message.timestamp > 0.0 for message in messages):
            messages.sort(key=lambda message: message.timestamp)

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
            entrypoint=entrypoint,
        )
        return ClaudeParseResult(
            projection=projection,
            cursor=cursor,
            rebuild=read_slice.rebuild,
            malformed_lines=malformed_lines,
            unknown_records=unknown_records,
            entrypoint=entrypoint,
        )

    def find_native_session(self, native_id: str) -> Path | None:
        matches = self.find_native_sessions(native_id)
        return matches[0] if matches else None

    def find_native_sessions(self, native_id: str) -> list[Path]:
        if not isinstance(native_id, str) or not native_id.strip():
            return []
        wanted = native_id.strip()
        paths = self.discover()
        matches: list[Path] = []
        for path in paths:
            if path.stem == wanted:
                matches.append(path)
        for path in paths:
            if path in matches:
                continue
            try:
                probed_native_id = _probe_native_id(path)
            except (OSError, ValueError):
                continue
            if probed_native_id == wanted:
                matches.append(path)
        return matches

    def find_native_sessions_by_stem(self, native_id: str) -> list[Path]:
        """Return cached Claude transcript filenames without record probes."""

        if not isinstance(native_id, str) or not native_id.strip():
            return []
        wanted = native_id.strip()
        return [path for path in self.discover() if path.stem == wanted]

    def find_native_sessions_by_stem_fresh(self, native_id: str) -> list[Path]:
        """Return live exact-filename matches without refreshing the full inventory."""

        if not isinstance(native_id, str) or not native_id.strip():
            return []
        wanted = native_id.strip()
        if wanted in {".", ".."} or Path(wanted).name != wanted:
            return []
        filename = f"{wanted}.jsonl"
        try:
            directories = list(self._projects_root.iterdir())
        except OSError:
            # A missing or non-directory projects root behaves like an empty
            # inventory, matching the recursive scan it replaced.
            return []
        matches = sorted(
            (
                directory / filename
                for directory in directories
                if directory.is_dir() and (directory / filename).is_file()
            ),
            key=lambda path: str(path),
        )
        cached = self._discover_cache
        if cached is not None:
            self._discover_cache = sorted(
                [path for path in cached if path.stem != wanted] + matches,
                key=lambda path: str(path),
            )
        return matches

    def projection_has_exact_marker(
        self, projection: SessionProjection, marker: str
    ) -> bool:
        for message in projection.messages:
            if message.role not in {"user", "assistant"} or not message.content:
                continue
            if any(
                match.group(0) == marker
                for match in _MARKER_CANDIDATE_RE.finditer(message.content)
            ):
                return True
        return False

    def projection_has_marker_payload(
        self, projection: SessionProjection, payload: BridgeMarkerPayload
    ) -> bool:
        marker = encode_bridge_marker(payload, self._marker_secret)
        return self.projection_has_exact_marker(projection, marker)


class ClaudeTargetAdapter:
    def __init__(
        self,
        source_adapter: ClaudeMarkerSource,
        *,
        marker_secret: bytes,
        claude_executable: str | Sequence[str] = "claude",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        process_timeout: float = 120.0,
        discovery_timeout: float = 15.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._source_adapter = source_adapter
        self._marker_secret = marker_secret
        self._claude_command = resolve_claude_command(claude_executable)
        self._runner = runner
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._process_timeout = _positive_number(
            process_timeout, label="Claude process timeout"
        )
        self._discovery_timeout = _nonnegative_number(
            discovery_timeout, label="Claude discovery timeout"
        )
        self._poll_interval = _nonnegative_number(
            poll_interval, label="Claude poll interval"
        )

    def create_placeholder(
        self,
        *,
        native_id: str,
        title: str,
        source_session_id: str,
        bridge_id: str,
        policy_generation: int,
        cwd: Path | str | None = None,
    ) -> PlaceholderResult:
        native_id = _canonical_uuid(native_id)
        title = _required_text(title, label="title")
        source_session_id = _single_line_required_text(
            source_session_id, label="source session ID"
        )
        bridge_id = _single_line_required_text(bridge_id, label="bridge ID")
        if (
            not isinstance(policy_generation, int)
            or isinstance(policy_generation, bool)
            or policy_generation < 0
        ):
            raise ValueError("policy generation must be a non-negative integer")
        marker = encode_bridge_marker(
            BridgeMarkerPayload(
                bridge_id=bridge_id,
                source_session_id=source_session_id,
                target_provider=Provider.CLAUDE,
                policy_generation=policy_generation,
            ),
            self._marker_secret,
        )
        registration_prompt = _registration_prompt(
            marker=marker,
            source_session_id=source_session_id,
            bridge_id=bridge_id,
        )
        args = [
            *self._claude_command,
            "--print",
            "--session-id",
            native_id,
            "--name",
            title,
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--max-budget-usd",
            CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
            "--output-format",
            "json",
            registration_prompt,
        ]
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": self._process_timeout,
            "stdin": subprocess.DEVNULL,
            "shell": False,
            "check": False,
        }
        run_cwd = _existing_directory(cwd)
        if run_cwd is not None:
            run_kwargs["cwd"] = run_cwd
        timed_out = False
        provider_failure: PlaceholderCreationError | None = None
        try:
            completed = self._runner(args, **run_kwargs)
        except subprocess.TimeoutExpired:
            timed_out = True
            completed = None
        except FileNotFoundError:
            completed = None
            provider_failure = PlaceholderCreationError("claude_executable_not_found")
        except Exception:
            completed = None
            provider_failure = PlaceholderCreationError("claude_process_failed")
        if provider_failure is not None:
            raise provider_failure

        process_failure: PlaceholderCreationError | None = None
        if completed is not None and completed.returncode != 0:
            code, observed_cost_usd, duration_ms, num_turns = (
                _claude_process_failure_details(completed)
            )
            process_failure = PlaceholderCreationError(
                code,
                observed_cost_usd=observed_cost_usd,
                duration_ms=duration_ms,
                num_turns=num_turns,
            )

        verified = self._poll_exact_target(
            native_id=native_id,
            bridge_id=bridge_id,
            marker=marker,
            title=title,
            cwd=run_cwd,
        )
        if not verified:
            if process_failure is not None:
                raise process_failure
            code = (
                "claude_creation_ambiguous"
                if timed_out or completed is not None
                else "claude_target_not_found"
            )
            raise AmbiguousPlaceholderCreation(code)
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=canonical_session_id(Provider.CLAUDE, native_id),
            used_registration_turn=False,
            verified_at=float(self._clock()),
        )

    def _poll_exact_target(
        self,
        *,
        native_id: str,
        bridge_id: str,
        marker: str,
        title: str,
        cwd: str | None,
    ) -> bool:
        deadline = self._monotonic() + self._discovery_timeout
        while True:
            path = self._source_adapter.find_native_session(native_id)
            if path is not None:
                parse_failure: PlaceholderCreationError | None = None
                try:
                    projection = self._source_adapter.parse(path).projection
                except Exception:
                    projection = None
                    parse_failure = PlaceholderCreationError("claude_target_unreadable")
                if parse_failure is not None:
                    raise parse_failure
                assert projection is not None
                if projection.native_id != native_id:
                    raise PlaceholderCreationError("claude_target_mismatch")
                if projection.title != title:
                    raise PlaceholderCreationError("claude_target_title_mismatch")
                if cwd is not None and not _same_filesystem_location(
                    projection.cwd, cwd
                ):
                    raise PlaceholderCreationError("claude_target_cwd_mismatch")
                if (
                    projection.origin_kind
                    not in (
                        OriginKind.BRIDGE_PLACEHOLDER,
                        OriginKind.BRIDGE_CONTINUATION,
                    )
                    or projection.origin_bridge_id != bridge_id
                    or not self._source_adapter.projection_has_exact_marker(
                        projection, marker
                    )
                ):
                    raise PlaceholderCreationError("claude_target_marker_mismatch")
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(self._poll_interval)


def _registration_prompt(*, marker: str, source_session_id: str, bridge_id: str) -> str:
    return (
        "Hermes Session Bridge registration metadata. "
        f"Signed marker: {marker}. "
        f"Canonical source session: {source_session_id}. "
        "This registration message is metadata, not a substantive user message. "
        "Do not call any tool now. "
        "Reply exactly REGISTERED and nothing else. "
        "On the first subsequent substantive user message, call "
        f"session_continue with bridge ID {bridge_id} before answering."
    )


def classify_claude_process_failure(
    completed: subprocess.CompletedProcess[str],
) -> str:
    code, _, _, _ = _claude_process_failure_details(completed)
    return code


def _claude_process_failure_details(
    completed: subprocess.CompletedProcess[str],
) -> tuple[str, float | None, float | None, int | None]:
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    subtype = payload.get("subtype") if isinstance(payload, dict) else None
    if (
        isinstance(subtype, str)
        and subtype.startswith("error_")
        and re.fullmatch(r"[a-z0-9_]{1,64}", subtype)
    ):
        code = f"claude_process_{subtype}"
    else:
        code = f"claude_process_exit_{int(completed.returncode)}"
    if not isinstance(payload, dict):
        return code, None, None, None
    return (
        code,
        _optional_nonnegative_float(payload.get("total_cost_usd")),
        _optional_nonnegative_float(payload.get("duration_ms")),
        _optional_nonnegative_int(payload.get("num_turns")),
    )


def _canonical_uuid(value: str) -> str:
    normalized = _required_text(value, label="native ID")
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, ValueError) as exc:
        raise ValueError("native ID must be a UUID") from exc
    if str(parsed) != normalized.lower():
        raise ValueError("native ID must use canonical UUID syntax")
    return str(parsed)


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value.strip()


def _single_line_required_text(value: Any, *, label: str) -> str:
    normalized = _required_text(value, label=label)
    if "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{label} must be single-line")
    return normalized


def _command_prefix(value: str | Sequence[str], *, label: str) -> tuple[str, ...]:
    values: Sequence[str] = (value,) if isinstance(value, str) else value
    if not values:
        raise ValueError(f"{label} must not be empty")
    normalized: list[str] = []
    for entry in values:
        item = _single_line_required_text(entry, label=f"{label} argv entry")
        normalized.append(item)
    return tuple(normalized)


def _desktop_shipped_claude() -> Path | None:
    """Newest Claude CLI build the Desktop app ships, or None.

    The Desktop app keeps one directory per CLI build under
    ``%APPDATA%/Claude/claude-code/<version>/claude.exe`` and auto-updates,
    unlike the (since-uninstalled) npm global. ``HERMES_CLAUDE_CODE_ROOT``
    overrides the root for tests. Versions compare NUMERICALLY -- a lexical
    sort ranks ``2.1.9`` above ``2.1.237``.
    """

    root_override = os.environ.get("HERMES_CLAUDE_CODE_ROOT")
    if root_override:
        root = Path(root_override)
    else:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        root = Path(appdata) / "Claude" / "claude-code"
    best: tuple[tuple[int, ...], Path] | None = None
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        parts = entry.name.split(".")
        if not parts or not all(part.isdigit() for part in parts):
            continue
        exe = entry / "claude.exe"
        if not exe.is_file():
            continue
        key = tuple(int(part) for part in parts)
        if best is None or key > best[0]:
            best = (key, exe)
    return best[1] if best is not None else None


def resolve_claude_command(
    value: str | Sequence[str],
    *,
    which: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Return a shell-free Claude argv prefix safe for direct subprocess use.

    Shell shims are never executed. An EXPLICITLY configured shim path is an
    operator error and always raises. A shim (or nothing at all) found via
    PATH lookup is a resolution miss: see through the npm layout when it
    exists, otherwise fall back to the Desktop-shipped native ``claude.exe``
    (2026-08-25: the npm global was uninstalled after silently drifting to
    2.1.216 while the Desktop app ran 2.1.237; a bare shim took its place on
    PATH and the old resolver could not see through it).
    """

    if not isinstance(value, str):
        command = _command_prefix(value, label="Claude executable")
        if Path(command[0]).suffix.casefold() in {".cmd", ".ps1", ".bat"}:
            raise RuntimeError("unsupported_shell_shim")
        return command

    normalized = _single_line_required_text(value, label="Claude executable")
    # A path (any directory separator) is EXPLICIT operator configuration:
    # never substitute another binary for it. Only a bare command name is a
    # PATH lookup, where a dead end may fall back to the Desktop-shipped CLI.
    bare_name = "/" not in normalized and "\\" not in normalized
    find = which or shutil.which
    found = find(normalized)
    resolved = str(found or normalized)
    candidate = Path(resolved).expanduser()
    suffix = candidate.suffix.casefold()
    if suffix not in {".cmd", ".ps1", ".bat"}:
        if candidate.exists():
            return (str(candidate.resolve()),)
        # PATH lookup found nothing launchable; prefer the Desktop-shipped
        # CLI over returning a name the spawn will immediately fail on.
        if bare_name and found is None:
            desktop = _desktop_shipped_claude()
            if desktop is not None:
                return (str(desktop.resolve()),)
        return (resolved,)
    if candidate.stem.casefold() != "claude" or suffix == ".bat":
        if bare_name:
            desktop = _desktop_shipped_claude()
            if desktop is not None:
                return (str(desktop.resolve()),)
        raise RuntimeError("unsupported_shell_shim")

    npm_root = candidate.parent
    package_root = npm_root / "node_modules" / "@anthropic-ai" / "claude-code"
    native = package_root / "bin" / "claude.exe"
    if native.is_file():
        return (str(native.resolve()),)
    cli = package_root / "cli.js"
    local_node = npm_root / "node.exe"
    resolved_node = (
        local_node
        if local_node.is_file()
        else Path(str(find("node.exe") or find("node") or ""))
    )
    if not cli.is_file() or not resolved_node.is_file():
        if bare_name:
            desktop = _desktop_shipped_claude()
            if desktop is not None:
                return (str(desktop.resolve()),)
        raise RuntimeError("unsupported_shell_shim")
    return (str(resolved_node.resolve()), str(cli.resolve()))


def _same_filesystem_location(observed: str | None, expected: str) -> bool:
    if not isinstance(observed, str) or not observed.strip():
        return False
    try:
        observed_path = Path(observed).expanduser()
        expected_path = Path(expected).expanduser()
        if observed_path.exists() and expected_path.exists():
            return os.path.samefile(observed_path, expected_path)
        observed_normalized = os.path.normcase(
            os.path.realpath(os.path.abspath(observed_path))
        )
        expected_normalized = os.path.normcase(
            os.path.realpath(os.path.abspath(expected_path))
        )
    except (OSError, TypeError, ValueError):
        return False
    return observed_normalized == expected_normalized


def _positive_number(value: Any, *, label: str) -> float:
    normalized = _nonnegative_number(value, label=label)
    if normalized == 0:
        raise ValueError(f"{label} must be positive")
    return normalized


def _nonnegative_number(value: Any, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} must be a non-negative finite number")
    return float(value)


def _optional_nonnegative_float(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _existing_directory(value: Path | str | None) -> str | None:
    if value is None:
        return None
    try:
        path = Path(value).expanduser()
        if path.is_dir():
            return str(path.resolve())
    except (OSError, TypeError, ValueError):
        pass
    return None


def _read_for_parse(
    path: Path,
    previous: ClaudeCursor | None,
    *,
    probe_head: bool = False,
) -> _ReadSlice:
    with path.open("rb") as stream:
        if previous is None:
            return _read_full(stream, rebuild=False)
        if not _valid_cursor_shape(previous):
            return _read_full(stream, rebuild=True)

        stream.seek(0, 2)
        file_size = stream.tell()
        stream.seek(0)
        requested_head = max(
            previous.head_length,
            min(
                previous.offset,
                _HEAD_SAMPLE_BYTES if probe_head else previous.head_length,
            ),
            0,
        )
        probed_head = stream.read(requested_head)
        previous_head = probed_head[: max(previous.head_length, 0)]
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
            head_data=probed_head,
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
    head_data = data[:head_length]
    completed_length = _completed_byte_length(data)
    return _ReadSlice(
        data=data,
        head_data=head_data,
        base_offset=0,
        completed_length=completed_length,
        cursor=ClaudeCursor(
            offset=completed_length,
            head_length=head_length,
            head_hash=_sha256(head_data),
        ),
        rebuild=rebuild,
    )


def _probe_native_id(path: Path) -> str | None:
    subagent_native_id = _subagent_native_id(path)
    if subagent_native_id is not None:
        return subagent_native_id
    with path.open("rb") as stream:
        prefix = stream.read(_NATIVE_ID_PROBE_BYTES)
    return _native_id_from_bytes(prefix)


def encode_claude_cursor(cursor: object) -> dict[str, object] | None:
    """Return a JSON-safe mapping for a cursor, or None when it is unusable.

    The coordinator persists cursors between scan cycles. Encoding refuses
    anything ``_read_for_parse`` would reject anyway, so a malformed cursor
    costs one full read instead of poisoning an offset.
    """

    if not isinstance(cursor, ClaudeCursor) or not _valid_cursor_shape(cursor):
        return None
    return {
        "head_hash": cursor.head_hash,
        "head_length": cursor.head_length,
        "offset": cursor.offset,
    }


def decode_claude_cursor(value: object) -> ClaudeCursor | None:
    """Rebuild a cursor from :func:`encode_claude_cursor` output.

    Returns None for anything that does not round-trip -- including a shape
    written by a future version -- and the caller falls back to a full read.
    """

    if not isinstance(value, Mapping):
        return None
    if set(value) != {"head_hash", "head_length", "offset"}:
        return None
    offset = value["offset"]
    head_length = value["head_length"]
    head_hash = value["head_hash"]
    if type(offset) is not int or type(head_length) is not int:
        return None
    if not isinstance(head_hash, str):
        return None
    cursor = ClaudeCursor(
        offset=offset,
        head_length=head_length,
        head_hash=head_hash,
    )
    return cursor if _valid_cursor_shape(cursor) else None


def _valid_cursor_shape(cursor: ClaudeCursor) -> bool:
    return (
        type(cursor.offset) is int
        and cursor.offset >= 0
        and type(cursor.head_length) is int
        and 0 <= cursor.head_length <= _HEAD_SAMPLE_BYTES
        and isinstance(cursor.head_hash, str)
        and _CURSOR_HASH_RE.fullmatch(cursor.head_hash) is not None
    )


def _native_id_from_bytes(value: bytes) -> str | None:
    native_ids: set[str] = set()
    fragments = value.splitlines()
    for index, fragment in enumerate(fragments):
        try:
            decoded = json.loads(fragment)
        except (UnicodeDecodeError, json.JSONDecodeError):
            is_truncated_tail = index == len(fragments) - 1 and not value.endswith(
                b"\n"
            )
            if not is_truncated_tail:
                continue
            record_type_match = _RECORD_TYPE_RE.search(fragment)
            native_id_match = _NATIVE_ID_RE.search(fragment)
            if record_type_match is None or native_id_match is None:
                continue
            try:
                record_type = json.loads(record_type_match.group(1))
                decoded_native_id = json.loads(native_id_match.group(1))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if record_type not in _RECOGNIZED_RECORD_TYPES:
                continue
            native_id = _nonempty_string(decoded_native_id)
        else:
            if (
                not isinstance(decoded, dict)
                or decoded.get("type") not in _RECOGNIZED_RECORD_TYPES
            ):
                continue
            native_id = _nonempty_string(decoded.get("sessionId"))
        if native_id is not None:
            native_ids.add(native_id)
    return _single_native_id(native_ids)


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


def _metadata_delta(
    records: list[dict[str, Any]], *, native_id: str | None
) -> _MetadataDelta:
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    timestamps: list[float] = []

    for record in records:
        if record.get("type") not in _RECOGNIZED_RECORD_TYPES:
            continue
        if not _is_eligible_record(record):
            continue
        if record.get("type") == "custom-title":
            candidate_title = _nonempty_string(record.get("customTitle"))
            if candidate_title is not None:
                title = candidate_title
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


def _materialize_metadata(
    delta: _MetadataDelta, path: Path, *, native_id: str
) -> _Metadata:
    if delta.timestamps:
        started_at = min(delta.timestamps)
        last_active = max(delta.timestamps)
    else:
        started_at = path.stat().st_mtime
        last_active = started_at
    return _Metadata(
        native_id=native_id,
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
        native_id=baseline.native_id,
        title=delta.title or baseline.title,
        cwd=delta.cwd or baseline.cwd,
        git_branch=delta.git_branch or baseline.git_branch,
        started_at=started_at,
        last_active=last_active,
    )


def _validated_record_native_id(
    records: list[dict[str, Any]],
    *,
    transcript_path: Path,
) -> str | None:
    native_ids = _record_native_ids(records)
    agent_ids: set[str] = set()
    for record in records:
        if record.get("type") not in _RECOGNIZED_RECORD_TYPES:
            continue
        agent_id = _nonempty_string(record.get("agentId"))
        if agent_id is not None:
            agent_ids.add(agent_id)
    subagent_native_id = _subagent_native_id(transcript_path)
    if subagent_native_id is not None:
        expected_agent_id = subagent_native_id.removeprefix("agent-")
        if len(agent_ids) > 1 or (agent_ids and agent_ids != {expected_agent_id}):
            raise ValueError("Claude transcript native identity conflict")
        return subagent_native_id
    if len(native_ids) > 1 and transcript_path.stem in native_ids:
        return transcript_path.stem
    return _single_native_id(native_ids)


def _record_native_ids(records: list[dict[str, Any]]) -> set[str]:
    return {
        native_id
        for record in records
        if record.get("type") in _RECOGNIZED_RECORD_TYPES
        and (native_id := _nonempty_string(record.get("sessionId"))) is not None
    }


def _subagent_native_id(path: Path) -> str | None:
    return path.stem if _SUBAGENT_STEM_RE.fullmatch(path.stem) else None


def _single_native_id(native_ids: set[str]) -> str | None:
    if len(native_ids) > 1:
        raise ValueError("Claude transcript native identity conflict")
    return next(iter(native_ids), None)


def _entrypoint_from_head(value: bytes) -> str | None:
    """Read bounded head metadata without letting later mode overwrite it."""

    records: list[dict[str, Any]] = []
    for fragment in value.splitlines():
        try:
            record = json.loads(fragment)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return _entrypoint_from_records(records)


def _entrypoint_from_records(records: Sequence[dict[str, Any]]) -> str | None:
    # Claude Desktop can change execution mode within one native session (for
    # example, ``claude-desktop`` to ``claude-desktop-3p``).  Entrypoint is
    # launch provenance, so preserve the first recorded value instead of
    # turning a legitimate mode transition into a permanently poisoned scan.
    for record in records:
        value = record.get("entrypoint")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _merge_entrypoints(*values: str | None) -> str | None:
    return next((value for value in values if value is not None), None)


def _is_eligible_record(record: dict[str, Any]) -> bool:
    return not bool(record.get("isSidechain", False)) and not bool(
        record.get("isMeta", False)
    )


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
        visible_parts: list[str] = []
        for block in value:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                visible_parts.append(block["text"])
            elif isinstance(block, dict):
                visible_parts.append(_omitted_content_descriptor(block))
            else:
                visible_parts.append("[content omitted]")
        return "\n".join(visible_parts)
    if isinstance(value, dict) and _contains_binary_content(value):
        return _omitted_content_descriptor(value)
    return _canonical_json(value)


def _omitted_content_descriptor(block: dict[str, Any]) -> str:
    content_type = _safe_descriptor_token(block.get("type"), fallback="content")
    media_type = block.get("media_type") or block.get("mime_type")
    source = block.get("source")
    if media_type is None and isinstance(source, dict):
        media_type = source.get("media_type") or source.get("mime_type")
    normalized_media_type = _safe_descriptor_token(media_type, fallback="")
    if normalized_media_type:
        return f"[{content_type} omitted: {normalized_media_type}]"
    return f"[{content_type} omitted]"


def _safe_descriptor_token(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = _DESCRIPTOR_UNSAFE_RE.sub("-", value.strip())[:64].strip("-")
    return normalized or fallback


def _contains_binary_content(value: Any) -> bool:
    if isinstance(value, dict):
        content_type = _nonempty_string(value.get("type"))
        if content_type is not None and content_type != "text":
            return True
        if any(
            key in value
            for key in ("source", "data", "payload", "media_type", "mime_type")
        ):
            return True
        return any(_contains_binary_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_binary_content(item) for item in value)
    return False


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
    marker_records: set[int] = set()
    marker_occurrences: list[tuple[int, str]] = []
    for index, record in enumerate(records):
        if record.get("type") not in {"user", "assistant"} or not _is_eligible_record(
            record
        ):
            continue
        for text in _record_text_blocks(record):
            for match in _MARKER_CANDIDATE_RE.finditer(text):
                try:
                    payload = decode_bridge_marker(match.group(0), marker_secret)
                except InvalidBridgeMarker:
                    continue
                if payload.target_provider is Provider.CLAUDE:
                    marker_records.add(index)
                    marker_occurrences.append((index, payload.bridge_id))

    marker_ids = {bridge_id for _, bridge_id in marker_occurrences}
    if len(marker_ids) > 1:
        raise ValueError("Claude transcript has conflicting bridge markers")
    marker_bridge_id = next(iter(marker_ids), None)

    if prior_bridge_id is not None:
        if marker_bridge_id is not None and marker_bridge_id != prior_bridge_id:
            raise ValueError("Claude transcript bridge marker changed")
        if prior_kind is OriginKind.BRIDGE_PLACEHOLDER and any(
            index not in marker_records and _is_human_user(record)
            for index, record in enumerate(records)
        ):
            return OriginKind.BRIDGE_CONTINUATION, prior_bridge_id
        return prior_kind, prior_bridge_id

    if marker_bridge_id is not None:
        first_marker_index = min(index for index, _ in marker_occurrences)
        continued = any(
            index > first_marker_index
            and index not in marker_records
            and _is_human_user(record)
            for index, record in enumerate(records)
        )
        kind = (
            OriginKind.BRIDGE_CONTINUATION
            if continued
            else OriginKind.BRIDGE_PLACEHOLDER
        )
        return kind, marker_bridge_id
    return OriginKind.NATIVE, None


def _is_human_user(record: dict[str, Any]) -> bool:
    if record.get("type") != "user" or not _is_eligible_record(record):
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
