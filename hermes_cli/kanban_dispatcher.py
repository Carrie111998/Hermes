"""Single-authority, multi-board Kanban dispatcher runtime."""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Mapping

from hermes_cli.dispatcher_authority import require_dispatcher_lease

_BOARD_FAILURES: dict[str, int] = {}
_QUARANTINED: dict[str, str] = {}
_STUCK_READY_TICKS: dict[str, int] = {}


def _load_kanban_config() -> dict[str, Any]:
    """Load dispatcher config fail-closed; mutation must not run on guesses."""
    from hermes_cli.config import load_config

    cfg = load_config()
    if not isinstance(cfg, dict):
        raise RuntimeError("config root must be a mapping")
    section = cfg.get("kanban", {})
    if not isinstance(section, dict):
        raise RuntimeError("kanban config must be a mapping")
    return dict(section)


def _optional_positive(cfg: Mapping[str, Any], key: str) -> int | None:
    if key not in cfg or cfg[key] is None:
        return None
    value = cfg[key]
    if isinstance(value, bool):
        raise RuntimeError(f"kanban.{key} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"kanban.{key} must be a positive integer") from exc
    if number <= 0:
        raise RuntimeError(f"kanban.{key} must be a positive integer")
    return number


def _nonnegative(cfg: Mapping[str, Any], key: str, default: int = 0) -> int:
    if key not in cfg or cfg[key] is None:
        return default
    value = cfg[key]
    if isinstance(value, bool):
        raise RuntimeError(f"kanban.{key} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"kanban.{key} must be a non-negative integer") from exc
    if number < 0:
        raise RuntimeError(f"kanban.{key} must be a non-negative integer")
    return number


def _strict_bool(cfg: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in cfg:
        return default
    value = cfg[key]
    if not isinstance(value, bool):
        raise RuntimeError(f"kanban.{key} must be true or false")
    return value


def reset_runtime_health_for_tests() -> None:
    _BOARD_FAILURES.clear()
    _QUARANTINED.clear()
    _STUCK_READY_TICKS.clear()


def runtime_health() -> dict[str, Any]:
    return {
        "board_failures": dict(_BOARD_FAILURES),
        "quarantined": dict(_QUARANTINED),
        "stuck_ready_ticks": dict(_STUCK_READY_TICKS),
    }


def run_dispatcher_tick(lease, *, config: Mapping[str, Any] | None = None):
    """Perform one isolated authorized tick on every active non-quarantined board."""
    require_dispatcher_lease(lease, "run_dispatcher_tick")
    from hermes_cli import kanban_db as kb

    cfg = dict(config) if config is not None else _load_kanban_config()
    quarantine_threshold = _optional_positive(cfg, "quarantine_failure_threshold") or 3
    max_spawn = _optional_positive(cfg, "max_spawn")
    max_in_progress = _optional_positive(cfg, "max_in_progress")
    failure_limit = _optional_positive(cfg, "failure_limit") or kb.DEFAULT_SPAWN_FAILURE_LIMIT
    stale_timeout = _nonnegative(cfg, "dispatch_stale_timeout_seconds")
    per_profile = _optional_positive(cfg, "max_in_progress_per_profile")
    reconcile = _strict_bool(cfg, "reconcile_orphans", True)
    default_assignee = str(cfg.get("default_assignee") or "").strip() or None

    boards = kb.list_boards(include_archived=False)
    if not isinstance(boards, list):
        raise RuntimeError("board discovery returned an invalid result")

    results = []
    for entry in boards:
        if not isinstance(entry, Mapping):
            raise RuntimeError("board metadata must be a mapping")
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            raise RuntimeError("board metadata is missing slug")
        if slug in _QUARANTINED:
            results.append((slug, {"status": "quarantined", "reason": _QUARANTINED[slug]}))
            continue
        conn = None
        try:
            conn = kb.connect(board=slug)
            result = kb.dispatch_once_authorized(
                lease,
                conn,
                board=slug,
                max_spawn=max_spawn,
                max_in_progress=max_in_progress,
                failure_limit=failure_limit,
                stale_timeout_seconds=stale_timeout,
                default_assignee=default_assignee,
                max_in_progress_per_profile=per_profile,
                reconcile_orphans=reconcile,
            )
            _BOARD_FAILURES.pop(slug, None)
            try:
                spawnable = kb.has_spawnable_ready(conn) or (
                    kb.review_dispatch_enabled() and kb.has_spawnable_review(conn)
                )
            except Exception:
                # Telemetry must never turn an otherwise successful board tick
                # into a quarantine candidate.
                _STUCK_READY_TICKS.pop(slug, None)
            else:
                if spawnable and not bool(getattr(result, "spawned", None)):
                    _STUCK_READY_TICKS[slug] = _STUCK_READY_TICKS.get(slug, 0) + 1
                else:
                    _STUCK_READY_TICKS.pop(slug, None)
            results.append((slug, result))
        except Exception as exc:
            _STUCK_READY_TICKS.pop(slug, None)
            failures = _BOARD_FAILURES.get(slug, 0) + 1
            _BOARD_FAILURES[slug] = failures
            immediate = isinstance(exc, sqlite3.DatabaseError)
            if immediate or failures >= quarantine_threshold:
                _QUARANTINED[slug] = f"{type(exc).__name__}: {exc}"
            results.append((slug, {"status": "failed", "error": type(exc).__name__}))
        finally:
            if conn is not None:
                conn.close()
    return results


def run_foreground_dispatcher(*, lease, interval: float = 60.0, once: bool = False) -> int:
    """Run until interrupted while retaining one validated lifetime lease."""
    require_dispatcher_lease(lease, "run_foreground_dispatcher")
    while True:
        run_dispatcher_tick(lease)
        if once:
            return 0
        try:
            time.sleep(max(1.0, interval))
        except KeyboardInterrupt:
            return 0
