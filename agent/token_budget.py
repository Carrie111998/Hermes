"""Global daily LLM token budget — reserve / settle ledger on ``state.db``.

A daily **admission budget** for the tokens spent by the **main agent loop**
(``agent/conversation_loop.run_conversation``), shared across every surface
that runs that loop (CLI, TUI, desktop, gateway, cron, subagents) and across
concurrent processes, because the ledger lives in the profile's ``state.db``
rather than in memory.

What it actually guarantees: no provider attempt is *admitted* unless its
estimate still fits in the day. It is not an absolute ceiling on tokens spent,
and cannot be — the spend of a call is only known once the provider answers,
by which point the tokens are gone. So a day can finish over ``daily_tokens``
by the amount the last admitted response exceeded its reservation: one API
response's overshoot, not an unbounded one, since the overrun is charged in
full and the next attempt is judged against the corrected total.

Scope — what is *not* metered (see also the ``budget`` section of
``website/docs/user-guide/configuration.md``): auxiliary provider calls that
do not go through the agent loop. Context compression / summarisation, memory
and curator passes, title generation, embeddings, and anything else built on
``agent/auxiliary_client.py`` spend real tokens that this ledger never sees;
so does a MoA advisor fan-out, whose per-advisor calls happen inside the
client and are not the loop's own attempt. The budget therefore bounds
admitted agent-loop attempts, not the process's total spend.

Configuration (top level of ``config.yaml``)::

    budget:
      daily_tokens: 2000000   # 0 / null disables the feature entirely
      timezone: "America/Sao_Paulo"   # empty → the global `timezone` setting

Accounting model — reserve, then settle, once per *physical provider attempt*
(a retried call reserves again, because a retry hits the wire again):

1. **Reserve** (before the attempt): the caller reserves its *estimate*
   (rough prompt estimate + the response cap). The reservation is a row, so a
   second process racing the same remaining budget sees it immediately and is
   denied. Reservations never count as spend.
2. **Settle** (the provider answered): the reservation row is swapped for the
   provider's *actual* token count in the same transaction. Over- and
   under-estimates self-correct on every attempt. A response that reports no
   usage is settled for the reservation amount instead — a real call charged
   at its estimate, never at zero — because the alternative (releasing it)
   would let unmetered responses run the day indefinitely.
3. **Release** (no provider response at all): the reservation row is dropped
   and nothing is charged. Reserved for attempts that raised or were cancelled
   before any answer arrived; an attempt holding a response settles instead,
   however the caller then disposes of that response (discarded on a redirect,
   retried after truncation, returned as a refusal).

Settle is exactly idempotent: it charges only when it is the call that removed
the reservation row. Settling twice, settling a released reservation, or
settling an unknown id records nothing, so no accounting bug can double-charge
a day.

Reservations are never reclaimed on a timer. Freeing a claim that cannot be
proven finished defeats the point of admission control: the freed call would
later settle into a row that no longer exists and, under exactly-once settle,
its spend would vanish. Claims are released only by their owner (the agent
loop releases at every retry, iteration, and turn boundary, including the
``finally`` on abort paths). A SIGKILLed process therefore holds its claim for
the remainder of that day — capacity lost, admission control intact. The rows
are swept by :data:`ABANDONED_ROW_MAX_AGE_SECONDS` only once they belong to a
past day *and* are a day old, which is pure table hygiene and can never touch
a reservation counted against the current day.

Threshold notifications (50% / 75%) are claimed inside the settle transaction,
so exactly one process per day emits each one no matter how many are running.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Table hygiene only — NOT budget reclamation. A reservation row is deleted as
# abandoned once it is this old *and* belongs to a day other than the one being
# written, so it is not counted against any live budget and cannot belong to a
# call still in flight (no provider call spans a day). Never applied to the
# current day: see the module docstring on why a timer must not free a claim.
ABANDONED_ROW_MAX_AGE_SECONDS = 86400.0

# Used-fraction marks that emit a one-time-per-day status notification.
NOTIFY_MARKS: Tuple[int, ...] = (50, 75)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_budget_days (
    day TEXT PRIMARY KEY,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    notified_marks TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_budget_reservations (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    created_at REAL NOT NULL,
    owner_pid INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_llm_budget_res_day
    ON llm_budget_reservations(day, created_at);
"""


# ── Settings ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetSettings:
    """Resolved ``budget:`` section of config.yaml."""

    daily_tokens: int = 0
    timezone: str = ""

    @property
    def enabled(self) -> bool:
        return self.daily_tokens > 0


