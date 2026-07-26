"""Validation, persistence, and query helpers for compact leaf verdicts."""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.sqlite_util import retrying_write_txn
from hermes_cli.verdict import schema
from hermes_cli.verdict.types import (
    ALLOWED_RUNG_IDS,
    DispatchEnvelope,
    LeafVerdict,
)

logger = logging.getLogger(__name__)

_OUTCOMES = frozenset(
    {
        "success",
        "failure",
        "partial",
        "aborted",
        "killed_by_cap",
        "killed_by_operator",
    }
)
_FAILURE_CLASSES = frozenset(
    {
        "infra",
        "quality",
        "capability",
        "budget",
        "ambiguous",
        "cost_cap",
        "operator",
    }
)
_MODES = frozenset({"single", "single_with_critic", "moa", "panel", "decompose"})
_MISSING_ATTRIBUTION_WARNED: set[tuple[str | None, str | None]] = set()
_ATTRIBUTION_WARNING_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _required_text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _normalize_attribution(
    profile: Any,
    route: Any,
) -> tuple[str | None, str | None]:
    normalized_profile = (
        str(profile).strip().lower()
        if profile is not None and str(profile).strip()
        else None
    )
    normalized_route = (
        str(route).strip().lower()
        if route is not None and str(route).strip()
        else None
    )
    if normalized_profile is None or normalized_route is None:
        key = (normalized_profile, normalized_route)
        with _ATTRIBUTION_WARNING_LOCK:
            first = key not in _MISSING_ATTRIBUTION_WARNED
            if first:
                _MISSING_ATTRIBUTION_WARNED.add(key)
        if first:
            logger.warning(
                "Verdict attribution incomplete: profile=%s route=%s",
                normalized_profile,
                normalized_route,
            )
    return normalized_profile, normalized_route


