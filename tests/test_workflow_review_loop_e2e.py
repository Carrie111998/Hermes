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

    def __init__(self, db_path, scripts, block_scripts=None):
        self.db_path = db_path
        self.scripts = scripts  # {title_prefix: [summary, ...]}
        self.block_scripts = block_scripts or {}  # {title_prefix: [reason, ...]}
        self.counters = {p: 0 for p in scripts}
        self.block_counters = {p: 0 for p in self.block_scripts}
        self.completions = {p: [] for p in scripts}  # [(card_id, summary)]
        self.blocks = {p: [] for p in self.block_scripts}  # [(card_id, reason)]
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
                        block_prefix = next(
                            (p for p in self.block_scripts if row["title"].startswith(p)),
                            None,
                        )
                        prefix = None
                        if block_prefix is not None:
                            bidx = self.block_counters[block_prefix]
                            if bidx >= len(self.block_scripts[block_prefix]):
                                # Block script exhausted — fall through to
                                # completion scripts (fresh reviewer cards
                                # after a re-arm complete with verdicts).
                                block_prefix = None
                        if block_prefix is None:
                            prefix = next(
                                (p for p in self.scripts if row["title"].startswith(p)),
                                None,
                            )
                        if prefix is None and block_prefix is None:
                            continue
                        if prefix is not None:
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
                        if block_prefix is not None:
                            bidx = self.block_counters[block_prefix]
                            if bidx >= len(self.block_scripts[block_prefix]):
                                continue
                            reason = self.block_scripts[block_prefix][bidx]
                            self.block_counters[block_prefix] += 1
                            kanban_db.block_task(
                                conn, row["id"], reason=reason, kind="needs_input"
                            )
                            conn.close()
                            conn = None
                            self.blocks[block_prefix].append((row["id"], reason))
                        else:
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


