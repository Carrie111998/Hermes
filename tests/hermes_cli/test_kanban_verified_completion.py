"""Tests for opt-in verified completion (#70806).

Covers the DB + policy layers of the verified-completion gate:

* Two new nullable task columns (``verify_mode`` / ``verify_cmd``) — NULL means
  no gate, i.e. the exact behaviour every task had before the columns existed.
* The additive ``_record_task_failure`` extension (``expected_run_id`` CAS,
  ``escalate_on_trip``, ``details_out``) that lets the verify path reuse the
  existing consecutive-failure breaker without duplicating it.
* ``record_verify_failure`` — one counted rejection per red verify, evidence
  attached as a ``completion_blocked_evidence`` event, ``blocked`` + comment on
  exhaustion, uncounted audit-only handling for stale-run ghosts.
* The ``hermes_cli.kanban_verify`` runner/ledger helpers (fail closed on infra
  errors, redact before cap, never consult the ledger's shared ``default``
  bucket, never upgrade targeted evidence into a repo-green claim).
* The ``build_worker_context`` gate section that announces the contract and
  feeds the latest red evidence to re-dispatched workers.
"""

from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_verify as kv


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", **create_kwargs):
    """Create a task and drive it to ``running`` with an open run."""
    tid = kb.create_task(conn, title=title, assignee="worker", **create_kwargs)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


# ---------------------------------------------------------------------------
# Schema + plumbing
# ---------------------------------------------------------------------------


def test_create_task_persists_verify_cmd(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_cmd="pytest -q")
        t = kb.get_task(conn, tid)
        # Passing a command implies cmd mode.
        assert t.verify_mode == "cmd"
        assert t.verify_cmd == "pytest -q"


def test_create_task_verify_auto_persists_mode(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_mode="auto")
        t = kb.get_task(conn, tid)
        assert t.verify_mode == "auto"
        assert t.verify_cmd is None


def test_create_task_rejects_invalid_verify_config(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="t", verify_mode="bogus")
        with pytest.raises(ValueError):
            kb.create_task(conn, title="t", verify_mode="cmd")
        with pytest.raises(ValueError):
            kb.create_task(conn, title="t", verify_mode="auto",
                           verify_cmd="pytest -q")


def test_task_from_row_key_guards_verify_columns(kanban_home: Path) -> None:
    """A row SELECTed without the verify columns must parse (legacy safety)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_cmd="pytest -q")
        row = conn.execute(
            "SELECT id, title, body, assignee, status, priority, created_by, "
            "created_at, started_at, completed_at, workspace_kind, "
            "workspace_path, claim_lock, claim_expires "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        t = kb.Task.from_row(row)
        assert t.verify_mode is None
        assert t.verify_cmd is None


# ---------------------------------------------------------------------------
# Funnel extension (_record_task_failure new kwargs)
# ---------------------------------------------------------------------------


def test_record_task_failure_escalate_below_limit_keeps_claim_and_run(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        info: dict = {}
        blocked = kb._record_task_failure(
            conn, tid, "verify red",
            outcome="verify_failed",
            failure_limit=3,
            escalate_on_trip=True,
            expected_run_id=run_id,
            details_out=info,
        )
        assert blocked is False
        t = kb.get_task(conn, tid)
        assert t.status == "running"
        assert t.claim_lock is not None
        assert t.current_run_id == run_id
        assert t.consecutive_failures == 1
        assert kb.latest_run(conn, tid).ended_at is None
        assert info == {
            "failures": 1,
            "effective_limit": 3,
            "limit_source": "dispatcher",
            "blocked": False,
            "stale_run": False,
        }


def test_record_task_failure_escalate_trip_releases_and_ends(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, max_retries=1)
        info: dict = {}
        blocked = kb._record_task_failure(
            conn, tid, "verify red",
            outcome="verify_failed",
            failure_limit=5,
            escalate_on_trip=True,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            details_out=info,
        )
        assert blocked is True
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.claim_lock is None
        assert t.worker_pid is None
        run = kb.latest_run(conn, tid)
        assert run.ended_at is not None
        assert run.outcome == "gave_up"
        assert info["blocked"] is True
        assert info["limit_source"] == "task"


def test_record_task_failure_expected_run_id_mismatch_is_noop(
    kanban_home: Path,
) -> None:
    """The stale-run CAS: a mismatched run id must mutate NOTHING."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        events_before = len(kb.list_events(conn, tid))
        info: dict = {}
        blocked = kb._record_task_failure(
            conn, tid, "ghost red",
            outcome="verify_failed",
            failure_limit=2,
            escalate_on_trip=True,
            expected_run_id=run_id + 999,
            details_out=info,
        )
        assert blocked is False
        assert info["stale_run"] is True
        t = kb.get_task(conn, tid)
        assert t.status == "running"
        assert t.consecutive_failures == 0
        assert t.claim_lock is not None
        assert t.current_run_id == run_id
        assert kb.latest_run(conn, tid).ended_at is None
        assert len(kb.list_events(conn, tid)) == events_before


