"""Regression checks for the production AgentRunService."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from server.agent_service import (AgentRunService, BaseRunExecutor, StubRunExecutor,
                                  _last_lines, extract_json)
from server.db import Database, json_dump, new_id, now
from server.run_types import READ_ONLY, REGISTRY


def service(executor=None):
    db = Database(Path(tempfile.mktemp(suffix=".db")))
    company_id, stamp = new_id("cmp"), now()
    db.execute("INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
               (company_id, "Acme", None, "active", json_dump({}), stamp, stamp))
    return AgentRunService(db, executor or StubRunExecutor()), company_id


def wait(runs, company_id, run_id):
    deadline = time.time() + 3
    while time.time() < deadline:
        run = runs.get(company_id, run_id)
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run timed out")


def test_company_profile_research_run_is_registered_read_only():
    skill, builder = REGISTRY["company_profile_research"]

    assert skill == "lead-research"
    assert callable(builder)


def test_lead_research_gap_run_is_registered_read_only():
    skill, builder = REGISTRY["lead_research_gap"]

    assert "lead_research_gap" in READ_ONLY
    assert skill == "lead-research"
    assert callable(builder)


def test_stub_dispatch_and_events():
    runs, company_id = service()
    run = runs.create(company_id, "lead_scan", {"countries": ["DE"]})
    runs.start(company_id, run["id"])
    completed = wait(runs, company_id, run["id"])
    assert completed["status"] == "succeeded"
    assert [event["kind"] for event in runs.events(company_id, run["id"])] == [
        "created", "started", "succeeded",
    ]


def test_idempotency_returns_same_run():
    runs, company_id = service()
    first = runs.create(company_id, "analytics_refresh", {}, "same")
    second = runs.create(company_id, "analytics_refresh", {}, "same")
    assert first["id"] == second["id"]


def test_send_runs_cannot_bypass_delivery_service():
    runs, company_id = service()
    for run_type in ("email_send", "whatsapp_send"):
        try:
            runs.create(company_id, run_type, {"approved": True})
            raise AssertionError("send run bypass was accepted")
        except HTTPException as exc:
            assert exc.status_code == 422


def test_six_country_scan_rejected_in_service():
    runs, company_id = service()
    try:
        runs.create(company_id, "lead_scan", {"countries": ["DE", "FR", "NL", "GB", "ES", "IT"]})
        raise AssertionError("six-country scan was accepted")
    except HTTPException as exc:
        assert exc.status_code == 422


def test_structured_output_extraction():
    value = extract_json("progress\n```json\n{\"leads\": []}\n```\n")
    assert value == {"leads": []}


def test_agent_transcript_tail_is_kept_for_the_error_message():
    """`hermes -z` exits 0 on its own failures, so the reason is on stdout.

    Without the tail every such run reads as "output did not contain a JSON
    object", which is true of an unset provider, an expired key and a model the
    account cannot reach alike.
    """
    assert _last_lines("") == "(no output)"
    assert _last_lines("   \n\n  ") == "(no output)"
    assert _last_lines("a\nb\nc\nd") == "b | c | d"
    assert _last_lines("only one line") == "only one line"
    assert _last_lines("a\n\n  b  \n", limit=2) == "a | b"


class BlockingExecutor(BaseRunExecutor):
    def __init__(self):
        self.cancelled = False

    def execute(self, runs, run):
        while not self.cancelled:
            time.sleep(0.01)
        raise InterruptedError("cancelled")

    def cancel(self, run_id):
        self.cancelled = True


def test_running_executor_is_cancelled():
    executor = BlockingExecutor()
    runs, company_id = service(executor)
    run = runs.create(company_id, "analytics_refresh", {})
    runs.start(company_id, run["id"])
    runs.cancel(company_id, run["id"])
    completed = wait(runs, company_id, run["id"])
    assert completed["status"] == "cancelled"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} run-service checks passed")
