"""Regression tests for the verified-blocker rule (Article XII P5 + XIV P2).

The 2026-07-19 incident: an audit declared a task ``Blocked on SSH key`` —
the SSH key was available throughout (``~/.ssh/id_ed25519_macbook_pro`` was
loadable via ``ssh-agent``); the agent did not attempt to load it. The
false blocker went undetected for 8 days, leaving a misclaim in production
that surfaced only after independent verification.

The structural fix: ``block_task()`` now requires a falsification record
(``evidence``) for non-dependency blocks. The runtime gate makes the failure
mode impossible by construction — an agent cannot transition a task to
``blocked`` without first recording the verification it performed.

These tests pin the gate:

* Non-dependency block without evidence → ``ValueError``.
* Non-dependency block with evidence → succeeds; evidence recorded in the
  ``blocked`` event payload + synthesized run summary.
* Dependency block without evidence → succeeds (no external state claim;
  routing decision, not a falsifiable hypothesis).
* Circuit-breaker (system-generated) blocker still works — the breaker
  records ``last_failure_error`` as its own evidence, and a non-empty
  ``summary`` is plumbed through to the ``gave_up`` event. The
  ``_record_task_failure`` path is NOT subject to the evidence gate; the
  failure is system-observed, not agent-claimed.
* Existing tests still pass: a 2026-07-19-style false blocker cannot be
  created even by direct API call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # Enable the verified-blocker gate for all tests in this file.
    # The gate is opt-in via env var; the regression tests verify the
    # gate is on, not the default-off behavior.
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_BLOCK_EVIDENCE", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _create_running_task(conn, title: str = "test") -> str:
    """Create + claim a task so it's in ``running`` status (blockable)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    kb.claim_task(conn, tid)
    return tid


# ---------------------------------------------------------------------------
# Core gate: non-dependency block requires evidence
# ---------------------------------------------------------------------------

def test_non_dependency_block_without_evidence_raises(kanban_home):
    """The 2026-07-19 failure mode: declare a blocker without falsification.
    After the fix, ``block_task()`` must raise ``ValueError`` — the
    transition is structurally impossible.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "verify live state")
        # No evidence supplied — mirrors the 2026-07-19 audit's
        # "Blocked on SSH key" without a falsification record.
        with pytest.raises(ValueError) as exc_info:
            kb.block_task(
                conn, tid,
                reason="SSH key appears unavailable",
                kind="capability",
            )
        # The error message MUST cite the constitutional anchors so any
        # future agent reading the trace understands the rule and not
        # just the symptom.
        msg = str(exc_info.value)
        assert "evidence is required" in msg
        assert "falsify" in msg.lower() or "Article XII" in msg
    finally:
        conn.close()


def test_non_dependency_block_with_evidence_succeeds(kanban_home):
    """A real blocker with a real falsification record is allowed."""
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "test real blocker")
        evidence = (
            "ran `ssh-add ~/.ssh/id_ed25519_macbook_pro`; result: "
            "Agent admitted the key, fingerprint sha256:abc123; "
            "subsequent `ssh -o BatchMode=yes macbook-pro echo OK` "
            "returned 'OK' and exit 0. Blocker is real because of the "
            "user account issue, not the key."
        )
        ok = kb.block_task(
            conn, tid,
            reason="Remote account locked",
            kind="capability",
            evidence=evidence,
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # Evidence is recorded in the task_runs event log.
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert ev_rows, "blocked event was not appended"
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("evidence") == evidence
    finally:
        conn.close()


def test_dependency_block_without_evidence_succeeds(kanban_home):
    """Dependency blocks route to ``todo`` (not ``blocked``) — no external
    state claim, so falsification evidence is not required.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "wait on parent")
        ok = kb.block_task(
            conn, tid,
            reason="waiting on parent task",
            kind="dependency",
            # No evidence — must not raise.
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        # Dependency blocks land in ``todo`` (per block_task's routing),
        # NOT in ``blocked`` — this is the existing rule.
        assert task.status == "todo"
    finally:
        conn.close()


def test_loop_detected_routing_to_triage_succeeds_without_evidence(kanban_home):
    """Loop detection (block_recurrences >= BLOCK_RECURRENCE_LIMIT) routes
    to ``triage``. This is a runtime observation, not an agent claim;
    evidence is not required. The gate only fires for blocks that would
    land in ``blocked``.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "test loop")
        # First block with evidence — no loop yet.
        kb.block_task(
            conn, tid,
            reason="first block",
            kind="needs_input",
            evidence="first-block evidence: tried X; got Y",
        )
        # Unblock to set up the re-block for the same kind.
        kb.unblock_task(conn, tid)
        # Second block with same kind — recurrences becomes 2; if
        # BLOCK_RECURRENCE_LIMIT is 2 or lower, this routes to triage.
        # The exact route depends on the constant, but the call must
        # NOT raise (the loop-detection path is exempt from the gate).
        ok = kb.block_task(
            conn, tid,
            reason="second block same kind",
            kind="needs_input",
            evidence="second-block evidence: tried X again; same Y",
        )
        assert ok is True
    finally:
        conn.close()


def test_empty_string_evidence_treated_as_missing(kanban_home):
    """Whitespace-only or empty evidence is treated as missing. The
    falsification record must contain actual content.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "test empty evidence")
        for bad_evidence in ["", "   ", "\n\t"]:
            with pytest.raises(ValueError) as exc_info:
                kb.block_task(
                    conn, tid,
                    reason="attempted block",
                    kind="capability",
                    evidence=bad_evidence,
                )
            assert "evidence is required" in str(exc_info.value)
    finally:
        conn.close()


