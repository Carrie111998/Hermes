"""Tests for the shared goal-mode completion gate.

Regression coverage for the stranded-completion bug: a judge API outage made
``judge_goal`` return ``("continue", "judge error: ...", False, None, True)``,
which both kanban completion paths read as a substantive rejection. Finished
cards were refused, the worker burned its turn budget, and the run ended
``blocked`` with the work done but never recorded.
"""

from __future__ import annotations

import json
import types

import pytest

from hermes_cli import kanban_judge_gate as gate


class _FakeConn:
    """Connection stub returning prior judge_rejected payload rows.

    ``count`` prior rejections are synthesised with DISTINCT evidence
    digests, matching the real counting rule (distinct evidence per run).
    Pass ``same_evidence=True`` to simulate a worker resubmitting the
    identical rejected summary.
    """

    def __init__(self, count: int = 0, same_evidence: bool = False):
        self.count = count
        self.same_evidence = same_evidence
        self.executed: list[tuple] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        rows = [
            (json.dumps({"resp": "same" if self.same_evidence else f"d{i}"}),)
            for i in range(self.count)
        ]
        return types.SimpleNamespace(fetchall=lambda: rows)


class _FakeKb:
    """Records gate events without touching a real database."""

    def __init__(self):
        self.events: list[tuple[str, str, dict | None]] = []

    def write_txn(self, conn):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield conn

        return _cm()

    # Mirrors kanban_db._append_event, which takes run_id keyword-only.
    def _append_event(self, conn, task_id, kind, payload=None, *, run_id=None):
        self.events.append((task_id, kind, payload))


def _task(goal_mode=True, title="Finish report", body="acceptance: criteria"):
    return types.SimpleNamespace(goal_mode=goal_mode, title=title, body=body)


def _patch_judge(monkeypatch, verdict, reason, transport_failed,
                 parse_failed=False):
    monkeypatch.setattr(gate, "judge_available", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: (verdict, reason, parse_failed, None, transport_failed),
    )


@pytest.fixture(autouse=True)
def _clean_config(monkeypatch):
    """Every test starts from stock config: gate on, default ceiling.

    Also clears the old env vars. They are no longer read, and pinning them
    empty keeps a stale export in a developer's shell from being mistaken for
    the code still honouring them.
    """
    monkeypatch.setattr(gate, "_kanban_config", lambda: {})
    monkeypatch.delenv("HERMES_KANBAN_JUDGE_GATE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_JUDGE_MAX_REJECTIONS", raising=False)


def _patch_config(monkeypatch, **kanban):
    """Point the gate at a synthetic ``kanban:`` config block."""
    monkeypatch.setattr(gate, "_kanban_config", lambda: dict(kanban))


def test_non_goal_mode_skips_gate(monkeypatch):
    monkeypatch.setattr(gate, "judge_available", lambda: True)
    d = gate.evaluate(_FakeKb(), _FakeConn(), _task(goal_mode=False), "x",
                      task_id="t1")
    assert d.allow is True
    assert d.transport is False and d.override is False


def test_judge_unavailable_fails_open(monkeypatch):
    monkeypatch.setattr(gate, "judge_available", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: pytest.fail("judge must not run when unavailable"),
    )
    assert gate.evaluate(_FakeKb(), _FakeConn(), _task(), "x",
                         task_id="t1").allow is True


def test_done_verdict_allows(monkeypatch):
    _patch_judge(monkeypatch, "done", "criteria met", False)
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(), _task(), "evidence", task_id="t1")
    assert d.allow is True and d.override is False and d.transport is False
    assert kb.events == [], "an accepted completion records no gate event"


def test_reachable_judge_rejection_still_blocks(monkeypatch):
    """The gate must keep its teeth: a real 'not done' verdict fails closed."""
    _patch_judge(monkeypatch, "continue", "criteria not met", False)
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(count=0), _task(), "half done", task_id="t1")
    assert d.allow is False
    assert d.reason == "criteria not met"
    assert len(kb.events) == 1
    tid, kind, payload = kb.events[0]
    assert (tid, kind) == ("t1", gate.EVENT_REJECTED)
    assert payload["reason"] == "criteria not met"
    assert payload["attempt"] == 1 and payload["ceiling"] == 2
    # Evidence digest is what makes repeat submissions non-counting.
    assert payload["resp"] == gate._evidence_digest("half done")