def test_record_task_failure_legacy_callers_unchanged(kanban_home: Path) -> None:
    """Callers that pass none of the new kwargs keep the documented contract."""
    with kb.connect_closing() as conn:
        # Spawn path: release_claim=True, end_run=True — below limit goes
        # back to ready with the claim cleared and the run closed.
        tid = _running_task(conn)
        blocked = kb._record_task_failure(
            conn, tid, "spawn boom",
            outcome="spawn_failed",
            failure_limit=5,
            release_claim=True,
            end_run=True,
        )
        assert blocked is False
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.claim_lock is None
        assert t.consecutive_failures == 1
        run = kb.latest_run(conn, tid)
        assert run.ended_at is not None
        assert run.outcome == "spawn_failed"

        # Counter-only path: release_claim=False, end_run=False — just
        # bookkeeps the counter; status untouched.
        tid2 = _running_task(conn, title="t2")
        blocked2 = kb._record_task_failure(
            conn, tid2, "crash",
            outcome="crashed",
            failure_limit=5,
        )
        assert blocked2 is False
        t2 = kb.get_task(conn, tid2)
        assert t2.status == "running"
        assert t2.consecutive_failures == 1


# ---------------------------------------------------------------------------
# record_verify_failure
# ---------------------------------------------------------------------------


def test_verify_failure_below_limit_keeps_task_running(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        run_id = kb.get_task(conn, tid).current_run_id
        info = kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd",
            command="pytest -q",
            exit_code=1,
            output_excerpt="1 failed, 3 passed",
            expected_run_id=run_id,
            failure_limit=2,
            summary_preview="did the thing",
        )
        assert info == {
            "blocked": False,
            "stale_run": False,
            "failures": 1,
            "effective_limit": 2,
            "limit_source": "dispatcher",
        }
        t = kb.get_task(conn, tid)
        assert t.status == "running"
        assert t.consecutive_failures == 1
        assert t.claim_lock is not None
        assert kb.latest_run(conn, tid).ended_at is None
        evidence = [e for e in kb.list_events(conn, tid)
                    if e.kind == "completion_blocked_evidence"]
        assert len(evidence) == 1
        payload = evidence[0].payload
        assert payload["gate"] == "verify_cmd"
        assert payload["command"] == "pytest -q"
        assert payload["exit_code"] == 1
        assert payload["output_excerpt"] == "1 failed, 3 passed"
        assert payload["failures"] == 1
        assert payload["effective_limit"] == 2
        assert payload["limit_source"] == "dispatcher"
        assert payload["exhausted"] is False
        assert payload["counted"] is True
        assert payload["summary_preview"] == "did the thing"