def _coerce_daily_tokens(raw: Any) -> int:
    """Parse ``budget.daily_tokens`` into a non-negative int (0 = disabled).

    Accepts ints, floats, and numeric strings (``"2_000_000"``, ``"2000000"``)
    so a hand-edited config.yaml with a quoted number still works. Anything
    unparseable disables the feature rather than raising — a malformed budget
    key must never block the agent loop.

    Non-finite values (``.inf`` / ``.nan``, which YAML parses into real floats,
    and their string spellings) disable the feature too: ``int(inf)`` raises
    OverflowError and ``int(nan)`` raises ValueError, and "no limit" is spelled
    ``0`` here, not infinity.
    """
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        # Exact: an arbitrarily large int must not round-trip through float.
        return raw if raw > 0 else 0
    if isinstance(raw, (float, str)):
        if isinstance(raw, str):
            text = raw.strip().replace("_", "").replace(",", "")
            if not text:
                return 0
        else:
            text = raw
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Invalid budget.daily_tokens %r — budget disabled", raw)
            return 0
        # NaN fails every comparison, so this also rejects it.
        if not (float("-inf") < number < float("inf")):
            logger.warning(
                "Non-finite budget.daily_tokens %r — budget disabled (use 0 for "
                "no limit)",
                raw,
            )
            return 0
        try:
            value = int(number)
        except (ValueError, OverflowError):  # pragma: no cover - defensive
            logger.warning("Invalid budget.daily_tokens %r — budget disabled", raw)
            return 0
    else:
        logger.warning("Invalid budget.daily_tokens %r — budget disabled", raw)
        return 0
    return value if value > 0 else 0


def load_budget_settings() -> BudgetSettings:
    """Read the ``budget:`` section from config.yaml (fail-open to disabled).

    Uses ``load_config_readonly()`` so both the CLI loader and the gateway see
    the same merged defaults, and so the per-API-call read is a cache hit.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        logger.debug("budget settings load failed (fail-open)", exc_info=True)
        return BudgetSettings()

    section = config.get("budget")
    if not isinstance(section, dict):
        return BudgetSettings()

    tz = section.get("timezone", "")
    return BudgetSettings(
        daily_tokens=_coerce_daily_tokens(section.get("daily_tokens")),
        timezone=tz.strip() if isinstance(tz, str) else "",
    )


def budget_now(settings: Optional[BudgetSettings] = None) -> datetime:
    """Current wall-clock time in the budget's timezone.

    ``budget.timezone`` wins; otherwise fall back to the global Hermes clock
    (``HERMES_TIMEZONE`` → ``timezone:`` → server local), so a user who
    already set their timezone once does not have to repeat it here.
    """
    settings = settings or load_budget_settings()
    name = settings.timezone
    if name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(name))
        except Exception:
            logger.warning(
                "Invalid budget.timezone %r — falling back to the Hermes clock",
                name,
            )
    from hermes_time import now as hermes_now

    return hermes_now()


def current_day(settings: Optional[BudgetSettings] = None) -> str:
    """The ledger key for "today" (``YYYY-MM-DD``) in the budget timezone."""
    return budget_now(settings).strftime("%Y-%m-%d")


# ── Value objects ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reservation:
    """A held claim on part of today's budget. Settle or release it."""

    id: str
    day: str
    tokens: int


@dataclass(frozen=True)
class BudgetSnapshot:
    """Point-in-time view of one day's ledger."""

    day: str
    limit: int
    used: int
    reserved: int
    timezone: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    @property
    def used_percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(100.0, self.used / self.limit * 100.0)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass
class ReserveOutcome:
    """Result of :meth:`DailyTokenBudget.reserve`.

    ``status`` is ``"disabled"`` (no budget configured), ``"granted"``, or
    ``"denied"``. Callers only need to block on ``"denied"``.
    """

    status: str
    reservation: Optional[Reservation] = None
    snapshot: Optional[BudgetSnapshot] = None
    message: str = ""

    @property
    def denied(self) -> bool:
        return self.status == "denied"


@dataclass
class SettleOutcome:
    """Result of :meth:`DailyTokenBudget.settle`."""

    snapshot: Optional[BudgetSnapshot] = None
    crossed_marks: List[int] = field(default_factory=list)


# ── Ledger ──────────────────────────────────────────────────────────────────


