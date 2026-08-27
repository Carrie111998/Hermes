"""Bounded, non-destructive readiness probes for authenticated health surfaces."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home


_DISK_DEGRADED_PERCENT = 90.0
_MAX_PID = (1 << 31) - 1
_MAX_START_TIME = (1 << 63) - 1


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _valid_writer_identity(pid: Any, start_time: Any) -> bool:
    """Return True only for a complete, non-boolean process fingerprint."""
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and 0 < pid <= _MAX_PID
        and isinstance(start_time, int)
        and not isinstance(start_time, bool)
        and 0 < start_time <= _MAX_START_TIME
    )


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    try:
        # A readiness probe must never compete with normal state writers. A
        # read-only schema query still catches unreadable/corrupt databases
        # without taking a write reservation on every health poll.
        # ``closing(...)`` is required: sqlite3's connection context manager
        # only commits/rolls back — it never closes, so a bare ``with
        # sqlite3.connect(...)`` leaks one connection (and its fds) per
        # health poll in the long-running gateway (#69678/#69567 bug class).
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return _check("ok")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
        status = "degraded" if used_pct >= _DISK_DEGRADED_PERCENT else "ok"
        return _check(status, used_percent=used_pct, free_bytes=usage.free)
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_gateway(runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime_status.get("gateway_state") or "unknown")
    platforms = runtime_status.get("platforms")
    connected = 0
    configured = 0
    unhealthy = 0
    if isinstance(platforms, dict):
        process_pid = runtime_status.get("pid")
        process_start = runtime_status.get("start_time")
        runtime_has_any_identity = process_pid is not None or process_start is not None
        runtime_has_writer_identity = _valid_writer_identity(
            process_pid, process_start
        )
        for value in platforms.values():
            if not isinstance(value, dict):
                continue
            # Runtime files can outlive a process. A provenance stamp is usable
            # only as the complete, exact writer identity pair. Unstamped
            # legacy entries retain their historical behavior only when the
            # enclosing runtime record also lacks a complete identity.
            writer_pid = value.get("writer_pid")
            writer_start = value.get("writer_start_time")
            has_any_writer_provenance = (
                writer_pid is not None or writer_start is not None
            )
            has_complete_writer_provenance = _valid_writer_identity(
                writer_pid, writer_start
            )
            if runtime_has_writer_identity and not (
                has_complete_writer_provenance
                and writer_pid is not None
                and writer_start is not None
                and writer_pid == process_pid
                and writer_start == process_start
            ):
                continue
            # Legacy compatibility is available only when BOTH the enclosing
            # runtime and the platform entry are entirely unstamped. A partial
            # or malformed enclosing fingerprint cannot prove current ownership.
            if not runtime_has_writer_identity and (
                runtime_has_any_identity or has_any_writer_provenance
            ):
                continue
            configured += 1
            platform_state = str(
                value.get("state") or value.get("status") or ""
            ).lower()
            if platform_state in {"connected", "running", "ok"}:
                connected += 1
            elif platform_state in {"retrying", "fatal", "paused"}:
                unhealthy += 1
    status = (
        "ok"
        if state in {"running", "draining"} and unhealthy == 0
        else "degraded"
    )
    return _check(
        status,
        state=state,
        connected_platforms=connected,
        unhealthy_platforms=unhealthy,
        platforms=configured,
    )


def _probe_session_store(
    runtime_status: dict[str, Any], state_db_probe: dict[str, Any]
) -> dict[str, Any]:
    """Report the running gateway cache state, not an independent reopen."""
    runtime_store = runtime_status.get("session_store")
    if isinstance(runtime_store, dict):
        state = str(runtime_store.get("status") or "unknown")
        if state in {"ok", "unavailable", "retrying"}:
            return _check(state)
    # Older gateways do not publish a cache state. Preserve their readiness
    # behavior until their process restarts onto a version that does.
    return _check("ok" if state_db_probe.get("status") == "ok" else "unavailable")


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return bounded readiness diagnostics without mutating runtime state.

    The detailed health endpoint is authenticated. Even there, probes expose
    status and counts only: never config values, credentials, paths, commands,
    queue payloads, or exception messages.
    """
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    state_db_probe = _probe_state_db(home)
    checks = {
        "state_db": state_db_probe,
        "session_store": _probe_session_store(runtime, state_db_probe),
        "config": _probe_config(home),
        "model": _check("ok" if str(configured_model or "").strip() else "degraded"),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    overall = "ok" if all(item.get("status") == "ok" for item in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


__all__ = ["collect_runtime_readiness"]