def test_verify_failure_trips_breaker_and_attaches_evidence(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q", max_retries=1)
        run_id = kb.get_task(conn, tid).current_run_id
        info = kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd",
            command="pytest -q",
            exit_code=2,
            output_excerpt="boom",
            expected_run_id=run_id,
            failure_limit=5,
        )
        assert info["blocked"] is True
        assert info["limit_source"] == "task"
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.claim_lock is None
        run = kb.latest_run(conn, tid)
        assert run.outcome == "gave_up"
        assert run.metadata["trigger_outcome"] == "verify_failed"
        events = kb.list_events(conn, tid)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert gave_up and gave_up[-1].payload["gate"] == "verify_cmd"
        evidence = [e for e in events
                    if e.kind == "completion_blocked_evidence"]
        assert evidence and evidence[-1].payload["exhausted"] is True
        comments = kb.list_comments(conn, tid)
        gate_comments = [c for c in comments if c.author == "verify-gate"]
        assert gate_comments
        assert "pytest -q" in gate_comments[-1].body
        assert "boom" in gate_comments[-1].body


def test_verify_failure_stale_run_audits_without_counting(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        run_id = kb.get_task(conn, tid).current_run_id
        info = kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd",
            command="pytest -q",
            exit_code=1,
            output_excerpt="late red",
            expected_run_id=run_id + 999,
            failure_limit=2,
        )
        assert info["blocked"] is False
        assert info["stale_run"] is True
        t = kb.get_task(conn, tid)
        assert t.status == "running"
        assert t.consecutive_failures == 0
        assert t.claim_lock is not None
        events = kb.list_events(conn, tid)
        evidence = [e for e in events
                    if e.kind == "completion_blocked_evidence"]
        assert len(evidence) == 1
        assert evidence[0].payload["stale_run"] is True
        assert evidence[0].payload["counted"] is False
        assert not [e for e in events if e.kind == "gave_up"]
        assert not [c for c in kb.list_comments(conn, tid)
                    if c.author == "verify-gate"]


def test_record_verify_failure_redacts_command(kanban_home: Path) -> None:
    """Output arrives pre-redacted, but the COMMAND string is a public-API
    input — a secret-bearing command must never reach last_failure_error,
    events, or the blocked-evidence comment."""
    secret = "ghp_" + "Abc123XyZ0" * 3
    raw_cmd = f"GH_TOKEN={secret} ./integration-check.sh"
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd=raw_cmd)
        info = kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd",
            command=raw_cmd,
            exit_code=1,
            output_excerpt="1 failed",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            failure_limit=1,
        )
        assert info["blocked"] is True
        t = kb.get_task(conn, tid)
        assert secret not in (t.last_failure_error or "")
        for e in kb.list_events(conn, tid):
            assert secret not in json.dumps(e.payload or {})
        comments = [c for c in kb.list_comments(conn, tid)
                    if c.author == "verify-gate"]
        assert comments
        assert secret not in comments[-1].body


