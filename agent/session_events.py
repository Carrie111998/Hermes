"""Append-only reconstructable session events.

The active Python agent emits small records; the Rust ``hermes-trace`` utility
validates and projects the resulting JSONL without loading long sessions into
Python memory. Recording is deliberately independent from ShareGPT trajectory
export, which remains a training-data format.
"""

from __future__ import annotations

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
        "password",
        "secret",
        "token",
    }
)
_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.Lock] = {}
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


def _is_sensitive(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("_api_key", "_password", "_secret", "_token"))


def redact_trace_data(value: Any) -> Any:
    """Return a JSON-safe copy with credential-shaped fields removed."""
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive(key) else redact_trace_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_trace_data(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
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
        self.session_id = session_id
        self._clock = clock
        self._lock = _path_lock(self.path)
        self._seq = self._last_sequence()

    def _last_sequence(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                    seq = int(event["seq"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid trace at line {line_number}") from exc
                if seq <= last:
                    raise ValueError(f"non-monotonic trace at line {line_number}")
                last = seq
        return last

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
            self._seq += 1
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
            return event


def configure_agent_event_recorder(
    agent: Any,
    *,
    enabled: bool | None = None,
    home: str | Path | None = None,
) -> SessionEventRecorder | None:
    """Attach one profile-scoped recorder when trajectory events are enabled."""
    if enabled is None:
        enabled = os.environ.get("HERMES_TRACE_EVENTS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if not enabled:
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
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    recorder = SessionEventRecorder(
        Path(home) / "trajectories" / "events" / f"{safe_id}.jsonl",
        session_id,
    )
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
    trace_requested = enabled is True or (
        enabled is None
        and os.environ.get("HERMES_TRACE_EVENTS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
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
    """Emit to memory providers and the optional trace without affecting the turn."""
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
