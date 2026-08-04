"""
End-to-end review-loop tests for the done-based review pipeline.

Drives the REAL engine.execute() monitoring loop against a temp kanban
DB, with a scripted card driver (claim + complete) standing in for the
kanban dispatcher + workers.

Scenarios (dummy workflow mirroring ideation's topology):
1. FAIL → rework → re-review → PASS (multi-reviewer, layer rewind)
2. Per-reviewer guard: one reviewer's PASS must NOT suppress another
   reviewer's FAIL (ideation run 48515 regression)
3. Retry limit: exhausted reviewer stays terminal, no infinite loop

Run: python3 -m pytest tests/test_workflow_review_loop_e2e.py -v
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    """Isolated kanban home + workflows dir."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WORKFLOW_FILES", str(wf_dir))

    board = "dummy-review-board"
    board_dir = home / "kanban" / "boards" / board
    board_dir.mkdir(parents=True)
    db_path = board_dir / "kanban.db"

    from hermes_cli import kanban_db
    kanban_db._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kanban_db.connect(db_path=db_path)
    conn.close()

    return {"home": home, "wf_dir": wf_dir, "db_path": db_path, "board": board}


YAML_TMPL = """\
name: dummy-review
description: "Dummy review loop"
roles:
  coder: newton
  qa: newton
  security: newton
nodes:
  implement:
    agent: "{{coder}}"
    task: "Write the spec."
    timeout_minutes: 1
    reviews:
      - qa-review
      - security-review
  qa-review:
    agent: "{{qa}}"
    task: "QA review. Complete with PASS or FAIL verdict."
    depends_on: [implement]
    timeout_minutes: 1
    max_retries: {qa_retries}
  security-review:
    agent: "{{security}}"
    task: "Security review. Complete with PASS or FAIL verdict."
    depends_on: [qa-review]
    timeout_minutes: 1
    max_retries: {sec_retries}
"""


class CardDriver:
    """Claims + completes ready cards per a scripted summary list.

    Each title-prefix maps to an ordered list of completion summaries.
    The driver mimics a real worker: claim (ready->running), wait so the
    engine's monitor observes the running transition, then complete with
    the summary (running->done). The verdict words in the summary drive
    the engine's pass/fail classification.
    """

    def __init__(self, db_path, scripts):
        self.db_path = db_path
        self.scripts = scripts  # {title_prefix: [summary, ...]}
        self.counters = {p: 0 for p in scripts}
        self.completions = {p: [] for p in scripts}  # [(card_id, summary)]
        self.stop = threading.Event()

    def run(self):
        from hermes_cli import kanban_db
        conn = None
        try:
            while not self.stop.is_set():
                try:
                    conn = sqlite3.connect(self.db_path, timeout=10)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, title, status FROM tasks "
                        "WHERE status IN ('ready', 'running') ORDER BY created_at"
                    ).fetchall()
                    conn.close()
                    conn = None
                    for row in rows:
                        prefix = next(
                            (p for p in self.scripts if row["title"].startswith(p)),
                            None,
                        )
                        if prefix is None:
                            continue
                        idx = self.counters[prefix]
                        if idx >= len(self.scripts[prefix]):
                            continue
                        summary = self.scripts[prefix][idx]
                        self.counters[prefix] += 1
                        # Engine-created cards are born 'running' (kanban
                        # create_task default); a claimed card is also
                        # 'running'. complete_task accepts running|ready,
                        # so no claim is needed here — the engine's own
                        # monitor polls for the done transition.
                        conn = sqlite3.connect(self.db_path, timeout=10)
                        conn.row_factory = sqlite3.Row  # kanban_db expects Row
                        kanban_db.complete_task(
                            conn, row["id"], summary=summary, result=summary
                        )
                        conn.close()
                        conn = None
                        self.completions[prefix].append((row["id"], summary))
                        time.sleep(0.4)
                except Exception:
                    # DB may be mid-write (locked) — retry next tick.
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                time.sleep(0.05)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _run_engine(review_env, qa_retries, sec_retries, scripts):
    """Write the dummy YAML and run execute() to completion."""
    wf_dir = review_env["wf_dir"]
    yaml_content = YAML_TMPL.format(qa_retries=qa_retries, sec_retries=sec_retries)
    (wf_dir / "dummy-review.yaml").write_text(yaml_content)

    from plugins.workflow.engine import WorkflowEngine
    # Test speed: shrink the poll interval. Instance-level set so the
    # class default stays untouched (Pyright literal type is respected).
    engine = WorkflowEngine(workflows_dir=str(wf_dir))
    engine.POLL_INTERVAL = 0.15  # type: ignore[assignment]

    driver = CardDriver(review_env["db_path"], scripts)
    t = threading.Thread(target=driver.run, daemon=True)
    t.start()
    try:
        results = engine.execute("dummy-review", board=review_env["board"])
        return engine, results, driver
    finally:
        driver.stop.set()
        t.join(timeout=10)


def _state_files(wf_dir):
    return sorted((wf_dir / ".engine-state").glob("dummy-review_*_state.json"))


def _comments(db_path, card_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT author, body FROM task_comments "
        "WHERE task_id = ? ORDER BY created_at, id",
        (card_id,),
    ).fetchall()
    conn.close()
    return [r["body"] for r in rows if r["author"] == "workflow-engine"]


