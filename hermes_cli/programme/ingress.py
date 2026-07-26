"""Universal programme admission at new conversation ingress.

Policy:

* Every new spend-incurring turn re-reads the existing CS-01 programme state.
* ``RUNNING`` admits the turn.
* Any non-running state rejects the turn before dispatch, verdict, cost, side
  effect, session, or accounting setup.
* State-read failures fail closed.
* The decision is never cached across turns.
* Turns admitted while ``RUNNING`` finish naturally if the programme changes
  to ``PAUSED`` while they are already in flight.

The Kanban ``admit_task`` check remains unchanged. This module is the second
enforcement point for direct, profile, gateway, API, TUI, and dashboard turns.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from hermes_cli.cost.errors import ProgrammeGatePausedAtIngress
from hermes_cli.programme import init as programme_init
from hermes_cli.programme.gate import get_state
from hermes_cli.sqlite_util import retrying_write_txn


logger = logging.getLogger(__name__)

_MIGRATED_PATHS: set[str] = set()
_MIGRATION_LOCK = threading.RLock()

_INGRESS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS ingress_rejection_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        route        TEXT NOT NULL,
        profile      TEXT,
        session_id   TEXT,
        task_id_hint TEXT,
        state        TEXT NOT NULL,
        reason       TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ingress_rejection_ts
        ON ingress_rejection_log(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ingress_rejection_route
        ON ingress_rejection_log(route)
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the shared programme DB, respecting explicit test overrides."""
    return programme_init.resolve_db_path(
        Path(db_path).expanduser() if db_path is not None else None
    )


def migrate(db_path: str | Path | None = None) -> None:
    """Create the append-only rejection log without altering programme state."""
    path = resolve_db_path(db_path)
    conn = programme_init.connect(path)
    try:
        with retrying_write_txn(conn):
            for statement in _INGRESS_SCHEMA:
                conn.execute(statement)
    finally:
        conn.close()
    _MIGRATED_PATHS.add(str(path.resolve()))


def ensure_migrated(db_path: str | Path | None = None) -> None:
    """Lazily create the rejection log once per selected Hermes home."""
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    if key in _MIGRATED_PATHS:
        return
    with _MIGRATION_LOCK:
        if key not in _MIGRATED_PATHS:
            migrate(path)


def resolve_turn_attribution(
    *,
    route: Optional[str] = None,
    profile: Optional[str] = None,
    platform: Optional[str] = None,
    task_id_hint: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Return stable route/profile labels for ingress and accounting rows."""
    resolved_profile = str(profile).strip().lower() if profile else None
    if resolved_profile is None:
        try:
            from hermes_cli.profiles import get_active_profile_name

            resolved_profile = (
                str(get_active_profile_name() or "").strip().lower() or None
            )
        except Exception:
            resolved_profile = None

    if route:
        return str(route).strip().lower() or "other", resolved_profile

    normalized_platform = str(platform or "cli").strip().lower()
    if task_id_hint:
        resolved_route = "kanban_claim"
    elif normalized_platform == "cli":
        resolved_route = (
            "forge_direct" if resolved_profile == "forge" else "direct_cli"
        )
    elif normalized_platform in {"tui", "tui_slash", "slash_worker"}:
        resolved_route = "tui_slash"
    elif normalized_platform in {"dashboard", "web_dashboard"}:
        resolved_route = "dashboard"
    elif normalized_platform in {
        "api",
        "api_server",
        "server",
        "codex_app",
        "codex_app_server",
    }:
        resolved_route = "api_server"
    elif normalized_platform in {
        "telegram",
        "discord",
        "whatsapp",
        "signal",
        "matrix",
        "slack",
        "gateway",
    }:
        resolved_route = "gateway"
    else:
        resolved_route = "other"
    return resolved_route, resolved_profile


def _write_rejection(
    *,
    route: str,
    profile: Optional[str],
    session_id: Optional[str],
    task_id_hint: Optional[str],
    state: str,
    reason: Optional[str],
    db_path: str | Path | None,
) -> None:
    ensure_migrated(db_path)
    conn = programme_init.connect(resolve_db_path(db_path))
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                INSERT INTO ingress_rejection_log (
                    ts, route, profile, session_id, task_id_hint, state, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    programme_init.utc_now(),
                    route,
                    profile,
                    session_id,
                    task_id_hint,
                    state,
                    reason,
                ),
            )
    finally:
        conn.close()


def admit_new_turn(
    *,
    route: str,
    profile: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id_hint: Optional[str] = None,
    db_path: str | Path | None = None,
) -> None:
    """Admit a new turn or raise before any dispatch or spend.

    The live programme state is re-read on every call. Missing or unreadable
    state fails closed. A rejection-log failure is observable but can never
    turn a rejection into admission.
    """
    normalized_route = str(route or "").strip().lower() or "other"
    normalized_profile = (
        str(profile).strip().lower() if profile is not None else None
    )
    resolved_path = resolve_db_path(db_path)
    # A genuinely fresh Hermes home has no board yet. Preserve the existing
    # CS-01 bootstrap contract by creating its default RUNNING singleton once.
    # An existing DB with a missing table/row is not repaired here: that is an
    # unreadable admission state and must fail closed.
    if not resolved_path.exists():
        try:
            programme_init.migrate(resolved_path)
        except Exception as exc:
            raise ProgrammeGatePausedAtIngress(
                "ingress: programme state unreadable "
                f"({type(exc).__name__}: {exc})"
            ) from exc
    try:
        state = get_state(
            db_path=resolved_path,
            migrate_if_missing=False,
        )
    except Exception as exc:
        raise ProgrammeGatePausedAtIngress(
            "ingress: programme state unreadable "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    if state is None:  # pragma: no cover - current reader raises instead.
        raise ProgrammeGatePausedAtIngress(
            "ingress: programme state missing (fail-closed)."
        )
    if state.state == "RUNNING":
        return

    reason = state.reason or state.state.lower()
    error = ProgrammeGatePausedAtIngress(
        f"ingress: programme is {state.state} ({reason}); "
        f"route={normalized_route} profile={normalized_profile}"
    )
    try:
        _write_rejection(
            route=normalized_route,
            profile=normalized_profile,
            session_id=(
                str(session_id) if session_id is not None else None
            ),
            task_id_hint=(
                str(task_id_hint) if task_id_hint is not None else None
            ),
            state=state.state,
            reason=state.reason,
            db_path=db_path,
        )
    except Exception as log_exc:
        logger.warning(
            "Ingress rejection log write failed; turn remains rejected: %s: %s",
            type(log_exc).__name__,
            log_exc,
        )
    raise error


def list_recent_rejections(
    limit: int = 20,
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, object]]:
    """Return newest rejection rows for CLI observability."""
    ensure_migrated(db_path)
    conn = programme_init.connect(resolve_db_path(db_path))
    try:
        rows = conn.execute(
            """
            SELECT id, ts, route, profile, session_id, task_id_hint, state,
                   reason
              FROM ingress_rejection_log
             ORDER BY id DESC
             LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "admit_new_turn",
    "ensure_migrated",
    "list_recent_rejections",
    "migrate",
    "resolve_db_path",
    "resolve_turn_attribution",
]