def test_transport_failure_fails_open(monkeypatch):
    """The core regression: judge API outage must not refuse a completion.

    judge_goal signals an unreachable judge with transport_failed=True and a
    "continue" verdict; reading only the verdict stranded finished cards.
    """
    _patch_judge(monkeypatch, "continue", "judge error: InternalServerError",
                 True)
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(count=0), _task(), "work done", task_id="t1")
    assert d.allow is True, "transport failure must fail open"
    assert d.transport is True
    assert d.override is False
    assert kb.events == [("t1", gate.EVENT_TRANSPORT,
                          {"reason": "judge error: InternalServerError"})]


def test_rejection_ceiling_allows_with_override(monkeypatch):
    """Past the ceiling, a repeatedly-rejected card completes with an audit
    trail instead of wedging the board forever."""
    _patch_judge(monkeypatch, "continue", "self-referential", False)
    kb = _FakeKb()
    # Two prior rejections already recorded; ceiling=2 tolerates two, so this
    # third attempt overrides.
    d = gate.evaluate(kb, _FakeConn(count=2), _task(), "done", task_id="t1")
    assert d.allow is True
    assert d.override is True
    assert kb.events[0][1] == gate.EVENT_OVERRIDE
    assert kb.events[0][2]["rejections"] == 3


def test_ceiling_tolerates_exactly_ceiling_rejections(monkeypatch):
    """ceiling=2 must reject twice before yielding, not once.

    With the original `prior + 1 >= ceiling` test the gate yielded on the
    second attempt, so a single rejection was all it ever enforced.
    """
    _patch_judge(monkeypatch, "continue", "not yet", False)
    # Second attempt (one prior rejection) must still be refused.
    d = gate.evaluate(_FakeKb(), _FakeConn(count=1), _task(), "x", task_id="t1")
    assert d.allow is False


def test_unrecordable_rejection_fails_open(monkeypatch):
    """If the rejection cannot be persisted, the ceiling can never advance —
    blocking would wedge the card forever, so fail open instead."""
    _patch_judge(monkeypatch, "continue", "criteria not met", False)

    class _UnwritableKb(_FakeKb):
        def write_txn(self, conn):
            raise RuntimeError("database is locked")

    d = gate.evaluate(_UnwritableKb(), _FakeConn(count=0), _task(), "x",
                      task_id="t1")
    assert d.allow is True
    # Must NOT report "ceiling": there are no judge_rejected events to
    # corroborate that claim, so stamping one would be a false audit trail.
    assert d.override is False
    assert d.bypass == gate.BYPASS_UNRECORDABLE


def test_repeat_submissions_cannot_brute_force_the_ceiling(monkeypatch):
    """Resubmitting the SAME rejected evidence must not open the escape hatch.

    Counting attempts rather than distinct evidence let a worker close any
    card by calling kanban_complete three times with identical junk — and the
    rejection message explicitly invites a retry.
    """
    _patch_judge(monkeypatch, "continue", "criteria not met", False)
    # Five prior rejections, all of the same summary -> one distinct piece of
    # evidence, well under ceiling 2.
    d = gate.evaluate(_FakeKb(), _FakeConn(count=5, same_evidence=True),
                      _task(), "same junk", task_id="t1")
    assert d.allow is False, "identical retries must not reach the ceiling"


def test_ceiling_counts_only_the_current_run(monkeypatch):
    """Rejections are scoped to run_id when it is known.

    Lifetime counting meant a contested card arrived at each respawn already
    at its ceiling, so the first attempt of every later run was ungated.
    """
    _patch_judge(monkeypatch, "continue", "nope", False)
    conn = _FakeConn(count=0)
    gate.evaluate(_FakeKb(), conn, _task(), "x", task_id="t1", run_id=42)
    sql, params = conn.executed[0]
    assert "run_id = ?" in sql
    assert params[-1] == 42


def test_ceiling_is_configurable(monkeypatch):
    _patch_judge(monkeypatch, "continue", "nope", False)
    _patch_config(monkeypatch, judge_max_rejections=5)
    # 2 priors is well under a ceiling of 5, so still blocked.
    d = gate.evaluate(_FakeKb(), _FakeConn(count=2), _task(), "x", task_id="t1")
    assert d.allow is False


def test_ceiling_zero_disables_override(monkeypatch):
    """Ceiling 0 restores the old reject-forever behaviour."""
    _patch_judge(monkeypatch, "continue", "nope", False)
    _patch_config(monkeypatch, judge_max_rejections=0)
    d = gate.evaluate(_FakeKb(), _FakeConn(count=99), _task(), "x", task_id="t1")
    assert d.allow is False and d.override is False