def _run_engine(review_env, qa_retries, sec_retries, scripts, block_scripts=None,
               patch_analyst=False):
    """Write the dummy YAML and run execute() to completion."""
    wf_dir = review_env["wf_dir"]
    yaml_content = YAML_TMPL.format(qa_retries=qa_retries, sec_retries=sec_retries)
    (wf_dir / "dummy-review.yaml").write_text(yaml_content)

    from plugins.workflow.engine import WorkflowEngine
    # Test speed: shrink the poll interval. Instance-level set so the
    # class default stays untouched (Pyright literal type is respected).
    engine = WorkflowEngine(workflows_dir=str(wf_dir))
    engine.POLL_INTERVAL = 0.15  # type: ignore[assignment]

    driver = CardDriver(review_env["db_path"], scripts, block_scripts=block_scripts)
    t = threading.Thread(target=driver.run, daemon=True)
    t.start()
    try:
        if patch_analyst:
            # Replace LLM auxiliary calls with fast stubs — these tests
            # exercise ENGINE mechanics, not the analyst.
            from unittest.mock import MagicMock
            import plugins.workflow.analyst as analyst_mod
            _fast = MagicMock()
            _fast.success = True
            _fast.result = {"decision": "loop", "block_type": "quality",
                            "layer_summary": [], "attention_needed": []}
            _orig = {n: getattr(analyst_mod, n) for n in (
                "analyze_block_notification", "analyze_status",
                "analyze_loop_decision", "analyze_escalation",
                "analyze_failure") if hasattr(analyst_mod, n)}
            for n in _orig:
                setattr(analyst_mod, n, lambda *a, _r=_fast, **k: _r)
            try:
                results = engine.execute("dummy-review", board=review_env["board"])
            finally:
                for n, f in _orig.items():
                    setattr(analyst_mod, n, f)
        else:
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
        """max_retries: 1 — after the second FAIL the reviewer is
        EXHAUSTED: the review chain dies, the run ends BLOCKED (not
        'completed'), and the exhausted reviewer is never re-dispatched.
        No infinite loop, no fresh cards."""
        scripts = {
            "[implement]": ["v1", "v2"],
            "[qa-review]": ["FAIL: bad", "FAIL: bad"],
            "[security-review]": ["PASS: ok"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=1, sec_retries=3, scripts=scripts
        )

        # qa reviewed twice (rounds 1+2), then hit its limit and the
        # chain stopped — no third review, no security review of a
        # failed revision (security depends on qa's chain).
        assert len(driver.completions["[qa-review]"]) == 2
        assert len(driver.completions["[security-review]"]) == 0
        # Implement did complete its WORK (twice) — but the workflow is
        # NOT complete: the exhausted reviewer blocks the run.

        # The exhausted reviewer's NODE state is terminal-blocked and the
        # run's final state records it — the run must be BLOCKED, never
        # 'completed' (live 2026-08-05: run ended '1 done, 0 blocked'
        # with verify still 'running' in state).
        state = json.loads(_state_files(review_env["wf_dir"])[-1].read_text())
        assert state["states"]["qa-review"]["status"] == "blocked", state["states"]["qa-review"]
        assert state["final_status"] == "blocked", state.get("final_status")
        # qa was assigned 2 rounds (round-1 FAIL reset bumps all reviewers,
        # so security's counter also shows an assigned round — it never
        # actually reviewed, which the completions assertion above proves).
        assert state["states"]["implement"]["review_counts"]["qa-review"] == 2


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


class TestBlockedReviewerRecovery:
    """Regression tests for the ideation 2026-08-04 deadlock.

    Scenario that broke: qa-review BLOCKED with findings (legacy YAML
    convention: "if blocking, include issues"), the engine enriched the
    spec card and reset it to ready, but nothing ever re-dispatched the
    blocked reviewer — the run was declared 'completed' with a live
    review loop and nobody was notified.
    """

    def test_blocked_reviewer_rearms_loop(self, review_env):
        """Reviewer blocks with findings → upstream reset → reviewer
        re-armed → upstream re-completes → FRESH reviewer card carries
        the verdict contract → PASS → workflow finishes."""
        scripts = {
            "[implement]": ["v1", "v2"],
            "[qa-review]": ["PASS: ok"],
            "[security-review]": ["PASS: ok"],
        }
        block_scripts = {
            "[qa-review]": [
                "CHANGES REQUIRED: missing tests and wrong config",
            ],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=3, sec_retries=3,
            scripts=scripts, block_scripts=block_scripts,
        )

        # The block was a quality verdict → upstream reset + reviewer re-armed
        assert len(driver.blocks["[qa-review]"]) == 1
        # SAME-CARD semantics (Randy 2026-08-05): the SAME reviewer card
        # completes each round — its comment history (prior verdicts) is
        # the round context. No fresh card is minted per round. The card
        # that was blocked with round-1 findings is the card that PASSes
        # in round 2.
        assert len(driver.completions["[qa-review]"]) == 1
        assert "PASS" in driver.completions["[qa-review]"][0][1]
        assert driver.blocks["[qa-review]"][0][0] == driver.completions["[qa-review]"][0][0], (
            "same-card violation: blocked card != completed card "
            f"({driver.blocks['[qa-review]'][0][0]} vs {driver.completions['[qa-review]'][0][0]})"
        )
        # Regression (live 2026-08-05): the same-card reuse path must append
        # a 'status' event (old=done, new=ready) on the reviewer card.
        # Without it, the real dispatcher's respawn guard sees a completed
        # run inside _RESPAWN_GUARD_SUCCESS_WINDOW with no requeue event and
        # refuses to re-spawn ("respawn_guarded: recent_success" xN) — round
        # 2 silently stalls until the engine's layer poll times out. The e2e
        # CardDriver bypasses the dispatcher, so this assertion is the only
        # guard that catches the missing event.
        qa_card = driver.completions["[qa-review]"][0][0]
        conn = sqlite3.connect(review_env["db_path"])
        conn.row_factory = sqlite3.Row
        status_events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'status' ORDER BY id",
            (qa_card,),
        ).fetchall()
        conn.close()
        assert any(
            "ready" in (e["payload"] or "") for e in status_events
        ), f"no status→ready requeue event on reused reviewer card: {status_events}"
        # The re-armed reviewer card must carry the done-based verdict contract
        conn = sqlite3.connect(review_env["db_path"])
        conn.row_factory = sqlite3.Row
        qa_card_ids = [cid for cid, _ in driver.completions["[qa-review]"]]
        for cid in qa_card_ids:
            body = conn.execute("SELECT body FROM tasks WHERE id=?", (cid,)).fetchone()["body"]
            assert "REVIEW VERDICT CONTRACT" in body
            assert "NEVER block" in body
            assert "PASS:" in body and "FAIL:" in body
        conn.close()

        # Workflow finished cleanly
        state = json.loads(_state_files(review_env["wf_dir"])[-1].read_text())
        assert state["states"]["implement"]["status"] == "done"
        assert state["states"]["qa-review"]["status"] == "done"
        assert state["states"]["security-review"]["status"] == "done"

    def test_block_feedback_body_has_findings(self, review_env):
        """Regression: quality-block handler read get_card_body() (the
        INPUT PROMPT) for blocked cards, producing an empty
        'Review Feedback (qa-review):' comment. It must read the block
        event's reason payload — the reviewer's actual findings."""
        scripts = {
            "[implement]": ["v1"],
            "[security-review]": ["PASS: ok"],
        }
        block_scripts = {
            "[qa-review]": [
                "CHANGES REQUIRED: RB1 missing await, RB2 phantom path",
            ],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=3, sec_retries=3,
            scripts=scripts, block_scripts=block_scripts,
        )

        # Find the implement card's workflow-engine comments
        conn = sqlite3.connect(review_env["db_path"])
        conn.row_factory = sqlite3.Row
        imp_card = conn.execute(
            "SELECT id FROM tasks WHERE title LIKE '[implement]%' ORDER BY created_at LIMIT 1"
        ).fetchone()
        comments = _comments(review_env["db_path"], imp_card["id"])
        conn.close()
        feedback = [c for c in comments if c.startswith("Review Feedback")]
        assert feedback, f"expected Review Feedback comment, got {comments}"
        # The findings from the block REASON must be in the comment
        assert "RB1 missing await" in feedback[0]
        assert "RB2 phantom path" in feedback[0]
        # The input prompt must NOT be the feedback
        assert "Write the spec" not in feedback[0]

    def test_exhausted_blocked_reviewer_ends_blocked_with_notification(self, review_env):
        """Budget exhaustion (max_retries: 1) on a BLOCKED reviewer ends
        the run with final_status=blocked (not 'completed'), and a
        BLOCKED delivery marker is written for the calling session."""
        scripts = {
            "[implement]": ["v1", "v2", "v3"],
            "[security-review]": ["PASS: ok"],
        }
        block_scripts = {
            "[qa-review]": ["CHANGES REQUIRED: still broken", "CHANGES REQUIRED: still broken"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=1, sec_retries=3,
            scripts=scripts, block_scripts=block_scripts,
            patch_analyst=True,
        )

        # The reviewer blocked once, was re-armed, blocked again, then
        # hit its budget — a THIRD re-arm must not happen.
        assert len(driver.blocks["[qa-review]"]) == 2

        # Executions DB records 'blocked', not 'completed'
        from hermes_cli.kanban_db import kanban_home
        db_path = kanban_home() / "workflows" / "executions.db"
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT status FROM workflow_executions WHERE run_id LIKE 'dummy-review%' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] == "blocked", f"expected blocked, got {row}"

        # The engine result records the blocked node (session delivery of
        # the BLOCKED marker is unit-tested in test_workflow_notification.py
        # — the e2e harness has no session routing).
        assert results.get("qa-review") == "blocked", results

    def test_verdict_contract_prepended_to_first_reviewer_card(self, review_env):
        """Even the FIRST reviewer dispatch (no prior block) carries the
        done-based verdict contract, so reviewers know to complete with
        PASS/FAIL instead of blocking."""
        scripts = {
            "[implement]": ["v1"],
            "[qa-review]": ["PASS: ok"],
            "[security-review]": ["PASS: ok"],
        }
        engine, results, driver = _run_engine(
            review_env, qa_retries=3, sec_retries=3, scripts=scripts
        )

        conn = sqlite3.connect(review_env["db_path"])
        conn.row_factory = sqlite3.Row
        for cid, _summary in driver.completions["[qa-review]"]:
            body = conn.execute("SELECT body FROM tasks WHERE id=?", (cid,)).fetchone()["body"]
            assert "REVIEW VERDICT CONTRACT" in body
            assert "NEVER block this card" in body
        conn.close()
        assert results["implement"] == "done"
        assert results["qa-review"] == "done"
