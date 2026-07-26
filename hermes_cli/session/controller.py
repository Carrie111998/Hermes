"""Rotation policy, atomic session replacement, and hard-limit alerting."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from hermes_cli.session import api, schema
from hermes_cli.session.estimator import estimate_next_turn_input_tokens
from hermes_cli.session.rotation_config import ROTATION_CAPS
from hermes_cli.sqlite_util import retrying_write_txn


logger = logging.getLogger(__name__)


def should_rotate(
    *,
    system_prompt: str,
    conversation_history: list,
    pending_user_message: str,
) -> Tuple[bool, str]:
    estimate = estimate_next_turn_input_tokens(
        system_prompt,
        conversation_history,
        pending_user_message,
    )
    if estimate >= ROTATION_CAPS.hard_limit_tokens:
        return True, "hard_limit"
    if estimate >= ROTATION_CAPS.soft_limit_tokens:
        return True, "soft_limit"
    return False, ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _send_hard_rotation_alert(
    *,
    task_id: str,
    old_session_id: str,
    new_session_id: str,
    token_count: int,
    db_path=None,
) -> bool:
    from hermes_cli.cost import telegram_alert
    from hermes_cli.side_effects import api as side_effects

    hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
    message = (
        "🚨 Hermes hard session rotation\n\n"
        f"Task: {task_id}\n"
        f"Estimated input: {token_count} tokens\n"
        f"Session: {old_session_id} -> {new_session_id}"
    )
    reservation = side_effects.reserve(
        task_id=str(task_id),
        lane="platform",
        action_type="telegram.send",
        payload={"target": "telegram", "message": message},
        idempotency_key=f"session_hard_rotate:{task_id}:{hour}",
        db_path=db_path,
    )
    if (
        reservation.already_done is not None
        or reservation.already_in_flight is not None
        or reservation.reserved_id is None
    ):
        return False
    row_id = reservation.reserved_id
    side_effects.mark_in_flight(reserved_id=row_id, db_path=db_path)
    try:
        telegram_alert.send_bridge_alert(message)
    except Exception as exc:
        side_effects.fail(
            reserved_id=row_id,
            error_class=type(exc).__name__,
            error_message=str(exc),
            db_path=db_path,
        )
        raise
    side_effects.confirm(
        reserved_id=row_id,
        external_ref=None,
        result_summary="hard session rotation warning delivered",
        db_path=db_path,
    )
    return True


def rotate_now(
    *,
    current_session_id: str,
    task_id: str,
    lane: str,
    profile: Optional[str],
    route: Optional[str],
    reason: str,
    token_count_at_close: int,
    db_path=None,
    new_session_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Atomically close one shared-ledger session and open its child."""
    if reason not in {"soft_limit", "hard_limit", "manual", "error"}:
        raise ValueError(f"invalid rotation reason: {reason!r}")
    count = int(token_count_at_close)
    if count < 0:
        raise ValueError("token_count_at_close must be non-negative")
    schema.ensure_migrated(db_path)
    summary = api.build_handoff_summary(task_id, db_path)
    prefix = api.serialize_handoff(summary)
    new_session_id = str(new_session_id or uuid.uuid4())

    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            current = conn.execute(
                "SELECT closed_ts FROM sessions WHERE id = ?",
                (str(current_session_id),),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown session: {current_session_id}")
            if current["closed_ts"] is not None:
                raise RuntimeError(
                    f"session is already closed: {current_session_id}"
                )
            conn.execute(
                """
                UPDATE sessions
                   SET closed_ts = ?, rotation_reason = ?,
                       token_count_at_close = ?, handoff_summary_json = ?
                 WHERE id = ? AND closed_ts IS NULL
                """,
                (
                    _utc_now(),
                    reason,
                    count,
                    json_dumps(summary),
                    str(current_session_id),
                ),
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    id, task_id, parent_session_id, lane, profile, route,
                    opened_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_session_id,
                    str(task_id),
                    str(current_session_id),
                    str(lane),
                    str(profile) if profile is not None else None,
                    str(route) if route is not None else None,
                    _utc_now(),
                ),
            )
    finally:
        conn.close()

    if reason == "hard_limit":
        try:
            _send_hard_rotation_alert(
                task_id=str(task_id),
                old_session_id=str(current_session_id),
                new_session_id=new_session_id,
                token_count=count,
                db_path=db_path,
            )
        except Exception:
            logger.exception(
                "Hard session rotation committed but Telegram warning failed"
            )
    return new_session_id, prefix


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = ["rotate_now", "should_rotate"]
