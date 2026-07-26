"""Durable, synchronous per-call spend ledger."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root
from hermes_cli.cost import config as cost_config
from hermes_cli.cost import ratecards, vendors
from hermes_cli.sqlite_util import (
    add_column_if_missing,
    open_connection,
    retrying_write_txn,
)


logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = get_default_hermes_root() / "kanban.db"
DB_PATH = _DEFAULT_DB_PATH
_MIGRATED_PATHS: set[str] = set()
_MIGRATION_LOCK = threading.RLock()
_MISSING_ATTRIBUTION_WARNED: set[tuple[str | None, str | None]] = set()
_ATTRIBUTION_WARNING_LOCK = threading.Lock()
_MISSING_TASK_CAP_WARNED: set[str] = set()
_TASK_CAP_WARNING_LOCK = threading.Lock()

_INDEX_SCHEMA = (
    """
    CREATE INDEX IF NOT EXISTS idx_cost_ledger_ts_lane
        ON cost_ledger(ts, lane)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_ledger_task
        ON cost_ledger(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_ledger_vendor_kind
        ON cost_ledger(vendor_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_ledger_lane_ts
        ON cost_ledger(lane, ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_ledger_session
        ON cost_ledger(session_id)
    """,
)

_BASE_COLUMNS = (
    "id",
    "ts",
    "task_id",
    "lane",
    "vendor",
    "model_slug",
    "attempt_number",
    "rung_id",
    "escalation",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "usd_amount",
    "aud_amount",
    "fx_rate",
    "surcharge_applied",
    "latency_ms",
    "request_id",
    "raw_response_meta",
)
_NEW_COLUMNS = (
    "vendor_kind",
    "voice_minutes",
    "api_call_kind",
    "is_free_tier",
    "is_subscription_bridge",
)
_ATTRIBUTION_COLUMNS = ("profile", "route", "session_id")


def _create_cost_table(conn: sqlite3.Connection, table_name: str) -> None:
    conn.execute(
        f"""
    CREATE TABLE {table_name} (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                  TEXT NOT NULL,
        task_id             TEXT,
        lane                TEXT NOT NULL
                            CHECK (lane IN (
                                'green_captains', 'dayroute', 'tihna',
                                'platform', 'reserve', 'escalation'
                            )),
        vendor              TEXT NOT NULL
                            CHECK (vendor IN (
                                'openrouter', 'anthropic', 'openai',
                                'openai-codex', 'retell', 'perplexity',
                                'apple', 'meta', 'github', 'other'
                            )),
        model_slug          TEXT NOT NULL,
        attempt_number      INTEGER,
        rung_id             TEXT,
        escalation          BOOLEAN NOT NULL DEFAULT 0,
        input_tokens        INTEGER,
        output_tokens       INTEGER,
        cached_input_tokens INTEGER DEFAULT 0,
        usd_amount          REAL NOT NULL,
        aud_amount          REAL NOT NULL,
        fx_rate             REAL NOT NULL,
        surcharge_applied   REAL DEFAULT 0.0,
        latency_ms          INTEGER,
        request_id          TEXT,
        raw_response_meta   TEXT,
        vendor_kind         TEXT,
        voice_minutes       REAL,
        api_call_kind       TEXT,
        is_free_tier        INTEGER NOT NULL DEFAULT 0,
        is_subscription_bridge INTEGER NOT NULL DEFAULT 0,
        profile             TEXT,
        route               TEXT,
        session_id          TEXT
    )
    """
    )


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    ts: str
    task_id: str | None
    lane: str
    vendor: str
    model_slug: str
    attempt_number: int | None
    rung_id: str | None
    escalation: bool
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int
    usd_amount: float
    aud_amount: float
    fx_rate: float
    surcharge_applied: float
    latency_ms: int | None
    request_id: str | None
    raw_response_meta: str | None
    vendor_kind: str | None
    voice_minutes: float | None
    api_call_kind: str | None
    is_free_tier: bool
    is_subscription_bridge: bool
    profile: str | None
    route: str | None
    session_id: str | None
    breached_cap: str | None = None
    breach_reason: str | None = None
    transitioned_to_paused: bool = False


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def resolve_db_path(db_path: Path | None = None) -> Path:
    """Resolve an explicit/test override or the active shared Hermes root."""
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the shared programme database without an implicit transaction."""
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"cost ledger ({path.name})",
    )


def migrate(db_path: Path | None = None) -> None:
    """Create or widen the cost ledger atomically and idempotently."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            table = conn.execute(
                """
                SELECT sql FROM sqlite_master
                 WHERE type = 'table' AND name = 'cost_ledger'
                """
            ).fetchone()
            if table is None:
                _create_cost_table(conn, "cost_ledger")
            else:
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(cost_ledger)")
                }
                table_sql = str(table["sql"] or "").lower()
                needs_rebuild = (
                    not set(_NEW_COLUMNS).issubset(columns)
                    or "'retell'" not in table_sql
                    or "'escalation'" not in table_sql
                )
                if needs_rebuild:
                    _rebuild_cost_table(conn, columns)
                else:
                    add_column_if_missing(
                        conn,
                        "cost_ledger",
                        "profile",
                        "profile TEXT",
                    )
                    add_column_if_missing(
                        conn,
                        "cost_ledger",
                        "route",
                        "route TEXT",
                    )
                    add_column_if_missing(
                        conn,
                        "cost_ledger",
                        "session_id",
                        "session_id TEXT",
                    )
            for statement in _INDEX_SCHEMA:
                conn.execute(statement)
    finally:
        conn.close()
    _MIGRATED_PATHS.add(str(path.resolve()))


def _rebuild_cost_table(
    conn: sqlite3.Connection, existing_columns: set[str]
) -> None:
    """Widen immutable SQLite CHECK constraints while preserving every row."""
    replacement = "cost_ledger__cs02c"
    conn.execute(f"DROP TABLE IF EXISTS {replacement}")
    _create_cost_table(conn, replacement)
    destination = (*_BASE_COLUMNS, *_NEW_COLUMNS, *_ATTRIBUTION_COLUMNS)
    fallbacks = {
        "vendor_kind": "NULL",
        "voice_minutes": "NULL",
        "api_call_kind": "NULL",
        "is_free_tier": "0",
        "is_subscription_bridge": "0",
        "profile": "NULL",
        "route": "NULL",
        "session_id": "NULL",
    }
    source = [
        column if column in existing_columns else fallbacks[column]
        for column in destination
    ]
    conn.execute(
        f"""
        INSERT INTO {replacement} ({", ".join(destination)})
        SELECT {", ".join(source)}
          FROM cost_ledger
         ORDER BY id
        """
    )
    conn.execute("DROP TABLE cost_ledger")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO cost_ledger")


def ensure_migrated(db_path: Path | None = None) -> None:
    """Lazily initialize a newly selected Hermes home once per process."""
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    if key not in _MIGRATED_PATHS:
        with _MIGRATION_LOCK:
            if key not in _MIGRATED_PATHS:
                migrate(path)
    from hermes_cli.cost import task_cap_schema

    task_cap_schema.ensure_migrated(path)


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    result = int(value)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _serialize_raw_meta(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        id=int(row["id"]),
        ts=str(row["ts"]),
        task_id=row["task_id"],
        lane=str(row["lane"]),
        vendor=str(row["vendor"]),
        model_slug=str(row["model_slug"]),
        attempt_number=row["attempt_number"],
        rung_id=row["rung_id"],
        escalation=bool(row["escalation"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cached_input_tokens=int(row["cached_input_tokens"] or 0),
        usd_amount=float(row["usd_amount"]),
        aud_amount=float(row["aud_amount"]),
        fx_rate=float(row["fx_rate"]),
        surcharge_applied=float(row["surcharge_applied"] or 0.0),
        latency_ms=row["latency_ms"],
        request_id=row["request_id"],
        raw_response_meta=row["raw_response_meta"],
        vendor_kind=row["vendor_kind"],
        voice_minutes=(
            float(row["voice_minutes"])
            if row["voice_minutes"] is not None
            else None
        ),
        api_call_kind=row["api_call_kind"],
        is_free_tier=bool(row["is_free_tier"]),
        is_subscription_bridge=bool(row["is_subscription_bridge"]),
        profile=row["profile"],
        route=row["route"],
        session_id=row["session_id"],
    )


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
                "Cost ledger attribution incomplete: profile=%s route=%s",
                normalized_profile,
                normalized_route,
            )
    return normalized_profile, normalized_route


def _task_tracking_threshold(
    conn: sqlite3.Connection,
    task_id: str,
) -> float:
    """Return a task-specific threshold or the general advisory threshold."""
    tasks_exists = conn.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type = 'table' AND name = 'tasks'
        """
    ).fetchone()
    if tasks_exists is not None:
        row = conn.execute(
            "SELECT task_cap_aud FROM tasks WHERE id = ?",
            (str(task_id),),
        ).fetchone()
        if row is not None and row["task_cap_aud"] is not None:
            return float(row["task_cap_aud"])
    return float(cost_config.PER_TASK_CAP_AUD)


def record_call(
    task_id,
    lane,
    vendor=None,
    model_slug=None,
    attempt_number=None,
    rung_id=None,
    escalation=False,
    input_tokens=None,
    output_tokens=None,
    cached_input_tokens=0,
    usd_amount=None,
    latency_ms=None,
    request_id=None,
    raw_response_meta=None,
    *,
    model=None,
    reported_usd=None,
    voice_minutes=None,
    api_call_kind=None,
    force_zero=False,
    profile=None,
    route=None,
    session_id=None,
    provider=None,
    amount_aud=None,
    enforce_task_cap: bool = False,
    enforce_programme_cap: bool = False,
    db_path: str | Path | None = None,
) -> LedgerEntry:
    """Synchronously append one attributed vendor-call ledger row.

    The original CS-02 positional parameters remain accepted. New vendor
    details are keyword-only, and omitting the required ``lane`` argument is a
    Python ``TypeError`` before any write occurs.
    """
    ensure_migrated(db_path)
    normalized_lane = str(lane).strip().lower()
    if vendor is not None and provider is not None:
        if str(vendor).strip().lower() != str(provider).strip().lower():
            raise ValueError("vendor and provider aliases disagree")
    normalized_vendor = str(vendor or provider or "").strip().lower()
    if not normalized_vendor:
        raise ValueError("vendor (or provider alias) is required")
    vendors.validate_lane(normalized_lane)
    vendor_spec = vendors.get_vendor(normalized_vendor)
    normalized_model = str(model or model_slug or f"{normalized_vendor}/api").strip()

    explicit_aud = (
        _nonnegative_decimal(amount_aud, "amount_aud")
        if amount_aud is not None
        else None
    )
    if explicit_aud is not None:
        base_usd = Decimal("0")
    elif vendor_spec.kind == "free_tier_attributed":
        if not str(api_call_kind or "").strip():
            raise ValueError("api_call_kind is required for free-tier attribution")
        base_usd = Decimal("0")
    elif vendor_spec.kind == "subscription_bridge":
        base_usd = Decimal("0")
    elif force_zero:
        base_usd = Decimal("0")
    elif vendor_spec.kind == "llm_self_reporting":
        amount = reported_usd if reported_usd is not None else usd_amount
        if amount is None:
            raise ValueError(
                f"reported_usd is required for vendor {normalized_vendor!r}"
            )
        base_usd = _nonnegative_decimal(amount, "reported_usd")
    elif vendor_spec.kind == "voice_metered":
        if voice_minutes is None:
            raise ValueError("voice_minutes is required for voice-metered calls")
        base_usd = _nonnegative_decimal(
            ratecards.retell_usd(float(voice_minutes)),
            "retell usd",
        )
    elif vendor_spec.kind == "search_metered":
        if input_tokens is None or output_tokens is None:
            raise ValueError(
                "input_tokens and output_tokens are required for search-metered calls"
            )
        base_usd = _nonnegative_decimal(
            ratecards.perplexity_usd(input_tokens, output_tokens),
            "perplexity usd",
        )
    else:  # pragma: no cover - registry kind validation protects this branch.
        raise ValueError(f"unsupported vendor kind: {vendor_spec.kind!r}")

    fx_rate = _nonnegative_decimal(cost_config.FX_RATE, "fx_rate")
    surcharge_rate = _nonnegative_decimal(
        Decimal(str(vendor_spec.surcharge_pct)) / Decimal("100"),
        "surcharge",
    )
    surcharge_amount = base_usd * surcharge_rate
    aud_amount = (
        explicit_aud
        if explicit_aud is not None
        else (base_usd + surcharge_amount) * fx_rate
    )
    if explicit_aud is not None:
        denominator = fx_rate * (Decimal("1") + surcharge_rate)
        base_usd = (
            explicit_aud / denominator
            if denominator > 0
            else Decimal("0")
        )
        surcharge_amount = base_usd * surcharge_rate

    normalized_attempt = _optional_nonnegative_int(
        attempt_number, "attempt_number"
    )
    normalized_input = _optional_nonnegative_int(input_tokens, "input_tokens")
    normalized_output = _optional_nonnegative_int(output_tokens, "output_tokens")
    normalized_cached = _optional_nonnegative_int(
        cached_input_tokens, "cached_input_tokens"
    )
    normalized_latency = _optional_nonnegative_int(latency_ms, "latency_ms")
    normalized_voice_minutes = (
        float(_nonnegative_decimal(voice_minutes, "voice_minutes"))
        if voice_minutes is not None
        else None
    )
    raw_meta = _serialize_raw_meta(raw_response_meta)
    is_free_tier = vendor_spec.kind == "free_tier_attributed"
    is_subscription_bridge = vendor_spec.kind == "subscription_bridge"
    normalized_profile, normalized_route = _normalize_attribution(
        profile,
        route,
    )

    normalized_task_id = str(task_id) if task_id is not None else None
    task_cap_exception: Exception | None = None
    resolved_db_path = resolve_db_path(db_path)
    conn = connect(resolved_db_path)
    try:
        with retrying_write_txn(conn):
            if normalized_task_id is not None:
                from hermes_cli.cost.kill_switch import (
                    KillSwitchTripped,
                    PerTaskCapExceeded,
                    is_task_killed,
                    kill_task,
                )

                killed = is_task_killed(normalized_task_id, conn=conn)
                if killed is not None:
                    task_cap_exception = KillSwitchTripped(
                        task_id=normalized_task_id,
                        reason=str(killed["reason"]),
                    )
                elif enforce_task_cap:
                    current_row = conn.execute(
                        """
                        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
                          FROM cost_ledger
                         WHERE task_id = ?
                        """,
                        (normalized_task_id,),
                    ).fetchone()
                    current_total = Decimal(
                        str(current_row["total"] if current_row else 0.0)
                    )
                    projected_total = current_total + aud_amount
                    tasks_exists = conn.execute(
                        """
                        SELECT 1
                          FROM sqlite_master
                         WHERE type = 'table' AND name = 'tasks'
                        """
                    ).fetchone()
                    cap_row = (
                        conn.execute(
                            "SELECT task_cap_aud FROM tasks WHERE id = ?",
                            (normalized_task_id,),
                        ).fetchone()
                        if tasks_exists is not None
                        else None
                    )
                    task_cap = (
                        None
                        if cap_row is None or cap_row["task_cap_aud"] is None
                        else Decimal(str(cap_row["task_cap_aud"]))
                    )
                    if task_cap is None:
                        with _TASK_CAP_WARNING_LOCK:
                            first_missing_cap = (
                                normalized_task_id
                                not in _MISSING_TASK_CAP_WARNED
                            )
                            if first_missing_cap:
                                _MISSING_TASK_CAP_WARNED.add(
                                    normalized_task_id
                                )
                        if first_missing_cap:
                            logger.warning(
                                "Task %s has no task_cap_aud (legacy row); "
                                "allowing cost write",
                                normalized_task_id,
                            )
                    elif projected_total > task_cap:
                        kill_task(
                            task_id=normalized_task_id,
                            killed_by="cost_gate",
                            reason="per_task_cap",
                            notes=(
                                f"current={float(current_total):.6f};"
                                f"projected={float(projected_total):.6f};"
                                f"cap={float(task_cap):.6f};"
                                f"lane={normalized_lane}"
                            ),
                            conn=conn,
                        )
                        conn.execute(
                            """
                            UPDATE tasks
                               SET status = 'failed',
                                   failure_reason = 'per_task_cap_hit'
                             WHERE id = ?
                            """,
                            (normalized_task_id,),
                        )
                        task_cap_exception = PerTaskCapExceeded(
                            task_id=normalized_task_id,
                            current_total=float(current_total),
                            projected_total=float(projected_total),
                            cap=float(task_cap),
                        )

            if task_cap_exception is None:
                cursor = conn.execute(
                    """
                    INSERT INTO cost_ledger (
                        ts, task_id, lane, vendor, model_slug, attempt_number,
                        rung_id, escalation, input_tokens, output_tokens,
                        cached_input_tokens, usd_amount, aud_amount, fx_rate,
                        surcharge_applied, latency_ms, request_id,
                        raw_response_meta, vendor_kind, voice_minutes,
                        api_call_kind, is_free_tier, is_subscription_bridge,
                        profile, route, session_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        utc_now(),
                        normalized_task_id,
                        normalized_lane,
                        normalized_vendor,
                        normalized_model,
                        normalized_attempt,
                        str(rung_id) if rung_id is not None else None,
                        int(bool(escalation)),
                        normalized_input,
                        normalized_output,
                        normalized_cached or 0,
                        float(base_usd),
                        float(aud_amount),
                        float(fx_rate),
                        float(surcharge_amount),
                        normalized_latency,
                        str(request_id) if request_id is not None else None,
                        raw_meta,
                        vendor_spec.kind,
                        normalized_voice_minutes,
                        (
                            str(api_call_kind)
                            if api_call_kind is not None
                            else None
                        ),
                        int(is_free_tier),
                        int(is_subscription_bridge),
                        normalized_profile,
                        normalized_route,
                        (
                            str(session_id)
                            if session_id is not None
                            else None
                        ),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM cost_ledger WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("inserted cost ledger row is missing")
                entry = _row_to_entry(row)

                # Daily/lane/task thresholds remain visible in the returned
                # entry. Normal runtime accounting is advisory; callers that
                # explicitly opt into hard programme enforcement retain the
                # legacy pause behavior.
                from hermes_cli.cost import caps

                breached, which_cap = caps.check_all_caps(
                    normalized_task_id,
                    normalized_lane,
                    bool(escalation),
                    conn=conn,
                )
                if breached and which_cap is not None:
                    amount = caps.breach_amount_aud(
                        which_cap,
                        task_id=normalized_task_id,
                        lane=normalized_lane,
                        conn=conn,
                    )
                    reason = f"cap hit: {which_cap}: {amount:.2f} AUD"
                    transitioned = False
                    if enforce_programme_cap:
                        from hermes_cli.programme import gate as programme_gate

                        _, transitioned = (
                            programme_gate.pause_for_cost_breach_in_transaction(
                                conn,
                                reason,
                            )
                        )
                    entry = replace(
                        entry,
                        breached_cap=which_cap,
                        breach_reason=reason,
                        transitioned_to_paused=transitioned,
                    )
    finally:
        conn.close()

    # Raising inside retrying_write_txn would roll back the kill + FAILED
    # transition. Commit the authoritative state first, alert best-effort,
    # then propagate the original typed exception without wrapping it.
    if task_cap_exception is not None:
        from hermes_cli.cost.kill_switch import PerTaskCapExceeded

        if isinstance(task_cap_exception, PerTaskCapExceeded):
            try:
                from hermes_cli.cost.gate_integration import (
                    send_task_cap_kill_alert,
                )

                send_task_cap_kill_alert(
                    task_id=normalized_task_id,
                    lane=normalized_lane,
                    projected_total=task_cap_exception.projected_total,
                    task_cap_aud=task_cap_exception.cap,
                    db_path=db_path,
                )
            except Exception:
                logger.exception(
                    "Task %s was killed by cap but Telegram alert failed",
                    normalized_task_id,
                )
        raise task_cap_exception

    if (
        not enforce_task_cap
        and not enforce_programme_cap
        and normalized_task_id is not None
        and not is_free_tier
        and not is_subscription_bridge
    ):
        try:
            from hermes_cli.cost import caps
            from hermes_cli.cost.gate_integration import (
                send_task_cost_advisory,
            )

            advisory_conn = connect(resolved_db_path)
            try:
                tracking_threshold = _task_tracking_threshold(
                    advisory_conn,
                    normalized_task_id,
                )
                task_total = caps.task_spend_aud_billable(
                    normalized_task_id,
                    conn=advisory_conn,
                )
                daily_total = caps.daily_spend_aud_billable(
                    conn=advisory_conn,
                )
            finally:
                advisory_conn.close()
            if task_total > tracking_threshold or entry.breach_reason:
                reason = entry.breach_reason or (
                    "task tracking threshold exceeded"
                )
                send_task_cost_advisory(
                    task_id=normalized_task_id,
                    lane=normalized_lane,
                    task_total_aud=task_total,
                    tracking_threshold_aud=tracking_threshold,
                    daily_total_aud=daily_total,
                    reason=reason,
                    db_path=resolved_db_path,
                )
        except Exception:
            logger.exception(
                "Task %s cost advisory failed after ledger row %s",
                normalized_task_id,
                entry.id,
            )

    if is_subscription_bridge:
        try:
            from hermes_cli.cost.gate_integration import record_bridge_turn

            record_bridge_turn(
                task_id=normalized_task_id,
                lane=normalized_lane,
                outcome="success",
                bridge_tier="pro",
                model_reported=normalized_model,
                model_requested=normalized_model,
                turns_consumed=1,
                latency_ms=normalized_latency,
                request_id=(
                    str(request_id) if request_id is not None else None
                ),
                raw_response_meta=raw_response_meta,
                db_path=db_path,
            )
        except Exception as exc:
            logger.warning(
                "Subscription bridge turn write failed after cost row %s: %s: %s",
                entry.id,
                type(exc).__name__,
                exc,
            )
    return entry


def last_entries(n: int = 5) -> list[LedgerEntry]:
    count = max(0, int(n))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM cost_ledger ORDER BY id DESC LIMIT ?", (count,)
        ).fetchall()
        return [_row_to_entry(row) for row in rows]
    finally:
        conn.close()


def task_call_count(task_id: str) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM cost_ledger WHERE task_id = ?",
            (str(task_id),),
        ).fetchone()
        return int(row["count"] if row is not None else 0)
    finally:
        conn.close()


def format_ledger_tail(n: int = 5) -> str:
    rows = last_entries(n)
    if not rows:
        return "(no calls)"
    return "\n".join(
        (
            f"{entry.ts} | {entry.lane} | {entry.vendor} | "
            f"{entry.model_slug} | AUD {entry.aud_amount:.4f} | "
            f"task={entry.task_id or '-'}"
        )
        for entry in rows
    )


migrate()


__all__ = [
    "DB_PATH",
    "LedgerEntry",
    "connect",
    "format_ledger_tail",
    "ensure_migrated",
    "last_entries",
    "migrate",
    "record_call",
    "resolve_db_path",
    "task_call_count",
    "utc_now",
]
