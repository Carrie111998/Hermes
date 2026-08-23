"""Append-only reconstructable session events.

The active Python agent emits small records; the Rust ``hermes-trace`` utility
validates and projects the resulting JSONL without loading long sessions into
Python memory. Recording is deliberately independent from ShareGPT trajectory
export, which remains a training-data format.

Recording is opt-in and append-only. Files are not rotated automatically;
operators enabling ``HERMES_TRACE_EVENTS`` own retention and cleanup of trace,
lock, and validation-state files under ``$HERMES_HOME/trajectories/events``.
Validation state is a performance hint, not tamper evidence: run
``hermes-trace verify`` before sharing an artifact. A local principal able to
rewrite a trace, its metadata, and its state file is outside this cache contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

_SCHEMA_VERSION = 1
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "mnemonic",
        "password",
        "private_key",
        "private_key_hex",
        "nsec",
        "seed_phrase",
        "secret",
        "token",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_credential",
    "_credentials",
    "_mnemonic",
    "_nsec",
    "_password",
    "_private_key",
    "_private_key_hex",
    "_seed_phrase",
    "_secret",
    "_token",
)
_NSEC_RE = re.compile(r"(?<![023456789acdefghjklmnpqrstuvwxyz])nsec1[023456789acdefghjklmnpqrstuvwxyz]{58}(?![023456789acdefghjklmnpqrstuvwxyz])", re.IGNORECASE)
_TRACE_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)), *(f"lpt{index}" for index in range(1, 10))}
)
_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.Lock] = {}
_validated_sequences: dict[Path, tuple[tuple[int, int, int, int], int, str]] = {}
_TRACE_COMMANDS = frozenset({"verify", "summary", "digest"})
_TRACE_SUMMARY_KEYS = (
    "turns",
    "steps",
    "tool_calls",
    "tool_errors",
    "input_tokens",
    "output_tokens",
)


def resolve_hermes_trace_binary(
    *,
    start: str | Path | None = None,
    binary: str | Path | None = None,
    binary_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> str:
    """Resolve a local hermes-trace build, falling back to PATH lookup."""
    if binary is not None:
        return str(binary)
    env_binary = str((os.environ if environ is None else environ).get("HERMES_TRACE_BINARY", "")).strip()
    if env_binary:
        return env_binary
    name = binary_name or ("hermes-trace.exe" if os.name == "nt" else "hermes-trace")
    anchor = Path(start) if start is not None else Path(__file__)
    if anchor.suffix:
        anchor = anchor.parent
    for root in (anchor, *anchor.parents):
        crate = root / "crates" / "hermes-trace" / "target"
        for profile in ("release", "debug"):
            candidate = crate / profile / name
            if candidate.is_file():
                return str(candidate)
    return path_lookup(name) or name


def _path_lock(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _locks_guard:
        return _path_locks.setdefault(resolved, threading.Lock())


def _acquire_with_timeout(acquire: Callable[[], None], timeout: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            acquire()
            return
        except OSError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("trace lock acquisition timed out") from exc
            time.sleep(min(0.01, remaining))


@contextmanager
def _interprocess_path_lock(path: Path, *, timeout: float = 1.0):
    """Serialize sequence allocation across processes sharing one trace path."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _acquire_with_timeout(lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1), timeout)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            _acquire_with_timeout(lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB), timeout)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _legacy_trace_filename(session_id: str) -> str:
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', session_id)}.jsonl"


