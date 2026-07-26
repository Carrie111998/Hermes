"""Synchronous glue between provider completion and durable cost gates."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.cost import caps, ledger, telegram_alert


logger = logging.getLogger(__name__)


def _hour_bucket(prefix: str) -> str:
    return f"{prefix}:{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}"


def _send_bridge_telegram(
    *,
    kind: str,
    bridge_caps: dict[str, Any],
    db_path: str | Path | None = None,
) -> bool:
    """Reserve, send, and confirm one hourly-deduplicated bridge alert."""
    from hermes_cli.side_effects import api as side_effects

    used = int(bridge_caps["turns_used"])
    soft = int(bridge_caps["soft_cap"])
    hard = int(bridge_caps["hard_cap"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "hard":
        message = (
            "🚨 Hermes Pro bridge hard turn cap reached\n\n"
            f"Turns: {used} / soft {soft} / hard {hard}\n"
            "Atlas fallthrough disabled; routing to next rung.\n"
            f"UTC: {now}"
        )
        bucket = _hour_bucket("bridge_hard")
    else:
        message = (
            "⚠️ Hermes Pro bridge soft turn cap reached\n\n"
            f"Turns: {used} / soft {soft} / hard {hard}\n"
            f"UTC: {now}"
        )
        bucket = _hour_bucket("bridge_warn")

    reservation = side_effects.reserve(
        task_id="system:bridge",
        lane="platform",
        action_type="telegram.send",
        payload={"target": "telegram", "message": message},
        idempotency_key=bucket,
        db_path=db_path,
    )
    if reservation.already_done is not None:
        return False
    if reservation.already_in_flight is not None:
        return False
    if reservation.reserved_id is None:
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
        result_summary="bridge threshold alert delivered",
        db_path=db_path,
    )
    return True


def _send_bridge_warn_telegram(
    bridge_caps: dict[str, Any],
    db_path: str | Path | None = None,
) -> bool:
    return _send_bridge_telegram(
        kind="warn",
        bridge_caps=bridge_caps,
        db_path=db_path,
    )


def _send_bridge_hard_alert_telegram(
    bridge_caps: dict[str, Any],
    db_path: str | Path | None = None,
) -> bool:
    return _send_bridge_telegram(
        kind="hard",
        bridge_caps=bridge_caps,
        db_path=db_path,
    )


def send_task_cap_kill_alert(
    *,
    task_id: str,
    lane: str,
    projected_total: float,
    task_cap_aud: float,
    db_path: str | Path | None = None,
) -> bool:
    """Send one hourly-deduplicated alert after a task-cap kill commits."""
    from hermes_cli.programme import gate as programme_gate
    from hermes_cli.side_effects import api as side_effects

    state = programme_gate.get_state(db_path).state
    message = (
        "⚠️ TASK CAP HIT\n"
        f"task_id: {task_id}\n"
        f"lane: {lane}\n"
        f"projected: {projected_total:.2f} AUD\n"
        f"cap: {task_cap_aud:.2f} AUD\n"
        f"programme_state: {state}\n"
        "(task marked FAILED, kill_switch row inserted)"
    )
    bucket = _hour_bucket(f"task_cap_kill:{task_id}")
    reservation = side_effects.reserve(
        task_id=str(task_id),
        lane="platform",
        action_type="telegram.send",
        payload={"target": "telegram", "message": message},
        idempotency_key=bucket,
        db_path=db_path,
    )
    if reservation.already_done is not None:
        return False
    if reservation.already_in_flight is not None:
        return False
    if reservation.reserved_id is None:
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
        result_summary="task cap kill alert delivered",
        db_path=db_path,
    )
    return True


def send_task_cost_advisory(
    *,
    task_id: str,
    lane: str,
    task_total_aud: float,
    tracking_threshold_aud: float,
    daily_total_aud: float,
    reason: str,
    db_path: str | Path | None = None,
) -> bool:
    """Send one daily, per-task spend advisory without changing task state."""
    from hermes_cli.side_effects import api as side_effects

    normalized_task = str(task_id)
    normalized_lane = str(lane)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    message = (
        "💰 TASK SPEND ADVISORY\n"
        f"task_id: {normalized_task}\n"
        f"lane: {normalized_lane}\n"
        f"task spend: {task_total_aud:.2f} AUD\n"
        f"tracking threshold: {tracking_threshold_aud:.2f} AUD\n"
        f"daily spend: {daily_total_aud:.2f} AUD\n"
        f"reason: {reason}\n"
        "advisory_only: yes\n"
        "task_paused: no\n"
        "programme_paused: no"
    )
    reservation = side_effects.reserve(
        task_id=normalized_task,
        lane=normalized_lane,
        action_type="telegram.send",
        payload={"target": "telegram", "message": message},
        idempotency_key=f"task_cost_advisory:{normalized_task}:{day}",
        db_path=db_path,
    )
    if reservation.already_done is not None:
        return False
    if reservation.already_in_flight is not None:
        return False
    if reservation.reserved_id is None:
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
        result_summary="per-task spend advisory delivered",
        db_path=db_path,
    )
    return True


def record_bridge_turn(
    *,
    task_id,
    lane,
    outcome,
    db_path: str | Path | None = None,
    **kwargs,
) -> int:
    """Record a Pro-bridge attempt and publish threshold state/alerts."""
    from hermes_cli.cost import bridge_state, turns_ledger

    row_id = turns_ledger.record_turn(
        task_id=task_id,
        lane=lane,
        outcome=outcome,
        db_path=db_path,
        **kwargs,
    )
    bridge_caps = turns_ledger.check_bridge_caps(db_path)
    if bridge_caps["hard_hit"]:
        bridge_state.set_fallthrough_disabled(
            True,
            reason="daily turns cap hit",
            db_path=db_path,
        )
        try:
            _send_bridge_hard_alert_telegram(bridge_caps, db_path)
        except Exception:
            logger.exception(
                "Pro bridge hard cap landed but Telegram alert failed"
            )
    elif bridge_caps["soft_hit"]:
        try:
            _send_bridge_warn_telegram(bridge_caps, db_path)
        except Exception:
            logger.exception(
                "Pro bridge soft cap landed but Telegram warning failed"
            )
    return row_id


def on_call_complete(
    task_id,
    lane,
    vendor,
    model_slug,
    attempt_number,
    rung_id,
    escalation,
    input_tokens,
    output_tokens,
    cached_input_tokens,
    usd_amount,
    latency_ms,
    request_id,
    raw_response_meta,
    *,
    profile=None,
    route=None,
    session_id=None,
    enforce_programme_cap: bool = False,
) -> None:
    """Record one call; cost thresholds are always advisory.

    ``enforce_programme_cap`` remains accepted for API compatibility but is
    intentionally inert.
    """
    entry = ledger.record_call(
        task_id=task_id,
        lane=lane,
        vendor=vendor,
        model_slug=model_slug,
        attempt_number=attempt_number,
        rung_id=rung_id,
        escalation=escalation,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        usd_amount=usd_amount,
        latency_ms=latency_ms,
        request_id=request_id,
        raw_response_meta=raw_response_meta,
        profile=profile,
        route=route,
        session_id=session_id,
        enforce_programme_cap=enforce_programme_cap,
    )
    if not entry.transitioned_to_paused or entry.breach_reason is None:
        return

    # Notification is deliberately outside the SQLite transaction. The
    # durable state transition is authoritative even if Telegram is down, and
    # a transport failure must never retry a paid provider call.
    try:
        telegram_alert.send_cost_alert(
            entry.breach_reason,
            caps.daily_spend_aud(),
            ledger.format_ledger_tail(5),
        )
    except Exception:
        logger.exception("Cost gate paused programme but Telegram alert failed")


__all__ = [
    "on_call_complete",
    "record_bridge_turn",
    "send_task_cap_kill_alert",
    "send_task_cost_advisory",
]