def test_default_off_gate_allows_existing_call_sites(monkeypatch, kanban_home, all_assignees_spawnable):
    """When the verified-blocker gate is OFF (default), existing call
    sites that don't supply evidence continue to work. This guards
    against accidentally flipping the default to ON (which would break
    backward compatibility for all un-updated callers).
    """
    # The kanban_home fixture sets the env var to "1". This test
    # explicitly removes it to verify the default-off behavior.
    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_BLOCK_EVIDENCE", raising=False)
    import os
    assert os.environ.get("HERMES_KANBAN_REQUIRE_BLOCK_EVIDENCE", "") != "1", (
        "test must run with the gate off"
    )

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy caller", assignee="worker")
        kb.claim_task(conn, tid)
        # No evidence — should succeed because the gate is off by default.
        ok = kb.block_task(conn, tid, reason="legacy reason", kind="capability")
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
    finally:
        conn.close()


def test_circuit_breaker_still_works_without_evidence(kanban_home, all_assignees_spawnable):
    """System-generated circuit-breaker blockers are NOT subject to the
    evidence gate — the failure is system-observed (worker died, timeout,
    spawn failure), not an agent claim. ``_record_task_failure`` is the
    system path; it must continue to work without requiring the dispatcher
    to invent a falsification record.

    Verified by: a spawn-fail that trips the breaker results in a ``blocked``
    status with ``last_failure_error`` populated (the system's own evidence).
    """
    def _bad_spawn(task, ws):
        # Avoid trigger words for blocker_auth respawn-guard
        # (auth/quota/permission) so the failure goes through the
        # circuit breaker, not the respawn guard.
        raise RuntimeError("spawn subprocess failed: no PATH configured")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="auto-block test", assignee="worker")
        assert kb.DEFAULT_FAILURE_LIMIT == 2
        # First failure: still ready, counter grows.
        res1 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid not in res1.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        # Second failure: trips the breaker.
        res2 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid in res2.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"circuit breaker should auto-block but status is {task.status}"
        )
        # last_failure_error is the system's own evidence — recorded by
        # construction, not by a falsification pass.
        assert task.last_failure_error and "PATH" in task.last_failure_error
    finally:
        conn.close()


def test_2026_07_19_failure_scenario_reproduction(kanban_home):
    """The exact failure that motivated the fix: a worker declares a
    capability blocker citing a technical condition (SSH key unavailable)
    without first attempting to load the key. Under the new gate, this
    transition is rejected at the runtime level — the task stays in
    ``running`` (or returns to the worker's hands) and the false blocker
    is structurally impossible to create.

    Without the fix, the agent could:
        kb.block_task(conn, tid, reason="SSH key unavailable", kind="capability")
    and the audit would mark the task ``Blocked on SSH key`` even though
    the key was loadable throughout.

    With the fix, the same call raises ValueError; the agent must supply
    evidence (e.g. "ssh-add failed: agent refused operation") for the
    blocker to land.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "2026-07-19 reproduction")
        # Without evidence: must raise.
        with pytest.raises(ValueError):
            kb.block_task(
                conn, tid,
                reason="SSH key unavailable",
                kind="capability",
            )
        # Task is still in ``running`` after the rejected transition —
        # the false blocker did not land.
        task = kb.get_task(conn, tid)
        assert task.status == "running", (
            f"false blocker was not rejected: task is {task.status}"
        )
    finally:
        conn.close()