def _hashed_trace_filename(session_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{safe_id}~{digest}.jsonl"


def _trace_filename(session_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    reserved_on_windows = safe_id.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_STEMS
    if safe_id != session_id or safe_id != safe_id.lower() or reserved_on_windows:
        return _hashed_trace_filename(session_id)
    return _legacy_trace_filename(session_id)


def _trace_owner_hint(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if raw.strip():
                    event = json.loads(raw)
                    owner = event.get("session_id") if isinstance(event, Mapping) else None
                    return str(owner) if owner is not None else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _trace_path(events_dir: Path, session_id: str) -> Path:
    preferred = events_dir / _trace_filename(session_id)
    legacy = events_dir / _legacy_trace_filename(session_id)
    if preferred.exists():
        owner = _trace_owner_hint(preferred)
        return preferred if owner is None or owner == session_id else events_dir / _hashed_trace_filename(session_id)
    if legacy != preferred and legacy.exists() and _trace_owner_hint(legacy) == session_id:
        return legacy
    return preferred


def _is_sensitive(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _trace_events_enabled(environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get("HERMES_TRACE_EVENTS", "")
    return str(value).strip().lower() in _TRACE_ENABLED_VALUES


def _redact_trace_text(value: str) -> str:
    from agent.redact import redact_sensitive_text

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(redact_trace_data(parsed), ensure_ascii=False, separators=(",", ":"))

    redacted = redact_sensitive_text(
        value,
        force=True,
        redact_url_credentials=True,
    )
    return _NSEC_RE.sub(_REDACTED, redacted)


def redact_trace_data(value: Any) -> Any:
    """Return a JSON-safe copy with credential-shaped fields removed."""
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive(key) else redact_trace_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_trace_data(item) for item in value]
    if isinstance(value, str):
        return _redact_trace_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


class SessionEventRecorder:
    """Append validated, monotonic events to one session JSONL file."""

    def __init__(
        self,
        path: str | Path,
        session_id: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        self.path = Path(path)
        self._cache_key = self.path.resolve()
        self._checkpoint_path = self.path.with_name(f"{self.path.name}.state.json")
        self.session_id = session_id
        self._clock = clock
        self._lock = _path_lock(self.path)
        with self._lock:
            with _interprocess_path_lock(self.path):
                self._seq = self._last_sequence()

    def _file_signature(self) -> tuple[int, int, int, int]:
        stat = self.path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _read_checkpoint(self, signature: tuple[int, int, int, int]) -> int | None:
        try:
            checkpoint = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
            if (
                isinstance(checkpoint, Mapping)
                and checkpoint.get("version") == 1
                and checkpoint.get("session_id") == self.session_id
                and checkpoint.get("signature") == list(signature)
                and type(checkpoint.get("seq")) is int
                and checkpoint["seq"] >= 0
            ):
                final_sequence = self._final_sequence()
                return checkpoint["seq"] if checkpoint["seq"] == final_sequence else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return None

    def _write_checkpoint(self, signature: tuple[int, int, int, int], sequence: int) -> None:
        try:
            self._checkpoint_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "signature": list(signature),
                        "seq": sequence,
                        "session_id": self.session_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _last_sequence(self, *, validate_history: bool = True) -> int:
        if not self.path.exists():
            _validated_sequences.pop(self._cache_key, None)
            return 0
        if validate_history:
            signature = self._file_signature()
            cached = _validated_sequences.get(self._cache_key)
            if cached is not None and cached[0] == signature and cached[2] == self.session_id:
                return cached[1]
            checkpoint_sequence = self._read_checkpoint(signature)
            if checkpoint_sequence is not None:
                _validated_sequences[self._cache_key] = (signature, checkpoint_sequence, self.session_id)
                return checkpoint_sequence
            last = 0
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, raw in enumerate(handle, start=1):
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                        if not isinstance(event, Mapping):
                            raise TypeError
                        seq = event["seq"]
                        owner = event["session_id"]
                        if type(seq) is not int or not isinstance(owner, str):
                            raise TypeError
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError(f"invalid trace at line {line_number}") from exc
                    if seq <= 0:
                        raise ValueError(f"non-positive trace sequence at line {line_number}")
                    if owner != self.session_id:
                        raise ValueError(f"session mismatch at line {line_number}")
                    if seq <= last:
                        raise ValueError(f"non-monotonic trace at line {line_number}")
                    last = seq
            _validated_sequences[self._cache_key] = (signature, last, self.session_id)
            self._write_checkpoint(signature, last)
            return last

        signature = self._file_signature()
        cached = _validated_sequences.get(self._cache_key)
        if cached is not None and cached[0] == signature and cached[2] == self.session_id:
            return cached[1]
        checkpoint_sequence = self._read_checkpoint(signature)
        if checkpoint_sequence is not None:
            _validated_sequences[self._cache_key] = (signature, checkpoint_sequence, self.session_id)
            return checkpoint_sequence
        return self._last_sequence(validate_history=True)

    def _final_sequence(self) -> int:
        raw = self._last_nonempty_line()
        if raw is None:
            return 0
        try:
            event = json.loads(raw)
            if not isinstance(event, Mapping):
                raise TypeError
            seq = event["seq"]
            owner = event["session_id"]
            if type(seq) is not int or not isinstance(owner, str):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid final trace record") from exc
        if seq <= 0:
            raise ValueError("non-positive final trace sequence")
        if owner != self.session_id:
            raise ValueError("final trace session mismatch")
        return seq

    def _last_nonempty_line(self, *, chunk_size: int = 8192) -> bytes | None:
        """Read the last non-empty JSONL record without scanning trace history."""
        with self.path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                pending = handle.read(read_size) + pending
                lines = pending.splitlines()
                if position > 0:
                    pending = lines.pop(0) if lines else pending
                for line in reversed(lines):
                    if line.strip():
                        return line
            return pending if pending.strip() else None

    def append(
        self,
        event_type: str,
        data: Mapping[str, Any] | None,
        *,
        turn: int | None = None,
        step: int | None = None,
    ) -> dict[str, Any]:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        with self._lock:
            with _interprocess_path_lock(self.path):
                self._seq = max(self._seq, self._last_sequence(validate_history=False)) + 1
                event: dict[str, Any] = {
                    "schema_version": _SCHEMA_VERSION,
                    "seq": self._seq,
                    "time": float(self._clock()),
                    "session_id": self.session_id,
                    "type": event_type,
                }
                if turn is not None:
                    event["turn"] = turn
                if step is not None:
                    event["step"] = step
                event["data"] = redact_trace_data(data or {})
                serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(serialized)
                    handle.write("\n")
                    handle.flush()
                    stat = os.fstat(handle.fileno())
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                _validated_sequences[self._cache_key] = (signature, self._seq, self.session_id)
                self._write_checkpoint(signature, self._seq)
                return event


def configure_agent_event_recorder(
    agent: Any,
    *,
    enabled: bool | None = None,
    home: str | Path | None = None,
) -> SessionEventRecorder | None:
    """Attach one profile-scoped recorder when trajectory events are enabled."""
    if enabled is None:
        enabled = _trace_events_enabled()
    if not enabled:
        agent._session_event_recorder = None
        return None

    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id:
        return None
    current = getattr(agent, "_session_event_recorder", None)
    if isinstance(current, SessionEventRecorder) and current.session_id == session_id:
        return current

    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    events_dir = Path(home) / "trajectories" / "events"
    path = _trace_path(events_dir, session_id)
    recorder = SessionEventRecorder(path, session_id)
    agent._session_event_recorder = recorder
    return recorder


def start_agent_turn_trace(
    agent: Any,
    *,
    turn_id: str,
    user_message: str,
    enabled: bool | None = None,
    home: str | Path | None = None,
) -> bool:
    """Emit turn start and report whether every requested durable sink succeeded."""
    trace_requested = bool(enabled) if enabled is not None else _trace_events_enabled()
    recorder = configure_agent_event_recorder(agent, enabled=enabled, home=home)
    agent._trajectory_turn_number = int(
        getattr(agent, "_trajectory_turn_number", 0) or 0
    ) + 1
    agent._trajectory_step_number = None
    delivered = emit_agent_event(
        agent,
        "turn/start",
        {
            "turn_id": turn_id,
            "user_message": user_message[:1000],
            "platform": getattr(agent, "platform", None),
            "provider": getattr(agent, "provider", None),
        },
    )
    return delivered and (not trace_requested or recorder is not None)


def start_agent_step_trace(
    agent: Any,
    *,
    step: int,
    previous_tools: list[dict[str, Any]] | None = None,
) -> bool:
    """Advance the live step coordinate and record its prior tool context."""
    agent._trajectory_step_number = step
    return emit_agent_event(
        agent,
        "step/start",
        {"previous_tools": previous_tools or []},
    )


def emit_agent_event(agent: Any, event_type: str, data: Mapping[str, Any] | None) -> bool:
    """Emit without affecting the turn; True means every requested sink delivered."""
    safe_data = redact_trace_data(data or {})
    memory_delivered: bool | None = None
    memory_manager = getattr(agent, "_memory_manager", None)
    notify = getattr(memory_manager, "on_session_event_all", None)
    if callable(notify):
        try:
            memory_delivered = notify(
                event_type,
                safe_data,
                session_id=str(getattr(agent, "session_id", "") or ""),
                turn=getattr(agent, "_trajectory_turn_number", None),
                step=getattr(agent, "_trajectory_step_number", None),
            ) is True
        except Exception:
            memory_delivered = False
    recorder = getattr(agent, "_session_event_recorder", None)
    if recorder is None:
        return memory_delivered is True
    try:
        recorder.append(
            event_type,
            safe_data,
            turn=getattr(agent, "_trajectory_turn_number", None),
            step=getattr(agent, "_trajectory_step_number", None),
        )
        return memory_delivered is not False
    except Exception:
        # Observability is fail-open: a trace disk or serialization failure must
        # never alter the conversation loop, tool execution, or persistence.
        agent._session_event_recorder = None
        return False


def _run_hermes_trace_command(
    command: str,
    trace_path: str | Path,
    *,
    binary: str | Path | None = None,
    timeout: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    if command not in _TRACE_COMMANDS:
        raise ValueError(f"unsupported hermes-trace command: {command}")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be a finite positive number")
    resolved_binary = resolve_hermes_trace_binary(binary=binary)
    completed = runner(
        [resolved_binary, command, str(Path(trace_path))],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "hermes-trace failed").strip()
        raise RuntimeError(f"hermes-trace {command} failed: {message}")
    return str(completed.stdout).strip()


def verify_trace_file(
    trace_path: str | Path,
    *,
    binary: str | Path | None = None,
    timeout: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> bool:
    _run_hermes_trace_command(
        "verify",
        trace_path,
        binary=binary,
        timeout=timeout,
        runner=runner,
    )
    return True


def summarize_trace_file(
    trace_path: str | Path,
    *,
    binary: str | Path | None = None,
    timeout: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, int]:
    output = _run_hermes_trace_command(
        "summary",
        trace_path,
        binary=binary,
        timeout=timeout,
        runner=runner,
    )
    summary = json.loads(output)
    if not isinstance(summary, Mapping):
        raise ValueError("hermes-trace summary output must be a JSON object")
    validated: dict[str, int] = {}
    for key in _TRACE_SUMMARY_KEYS:
        if key not in summary:
            raise ValueError(f"hermes-trace summary field {key!r} is required")
        value = summary[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"hermes-trace summary field {key!r} must be a non-negative integer")
        validated[key] = value
    return validated


def digest_trace_file(
    trace_path: str | Path,
    *,
    binary: str | Path | None = None,
    timeout: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    digest = _run_hermes_trace_command(
        "digest",
        trace_path,
        binary=binary,
        timeout=timeout,
        runner=runner,
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("hermes-trace digest output must be 64 lowercase hex characters")
    return digest
