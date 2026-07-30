"""Regression tests for the verified-blocker rule (Article XII P5 + XIV P2).

The 2026-07-19 incident: an audit declared a task ``Blocked on SSH key`` —
the SSH key was available throughout (``~/.ssh/id_ed25519_macbook_pro`` was
loadable via ``ssh-agent``); the agent did not attempt to load it. The
false blocker went undetected for 8 days, leaving a misclaim in production
that surfaced only after independent verification.

The structural fix: ``block_task()`` now requires a falsification record
(``evidence``) on a block that would land in ``blocked`` (the human queue)
when ``kanban.require_block_evidence`` is enabled in ``config.yaml``.
Dependency blocks (route to ``todo``) and loop-detected routing to
``triage`` are exempt at the routing layer; their destinations are not
the human queue. The circuit breaker (system-observed) and the goal-loop
budget fallback pass ``_system_observed=True`` so the gate recognises them
as runtime observations rather than caller claims.

These tests pin the gate end-to-end:

* Non-dependency block without evidence → ``ValueError`` (gate on).
* Non-dependency block with evidence → succeeds; evidence recorded in the
  ``blocked`` event payload and synthesized run summary.
* Dependency block without evidence → succeeds (exempt at routing).
* Loop-detected routing-to-triage → succeeds WITHOUT evidence on the
  loop-triggering block (exempt at routing). The re-block that trips the
  counter must not require evidence just because the next call would
  route to triage.
* Circuit-breaker (system-observed) blocker works without evidence.
* ``_system_observed=True`` lets a system path bypass the gate cleanly.
* Whitespace-only evidence is treated as missing.
* Default off (legacy call sites without evidence still work).
* Unset config (no ``kanban`` key) keeps legacy behavior.
* 2026-07-19 failure scenario is structurally impossible.
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gate_enabled(monkeypatch):
    """Enable the verified-blocker gate by patching load_config().

    ``load_config`` is patched at the import location used by kanban_db
    (the import happens inside the function body), so the patch is
    applied at the hermes_cli.config module level.
    """
    import hermes_cli.config as cfg_module

    def _patched_load():
        return {"kanban": {"require_block_evidence": True}}

    monkeypatch.setattr(cfg_module, "load_config", _patched_load)
    return _patched_load


@pytest.fixture
def gate_disabled(monkeypatch):
    """Explicitly disable the gate (the default)."""
    import hermes_cli.config as cfg_module

    def _patched_load():
        return {"kanban": {"require_block_evidence": False}}

    monkeypatch.setattr(cfg_module, "load_config", _patched_load)


def _create_running_task(conn, title: str = "test") -> str:
    """Create + claim a task so it's in ``running`` status (blockable)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    kb.claim_task(conn, tid)
    return tid


# ---------------------------------------------------------------------------
# Core gate: non-dependency block requires evidence (gate ON)
# ---------------------------------------------------------------------------

def test_non_dependency_block_without_evidence_raises(kanban_home, gate_enabled):
    """The 2026-07-19 failure mode: declare a blocker without falsification.
    After the fix, ``block_task()`` must raise ``ValueError`` — the
    transition is structurally impossible.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "verify live state")
        with pytest.raises(ValueError) as exc_info:
            kb.block_task(
                conn, tid,
                reason="SSH key appears unavailable",
                kind="capability",
            )
        msg = str(exc_info.value)
        assert "evidence is required" in msg
        assert "Article XII P5" in msg
        assert "Article XIV P2" in msg
    finally:
        conn.close()


def test_non_dependency_block_with_evidence_succeeds(kanban_home, gate_enabled):
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
        # Evidence is recorded in the task_events log.
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert ev_rows, "blocked event was not appended"
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("evidence") == evidence
        # Evidence is also appended to the synthesized run summary.
        runs = conn.execute(
            "SELECT summary FROM task_runs WHERE task_id = ? ORDER BY id DESC",
            (tid,),
        ).fetchall()
        assert runs, "expected at least one task_runs row"
        assert "[evidence]" in (runs[0]["summary"] or "")
        assert evidence in (runs[0]["summary"] or "")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routing-layer exemptions: dependency → todo, loop → triage
# ---------------------------------------------------------------------------

def test_dependency_block_without_evidence_succeeds(kanban_home, gate_enabled):
    """Dependency blocks route to ``todo`` (not ``blocked``) — no
    external state claim, so falsification evidence is not required.
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
        assert task.status == "todo"
    finally:
        conn.close()