class TestReviewLoopE2E:
    def test_fail_then_rework_then_pass_full_loop(self, review_env):
        """FAIL resets upstream + ALL reviewers, layer rewinds, revised
        work re-reviews, final PASS accepts the work (stays done)."""
        scripts = {
            "[implement]": ["v1", "v2"],
            "[qa-review]": ["FAIL: missing edge cases", "PASS: fixed"],
            "[security-review]": ["PASS: secure"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=3, sec_retries=3, scripts=scripts
        )

        assert results["implement"] == "done"
        assert results["qa-review"] == "done"
        assert results["security-review"] == "done"

        # The revision loop actually re-engaged: implement ran twice,
        # qa reviewed twice (FAIL then PASS), security once.
        assert len(driver.completions["[implement]"]) == 2
        assert len(driver.completions["[qa-review]"]) == 2
        assert len(driver.completions["[security-review]"]) == 1

        # Same implement card reused across the revision (no duplicate).
        impl_cards = {cid for cid, _ in driver.completions["[implement]"]}
        assert len(impl_cards) == 1
        impl_card = impl_cards.pop()

        # Comments tell the story: one FAIL, then two PASSes.
        bodies = _comments(review_env["db_path"], impl_card)
        assert any(b.startswith("Review Failed (qa-review)") for b in bodies)
        assert any(b.startswith("Review Passed (qa-review)") for b in bodies)
        assert any(b.startswith("Review Passed (security-review)") for b in bodies)

        # Final state: implement accepted (done), review rounds recorded.
        state = json.loads(_state_files(review_env["wf_dir"])[-1].read_text())
        impl = state["states"]["implement"]
        assert impl["status"] == "done"
        assert impl["review_counts"] == {"qa-review": 1, "security-review": 1}

    def test_reviewer_pass_does_not_suppress_other_reviewer_fail(self, review_env):
        """Regression: qa's PASS must not suppress security's FAIL
        (ideation 48515: security's verdict was skipped entirely)."""
        scripts = {
            "[implement]": ["v1", "v2"],
            "[qa-review]": ["PASS: ok", "PASS: ok"],
            "[security-review]": ["FAIL: vuln", "PASS: fixed"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=3, sec_retries=3, scripts=scripts
        )

        assert results["implement"] == "done"
        # Both reviewers reviewed the revision (2 rounds each).
        assert len(driver.completions["[qa-review]"]) == 2
        assert len(driver.completions["[security-review]"]) == 2

        impl_card = driver.completions["[implement]"][0][0]
        bodies = _comments(review_env["db_path"], impl_card)
        # Security's FAIL was applied even though qa passed first.
        assert any(b.startswith("Review Failed (security-review)") for b in bodies)
        assert any(b.startswith("Review Passed (qa-review)") for b in bodies)
        assert any(b.startswith("Review Passed (security-review)") for b in bodies)

    def test_review_retry_limit_stops_loop(self, review_env):
        """max_retries: 1 — after the second FAIL the reviewer stays
        terminal; the workflow still advances and terminates (no
        infinite loop, no re-dispatch of an exhausted reviewer)."""
        scripts = {
            "[implement]": ["v1", "v2", "v3"],
            "[qa-review]": ["FAIL: bad", "FAIL: bad"],
            "[security-review]": ["PASS: ok"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=1, sec_retries=3, scripts=scripts
        )

        assert results["implement"] == "done"
        # qa reviewed twice (rounds 1+2), then hit its limit and was
        # left terminal — no third review.
        assert len(driver.completions["[qa-review]"]) == 2
        # security reviewed the final revision once.
        assert len(driver.completions["[security-review]"]) == 1

        state = json.loads(_state_files(review_env["wf_dir"])[-1].read_text())
        impl = state["states"]["implement"]
        assert impl["status"] == "done"
        assert impl["review_counts"] == {"qa-review": 2, "security-review": 2}


class TestGetCardStatus:
    def test_latest_summary_reads_run_output_not_prompt(self, review_env):
        """Regression: get_card_status previously queried a nonexistent
        task_events.message column, so latest_summary was always empty
        and get_card_body fell through to the INPUT PROMPT. The verdict
        detector must see the reviewer's actual output."""
        from hermes_cli import kanban_db
        from plugins.workflow.engine import WorkflowEngine

        engine = WorkflowEngine(workflows_dir=str(review_env["wf_dir"]))
        engine.kanban_board = review_env["board"]

        conn = kanban_db.connect(board=review_env["board"])
        tid = kanban_db.create_task(
            conn,
            title="[test] runner: card",
            body="Run the tests. If pass say PASS, if fail say FAIL.",
            assignee="newton",
            parents=(),
            tenant=review_env["board"],
        )
        conn.close()

        conn = kanban_db.connect(board=review_env["board"])
        kanban_db.claim_task(conn, tid, ttl_seconds=3600, claimer="t")
        kanban_db.complete_task(
            conn, tid, summary="FAIL: blockers found", result="FAIL: blockers found"
        )
        conn.close()

        status = engine.get_card_status(tid)
        assert status["latest_summary"] == "FAIL: blockers found"
        assert status["result"] == "FAIL: blockers found"
        # The prompt itself must NOT be the summary.
        assert status["latest_summary"] != status["body"]
