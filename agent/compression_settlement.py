"""Compression late settlement — DB-authoritative projection for late ACKs.

Bounded module owning the gateway-side projection logic when a compression
attempt completes after the original waiter has timed out or disappeared.
Bridges DB authority (SessionDB) with gateway state (_sessions dict,
_restart_slash_worker).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def _find_target_session(
    sessions: Dict[str, Any],
    attempt_id: str,
    parent_id: str,
) -> tuple[str | None, Dict[str, Any] | None]:
    """Locate the gateway session owning this attempt.

    First tries exact attempt_id match, then falls back to parent_id match.
    Returns (sid, session_dict) or (None, None).
    """
    # Pass 1: exact attempt_id match
    for sid, sess in sessions.items():
        try:
            if sess.get("_active_compression_attempt_id") == attempt_id:
                return sid, sess
        except Exception:
            continue
    # Pass 2: parent session_key match
    for sid, sess in sessions.items():
        try:
            if str(sess.get("session_key") or "") == parent_id:
                return sid, sess
        except Exception:
            continue
    return None, None


def _apply_projection(
    target_sid: str,
    target_sess: Dict[str, Any],
    child_id: str,
    conv: list,
    restart_slash_fn: Callable,
) -> None:
    """Apply canonical re-anchor to a gateway session (mirrors _sync_session_key_after_compress)."""
    with target_sess.get("history_lock", contextlib.nullcontext()):
        target_sess["session_key"] = child_id
        if isinstance(conv, list):
            target_sess["history"] = list(conv)
        try:
            target_sess["history_version"] = int(target_sess.get("history_version", 0) or 0) + 1
        except Exception:
            pass
        # Invalidate stale drain claims (mirrors :7224)
        target_sess["_queued_prompt_generation"] = int(
            target_sess.get("_queued_prompt_generation", 0)
        ) + 1
        target_sess.pop("_active_compression_attempt_id", None)
    # Restart slash worker (mirrors :7230-7233)
    try:
        restart_slash_fn(target_sid, target_sess)
    except Exception:
        pass


def resolve_late_compression_ack(
    attempt_id: str,
    frame: dict,
    sessions: Dict[str, Any],
    restart_slash_fn: Callable,
) -> None:
    """DB-authoritative late projection for committed attempt when waiter gone.

    Called from HostSupervisor._handle_late_compression_ack when:
    - waiter queue is already removed (q is None)
    - route_name == session.compress
    - frame type is control.ack or control.error

    Steps: DB lookup → committed? → tip check → locate session → project.
    Duplicate/late ACKs are idempotent.
    """
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        attempt = db.get_compression_attempt(attempt_id)
        if not attempt:
            logger.info("late compression attempt not found: %s", attempt_id)
            return
        state = str(attempt.get("state") or "")
        if state != "committed":
            logger.info("late compression attempt %s state=%s not committed; no projection",
                        attempt_id, state)
            return
        child_id = str(attempt.get("child_session_key") or "")
        parent_id = str(attempt.get("parent_session_id") or "")
        if not child_id or not parent_id:
            return
        parent_row = db.get_session(parent_id)
        if parent_row is None:
            logger.warning("late compression parent not found %s for attempt %s",
                           parent_id, attempt_id)
            return
        family = str(parent_row.get("session_key") or attempt.get("session_key") or "")
        source = str(parent_row.get("source") or "")

        # Stale check: tip for same source+family must still be this child
        try:
            tip = db.find_latest_gateway_session_for_peer(
                source=source, session_key=family
            ) if family else None
            tip_id = None
            if isinstance(tip, dict):
                tip_id = str(tip.get("id") or tip.get("session_id") or "")
            elif tip is not None:
                tip_id = str(getattr(tip, "id", "") or getattr(tip, "session_id", "") or "")
            if tip_id and tip_id != child_id:
                logger.info("late compression stale: attempt %s child %s tip %s family %s",
                            attempt_id, child_id, tip_id, family)
                return
        except Exception as _tip_exc:
            logger.warning("late compression tip check failed %s: %s", attempt_id, _tip_exc)
            return

        # Locate gateway session
        target_sid, target_sess = _find_target_session(sessions, attempt_id, parent_id)
        if target_sess is None or target_sid is None:
            logger.info("late compression no live gateway session for attempt %s parent %s",
                        attempt_id, parent_id)
            return

        # Idempotent: if already projected, skip
        if str(target_sess.get("session_key") or "") == child_id:
            return

        # Reload child's authoritative messages from DB
        try:
            conv = db.get_messages_as_conversation(child_id, include_ancestors=False)
        except TypeError:
            conv = db.get_messages_as_conversation(child_id)
        except Exception as _conv_exc:
            logger.warning("late compression get_messages failed %s: %s", child_id, _conv_exc)
            return

        _apply_projection(target_sid, target_sess, child_id, conv, restart_slash_fn)
        logger.info("late compression projected attempt %s -> child %s sid %s",
                    attempt_id, child_id, target_sid)
    except Exception as _e:
        logger.warning("late compression handler failed %s: %s", attempt_id, _e)


def project_attempt_to_session(
    db: Any,
    attempt_id: str,
    sessions: Dict[str, Any],
    restart_slash_fn: Callable,
) -> None:
    """Lazy projection from session.status(attempt_id) when committed + not stale.

    Mirrors resolve_late_compression_ack but called from the status RPC.
    """
    attempt = db.get_compression_attempt(attempt_id)
    if not attempt:
        return
    if str(attempt.get("state") or "") != "committed":
        return
    child_id = str(attempt.get("child_session_key") or "")
    parent_id = str(attempt.get("parent_session_id") or "")
    if not child_id or not parent_id:
        return

    stale = db._is_compression_attempt_stale(attempt)
    if stale is not False:
        # True = stale, None = unknown lineage — refuse projection in both cases
        return

    # Locate target session
    target_sid, target_sess = _find_target_session(sessions, attempt_id, parent_id)
    if target_sess is None or target_sid is None:
        return
    if str(target_sess.get("session_key") or "") == child_id:
        return  # already projected

    try:
        conv = db.get_messages_as_conversation(child_id, include_ancestors=False)
    except TypeError:
        conv = db.get_messages_as_conversation(child_id)
    except Exception:
        return

    _apply_projection(target_sid, target_sess, child_id, conv, restart_slash_fn)