def test_verify_exhaustion_holds_under_recompute_ready(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q", max_retries=1)
        kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd", command="pytest -q", exit_code=1,
            output_excerpt="red",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_unblock_after_verify_exhaustion_grants_fresh_budget(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q", max_retries=1)
        kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd", command="pytest -q", exit_code=1,
            output_excerpt="red",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.consecutive_failures == 0


def test_verify_failure_max_retries_overrides_failure_limit(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q", max_retries=3)
        info = kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd", command="pytest -q", exit_code=1,
            output_excerpt="red",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            failure_limit=1,
        )
        assert info["blocked"] is False
        assert info["limit_source"] == "task"
        assert info["effective_limit"] == 3
        assert kb.get_task(conn, tid).status == "running"


def test_successful_completion_resets_verify_failures(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd", command="pytest -q", exit_code=1,
            output_excerpt="red",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            failure_limit=5,
        )
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert kb.complete_task(conn, tid, result="done", verify_gate="passed")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.consecutive_failures == 0


# ---------------------------------------------------------------------------
# run_verify_command (cmd mode)
# ---------------------------------------------------------------------------


def test_run_verify_command_green(tmp_path: Path) -> None:
    out = kv.run_verify_command("exit 0", cwd=str(tmp_path))
    assert out.ok is True
    assert out.exit_code == 0
    assert out.timed_out is False
    assert out.gate == "verify_cmd"


def test_run_verify_command_red_captures_merged_output(tmp_path: Path) -> None:
    out = kv.run_verify_command(
        "echo from-stdout; echo from-stderr >&2; exit 3", cwd=str(tmp_path)
    )
    assert out.ok is False
    assert out.exit_code == 3
    assert "from-stdout" in out.detail
    assert "from-stderr" in out.detail


def test_run_verify_command_caps_output(tmp_path: Path) -> None:
    cmd = ("i=0; while [ $i -lt 300 ]; do "
           "echo 0123456789012345678901234567890123456789; "
           "i=$((i+1)); done")
    out = kv.run_verify_command(cmd, cwd=str(tmp_path))
    assert out.ok is True
    assert "chars omitted" in out.detail
    # Head + tail both survive the cap.
    assert out.detail.startswith("0123456789")
    assert out.detail.rstrip().endswith("0123456789")
    assert len(out.detail) < kv.MAX_VERIFY_OUTPUT_CHARS + 100


def test_run_verify_command_redacts_secrets(tmp_path: Path) -> None:
    secret = "ghp_" + "Abc123XyZ0" * 3
    out = kv.run_verify_command(f"echo {secret}; exit 1", cwd=str(tmp_path))
    assert out.ok is False
    assert secret not in out.detail


def test_run_verify_command_non_utf8_output_still_green(tmp_path: Path) -> None:
    """A green suite emitting one undecodable byte (latin-1 fixture data,
    binary spew) must stay green with mojibake — never a counted infra
    failure that burns the retry budget on exit-0 runs."""
    out = kv.run_verify_command(
        "printf 'ok \\377\\376 done'; exit 0", cwd=str(tmp_path)
    )
    assert out.ok is True
    assert out.exit_code == 0
    assert "ok" in out.detail
    assert "done" in out.detail


def test_run_verify_command_reports_redacted_command(tmp_path: Path) -> None:
    """The RAW command is executed, but the reported ``command`` field flows
    into events/comments/tool_error — an inline credential must not ride
    along."""
    secret = "ghp_" + "Abc123XyZ0" * 3
    out = kv.run_verify_command(f"GH_TOKEN={secret} exit 0", cwd=str(tmp_path))
    assert out.ok is True
    assert secret not in (out.command or "")


def test_run_verify_command_double_timeout_salvages_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escaped-setsid grandchild holds the pipe through the bounded
    reap: the partial capture must be salvaged off the exception — it is
    the evidence a human unblocking the card needs."""
    monkeypatch.setattr(kv, "REAP_TIMEOUT_SECONDS", 1)
    cmd = (
        "echo partial-marker; "
        f"{sys.executable} -c 'import os,time\n"
        "if os.fork() == 0:\n"
        "    os.setsid(); time.sleep(8)\n"
        "else:\n"
        "    os._exit(0)'; "
        "sleep 30"
    )
    out = kv.run_verify_command(cmd, cwd=str(tmp_path), timeout=1)
    assert out.ok is False
    assert out.timed_out is True
    assert out.exit_code is None
    assert "timed out" in out.detail
    assert "partial-marker" in out.detail


def test_run_verify_command_timeout_is_red(tmp_path: Path) -> None:
    out = kv.run_verify_command("sleep 30", cwd=str(tmp_path), timeout=1)
    assert out.ok is False
    assert out.timed_out is True
    assert out.exit_code is None
    assert "timed out" in out.detail


def test_run_verify_command_missing_workspace_fails_closed(
    tmp_path: Path,
) -> None:
    for cwd in (None, "", str(tmp_path / "does-not-exist")):
        out = kv.run_verify_command("exit 0", cwd=cwd)
        assert out.ok is False
        assert out.exit_code is None
        assert "workspace unavailable" in out.detail


# ---------------------------------------------------------------------------
# check_auto_evidence (auto mode)
# ---------------------------------------------------------------------------


def test_auto_accepts_full_scope_passed(tmp_path, monkeypatch) -> None:
    def fake_status(*, session_id, cwd):
        return {
            "status": "passed",
            "evidence": {
                "scope": "full",
                "canonical_command": "pytest -q",
                "command": "pytest -q",
                "exit_code": 0,
                "id": 7,
                "created_at": "2026-07-28T00:00:00+00:00",
            },
        }

    monkeypatch.setattr(kv, "verification_status", fake_status)
    out = kv.check_auto_evidence(["sess-1"], str(tmp_path))
    assert out.ok is True
    assert out.gate == "verify_auto"
    assert out.command == "pytest -q"
    assert out.exit_code == 0


def test_auto_rejects_targeted_scope_even_when_passed(
    tmp_path, monkeypatch
) -> None:
    def fake_status(*, session_id, cwd):
        return {
            "status": "passed",
            "evidence": {
                "scope": "targeted",
                "canonical_command": "pytest -q",
                "command": "pytest tests/test_one.py -q",
                "exit_code": 0,
            },
        }

    monkeypatch.setattr(kv, "verification_status", fake_status)
    out = kv.check_auto_evidence(["sess-1"], str(tmp_path))
    assert out.ok is False
    assert "targeted" in out.detail


@pytest.mark.parametrize(
    "status_payload,expected",
    [
        (
            {
                "status": "failed",
                "evidence": {
                    "scope": "full",
                    "canonical_command": "pytest -q",
                    "command": "pytest -q",
                    "exit_code": 1,
                    "output_summary": "3 failed, 12 passed",
                },
            },
            "3 failed, 12 passed",
        ),
        (
            {"status": "stale", "evidence": {"scope": "full",
                                             "canonical_command": "pytest -q",
                                             "exit_code": 0}},
            "re-run",
        ),
        ({"status": "unverified", "evidence": None}, "no verification evidence"),
        ({"status": "not_applicable", "evidence": None}, "not recognize"),
    ],
)
def test_auto_rejects_failed_stale_unverified_not_applicable(
    tmp_path, monkeypatch, status_payload, expected
) -> None:
    monkeypatch.setattr(
        kv, "verification_status", lambda *, session_id, cwd: status_payload
    )
    out = kv.check_auto_evidence(["sess-1"], str(tmp_path))
    assert out.ok is False
    assert expected in out.detail


def test_auto_missing_workspace_fails_closed(tmp_path, monkeypatch) -> None:
    """The cwd guard must fire BEFORE the ledger is consulted — a NULL
    workspace must never let evidence for the calling process's CWD vouch
    for the task."""

    def fail_status(**kw):
        raise AssertionError("ledger must not be consulted without a workspace")

    monkeypatch.setattr(kv, "verification_status", fail_status)
    for cwd in (None, "", str(tmp_path / "missing")):
        out = kv.check_auto_evidence(["sess-1"], cwd)
        assert out.ok is False
        assert "workspace unavailable" in out.detail


def test_auto_tries_candidate_keys_in_order(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_status(*, session_id, cwd):
        calls.append(session_id)
        if session_id == "t_task":
            return {
                "status": "passed",
                "evidence": {"scope": "full",
                             "canonical_command": "pytest -q",
                             "exit_code": 0},
            }
        return {"status": "unverified", "evidence": None}

    monkeypatch.setattr(kv, "verification_status", fake_status)
    out = kv.check_auto_evidence(["sess-1", "t_task"], str(tmp_path))
    assert out.ok is True
    assert calls == ["sess-1", "t_task"]

    # No candidate has evidence: rejected, with the PRIMARY key's status
    # driving the rejection detail.
    calls.clear()
    out2 = kv.check_auto_evidence(["sess-1", "sess-2"], str(tmp_path))
    assert out2.ok is False
    assert "no verification evidence" in out2.detail


def test_auto_freshness_bound_rejects_prior_incarnation_evidence(
    tmp_path, monkeypatch
) -> None:
    """Edit-staleness is per (session, root) bucket, so a previous
    incarnation's green evidence in the shared task-id bucket can never be
    staled by THIS run's edits — the ``not_before`` bound (the active
    run's start time) is what rejects it."""

    def status_created_at(created_at):
        return lambda *, session_id, cwd: {
            "status": "passed",
            "evidence": {"scope": "full", "canonical_command": "pytest -q",
                         "exit_code": 0, "created_at": created_at},
        }

    # Evidence recorded long before this dispatch: rejected, actionable.
    monkeypatch.setattr(
        kv, "verification_status",
        status_created_at("2020-01-01T00:00:00+00:00"),
    )
    out = kv.check_auto_evidence(
        ["t_task"], str(tmp_path), not_before=time.time()
    )
    assert out.ok is False
    assert "predates" in out.detail
    assert "re-run" in out.detail.lower()

    # Evidence recorded after the run started: accepted.
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(kv, "verification_status", status_created_at(fresh))
    out2 = kv.check_auto_evidence(
        ["t_task"], str(tmp_path), not_before=time.time() - 3600
    )
    assert out2.ok is True

    # Unparseable / missing created_at with a bound set: fail closed.
    for bad in ("not-a-timestamp", None):
        monkeypatch.setattr(kv, "verification_status", status_created_at(bad))
        out3 = kv.check_auto_evidence(
            ["t_task"], str(tmp_path), not_before=time.time() - 3600
        )
        assert out3.ok is False

    # No bound (not_before=None): behavior unchanged — accepted.
    monkeypatch.setattr(
        kv, "verification_status",
        status_created_at("2020-01-01T00:00:00+00:00"),
    )
    out4 = kv.check_auto_evidence(["t_task"], str(tmp_path))
    assert out4.ok is True


def test_auto_without_session_identity_fails_closed(
    tmp_path, monkeypatch
) -> None:
    def fail_status(**kw):
        raise AssertionError(
            "the shared 'default' ledger bucket must never be consulted"
        )

    monkeypatch.setattr(kv, "verification_status", fail_status)
    for candidates in ([], ["", None], ["default"]):
        out = kv.check_auto_evidence(candidates, str(tmp_path))
        assert out.ok is False
        assert "session identity" in out.detail


def test_evaluate_returns_none_without_verify_mode(tmp_path) -> None:
    task = types.SimpleNamespace(
        verify_mode=None, verify_cmd=None, workspace_path=str(tmp_path)
    )
    assert kv.evaluate_task_verification(task, session_ids=["s"]) is None


def test_evaluate_unknown_verify_mode_fails_closed(tmp_path) -> None:
    """A truthy-but-unrecognized verify_mode (hand-edited row,
    cross-version DB) must fail loudly, not silently degrade into
    auto-mode semantics."""
    task = types.SimpleNamespace(
        verify_mode="bogus", verify_cmd=None, workspace_path=str(tmp_path)
    )
    out = kv.evaluate_task_verification(task, session_ids=["s"])
    assert out is not None
    assert out.ok is False
    assert out.gate == "verify_invalid"
    assert "bogus" in out.detail


# ---------------------------------------------------------------------------
# build_worker_context
# ---------------------------------------------------------------------------


def test_worker_context_announces_verify_cmd_gate(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_cmd="pytest -q")
        ctx = kb.build_worker_context(conn, tid)
        assert "## Verified completion gate" in ctx
        assert "pytest -q" in ctx


def test_worker_context_announces_verify_auto_gate(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_mode="auto")
        ctx = kb.build_worker_context(conn, tid)
        assert "## Verified completion gate" in ctx
        assert "full-scope" in ctx


def test_worker_context_renders_latest_red_evidence(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        kb.record_verify_failure(
            conn, tid,
            gate="verify_cmd", command="pytest -q", exit_code=1,
            output_excerpt="2 failed, 1 passed",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            failure_limit=5,
        )
        ctx = kb.build_worker_context(conn, tid)
        assert "2 failed, 1 passed" in ctx
        assert "Exit code: 1" in ctx


def test_worker_context_redacts_verify_cmd(kanban_home: Path) -> None:
    """The worker prompt is broadcast into transcripts/logs — an
    inline-credential verify command must not ride along raw."""
    secret = "ghp_" + "Abc123XyZ0" * 3
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker",
                             verify_cmd=f"API_TOKEN={secret} pytest -q")
        ctx = kb.build_worker_context(conn, tid)
        assert "## Verified completion gate" in ctx
        assert secret not in ctx
        assert "pytest -q" in ctx


def test_worker_context_unchanged_without_verify_config(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        ctx = kb.build_worker_context(conn, tid)
        assert "Verified completion gate" not in ctx


# ---------------------------------------------------------------------------
# complete_task chokepoint: verify-mode tasks refuse ungated completion
# ---------------------------------------------------------------------------


def test_complete_task_refuses_verify_task_without_gate(kanban_home: Path) -> None:
    """complete_task is the sole writer of status='done'; a verify-mode task
    must not complete unless the caller declares how the gate was satisfied.
    Fail-closed for every current and future caller by default."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        with pytest.raises(kb.VerifyGateRequiredError):
            kb.complete_task(conn, tid, result="did it")
        t = kb.get_task(conn, tid)
        assert t.status == "running"  # untouched
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "completion_refused_unverified" in kinds


def test_complete_task_verify_gate_passed_completes(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        assert kb.complete_task(conn, tid, result="ok", verify_gate="passed")
        assert kb.get_task(conn, tid).status == "done"


def test_complete_task_verify_gate_waived_completes(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        assert kb.complete_task(conn, tid, result="ok", verify_gate="waived")
        assert kb.get_task(conn, tid).status == "done"


def test_complete_task_rejects_unknown_verify_gate_value(kanban_home: Path) -> None:
    """A typo'd gate token must fail closed, not slip through."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        with pytest.raises(ValueError):
            kb.complete_task(conn, tid, result="ok", verify_gate="pased")
        assert kb.get_task(conn, tid).status == "running"


def test_complete_task_plain_task_unaffected_by_default(kanban_home: Path) -> None:
    """Zero-change guard: tasks without verify_mode complete exactly as
    before with no gate argument."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.complete_task(conn, tid, result="ok")
        assert kb.get_task(conn, tid).status == "done"


def test_complete_task_refusal_never_burns_retry_budget(kanban_home: Path) -> None:
    """The chokepoint refusal is an API-misuse/bypass signal, not a red
    verification: consecutive_failures must not move."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, verify_cmd="pytest -q")
        before = kb.get_task(conn, tid).consecutive_failures
        with pytest.raises(kb.VerifyGateRequiredError):
            kb.complete_task(conn, tid, result="x")
        assert kb.get_task(conn, tid).consecutive_failures == before


# ---------------------------------------------------------------------------
# Platform guard: cmd mode is POSIX-only, rejected without burning budget
# ---------------------------------------------------------------------------


def test_run_verify_command_rejects_unsupported_platform(
    monkeypatch, tmp_path: Path
) -> None:
    """cmd mode shells through /bin/sh with process-group reaping — on a
    non-POSIX host it must return a distinct unsupported outcome, never
    attempt the spawn."""
    monkeypatch.setattr(kv, "platform_supported", lambda: False)
    out = kv.run_verify_command("echo hi", cwd=str(tmp_path))
    assert out.ok is False
    assert out.gate == "verify_unsupported_platform"
    assert out.exit_code is None
    assert "posix" in out.detail.lower() or "platform" in out.detail.lower()