def test_env_var_no_longer_disables_the_gate(monkeypatch):
    """The old env switch must be inert.

    A dispatcher worker owns its own environment, so honouring an env toggle
    would let the process being gated switch the gate off for itself. This is
    the regression test for that: setting the historical variable must not
    change behaviour.
    """
    monkeypatch.setenv("HERMES_KANBAN_JUDGE_GATE", "off")
    _patch_judge(monkeypatch, "continue", "nope", False)
    d = gate.evaluate(_FakeKb(), _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is False


def test_unreadable_config_leaves_the_gate_on(monkeypatch):
    """A broken config file is not consent to stop checking.

    Fails ``load_config`` itself rather than stubbing ``_kanban_config``: the
    tolerance being tested lives *inside* that helper, so replacing it would
    stub out the very guard under test.
    """
    monkeypatch.undo()  # drop the autouse _kanban_config stub for this test

    def _boom(*a, **kw):
        raise RuntimeError("config.yaml is unparseable")

    import hermes_cli.config as _cfg
    monkeypatch.setattr(_cfg, "load_config", _boom)

    assert gate._kanban_config() == {}
    assert gate.gate_enabled() is True
    assert gate.max_rejections() == gate.DEFAULT_MAX_REJECTIONS

    _patch_judge(monkeypatch, "continue", "nope", False)
    d = gate.evaluate(_FakeKb(), _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is False


def test_gate_can_be_disabled_by_config(monkeypatch):
    _patch_config(monkeypatch, judge_gate=False)
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: pytest.fail("judge must not run when gate is disabled"),
    )
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is True
    # Operator-owned or not, the skip must leave a trace — otherwise it is an
    # untraceable way past the judge.
    assert kb.events == [("t1", gate.EVENT_DISABLED,
                          {"config": "kanban.judge_gate"})]
    # Its own kind: claiming "unrecordable" would assert that a DB write
    # failed when it in fact succeeded.
    assert d.bypass == gate.BYPASS_DISABLED


def test_parse_failure_fails_open(monkeypatch):
    """A judge that replies with garbage is broken, not a finding.

    goals.py returns parse_failed=True with verdict="continue" for an empty or
    non-JSON reply. Reading only the verdict repeats the original transport
    bug one tuple element over.
    """
    _patch_judge(monkeypatch, "continue", "judge reply was not JSON",
                 False, parse_failed=True)
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(count=0), _task(), "work done", task_id="t1")
    assert d.allow is True
    assert kb.events == [("t1", gate.EVENT_PARSE_FAILED,
                          {"reason": "judge reply was not JSON"})]


@pytest.mark.parametrize("verdict", ["wait", "skipped", "weird-new-verdict"])
def test_non_substantive_verdicts_never_block(monkeypatch, verdict):
    """Only an explicit "continue" is a finding about the card.

    "skipped" is documented as "the judge couldn't be reached"; "wait" is
    normalised away by the kanban loop because workers have no wait-barrier.
    """
    _patch_judge(monkeypatch, verdict, "n/a", False)
    d = gate.evaluate(_FakeKb(), _FakeConn(count=0), _task(), "done",
                      task_id="t1")
    assert d.allow is True


@pytest.mark.parametrize("response", ["", "   ", "\n\t "])
def test_blank_evidence_is_refused(monkeypatch, response):
    """A completion with no evidence must be REFUSED, not waved through.

    Regression for a self-inflicted hole: an earlier version allowed blank
    evidence, so `hermes kanban complete <id>` with no flags closed any
    goal_mode card with the judge never consulted and nothing stamped — the
    gate rewarded omitting evidence. The justification ("bulk closes can't
    carry a summary") was false: --result applies to all ids by design.
    """
    monkeypatch.setattr(gate, "judge_available", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: pytest.fail("judge must not run on blank evidence"),
    )
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(count=0), _task(), response, task_id="t1")
    assert d.allow is False
    assert d.bypass is None
    # The judge was never consulted, so this must not consume a ceiling slot.
    assert kb.events == []


def test_empty_goal_allows_but_is_stamped(monkeypatch):
    """A card with no title/body has nothing to judge against — that is the
    card's fault, not the worker's. Allow, but never silently."""
    monkeypatch.setattr(gate, "judge_available", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: pytest.fail("judge must not run on an empty goal"),
    )
    d = gate.evaluate(_FakeKb(), _FakeConn(), _task(title="", body=""),
                      "evidence", task_id="t1")
    assert d.allow is True
    assert d.bypass == gate.BYPASS_NO_JUDGE