def _nonnegative_int(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _validate_rung(rung_id: Any) -> str:
    rung = _required_text(rung_id, "rung_id")
    if rung not in ALLOWED_RUNG_IDS:
        raise ValueError(f"invalid rung_id: {rung!r}")
    return rung


def _validate_dispatch(envelope: DispatchEnvelope) -> None:
    _required_text(envelope.task_id, "task_id")
    _nonnegative_int(envelope.attempt_number, "attempt_number")
    _validate_rung(envelope.rung_id)
    _required_text(envelope.model_slug, "model_slug")
    if envelope.mode not in _MODES:
        raise ValueError(f"invalid mode: {envelope.mode!r}")
    if not isinstance(envelope.strategy_payload, dict):
        raise ValueError("strategy_payload must be a dict")
    if envelope.task_run_id is not None:
        _nonnegative_int(envelope.task_run_id, "task_run_id")
    if envelope.parent_verdict_id is not None:
        _nonnegative_int(envelope.parent_verdict_id, "parent_verdict_id")
    if envelope.expected_cost_aud is not None:
        _finite_nonnegative(envelope.expected_cost_aud, "expected_cost_aud")


def _validate_verdict(verdict: LeafVerdict) -> None:
    _required_text(verdict.task_id, "task_id")
    _nonnegative_int(verdict.attempt_number, "attempt_number")
    _validate_rung(verdict.rung_id)
    _required_text(verdict.model_used, "model_used")
    _required_text(verdict.strategy_hash, "strategy_hash")
    if verdict.outcome not in _OUTCOMES:
        raise ValueError(f"invalid outcome: {verdict.outcome!r}")
    if (
        verdict.failure_class is not None
        and verdict.failure_class not in _FAILURE_CLASSES
    ):
        raise ValueError(f"invalid failure_class: {verdict.failure_class!r}")
    if verdict.outcome == "success":
        if verdict.failure_class is not None:
            raise ValueError("success verdict must not carry failure_class")
        if verdict.escalation_recommended:
            raise ValueError("success verdict cannot recommend escalation")
    elif verdict.failure_class is None:
        raise ValueError(
            f"{verdict.outcome} verdict must carry failure_class"
        )
    if verdict.failure_class in {"budget", "cost_cap"} and verdict.escalation_recommended:
        raise ValueError("budget failure cannot recommend escalation")
    if verdict.failure_class == "infra" and verdict.escalation_recommended:
        raise ValueError("infra failure cannot recommend escalation")
    try:
        confidence = float(verdict.confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    _finite_nonnegative(verdict.cost_aud, "cost_aud")
    if verdict.task_run_id is not None:
        _nonnegative_int(verdict.task_run_id, "task_run_id")
    if verdict.dispatch_envelope_id is not None:
        _nonnegative_int(verdict.dispatch_envelope_id, "dispatch_envelope_id")
    for field, value in (
        ("input_tokens", verdict.input_tokens),
        ("output_tokens", verdict.output_tokens),
        ("wall_ms", verdict.wall_ms),
    ):
        if value is not None:
            _nonnegative_int(value, field)
    if not isinstance(verdict.failure_signals, list) or not all(
        isinstance(item, str) for item in verdict.failure_signals
    ):
        raise ValueError("failure_signals must be a list of strings")
    if not isinstance(verdict.side_effects, list) or not all(
        isinstance(item, int) and item >= 0 for item in verdict.side_effects
    ):
        raise ValueError("side_effects must be a list of non-negative integers")


def _warn_missing_side_effects(
    conn, side_effect_ids: list[int], task_id: str
) -> None:
    if not side_effect_ids:
        return
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='side_effects'"
    ).fetchone()
    existing: set[int] = set()
    if table_exists:
        placeholders = ",".join("?" for _ in side_effect_ids)
        rows = conn.execute(
            f"SELECT id FROM side_effects WHERE id IN ({placeholders})",
            side_effect_ids,
        ).fetchall()
        existing = {int(row["id"]) for row in rows}
    missing = [item for item in side_effect_ids if item not in existing]
    if missing:
        logger.warning(
            "Leaf verdict for task %s references missing side-effect ids: %s",
            task_id,
            missing,
        )


def record_dispatch(
    envelope: DispatchEnvelope,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    _validate_dispatch(envelope)
    schema.ensure_migrated(db_path)
    normalized_profile, normalized_route = _normalize_attribution(
        profile,
        route,
    )
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO dispatch_envelopes (
                    ts, task_id, task_run_id, attempt_number, rung_id,
                    model_slug, mode, strategy_hash, strategy_payload,
                    parent_verdict_id, expected_cost_aud, issued_by, profile,
                    route, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    envelope.task_id,
                    envelope.task_run_id,
                    envelope.attempt_number,
                    envelope.rung_id,
                    envelope.model_slug,
                    envelope.mode,
                    envelope.strategy_hash,
                    _json(envelope.strategy_payload),
                    envelope.parent_verdict_id,
                    envelope.expected_cost_aud,
                    envelope.issued_by,
                    normalized_profile,
                    normalized_route,
                    str(session_id) if session_id is not None else None,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def record_verdict(
    verdict: LeafVerdict,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    _validate_verdict(verdict)
    schema.ensure_migrated(db_path)
    normalized_profile, normalized_route = _normalize_attribution(
        profile,
        route,
    )
    conn = schema.connect(db_path)
    try:
        _warn_missing_side_effects(conn, verdict.side_effects, verdict.task_id)
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO leaf_verdicts (
                    ts, task_id, task_run_id, attempt_number, rung_id,
                    dispatch_envelope_id, model_used, outcome, failure_class,
                    failure_signals, confidence, cost_aud, side_effects,
                    escalation_recommended, recommendation_reason,
                    input_tokens, output_tokens, wall_ms, strategy_hash,
                    error_class, error_message, raw_meta, profile, route,
                    session_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    _utc_now(),
                    verdict.task_id,
                    verdict.task_run_id,
                    verdict.attempt_number,
                    verdict.rung_id,
                    verdict.dispatch_envelope_id,
                    verdict.model_used,
                    verdict.outcome,
                    verdict.failure_class,
                    _json(verdict.failure_signals),
                    float(verdict.confidence),
                    float(verdict.cost_aud),
                    _json(verdict.side_effects),
                    int(verdict.escalation_recommended),
                    verdict.recommendation_reason,
                    verdict.input_tokens,
                    verdict.output_tokens,
                    verdict.wall_ms,
                    verdict.strategy_hash,
                    verdict.error_class,
                    verdict.error_message,
                    _json(verdict.raw_meta) if verdict.raw_meta is not None else None,
                    normalized_profile,
                    normalized_route,
                    str(session_id) if session_id is not None else None,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _row_to_dispatch(row) -> DispatchEnvelope:
    return DispatchEnvelope(
        task_id=str(row["task_id"]),
        task_run_id=row["task_run_id"],
        attempt_number=int(row["attempt_number"]),
        rung_id=str(row["rung_id"]),
        model_slug=str(row["model_slug"]),
        mode=str(row["mode"]),
        strategy_payload=json.loads(row["strategy_payload"]),
        parent_verdict_id=row["parent_verdict_id"],
        expected_cost_aud=row["expected_cost_aud"],
        issued_by=str(row["issued_by"] or ""),
    )


def _row_to_verdict(row) -> LeafVerdict:
    return LeafVerdict(
        task_id=str(row["task_id"]),
        task_run_id=row["task_run_id"],
        attempt_number=int(row["attempt_number"]),
        rung_id=str(row["rung_id"]),
        dispatch_envelope_id=row["dispatch_envelope_id"],
        model_used=str(row["model_used"]),
        outcome=str(row["outcome"]),
        failure_class=row["failure_class"],
        failure_signals=list(json.loads(row["failure_signals"] or "[]")),
        confidence=float(row["confidence"]),
        cost_aud=float(row["cost_aud"]),
        side_effects=list(json.loads(row["side_effects"] or "[]")),
        escalation_recommended=bool(row["escalation_recommended"]),
        recommendation_reason=row["recommendation_reason"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        wall_ms=row["wall_ms"],
        strategy_hash=str(row["strategy_hash"]),
        error_class=row["error_class"],
        error_message=row["error_message"],
        raw_meta=json.loads(row["raw_meta"]) if row["raw_meta"] else None,
    )


def get_dispatch(
    dispatch_id: int, db_path: str | Path | None = None
) -> DispatchEnvelope | None:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM dispatch_envelopes WHERE id = ?",
            (int(dispatch_id),),
        ).fetchone()
        return _row_to_dispatch(row) if row else None
    finally:
        conn.close()


def get_verdict(
    verdict_id: int, db_path: str | Path | None = None
) -> LeafVerdict | None:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM leaf_verdicts WHERE id = ?",
            (int(verdict_id),),
        ).fetchone()
        return _row_to_verdict(row) if row else None
    finally:
        conn.close()


def list_verdicts_for_task(
    task_id: str, db_path: str | Path | None = None
) -> list[LeafVerdict]:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM leaf_verdicts
             WHERE task_id = ?
             ORDER BY attempt_number ASC, id ASC
            """,
            (task_id,),
        ).fetchall()
        return [_row_to_verdict(row) for row in rows]
    finally:
        conn.close()


def attempts_at_current_rung(
    task_id: str, rung_id: str, db_path: str | Path | None = None
) -> int:
    _validate_rung(rung_id)
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM leaf_verdicts
             WHERE task_id = ? AND rung_id = ?
            """,
            (task_id, rung_id),
        ).fetchone()
        return int(row["count"])
    finally:
        conn.close()


def has_strategy_changed(
    task_id: str, strategy_hash: str, db_path: str | Path | None = None
) -> bool:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM leaf_verdicts
             WHERE task_id = ? AND strategy_hash = ?
             LIMIT 1
            """,
            (task_id, strategy_hash),
        ).fetchone()
        return row is None
    finally:
        conn.close()


def last_cost_aud_for_task(
    task_id: str, db_path: str | Path | None = None
) -> float:
    """Return the newest recorded AUD cost for a task, or zero when absent."""
    conn = schema.connect(db_path)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_ledger'"
        ).fetchone()
        if not table_exists:
            return 0.0
        row = conn.execute(
            """
            SELECT aud_amount FROM cost_ledger
             WHERE task_id = ?
             ORDER BY id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return float(row["aud_amount"]) if row else 0.0
    finally:
        conn.close()


__all__ = [
    "attempts_at_current_rung",
    "get_dispatch",
    "get_verdict",
    "has_strategy_changed",
    "last_cost_aud_for_task",
    "list_verdicts_for_task",
    "record_dispatch",
    "record_verdict",
]