def test_loop_detected_routing_to_triage_succeeds_without_evidence(kanban_home, gate_enabled):
    """Loop detection routes to ``triage`` (not ``blocked``), so the
    evidence gate does NOT fire on the loop-triggering block.

    This is the test the v1 PR got wrong: it supplied evidence on both
    calls, which masked the fact that the gate was firing before
    routing determination. The fix here moves the gate after
    routing — the loop-triggering block is the second block of the
    same kind, and it must land in ``triage`` without any evidence
    from the caller.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "test loop")
        # First block with evidence — no loop yet, lands in 'blocked'.
        kb.block_task(
            conn, tid,
            reason="first block",
            kind="needs_input",
            evidence="first-block evidence: tried X; got Y",
        )
        # Unblock to set up the re-block for the same kind.
        kb.unblock_task(conn, tid)
        # Re-claim so the task is blockable again.
        kb.claim_task(conn, tid)
        # Second block with same kind — recurrences becomes 2, which is
        # >= BLOCK_RECURRENCE_LIMIT (2), so this routes to ``triage``.
        # The gate must NOT fire here — destination is ``triage``, not
        # the human queue. The call must succeed without evidence.
        ok = kb.block_task(
            conn, tid,
            reason="second block same kind",
            kind="needs_input",
            # No evidence — destination is triage, exempt at routing.
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "triage", (
            f"expected loop-triggered re-block to land in 'triage', "
            f"got {task.status!r}"
        )
        # The loop event is recorded with the evidence field (None when
        # not supplied) — the audit trail still has the falsification
        # null-marker, signalling "no caller-supplied evidence".
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'block_loop_detected'",
            (tid,),
        ).fetchall()
        assert ev_rows, "expected a block_loop_detected event"
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("recurrences") >= 2
        assert payload.get("kind") == "needs_input"
    finally:
        conn.close()


def test_empty_string_evidence_treated_as_missing(kanban_home, gate_enabled):
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


# ---------------------------------------------------------------------------
# System-observed exemption: circuit breaker + goal-loop fallback
# ---------------------------------------------------------------------------

def test_system_observed_bypasses_gate(kanban_home, gate_enabled):
    """``_system_observed=True`` lets a system path bypass the gate.

    The circuit breaker (``_record_task_failure``) and the goal-loop
    budget fallback are the two callers that set this flag. The
    verification of the breaker path itself is in
    ``test_circuit_breaker_still_works_without_evidence`` below; here
    we pin the public contract: any caller can pass
    ``_system_observed=True`` and the gate won't fire.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "system observed")
        ok = kb.block_task(
            conn, tid,
            reason="system-observed fallback",
            kind="capability",
            # No evidence, but _system_observed=True → gate is bypassed.
            _system_observed=True,
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # The audit trail records system_observed=True so any future
        # investigator can see this was a system-observed transition.
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert ev_rows
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("system_observed") is True
    finally:
        conn.close()


def test_system_observed_false_with_evidence_records_flag_correctly(kanban_home, gate_enabled):
    """When a caller passes both ``_system_observed=False`` and a real
    evidence, the gate is satisfied by the evidence, and the audit
    trail records ``system_observed=False`` so it's clear this was a
    caller claim rather than a runtime observation.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "explicit claim")
        ok = kb.block_task(
            conn, tid,
            reason="explicit",
            kind="capability",
            evidence="verified by running X",
            _system_observed=False,
        )
        assert ok is True
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert ev_rows
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("system_observed") is False
        assert payload.get("evidence") == "verified by running X"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Default-off behavior (gate OFF — legacy call sites)
# ---------------------------------------------------------------------------

def test_default_off_allows_existing_call_sites(kanban_home, gate_disabled):
    """When the verified-blocker gate is OFF (default — set in
    config.yaml ``kanban.require_block_evidence: false`` or unset),
    existing call sites that don't supply evidence continue to work.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy caller", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.block_task(conn, tid, reason="legacy reason", kind="capability")
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
    finally:
        conn.close()


def test_unset_config_allows_legacy_call_sites(kanban_home, monkeypatch):
    """When config.yaml is absent or doesn't have the key, the gate is
    off (no behavior change from current default). Operators who want
    the gate MUST opt in via ``kanban.require_block_evidence: true``.
    """
    import hermes_cli.config as cfg_module

    def _patched_load():
        return {}

    monkeypatch.setattr(cfg_module, "load_config", _patched_load)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no-config caller", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.block_task(conn, tid, reason="no config", kind="capability")
        assert ok is True
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_malformed_config_does_not_break_block_task(kanban_home, monkeypatch):
    """If load_config() raises (e.g. malformed YAML), ``block_task``
    defaults the gate to OFF rather than blowing up. The block still
    succeeds with whatever evidence the caller passed (or none).
    """
    import hermes_cli.config as cfg_module

    def _patched_load():
        raise RuntimeError("simulated malformed config")

    monkeypatch.setattr(cfg_module, "load_config", _patched_load)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="malformed config", assignee="worker")
        kb.claim_task(conn, tid)
        # No evidence, gate defaulted off → block succeeds.
        ok = kb.block_task(conn, tid, reason="legacy", kind="capability")
        assert ok is True
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Circuit breaker is system-observed, not agent-claimed
# ---------------------------------------------------------------------------