def default_db_path() -> Path:
    """Profile-scoped ledger location — the shared ``state.db``."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


class DailyTokenBudget:
    """Atomic reserve/settle ledger backed by ``state.db``.

    One connection is opened per operation and closed immediately: the
    gateway calls this from many threads, and the ledger writes are tiny
    compared with the provider call they guard.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._init_lock = threading.Lock()
        self._initialized = False

    # -- plumbing ----------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self._db_path if self._db_path is not None else default_db_path()

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        from hermes_cli.sqlite_safe_read import connect_tracked

        conn = connect_tracked(
            path,
            connect_fn=sqlite3.connect,
            isolation_level=None,  # explicit BEGIN IMMEDIATE below
            timeout=30.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            with self._init_lock:
                if not self._initialized:
                    from hermes_state import apply_wal_with_fallback

                    apply_wal_with_fallback(conn, db_label="state.db")
                    conn.executescript(_SCHEMA_SQL)
                    self._initialized = True
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _sweep_abandoned(conn: sqlite3.Connection, day: str, now: float) -> None:
        """Delete reservation rows that no live budget can ever depend on.

        Both guards matter and neither is sufficient alone: ``day <> ?`` keeps
        this from touching anything counted against the day being written, and
        the age guard keeps it from touching a call that crossed midnight and
        is still in flight. What is left is a row from a past day that is over
        a day old — dead weight in the table, worth nothing to anyone.

        This is deliberately *not* a reclamation timer. Freeing a claim whose
        owner may still be talking to the provider would let that spend settle
        into nothing, so the day would keep admitting attempts against budget
        it had already spent.
        """
        conn.execute(
            "DELETE FROM llm_budget_reservations WHERE day <> ? AND created_at < ?",
            (day, now - ABANDONED_ROW_MAX_AGE_SECONDS),
        )

    @staticmethod
    def _read_day(conn: sqlite3.Connection, day: str) -> Tuple[int, str]:
        row = conn.execute(
            "SELECT used_tokens, notified_marks FROM llm_budget_days WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            return 0, ""
        return int(row["used_tokens"] or 0), str(row["notified_marks"] or "")

    @staticmethod
    def _read_reserved(conn: sqlite3.Connection, day: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS total "
            "FROM llm_budget_reservations WHERE day = ?",
            (day,),
        ).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    # -- operations --------------------------------------------------------

    def reserve(
        self,
        estimated_tokens: int,
        *,
        settings: Optional[BudgetSettings] = None,
    ) -> ReserveOutcome:
        """Claim ``estimated_tokens`` of today's budget before a provider call.

        Denies when the estimate does not fit in what is left after committed
        spend and other in-flight reservations. The whole read-decide-write
        runs inside one ``BEGIN IMMEDIATE`` transaction, so two processes
        cannot both be granted the last of the budget.
        """
        settings = settings or load_budget_settings()
        if not settings.enabled:
            return ReserveOutcome(status="disabled")

        estimate = max(0, int(estimated_tokens or 0))
        day = current_day(settings)
        now = time.time()
        reservation_id = uuid.uuid4().hex

        try:
            conn = self._connect()
        except Exception:
            # Ledger unavailable (locked / read-only / corrupt). Fail OPEN:
            # a budget we cannot read must not brick the agent.
            logger.warning("daily budget ledger unavailable — allowing call", exc_info=True)
            return ReserveOutcome(status="disabled")

        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._sweep_abandoned(conn, day, now)
                used, _marks = self._read_day(conn, day)
                reserved = self._read_reserved(conn, day)
                snapshot = BudgetSnapshot(
                    day=day,
                    limit=settings.daily_tokens,
                    used=used,
                    reserved=reserved,
                    timezone=settings.timezone,
                )
                if used + reserved + estimate > settings.daily_tokens:
                    conn.execute("ROLLBACK")
                    return ReserveOutcome(
                        status="denied",
                        snapshot=snapshot,
                        message=denial_message(snapshot, estimate),
                    )
                conn.execute(
                    "INSERT INTO llm_budget_reservations "
                    "(id, day, tokens, created_at, owner_pid) VALUES (?, ?, ?, ?, ?)",
                    (reservation_id, day, estimate, now, os.getpid()),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        except Exception:
            logger.warning("daily budget reserve failed — allowing call", exc_info=True)
            return ReserveOutcome(status="disabled")
        finally:
            conn.close()

        return ReserveOutcome(
            status="granted",
            reservation=Reservation(id=reservation_id, day=day, tokens=estimate),
            snapshot=BudgetSnapshot(
                day=day,
                limit=settings.daily_tokens,
                used=snapshot.used,
                reserved=snapshot.reserved + estimate,
                timezone=settings.timezone,
            ),
        )

    def settle(
        self,
        reservation: Optional[Reservation],
        actual_tokens: int,
        *,
        settings: Optional[BudgetSettings] = None,
    ) -> SettleOutcome:
        """Swap a reservation for the tokens the attempt actually cost.

        ``actual_tokens`` is the provider's reported total when there is one;
        callers that got a response *without* usage pass the reservation
        amount, because an unmeasured response is still a charge (see the
        module docstring). Only an attempt with no provider response at all
        goes to :meth:`release`.

        Exactly idempotent. The ``DELETE`` of the reservation row is the
        permit: usage is added only by the call that removed the row, in the
        same transaction. A second settle of the same reservation, a settle
        after :meth:`release`, and a settle of an id that was never in the
        table all find nothing to delete and therefore charge nothing — they
        report the current ledger and announce no marks. Callers may retry
        freely without inflating the day.

        Returns the post-settlement snapshot plus any threshold marks this
        call is responsible for announcing. A mark is claimed inside the same
        transaction, so exactly one process ever emits it for a given day.
        """
        if reservation is None:
            return SettleOutcome()
        settings = settings or load_budget_settings()
        actual = max(0, int(actual_tokens or 0))
        day = reservation.day
        now = time.time()

        try:
            conn = self._connect()
        except Exception:
            logger.warning("daily budget ledger unavailable — spend not recorded", exc_info=True)
            return SettleOutcome()

        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                charged = bool(
                    conn.execute(
                        "DELETE FROM llm_budget_reservations WHERE id = ?",
                        (reservation.id,),
                    ).rowcount
                )
                if charged:
                    conn.execute(
                        "INSERT INTO llm_budget_days (day, used_tokens, notified_marks, updated_at) "
                        "VALUES (?, ?, '', ?) "
                        "ON CONFLICT(day) DO UPDATE SET "
                        "used_tokens = used_tokens + excluded.used_tokens, "
                        "updated_at = excluded.updated_at",
                        (day, actual, now),
                    )
                else:
                    # Already settled or released. Charging here is how a
                    # retried settle would double-bill a day, so don't.
                    logger.debug(
                        "budget reservation %s is not outstanding; settle is a no-op",
                        reservation.id,
                    )
                used, marks_raw = self._read_day(conn, day)
                reserved = self._read_reserved(conn, day)
                snapshot = BudgetSnapshot(
                    day=day,
                    limit=settings.daily_tokens,
                    used=used,
                    reserved=reserved,
                    timezone=settings.timezone,
                )
                claimed = {m for m in marks_raw.split(",") if m}
                crossed: List[int] = []
                if charged and settings.enabled:
                    for mark in NOTIFY_MARKS:
                        if snapshot.used_percent >= mark and str(mark) not in claimed:
                            claimed.add(str(mark))
                            crossed.append(mark)
                if crossed:
                    conn.execute(
                        "UPDATE llm_budget_days SET notified_marks = ? WHERE day = ?",
                        (",".join(sorted(claimed, key=int)), day),
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        except Exception:
            logger.warning("daily budget settle failed", exc_info=True)
            return SettleOutcome()
        finally:
            conn.close()

        return SettleOutcome(snapshot=snapshot, crossed_marks=crossed)

    def release(self, reservation: Optional[Reservation]) -> None:
        """Drop a reservation for an attempt that got no provider response.

        Nothing is charged, so this is only correct when the attempt is known
        to have produced no response (transport exception, cancellation before
        an answer). Anything that did come back off the wire settles.
        """
        if reservation is None:
            return
        try:
            conn = self._connect()
        except Exception:
            logger.debug("daily budget release skipped (ledger unavailable)", exc_info=True)
            return
        try:
            conn.execute(
                "DELETE FROM llm_budget_reservations WHERE id = ?",
                (reservation.id,),
            )
        except Exception:
            logger.debug("daily budget release failed", exc_info=True)
        finally:
            conn.close()

    def snapshot(
        self, *, settings: Optional[BudgetSettings] = None
    ) -> Optional[BudgetSnapshot]:
        """Today's ledger state, or ``None`` when no budget is configured."""
        settings = settings or load_budget_settings()
        if not settings.enabled:
            return None
        day = current_day(settings)
        try:
            conn = self._connect()
        except Exception:
            logger.debug("daily budget snapshot unavailable", exc_info=True)
            return None
        try:
            # Read-only by construction: rendering ``/usage`` or a status line
            # must never mutate the ledger, least of all by freeing someone
            # else's in-flight claim.
            used, _marks = self._read_day(conn, day)
            reserved = self._read_reserved(conn, day)
        except Exception:
            logger.debug("daily budget snapshot failed", exc_info=True)
            return None
        finally:
            conn.close()
        return BudgetSnapshot(
            day=day,
            limit=settings.daily_tokens,
            used=used,
            reserved=reserved,
            timezone=settings.timezone,
        )


# ── Process-wide accessor ───────────────────────────────────────────────────

_LEDGER_LOCK = threading.Lock()
_LEDGER: Optional[DailyTokenBudget] = None
_LEDGER_PATH: Optional[str] = None


def get_ledger() -> DailyTokenBudget:
    """Return the ledger for the active profile.

    Re-created when ``HERMES_HOME`` changes (profile switch, test isolation)
    so a cached instance never writes into the previous profile's state.db.
    """
    global _LEDGER, _LEDGER_PATH
    path = str(default_db_path())
    with _LEDGER_LOCK:
        if _LEDGER is None or _LEDGER_PATH != path:
            _LEDGER = DailyTokenBudget()
            _LEDGER_PATH = path
        return _LEDGER


def reset_ledger_cache() -> None:
    """Drop the cached ledger (tests / profile switches)."""
    global _LEDGER, _LEDGER_PATH
    with _LEDGER_LOCK:
        _LEDGER = None
        _LEDGER_PATH = None


# ── Rendering ───────────────────────────────────────────────────────────────


def _reset_hint(snapshot: BudgetSnapshot) -> str:
    tz = snapshot.timezone or ""
    return f" • resets at midnight {tz}".rstrip() if tz else " • resets at midnight"


def denial_message(snapshot: BudgetSnapshot, estimated_tokens: int) -> str:
    """User-facing text for a request that does not fit in today's budget."""
    return (
        "🛑 Daily LLM token budget reached — this request needs about "
        f"{estimated_tokens:,} tokens but only {snapshot.remaining:,} of the "
        f"{snapshot.limit:,}-token daily budget remain "
        f"({snapshot.used:,} used today{_reset_hint(snapshot)}). "
        "Raise `budget.daily_tokens` in config.yaml or continue tomorrow."
    )


def threshold_message(snapshot: BudgetSnapshot, mark: int) -> str:
    """One-time-per-day notification text for a crossed usage mark."""
    return (
        f"📉 Daily LLM budget {mark}% used — {snapshot.used:,} / "
        f"{snapshot.limit:,} tokens today ({snapshot.remaining:,} remaining"
        f"{_reset_hint(snapshot)})."
    )


def _gauge(percent: float, width: int = 20) -> str:
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def daily_budget_lines(
    *,
    markdown: bool = False,
    snapshot: Optional[BudgetSnapshot] = None,
) -> List[str]:
    """Render the global daily-budget block for ``/usage``.

    Returns ``[]`` when no budget is configured, so both call sites can
    unconditionally extend their output. Shared by the CLI and the gateway so
    the two surfaces cannot drift.
    """
    if snapshot is None:
        try:
            snapshot = get_ledger().snapshot()
        except Exception:
            logger.debug("daily budget lines unavailable", exc_info=True)
            return []
    if snapshot is None:
        return []

    bold = "**" if markdown else ""
    pct = snapshot.used_percent
    lines = [f"🌐 {bold}Daily LLM Budget{bold} ({snapshot.day})"]
    lines.append(
        f"{_gauge(pct)} {pct:.0f}% — {snapshot.used:,} / {snapshot.limit:,} tokens"
    )
    lines.append(f"Remaining: {snapshot.remaining:,} tokens")
    if snapshot.reserved:
        lines.append(f"In flight (reserved): {snapshot.reserved:,} tokens")
    if snapshot.timezone:
        lines.append(f"Resets at midnight {snapshot.timezone}")
    return lines


# ── Convenience wrappers (module-level API used by the agent loop) ──────────


def reserve(estimated_tokens: int) -> ReserveOutcome:
    return get_ledger().reserve(estimated_tokens)


def settle(reservation: Optional[Reservation], actual_tokens: int) -> SettleOutcome:
    return get_ledger().settle(reservation, actual_tokens)


def release(reservation: Optional[Reservation]) -> None:
    get_ledger().release(reservation)


def snapshot() -> Optional[BudgetSnapshot]:
    return get_ledger().snapshot()


__all__ = [
    "BudgetSettings",
    "BudgetSnapshot",
    "DailyTokenBudget",
    "ABANDONED_ROW_MAX_AGE_SECONDS",
    "NOTIFY_MARKS",
    "Reservation",
    "ReserveOutcome",
    "SettleOutcome",
    "current_day",
    "daily_budget_lines",
    "default_db_path",
    "denial_message",
    "get_ledger",
    "load_budget_settings",
    "release",
    "reserve",
    "reset_ledger_cache",
    "settle",
    "snapshot",
    "threshold_message",
]
