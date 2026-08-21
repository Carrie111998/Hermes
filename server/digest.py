"""Daily plan and report.

company-packs/silverline/business-rules.md:17-21 specifies the operating rhythm
this module implements: the operator gets a plan each morning and a results
report each evening, written as business reports — "never technical logs, error
dumps, or workflow mechanics".

Two rules shape everything here:

1. **Digests are stored, not recomputed.** A report read at 22:00 must be the
   one assembled at 18:00, and a plan must not silently rewrite itself as the
   day goes on. Recomputing on read would make the briefing disagree with
   itself between refreshes.
2. **No mechanics leak.** Every value assembled here is a business fact —
   companies found, emails written, replies received. Run ids, run types,
   progress percentages and log lines never enter a digest payload; the
   frontend renders these numbers as sentences.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .db import Database, json_dump, json_load, new_id, now

PLAN = "plan"
REPORT = "report"
KINDS = (PLAN, REPORT)


def day_key(moment: float | None = None) -> str:
    """YYYY-MM-DD for a timestamp. The tenant day is server-local by design:
    a single connected mailbox belongs to one office, and inventing per-tenant
    timezones here would disagree with the send-window logic in
    outreach_service, which already resolves times per recipient country."""
    stamp = dt.datetime.fromtimestamp(moment if moment is not None else now())
    return stamp.strftime("%Y-%m-%d")


def day_bounds(date: str) -> tuple[float, float]:
    """[start, end) epoch bounds for a YYYY-MM-DD day."""
    start = dt.datetime.strptime(date, "%Y-%m-%d")
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def _count(db: Database, sql: str, params: tuple) -> int:
    row = db.one(sql, params)
    return int(row[0]) if row and row[0] is not None else 0


def build_plan(db: Database, company_id: str, date: str) -> dict[str, Any]:
    """What the agent intends to do today, from the tenant's own setup."""
    markets = [row["country_code"] for row in db.all(
        "SELECT country_code FROM selected_countries WHERE company_id=? ORDER BY country_code",
        (company_id,),
    )]
    waiting = _count(
        db,
        "SELECT COUNT(*) FROM outreach_messages WHERE company_id=? "
        "AND status IN ('pending_approval','qa_failed') AND superseded_by IS NULL",
        (company_id,),
    )
    unresearched = _count(
        db,
        "SELECT COUNT(*) FROM leads WHERE company_id=? AND status='new' AND do_not_contact=0",
        (company_id,),
    )
    return {
        "date": date,
        "markets": markets,
        "emails_waiting": waiting,
        "buyers_to_research": unresearched,
    }


def build_report(db: Database, company_id: str, date: str) -> dict[str, Any]:
    """What actually happened today. Counted from persisted business records,
    never from run bookkeeping."""
    start, end = day_bounds(date)
    window = (company_id, start, end)
    return {
        "date": date,
        "buyers_found": _count(
            db, "SELECT COUNT(*) FROM leads WHERE company_id=? AND created_at>=? AND created_at<?", window),
        "emails_written": _count(
            db,
            "SELECT COUNT(*) FROM outreach_messages WHERE company_id=? AND created_at>=? AND created_at<? "
            "AND superseded_by IS NULL",
            window),
        "emails_sent": _count(
            db,
            "SELECT COUNT(*) FROM outreach_messages WHERE company_id=? AND sent_at>=? AND sent_at<?", window),
        "replies": _count(
            db,
            "SELECT COUNT(*) FROM outreach_messages WHERE company_id=? AND replied_at>=? AND replied_at<?", window),
        "contacts_found": _count(
            db, "SELECT COUNT(*) FROM contacts WHERE company_id=? AND created_at>=? AND created_at<?", window),
        # Carried so the evening report can still say what needs the human.
        "emails_waiting": _count(
            db,
            "SELECT COUNT(*) FROM outreach_messages WHERE company_id=? "
            "AND status IN ('pending_approval','qa_failed') AND superseded_by IS NULL",
            (company_id,),
        ),
        # Work the agent could not finish, phrased for the frontend to translate.
        "unfinished": _count(
            db,
            "SELECT COUNT(*) FROM agent_runs WHERE company_id=? AND status='failed' "
            "AND created_at>=? AND created_at<?",
            window),
    }


BUILDERS = {PLAN: build_plan, REPORT: build_report}


def get_digest(db: Database, company_id: str, date: str, kind: str) -> dict[str, Any] | None:
    row = db.one(
        "SELECT * FROM daily_digests WHERE company_id=? AND digest_date=? AND kind=?",
        (company_id, date, kind),
    )
    if not row:
        return None
    return {
        "date": row["digest_date"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        **json_load(row["data"], {}),
    }


def write_digest(db: Database, company_id: str, date: str, kind: str) -> dict[str, Any]:
    """Assemble and persist one digest. Idempotent: a digest already written for
    this (company, date, kind) is returned untouched, so a scheduler restart or
    a double tick never rewrites a briefing the operator has already read."""
    if kind not in BUILDERS:
        raise ValueError(f"unknown digest kind: {kind}")
    existing = get_digest(db, company_id, date, kind)
    if existing:
        return existing
    payload = BUILDERS[kind](db, company_id, date)
    db.execute(
        # ON CONFLICT, not INSERT OR IGNORE: the latter is SQLite-only syntax and
        # this statement runs on Postgres in production. The conflict target is
        # the table's own uniqueness — one digest per tenant, day and kind.
        "INSERT INTO daily_digests(id,company_id,digest_date,kind,data,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,digest_date,kind) DO NOTHING",
        (new_id("dig"), company_id, date, kind, json_dump(payload), now()),
    )
    db.activity(company_id, None, f"daily_{kind}_ready", "digest", date, {"kind": kind})
    return get_digest(db, company_id, date, kind) or payload