def test_circuit_breaker_still_works_without_evidence(
    kanban_home, all_assignees_spawnable, gate_enabled,
):
    """System-generated circuit-breaker blockers are NOT subject to the
    evidence gate — the failure is system-observed (worker died, timeout,
    spawn failure), not an agent claim. ``_record_task_failure`` sets
    ``_system_observed=True`` (and the dispatched-failure reason is
    the falsification), so the breaker still trips when the gate is on.

    Verified by: a spawn-fail that trips the breaker results in a
    ``blocked`` status with ``last_failure_error`` populated.
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
        res1 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid not in res1.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        res2 = kb.dispatch_once(conn, spawn_fn=_bad_spawn)
        assert tid in res2.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"circuit breaker should auto-block but status is {task.status}"
        )
        # last_failure_error is the system's own evidence.
        assert task.last_failure_error and "PATH" in task.last_failure_error
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The 2026-07-19 failure scenario — must be rejected at runtime
# ---------------------------------------------------------------------------

def test_2026_07_19_failure_scenario_reproduction(kanban_home, gate_enabled):
    """The exact failure that motivated the fix: a worker declares a
    capability blocker citing a technical condition (SSH key unavailable)
    without first attempting to load the key. Under the new gate, this
    transition is rejected at the runtime level — the task stays in
    ``running`` (or returns to the worker's hands) and the false blocker
    is structurally impossible to create.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "2026-07-19 repro")
        with pytest.raises(ValueError) as exc_info:
            kb.block_task(
                conn, tid,
                reason="SSH key unavailable",
                kind="capability",
            )
        assert "evidence is required" in str(exc_info.value)
        task = kb.get_task(conn, tid)
        assert task.status == "running", (
            f"task should still be running after rejected false blocker; "
            f"got {task.status}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Caller-claim invariant: every agent claim lands with system_observed=False
# ---------------------------------------------------------------------------

def test_default_system_observed_is_false(kanban_home, gate_enabled):
    """Omitting ``_system_observed`` defaults to False, so every
    caller-claim path that forgets to set it is correctly classified
    as a claim (not a runtime observation). This pins the
    never-silent-bypass invariant.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "default flag")
        kb.block_task(
            conn, tid,
            reason="explicit claim",
            kind="capability",
            evidence="ran X; observed Y",
            # _system_observed not passed → defaults to False.
        )
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert ev_rows
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("system_observed") is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Evidence is recorded for dependency and loop paths even though gate
# doesn't fire there — audit trail parity.
# ---------------------------------------------------------------------------

def test_dependency_block_records_evidence_field_when_supplied(kanban_home, gate_enabled):
    """Even though the dependency branch is exempt at the routing
    layer, the evidence field is still plumbed into the event payload
    when the caller supplies one. This keeps the audit trail uniform.
    """
    conn = kb.connect()
    try:
        tid = _create_running_task(conn, "dep with evidence")
        kb.block_task(
            conn, tid,
            reason="waiting on parent",
            kind="dependency",
            evidence="verified parent is still open: task ABC status=running",
        )
        ev_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'dependency_wait'",
            (tid,),
        ).fetchall()
        assert ev_rows
        payload = json.loads(ev_rows[-1]["payload"])
        assert payload.get("evidence", "").startswith("verified parent")
    finally:
        conn.close()