"""Daily rhythm and message supersession.

Covers docs/ux-redesign-plan.md Phase 6: the digest that lets Today report what
happened instead of exposing run mechanics, the scheduler that assembles it, and
the supersession contract that retires a rewritten message server-side.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.server.test_api_mvp import make_client, seed_lead_and_contact, wait_for_run

from server.digest import PLAN, REPORT, build_report, day_key, get_digest, write_digest
from server.scheduler import DailyDigestScheduler


def test_digest_is_written_once_and_never_rewritten():
    """A briefing the operator has read must not change underneath them."""
    app, client, headers, company_id = make_client()
    db = app.state.db
    today = day_key()

    first = write_digest(db, company_id, today, REPORT)
    seed_lead_and_contact(client, headers)  # more activity after the digest
    second = write_digest(db, company_id, today, REPORT)

    assert second == first, "a second write must return the stored digest untouched"
    rows = db.all(
        "SELECT id FROM daily_digests WHERE company_id=? AND digest_date=? AND kind=?",
        (company_id, today, REPORT),
    )
    assert len(rows) == 1


def test_report_counts_business_facts_not_run_mechanics():
    app, client, headers, company_id = make_client()
    seed_lead_and_contact(client, headers)

    report = build_report(app.state.db, company_id, day_key())

    assert report["buyers_found"] == 1
    assert report["contacts_found"] == 1
    # Nothing was approved or delivered, so nothing may claim otherwise.
    assert report["emails_sent"] == 0
    assert report["replies"] == 0
    # No run ids, types, progress or log text may reach a digest payload.
    serialized = str(report)
    for leaked in ("run_", "agent_run", "progress", "lead_scan", "outreach_generation"):
        assert leaked not in serialized, leaked


def test_scheduler_writes_due_digests_and_is_idempotent():
    app, client, headers, company_id = make_client()
    scheduler = DailyDigestScheduler(app.state.db, plan_hour=8, report_hour=18)

    morning = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    assert scheduler.tick(morning) == 1, "only the plan is due at 09:00"
    assert scheduler.tick(morning) == 0, "a second tick writes nothing"

    evening = dt.datetime.now().replace(hour=19, minute=0, second=0, microsecond=0).timestamp()
    assert scheduler.tick(evening) == 1, "the report becomes due at 19:00"
    assert scheduler.tick(evening) == 0

    today = day_key()
    assert get_digest(app.state.db, company_id, today, PLAN) is not None
    assert get_digest(app.state.db, company_id, today, REPORT) is not None


def test_scheduler_writes_nothing_before_the_first_hour():
    app, _, _, _ = make_client()
    scheduler = DailyDigestScheduler(app.state.db, plan_hour=8, report_hour=18)
    dawn = dt.datetime.now().replace(hour=6, minute=0, second=0, microsecond=0).timestamp()
    assert scheduler.tick(dawn) == 0


def test_digest_endpoint_reports_whether_anything_was_scheduled():
    _, client, headers, _ = make_client()

    empty = client.get("/api/v1/activity/digest", headers=headers)
    assert empty.status_code == 200, empty.text
    body = empty.json()
    # Nothing ran, so the endpoint must say so rather than inventing a briefing.
    assert body["plan"] is None and body["report"] is None
    assert body["scheduled"] is False

    built = client.get("/api/v1/activity/digest", headers=headers, params={"refresh": "true"})
    assert built.status_code == 200, built.text
    assert built.json()["report"]["date"] == day_key()

    assert client.get("/api/v1/activity/digest", headers=headers,
                      params={"date": "not-a-date"}).status_code == 422


def test_activity_since_filter_bounds_the_feed():
    _, client, headers, _ = make_client()
    seed_lead_and_contact(client, headers)
    everything = client.get("/api/v1/activity", headers=headers)
    assert everything.status_code == 200
    assert len(everything.json()) > 0

    future = client.get("/api/v1/activity", headers=headers,
                        params={"since": time.time() + 3600})
    assert future.status_code == 200 and future.json() == []

    assert client.get("/api/v1/activity", headers=headers,
                      params={"since": -1}).status_code == 422


def test_rewrite_supersedes_the_original_server_side():
    """The approval queue must not have to guess which version is current."""
    _, client, headers, _ = make_client()
    lead, contact = seed_lead_and_contact(client, headers)

    generated = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers, json={})
    assert generated.status_code in (200, 202), generated.text
    original_id = wait_for_run(client, headers, generated.json()["id"])["output_ref"]
    assert client.get(f"/api/v1/outreach/messages/{original_id}",
                      headers=headers).json()["superseded_by"] is None

    rewrite = client.post(f"/api/v1/outreach/messages/{original_id}/regenerate", headers=headers)
    assert rewrite.status_code == 202, rewrite.text
    replacement_id = wait_for_run(client, headers, rewrite.json()["id"])["output_ref"]
    assert replacement_id != original_id

    original = client.get(f"/api/v1/outreach/messages/{original_id}", headers=headers).json()
    replacement = client.get(f"/api/v1/outreach/messages/{replacement_id}", headers=headers).json()
    assert original["superseded_by"] == replacement_id
    assert replacement["superseded_by"] is None


def test_supersession_never_rewrites_delivered_history():
    """Only a still-reviewable message may be retired: an approved or sent
    message is a record of what the operator agreed to."""
    app, client, headers, company_id = make_client()
    lead, contact = seed_lead_and_contact(client, headers)
    generated = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers, json={})
    message_id = wait_for_run(client, headers, generated.json()["id"])["output_ref"]

    app.state.db.execute("UPDATE outreach_messages SET status='sent' WHERE id=?", (message_id,))
    rewrite = client.post(f"/api/v1/outreach/messages/{message_id}/regenerate", headers=headers)
    wait_for_run(client, headers, rewrite.json()["id"])

    sent = client.get(f"/api/v1/outreach/messages/{message_id}", headers=headers).json()
    assert sent["superseded_by"] is None, "a sent message must never be retired by a rewrite"
