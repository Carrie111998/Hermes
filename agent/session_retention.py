"""Opt-in lifecycle for explicitly owned temporary sessions.

Delegate children are marked at creation with ``model_config._delegate_from``.
External one-shot workers are marked with ``model_config._ephemeral``.
``source`` tags (including ``tool``) are visibility markers only and never
authorize deletion.

Cleanup is fail-closed: a row is released only when it is explicitly owned,
terminal, and its useful result has been durably accepted. Nested owned
descendants must also be ready; otherwise the whole subtree is retained.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

RETENTION_KEEP = "keep"
RETENTION_ARCHIVE = "archive"
RETENTION_DELETE = "delete"
VALID_RETENTION = frozenset({RETENTION_KEEP, RETENTION_ARCHIVE, RETENTION_DELETE})

EPHEMERAL_KEY = "_ephemeral"
RESULT_ACCEPTED_KEY = "_result_accepted"
DELEGATE_FROM_KEY = "_delegate_from"


def parse_model_config(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def session_model_config(session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not session:
        return {}
    return parse_model_config(session.get("model_config"))


def is_explicitly_temporary(session_or_cfg: Optional[Dict[str, Any]]) -> bool:
    """True only for explicitly owned temporary rows — never inferred from source."""
    if not session_or_cfg:
        return False
    cfg = session_or_cfg
    if "model_config" in session_or_cfg or "source" in session_or_cfg:
        cfg = session_model_config(session_or_cfg)
    if cfg.get(EPHEMERAL_KEY) is True:
        return True
    delegate_from = cfg.get(DELEGATE_FROM_KEY)
    return bool(isinstance(delegate_from, str) and delegate_from.strip())


def is_result_accepted(session: Optional[Dict[str, Any]]) -> bool:
    return session_model_config(session).get(RESULT_ACCEPTED_KEY) is True


def is_session_terminal(session: Optional[Dict[str, Any]]) -> bool:
    if not session:
        return False
    return session.get("ended_at") is not None


def resolve_retention_policy(config: Optional[Dict[str, Any]] = None) -> str:
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            config = {}
    raw = ""
    if isinstance(config, dict):
        raw = (config.get("delegation") or {}).get("completed_session_retention") or ""
    policy = str(raw).strip().lower() or RETENTION_KEEP
    if policy not in VALID_RETENTION:
        logger.warning(
            "Unknown delegation.completed_session_retention %r; using keep",
            raw,
        )
        return RETENTION_KEEP
    return policy


def mark_result_accepted(session_db, session_id: str) -> None:
    if not session_db or not session_id:
        return
    session_db.patch_session_model_config(session_id, {RESULT_ACCEPTED_KEY: True})


def child_session_ids_from_result(result: Any) -> List[str]:
    """Collect child session ids from a sync or persisted async result payload."""
    found: List[str] = []
    seen = set()

    def _add(sid: Any) -> None:
        if isinstance(sid, str) and sid.strip() and sid not in seen:
            seen.add(sid)
            found.append(sid)

    if isinstance(result, dict):
        _add(result.get("child_session_id"))
        rows = result.get("results")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    _add(row.get("child_session_id"))
    return found


def _sessions_dir():
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "sessions"
    except Exception:
        return None


def _owned_descendant_ids(session_db, session_id: str) -> List[str]:
    from hermes_state import _collect_delegate_child_ids

    try:
        with session_db._read_ctx() as conn:
            return list(_collect_delegate_child_ids(conn, [session_id]))
    except Exception:
        logger.debug("owned descendant walk failed for %s", session_id, exc_info=True)
        return []


def _ready_for_release(session: Optional[Dict[str, Any]]) -> bool:
    if not session:
        return False
    if session.get("pinned"):
        return False
    if not is_explicitly_temporary(session):
        return False
    if not is_session_terminal(session):
        return False
    if not is_result_accepted(session):
        return False
    return True


def apply_completed_retention(
    session_db,
    session_id: str,
    *,
    policy: Optional[str] = None,
    force_delete: bool = False,
    sessions_dir=None,
) -> str:
    """Release one owned temporary session after its result is accepted.

    Returns ``skipped``, ``kept``, ``archived``, or ``deleted``. Missing rows
    are ``deleted`` (idempotent). Fail-closed on any uncertainty.
    """
    if not session_db or not session_id:
        return "skipped"
    session = session_db.get_session(session_id)
    if session is None:
        return "deleted"
    if not _ready_for_release(session):
        return "skipped"

    descendants = _owned_descendant_ids(session_db, session_id)
    for child_id in descendants:
        child = session_db.get_session(child_id)
        if child is None:
            continue
        if not _ready_for_release(child):
            return "skipped"

    effective = RETENTION_DELETE if force_delete else (policy or resolve_retention_policy())
    target_dir = sessions_dir if sessions_dir is not None else _sessions_dir()

    if effective == RETENTION_KEEP:
        return "kept"

    # Child-first: descendants, then the requested row.
    ordered = list(reversed(descendants)) + [session_id]
    if effective == RETENTION_ARCHIVE:
        for sid in ordered:
            try:
                session_db.set_session_archived(sid, True)
            except Exception:
                logger.debug("archive temporary session %s failed", sid, exc_info=True)
                return "skipped"
        return "archived"

    if effective == RETENTION_DELETE:
        for sid in ordered:
            try:
                session_db.delete_session(sid, sessions_dir=target_dir)
            except Exception:
                logger.debug("delete temporary session %s failed", sid, exc_info=True)
                return "skipped"
        return "deleted"

    return "skipped"


def accept_temporary_child_result(
    session_db,
    session_id: str,
    *,
    policy: Optional[str] = None,
    sessions_dir=None,
) -> str:
    """Stamp durable acceptance, then apply the configured retention policy."""
    if not session_db or not session_id:
        return "skipped"
    session = session_db.get_session(session_id)
    if session is None:
        return "deleted"
    if not is_explicitly_temporary(session):
        return "skipped"
    if not is_session_terminal(session):
        try:
            session_db.end_session(session_id, "agent_close")
        except Exception:
            logger.debug("end_session before retention failed for %s", session_id, exc_info=True)
            return "skipped"
    mark_result_accepted(session_db, session_id)
    return apply_completed_retention(
        session_db,
        session_id,
        policy=policy,
        sessions_dir=sessions_dir,
    )


def accept_child_sessions(
    session_db,
    session_ids: Iterable[str],
    *,
    policy: Optional[str] = None,
    sessions_dir=None,
) -> List[str]:
    outcomes = []
    for sid in session_ids:
        outcomes.append(
            accept_temporary_child_result(
                session_db,
                sid,
                policy=policy,
                sessions_dir=sessions_dir,
            )
        )
    return outcomes


def sweep_accepted_temporary_sessions(session_db, *, policy: Optional[str] = None) -> int:
    """Retry cleanup for owned rows already stamped accepted (crash recovery)."""
    if session_db is None:
        return 0
    effective = policy or resolve_retention_policy()
    if effective == RETENTION_KEEP:
        return 0

    try:
        with session_db._read_ctx() as conn:
            fetched = conn.execute(
                """
                SELECT id, model_config, ended_at, pinned
                FROM sessions
                WHERE ended_at IS NOT NULL
                """
            ).fetchall()
            rows = [
                {
                    "id": row["id"] if hasattr(row, "keys") else row[0],
                    "model_config": row["model_config"] if hasattr(row, "keys") else row[1],
                    "ended_at": row["ended_at"] if hasattr(row, "keys") else row[2],
                    "pinned": row["pinned"] if hasattr(row, "keys") else row[3],
                }
                for row in fetched
            ]
    except Exception:
        logger.debug("temporary session sweep listing failed", exc_info=True)
        return 0

    released = 0
    for row in rows:
        sid = row.get("id")
        if not sid or not _ready_for_release(row):
            continue
        outcome = apply_completed_retention(session_db, sid, policy=effective)
        if outcome in ("archived", "deleted"):
            released += 1
    return released


def accept_from_delegation_result(
    session_db,
    result: Any,
    *,
    policy: Optional[str] = None,
) -> List[str]:
    return accept_child_sessions(
        session_db,
        child_session_ids_from_result(result),
        policy=policy,
    )


def default_session_db():
    from hermes_state import SessionDB
    return SessionDB()
