"""Tests for the local pending human-intervention JSON store.

These exercise hermes_cli.human_intervention_remote_control — a process-local
store that tracks pending CLI human-intervention prompts so a mobile gateway
command can later deny/extend them.

Time is monkeypatched via the module-level ``_now`` indirection so expiry and
cleanup behaviour is deterministic.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def hic():
    """Import (fresh) the module under test."""
    import hermes_cli.human_intervention_remote_control as mod

    return importlib.reload(mod)


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    """Point HERMES_HOME at a temp dir for every test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_create_pending_intervention_writes_record(hic):
    rec = hic.create_pending_intervention(
        kind="approval",
        title="Run rm -rf?",
        preview="rm -rf /tmp/foo",
        session_key="sess-1",
        timeout_seconds=120,
        max_total_wait_minutes=15,
        code="1234",
    )
    assert rec.code == "1234"
    assert rec.kind == "approval"
    assert rec.state == "pending"
    assert rec.session_key == "sess-1"
    assert rec.deadline_ts > rec.created_ts
    assert rec.max_deadline_ts >= rec.deadline_ts

    fetched = hic.get_pending_intervention("1234")
    assert fetched is not None
    assert fetched.code == "1234"
    assert fetched.kind == "approval"
    assert fetched.state == "pending"
    assert fetched.session_key == "sess-1"
    assert fetched.deadline_ts > fetched.created_ts
    assert fetched.max_deadline_ts >= fetched.deadline_ts


def test_create_generates_unique_4digit_code(hic):
    rec = hic.create_pending_intervention(
        kind="sudo",
        title="sudo password",
        preview="sudo apt update",
        session_key="sess-2",
        timeout_seconds=60,
    )
    assert rec.code.isdigit()
    assert len(rec.code) == 4
    # Round-trips through the store.
    assert hic.get_pending_intervention(rec.code) is not None


def test_get_missing_returns_none(hic):
    assert hic.get_pending_intervention("9999") is None


def test_set_remote_decision_deny_marks_denied(hic):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=120,
        code="2222",
    )
    ok, reason, rec = hic.set_remote_decision("2222", "deny", source="mobile")
    assert ok is True
    assert reason == "ok"
    assert rec is not None
    assert rec.state == "denied"
    assert rec.decision == "deny"
    assert rec.decision_ts is not None
    assert rec.decision_source == "mobile"

    stored = hic.get_pending_intervention("2222")
    assert stored.state == "denied"


def test_set_remote_decision_not_found(hic):
    ok, reason, rec = hic.set_remote_decision("0000", "deny")
    assert ok is False
    assert reason == "not_found"
    assert rec is None


def test_set_remote_decision_extend_bounded(hic):
    rec = hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=60,
        max_total_wait_minutes=15,
        code="3333",
    )
    created = rec.created_ts
    base_deadline = rec.deadline_ts
    max_deadline = rec.max_deadline_ts

    # Extend by 5 minutes — stays under max.
    ok, reason, r1 = hic.set_remote_decision("3333", "extend", minutes=5)
    assert ok is True
    assert reason in ("ok", "clamped")
    assert r1.state == "extended"
    assert r1.decision == "extend"
    assert r1.deadline_ts == pytest.approx(base_deadline + 5 * 60)
    assert r1.deadline_ts <= max_deadline

    # Extend by a huge amount — must clamp to max_deadline_ts.
    ok, reason, r2 = hic.set_remote_decision("3333", "extend", minutes=120)
    assert ok is True
    assert r2.deadline_ts == pytest.approx(max_deadline)
    assert r2.deadline_ts <= max_deadline
    # Sanity: max is created + 15 minutes.
    assert max_deadline == pytest.approx(created + 15 * 60)


def test_expired_token_cannot_be_denied(hic, monkeypatch):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=60,
        code="4444",
    )
    # Jump far into the future so the deadline + grace is well past.
    base = hic._now()
    monkeypatch.setattr(hic, "_now", lambda: base + 10_000)

    ok, reason, rec = hic.set_remote_decision("4444", "deny")
    assert ok is False
    assert reason == "expired"
    assert rec is not None  # record still returned for context


def test_approve_action_rejected(hic):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=120,
        code="5555",
    )
    ok, reason, rec = hic.set_remote_decision("5555", "approve")
    assert ok is False
    assert reason in ("action_not_allowed", "approve_disabled")


def test_action_not_allowed_when_restricted(hic):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=120,
        code="6666",
        allowed_actions=["extend"],
    )
    ok, reason, rec = hic.set_remote_decision("6666", "deny")
    assert ok is False
    assert reason == "action_not_allowed"

    ok2, reason2, rec2 = hic.set_remote_decision("6666", "extend", minutes=3)
    assert ok2 is True
    assert rec2.state == "extended"


def test_consume_remote_decision_deny_then_resolved(hic):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=120,
        code="7777",
    )
    hic.set_remote_decision("7777", "deny", source="mobile")

    consumed = hic.consume_remote_decision("7777")
    assert consumed is not None
    assert consumed.decision == "deny"

    stored = hic.get_pending_intervention("7777")
    assert stored.state == "resolved"

    # Second consume yields no actionable decision.
    again = hic.consume_remote_decision("7777")
    assert again is None or again.state == "resolved"


def test_consume_remote_decision_extend_resets_to_pending(hic):
    rec = hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=60,
        max_total_wait_minutes=15,
        code="8888",
    )
    base_deadline = rec.deadline_ts
    hic.set_remote_decision("8888", "extend", minutes=5)

    consumed = hic.consume_remote_decision("8888")
    assert consumed is not None
    assert consumed.decision == "extend"
    assert consumed.deadline_ts == pytest.approx(base_deadline + 5 * 60)

    stored = hic.get_pending_intervention("8888")
    assert stored.state == "pending"
    assert stored.decision is None
    # The extended deadline is preserved after reset.
    assert stored.deadline_ts == pytest.approx(base_deadline + 5 * 60)


def test_consume_pending_returns_none(hic):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=120,
        code="1212",
    )
    # No decision yet → nothing to consume.
    assert hic.consume_remote_decision("1212") is None


def test_cleanup_expired_removes_old(hic, monkeypatch):
    hic.create_pending_intervention(
        kind="approval",
        title="t",
        preview="p",
        session_key="s",
        timeout_seconds=60,
        max_total_wait_minutes=15,
        code="9090",
    )
    base = hic._now()
    # Jump past max_deadline_ts + grace.
    monkeypatch.setattr(hic, "_now", lambda: base + 15 * 60 + 1000)

    removed = hic.cleanup_expired()
    assert removed >= 1
    assert hic.get_pending_intervention("9090") is None