def test_missing_judge_is_never_silent(monkeypatch):
    """The default-firing branch. If auxiliary credentials ever change, the
    whole gate becomes a no-op — that must leave a trace."""
    monkeypatch.setattr(gate, "judge_available", lambda: False)
    kb = _FakeKb()
    d = gate.evaluate(kb, _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is True
    assert d.bypass == gate.BYPASS_NO_JUDGE
    assert kb.events == [("t1", gate.EVENT_NO_JUDGE, None)]


def test_every_bypass_label_matches_its_event(monkeypatch):
    """An audit trail that contradicts itself invites confident wrong
    conclusions, so the stamp and the event row must agree."""
    kb = _FakeKb()
    _patch_judge(monkeypatch, "continue", "boom", True)
    assert gate.evaluate(kb, _FakeConn(), _task(), "x",
                         task_id="t1").bypass == gate.BYPASS_TRANSPORT
    assert kb.events[-1][1] == gate.EVENT_TRANSPORT

    kb = _FakeKb()
    _patch_judge(monkeypatch, "continue", "junk", False, parse_failed=True)
    assert gate.evaluate(kb, _FakeConn(), _task(), "x",
                         task_id="t1").bypass == gate.BYPASS_PARSE
    assert kb.events[-1][1] == gate.EVENT_PARSE_FAILED

    kb = _FakeKb()
    _patch_judge(monkeypatch, "continue", "nope", False)
    assert gate.evaluate(kb, _FakeConn(count=2), _task(), "x",
                         task_id="t1").bypass == gate.BYPASS_CEILING
    assert kb.events[-1][1] == gate.EVENT_OVERRIDE


def test_card_text_is_redacted_before_reaching_the_judge(monkeypatch):
    """The CLI path had no redaction of its own and previously made no network
    call at all; the gate must not become an unredacted egress."""
    seen = {}

    monkeypatch.setattr(gate, "judge_available", lambda: True)
    monkeypatch.setattr("agent.redact.redact_sensitive_text",
                        lambda text, **kw: "[REDACTED]")

    def _capture(**kw):
        seen.update(kw)
        return ("done", "ok", False, None, False)

    monkeypatch.setattr("hermes_cli.goals.judge_goal", _capture)
    gate.evaluate(_FakeKb(), _FakeConn(), _task(body="patient MRN 12345"),
                  "ssn 000-00-0000", task_id="t1")
    assert seen["goal"] == "[REDACTED]"
    assert seen["last_response"] == "[REDACTED]"


def test_judge_exception_fails_open(monkeypatch):
    """judge_goal swallows its own errors, but a raise must not wedge work."""
    monkeypatch.setattr(gate, "judge_available", lambda: True)

    def _boom(**kw):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr("hermes_cli.goals.judge_goal", _boom)
    d = gate.evaluate(_FakeKb(), _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is True
    assert "RuntimeError" in d.reason


def test_mocked_connection_does_not_trip_ceiling(monkeypatch):
    """A MagicMock conn coerces to 1 via __int__; that must not count as a
    real prior rejection and silently disable the gate."""
    from unittest.mock import MagicMock

    _patch_judge(monkeypatch, "continue", "criteria not met", False)
    d = gate.evaluate(_FakeKb(), MagicMock(), _task(), "x", task_id="t1")
    assert d.allow is False, "a mocked count must not be treated as a rejection"


def test_event_recording_failure_does_not_block_completion(monkeypatch):
    """A broken event write must never strand finished work."""
    _patch_judge(monkeypatch, "continue", "judge error: Timeout", True)

    class _BrokenKb(_FakeKb):
        def write_txn(self, conn):
            raise RuntimeError("db is read-only")

    d = gate.evaluate(_BrokenKb(), _FakeConn(), _task(), "x", task_id="t1")
    assert d.allow is True and d.transport is True


@pytest.mark.parametrize("variant", ["done.", "done!", "  DONE  ", "Done,,,"])
def test_trivial_edits_do_not_buy_a_ceiling_slot(monkeypatch, variant):
    """Regression: sha256(strip()) let "done" -> "done." -> "done!" close any
    goal card in three junk summaries. The guarding test passed IDENTICAL
    evidence, so it stayed green while the hole was wide open."""
    assert gate._evidence_digest(variant) == gate._evidence_digest("done")


def test_substantively_different_evidence_still_counts():
    """Normalisation must not collapse genuinely different attempts."""
    assert gate._evidence_digest("opened PR #12, tests green") != \
        gate._evidence_digest("rebased onto main")
