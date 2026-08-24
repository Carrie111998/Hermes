"""Bounded stale-fact refresh scheduling."""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.agent_service import AgentRunService, StubRunExecutor
from server.db import Database, json_dump, now
from server.lead_research.facts import FactRepository
from server.lead_research.models import (
    AgenticResearchResult,
    EvidenceSpan,
    ProposedFact,
    ResearchFact,
    ResearchPage,
)
from server.lead_research.service import ResearchRefreshService
from server.scheduler import DailyDigestScheduler


NOW = 2_000_000_000.0


def _context(tmp_path, executor=None):
    db = Database(tmp_path / "refresh.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "Acme", "active", "{}", stamp, stamp),
    )
    db.execute(
        "INSERT INTO organizations("
        "id,company_id,display_name,normalized_name,domain,country,data,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        ("org_a", "cmp_a", "Buyer GmbH", "buyer gmbh", "buyer.example", "DE", "{}", stamp, stamp),
    )
    runs = AgentRunService(db, executor or StubRunExecutor())
    return db, runs, ResearchRefreshService(db, runs)


def _fact(field: str, expires_at: float, *, evidence_id: str) -> ResearchFact:
    return ResearchFact(
        organization_id="org_a", field=field, value_en=f"{field} value",
        original_text=f"{field} source", source_language="en",
        derivation_kind="observed", status="observed", confidence=.9,
        validation_basis="exact official span", evidence_id=evidence_id,
        span=EvidenceSpan(original=f"{field} source", start=0, end=len(field) + 7),
        source_class="official", visibility="public", mechanically_validated=True,
        observed_at=NOW - 100, retrieved_at=NOW - 50, expires_at=expires_at,
    )


def test_refresh_enqueues_only_due_consumed_facts_and_respects_limit(tmp_path):
    db, runs, refresh = _context(tmp_path)
    facts = FactRepository(db)
    stale = facts.accept("cmp_a", _fact("recent_hiring", NOW - 1, evidence_id="ev_stale"))
    facts.accept("cmp_a", _fact("founded_year", NOW + 10_000, evidence_id="ev_fresh"))

    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=1) == 1
    scheduled = [run for run in runs.list("cmp_a") if run["run_type"] == "lead_research_refresh"]
    assert len(scheduled) == 1
    assert scheduled[0]["status"] != "queued"
    assert scheduled[0]["payload"]["fact_id"] == stale.id
    assert scheduled[0]["payload"]["field"] == "recent_hiring"
    assert scheduled[0]["payload"]["budget"]["page_limit"] <= 2


def test_same_due_fact_is_not_enqueued_twice(tmp_path):
    db, runs, refresh = _context(tmp_path)
    FactRepository(db).accept(
        "cmp_a", _fact("procurement_signal", NOW - 1, evidence_id="ev_due"),
    )

    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=10) == 1
    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=10) == 0
    assert len(runs.list("cmp_a")) == 1


def test_zero_limit_never_scans_or_enqueues(tmp_path):
    db, runs, refresh = _context(tmp_path)
    FactRepository(db).accept("cmp_a", _fact("legal_status", NOW - 1, evidence_id="ev_due"))

    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=0) == 0
    assert runs.list("cmp_a") == []


def test_warm_refresh_does_not_rewrite_historical_campaign_score(tmp_path):
    db, _runs, refresh = _context(tmp_path)
    fact = FactRepository(db).accept(
        "cmp_a", _fact("recent_hiring", NOW - 1, evidence_id="ev_due"),
    )
    snapshot = {"fit_score": 72, "fact_ids": [fact.id], "evidence_confidence": 61}
    db.execute(
        "INSERT INTO research_campaigns(id,company_id,name,config,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("campaign_1", "cmp_a", "Historical", "{}", NOW - 200, NOW - 200),
    )
    db.execute(
        "INSERT INTO research_score_snapshots("
        "id,company_id,result_id,campaign_id,profile_version_id,organization_id,snapshot_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        ("score_1", "cmp_a", "result_1", "campaign_1", None, "org_a",
         json_dump(snapshot), NOW - 100),
    )

    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=10) == 1
    stored = db.one("SELECT snapshot_json FROM research_score_snapshots WHERE id='score_1'")
    assert stored["snapshot_json"] == json_dump(snapshot)


def test_scheduler_invokes_refresh_once_due_hour_without_changing_digest_count(tmp_path):
    db, runs, refresh = _context(tmp_path)
    FactRepository(db).accept("cmp_a", _fact("recent_hiring", NOW - 1, evidence_id="ev_due"))
    scheduler = DailyDigestScheduler(
        db, plan_hour=23, report_hour=23,
        research_refresh=refresh, research_refresh_enabled=True,
        research_refresh_hour=8, research_refresh_batch_limit=3,
    )
    morning = dt.datetime.fromtimestamp(NOW).replace(hour=9).timestamp()

    assert scheduler.tick(morning) == 0
    assert len([run for run in runs.list("cmp_a") if run["run_type"] == "lead_research_refresh"]) == 1
    assert scheduler.tick(morning) == 0
    assert len(runs.list("cmp_a")) == 1


class RefreshExecutor(StubRunExecutor):
    def execute(self, service, run):
        if run["run_type"] != "lead_research_refresh":
            return super().execute(service, run)
        content = "Buyer GmbH now has recent hiring activity."
        literal = "recent hiring activity"
        start = content.index(literal)
        return AgenticResearchResult(
            pages=[ResearchPage(
                page_id="refresh-page",
                source_id="agentic-web",
                canonical_url="https://buyer.example/careers",
                snapshot_content=content,
                raw_hash=hashlib.sha256(content.encode()).hexdigest(),
                source_language="en",
                source_class="official",
                visibility="public",
                retrieved_at=datetime.now(timezone.utc),
            )],
            facts=[ProposedFact(
                field="recent_hiring",
                value_en=literal,
                original_text=literal,
                source_language="en",
                derivation_kind="observed",
                confidence=.9,
                validation_basis="refresh exact span",
                page_id="refresh-page",
                span=EvidenceSpan(
                    original=literal, start=start, end=start + len(literal),
                ),
                observed_at=NOW,
            )],
            unresolved_fields=[],
            requests_started=1,
            tokens_used=100,
            stop_reason="required_coverage",
        ).model_dump(mode="json")


def test_due_refresh_is_started_and_persists_only_the_target_field(tmp_path):
    db, runs, refresh = _context(tmp_path, RefreshExecutor())
    stale = FactRepository(db).accept(
        "cmp_a", _fact("recent_hiring", NOW - 1, evidence_id="ev_due"),
    )

    assert refresh.enqueue_due(dt.datetime.fromtimestamp(NOW), limit=10) == 1
    deadline = time.monotonic() + 3
    run = runs.list("cmp_a")[0]
    while run["status"] not in {"succeeded", "failed", "cancelled"}:
        assert time.monotonic() < deadline
        time.sleep(.01)
        run = runs.get("cmp_a", run["id"])

    assert run["status"] == "succeeded", run
    refreshed = FactRepository(db).reusable(
        "cmp_a", "org_a", {"recent_hiring"}, NOW + 1,
    )
    assert stale.id not in {fact.id for fact in refreshed}
    assert [fact.value_en for fact in refreshed] == ["recent hiring activity"]
    assert db.one(
        "SELECT COUNT(*) AS n FROM tenant_facts "
        "WHERE company_id='cmp_a' AND field<>'recent_hiring'",
    )["n"] == 0
