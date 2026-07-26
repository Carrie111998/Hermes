"""Feature-flagged routing facade and symmetric decision audit."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_cli.cost import telegram_alert
from hermes_cli.routing import bootstrap, schema
from hermes_cli.routing import drift, drift_schema
from hermes_cli.routing.reader import DoctrineReader
from hermes_cli.sqlite_util import retrying_write_txn


_READERS: dict[str, DoctrineReader] = {}
_READERS_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _reader_for(db_path=None) -> DoctrineReader:
    path = schema.resolve_db_path(db_path)
    key = str(path.resolve())
    with _READERS_LOCK:
        reader = _READERS.get(key)
        if reader is None:
            reader = DoctrineReader(path)
            _READERS[key] = reader
        return reader


def _serialize_failure_history(failure_history: Optional[list]) -> str:
    value = [] if failure_history is None else failure_history
    if not isinstance(value, list):
        raise ValueError("failure_history must be a list or None")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_fallbacks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        provider = _optional_text(entry.get("provider"))
        model = _optional_text(entry.get("model"))
        if provider is not None and model is not None:
            normalized.append({"provider": provider, "model": model})
    return normalized


def _maybe_emit_doctrine_live_alert(
    conn,
    decision_row_id: int,
    task_id: str | None,
    lane: str,
    chosen_provider: str,
    chosen_model: str,
    doctrine_version: int | None,
) -> None:
    """Emit one fleet-wide, per-day signal when doctrine becomes live."""
    del decision_row_id
    try:
        from hermes_cli.side_effects import api as side_effects

        message = (
            "✅ DOCTRINE LIVE\n"
            "First doctrine-following decision recorded.\n"
            f"task_id: {task_id or '-'}\n"
            f"lane: {lane}\n"
            f"provider: {chosen_provider}\n"
            f"model: {chosen_model}\n"
            f"doctrine_version: {doctrine_version}"
        )
        bucket = (
            "doctrine_live:"
            f"{datetime.now(timezone.utc).date().isoformat()}"
        )
        reservation = side_effects.reserve(
            task_id="system:doctrine_live",
            lane="platform",
            action_type="telegram.send",
            payload={"target": "telegram", "message": message},
            idempotency_key=bucket,
            conn=conn,
        )
        if (
            reservation.already_done is not None
            or reservation.already_in_flight is not None
            or reservation.reserved_id is None
        ):
            return
        row_id = int(reservation.reserved_id)
        side_effects.mark_in_flight(reserved_id=row_id, conn=conn)
        try:
            telegram_alert.send_bridge_alert(message)
        except Exception as exc:
            side_effects.fail(
                reserved_id=row_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
                conn=conn,
            )
            logger.warning(
                "Doctrine-live alert send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return
        side_effects.confirm(
            reserved_id=row_id,
            external_ref=None,
            result_summary="doctrine-live alert delivered",
            conn=conn,
        )
    except Exception as exc:
        logger.warning(
            "Doctrine-live alert skipped without blocking route decision: "
            "%s: %s",
            type(exc).__name__,
            exc,
        )


def _write_decision(
    *,
    lane: str,
    rung: str,
    complexity: str,
    chosen_provider: str,
    chosen_model: str,
    doctrine_version: int | None,
    matched_rule_id: int | None,
    match_specificity: str | None,
    used_doctrine_reader: bool,
    overridden_by_caller: bool,
    doctrine_suggested_provider: str | None,
    doctrine_suggested_model: str | None,
    failure_history_json: str,
    task_id: str | None,
    session_id: str | None,
    profile: str | None,
    route: str | None,
    forced_legacy: bool,
    db_path=None,
) -> int:
    drift_schema.ensure_migrated(db_path)
    chosen_at = _utc_now()
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO routing_decisions (
                    session_id, task_id, profile, route, lane, rung,
                    complexity, chosen_provider, chosen_model,
                    doctrine_version, matched_rule_id, match_specificity,
                    used_doctrine_reader, overridden_by_caller,
                    doctrine_suggested_provider, doctrine_suggested_model,
                    failure_history_json, chosen_at, forced_legacy
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    session_id,
                    task_id,
                    profile,
                    route,
                    lane,
                    rung,
                    complexity,
                    chosen_provider,
                    chosen_model,
                    doctrine_version,
                    matched_rule_id,
                    match_specificity,
                    int(used_doctrine_reader),
                    int(overridden_by_caller),
                    doctrine_suggested_provider,
                    doctrine_suggested_model,
                    failure_history_json,
                    chosen_at,
                    int(forced_legacy),
                ),
            )
            decision_row_id = int(cursor.lastrowid)
            if used_doctrine_reader and not forced_legacy:
                _maybe_emit_doctrine_live_alert(
                    conn,
                    decision_row_id,
                    task_id,
                    lane,
                    chosen_provider,
                    chosen_model,
                    doctrine_version,
                )
            # The decision is authoritative. Drift materialization shares the
            # same transaction and connection, but is isolated by a savepoint
            # so an observational failure can never block routing.
            conn.execute("SAVEPOINT routing_drift_refresh")
            try:
                drift.refresh_bucket(conn, drift._hour_bucket(chosen_at))
                drift.maybe_alert(conn)
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT routing_drift_refresh")
                logger.exception(
                    "Routing decision persisted while drift refresh failed"
                )
            finally:
                conn.execute("RELEASE SAVEPOINT routing_drift_refresh")
    finally:
        conn.close()
    return decision_row_id


def append_failure_history(
    decision_row_id: int,
    failure_history: list,
    *,
    conn=None,
    db_path=None,
) -> None:
    """Replace one decision's compact provider-switch history."""
    history_json = _serialize_failure_history(failure_history)
    if conn is None:
        owned = schema.connect(db_path)
        try:
            with retrying_write_txn(owned):
                append_failure_history(
                    decision_row_id,
                    failure_history,
                    conn=owned,
                )
        finally:
            owned.close()
        return
    cursor = conn.execute(
        """
        UPDATE routing_decisions
           SET failure_history_json = ?
         WHERE id = ?
        """,
        (history_json, int(decision_row_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError(
            f"routing decision does not exist: {int(decision_row_id)}"
        )


def update_chosen_after_fallback(
    decision_row_id: int,
    *,
    chosen_provider: str,
    chosen_model: str,
    conn=None,
    db_path=None,
) -> None:
    """Update the winning route after the existing engine finishes."""
    provider = _optional_text(chosen_provider)
    model = _optional_text(chosen_model)
    if provider is None or model is None:
        raise ValueError("chosen_provider and chosen_model are required")
    if conn is None:
        owned = schema.connect(db_path)
        try:
            with retrying_write_txn(owned):
                update_chosen_after_fallback(
                    decision_row_id,
                    chosen_provider=provider,
                    chosen_model=model,
                    conn=owned,
                )
        finally:
            owned.close()
        return
    cursor = conn.execute(
        """
        UPDATE routing_decisions
           SET chosen_provider = ?, chosen_model = ?
         WHERE id = ?
        """,
        (provider, model, int(decision_row_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError(
            f"routing decision does not exist: {int(decision_row_id)}"
        )


def persist_fallback_result(
    *,
    decision_row_id: int,
    failure_history: list,
    chosen_provider: str,
    chosen_model: str,
    db_path=None,
) -> None:
    """Atomically persist cascade history, winner, and refreshed drift."""
    drift_schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            append_failure_history(
                decision_row_id,
                failure_history,
                conn=conn,
            )
            update_chosen_after_fallback(
                decision_row_id,
                chosen_provider=chosen_provider,
                chosen_model=chosen_model,
                conn=conn,
            )
            row = conn.execute(
                "SELECT chosen_at FROM routing_decisions WHERE id = ?",
                (int(decision_row_id),),
            ).fetchone()
            if row is not None:
                conn.execute("SAVEPOINT fallback_drift_refresh")
                try:
                    drift.refresh_bucket(
                        conn,
                        drift._hour_bucket(str(row["chosen_at"])),
                    )
                    drift.maybe_alert(conn)
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT fallback_drift_refresh")
                    logger.exception(
                        "Fallback outcome persisted while drift refresh failed"
                    )
                finally:
                    conn.execute("RELEASE SAVEPOINT fallback_drift_refresh")
    finally:
        conn.close()


def route_for_turn(
    *,
    lane: str,
    rung: str,
    complexity: str,
    caller_provider: Optional[str] = None,
    caller_model: Optional[str] = None,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    profile: Optional[str] = None,
    route: Optional[str] = None,
    failure_history: Optional[list] = None,
    use_doctrine_reader: bool = False,
    forced_legacy: bool = False,
    db_path=None,
) -> dict[str, Any]:
    """Choose a route and always persist its audit decision."""
    bootstrap.bootstrap_if_needed(db_path)
    normalized_lane = str(lane).strip()
    normalized_rung = str(rung).strip()
    normalized_complexity = str(complexity).strip()
    if not normalized_lane or not normalized_rung or not normalized_complexity:
        raise ValueError("lane, rung and complexity must be non-empty")
    provider = _optional_text(caller_provider)
    model = _optional_text(caller_model)
    if (provider is None) != (model is None):
        raise ValueError("provider and model must be supplied together")
    failure_json = _serialize_failure_history(failure_history)

    effective_use_doctrine = bool(use_doctrine_reader) and not bool(
        forced_legacy
    )
    if not effective_use_doctrine:
        if provider is None or model is None:
            raise ValueError(
                "provider and model are required when route='single'"
            )
        result = {
            "provider": provider,
            "model": model,
            "fallbacks": [],
            "used_doctrine_reader": False,
            "overridden_by_caller": False,
            "doctrine_version": None,
            "matched_rule_id": None,
            "match_specificity": None,
            "doctrine_suggested_provider": None,
            "doctrine_suggested_model": None,
            "forced_legacy": int(bool(forced_legacy)),
        }
    else:
        suggestion = _reader_for(db_path).choose(
            lane=normalized_lane,
            rung=normalized_rung,
            complexity=normalized_complexity,
            failure_history=failure_history,
        )
        overridden = provider is not None and model is not None
        result = {
            "provider": provider if overridden else suggestion["provider"],
            "model": model if overridden else suggestion["model"],
            "fallbacks": _normalize_fallbacks(suggestion["fallbacks"]),
            "used_doctrine_reader": True,
            "overridden_by_caller": overridden,
            "doctrine_version": suggestion["doctrine_version"],
            "matched_rule_id": suggestion["matched_rule_id"],
            "match_specificity": suggestion["match_specificity"],
            "doctrine_suggested_provider": suggestion["provider"],
            "doctrine_suggested_model": suggestion["model"],
            "forced_legacy": 0,
        }

    decision_row_id = _write_decision(
        lane=normalized_lane,
        rung=normalized_rung,
        complexity=normalized_complexity,
        chosen_provider=str(result["provider"]),
        chosen_model=str(result["model"]),
        doctrine_version=result["doctrine_version"],
        matched_rule_id=result["matched_rule_id"],
        match_specificity=result["match_specificity"],
        used_doctrine_reader=bool(result["used_doctrine_reader"]),
        overridden_by_caller=bool(result["overridden_by_caller"]),
        doctrine_suggested_provider=result["doctrine_suggested_provider"],
        doctrine_suggested_model=result["doctrine_suggested_model"],
        failure_history_json=failure_json,
        task_id=_optional_text(task_id),
        session_id=_optional_text(session_id),
        profile=_optional_text(profile),
        route=_optional_text(route),
        forced_legacy=bool(result["forced_legacy"]),
        db_path=db_path,
    )
    result["decision_row_id"] = decision_row_id
    return result


def record_non_single_route(
    *,
    lane: str,
    rung: str,
    complexity: str,
    chosen_provider: str,
    chosen_model: str,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    profile: Optional[str] = None,
    route: Optional[str] = None,
    failure_history: Optional[list] = None,
    db_path=None,
) -> dict[str, Any]:
    """Audit an existing Auto, Fusion, or MoA route without doctrine."""
    bootstrap.bootstrap_if_needed(db_path)
    failure_json = _serialize_failure_history(failure_history)
    provider = _optional_text(chosen_provider)
    model = _optional_text(chosen_model)
    if provider is None or model is None:
        raise ValueError("chosen_provider and chosen_model are required")
    normalized_lane = str(lane).strip() or "default"
    normalized_rung = str(rung).strip() or "default"
    normalized_complexity = str(complexity).strip() or "default"
    decision_row_id = _write_decision(
        lane=normalized_lane,
        rung=normalized_rung,
        complexity=normalized_complexity,
        chosen_provider=provider,
        chosen_model=model,
        doctrine_version=None,
        matched_rule_id=None,
        match_specificity=None,
        used_doctrine_reader=False,
        overridden_by_caller=False,
        doctrine_suggested_provider=None,
        doctrine_suggested_model=None,
        failure_history_json=failure_json,
        task_id=_optional_text(task_id),
        session_id=_optional_text(session_id),
        profile=_optional_text(profile),
        route=_optional_text(route),
        forced_legacy=False,
        db_path=db_path,
    )
    return {
        "decision_row_id": decision_row_id,
        "provider": provider,
        "model": model,
        "fallbacks": [],
        "used_doctrine_reader": False,
        "overridden_by_caller": False,
        "doctrine_version": None,
        "matched_rule_id": None,
        "match_specificity": None,
        "doctrine_suggested_provider": None,
        "doctrine_suggested_model": None,
        "forced_legacy": 0,
    }


__all__ = [
    "append_failure_history",
    "persist_fallback_result",
    "record_non_single_route",
    "route_for_turn",
    "update_chosen_after_fallback",
]
