"""Unit tests for the kill-and-report enforcer and the report formatter."""

from gateway.fleet_safety.deadloop_guard import GuardOutcome, Trip, TripReason
from gateway.fleet_safety.enforcer import GuardEnforcer
from gateway.fleet_safety.report import format_kill_report


def _trip(**kw):
    base = dict(
        session_id="20260725_001655_7eff2a",
        reason=TripReason.WALL_CLOCK,
        detail="turn ran 780.0 min (cap 60 min)",
        estimated_tokens=288_000_000,
        estimated_calls=1800,
        runtime_seconds=780 * 60,
        provider="xai",
        model="grok-4.5",
        effort="max",
        last_state="frozen:terminal",
    )
    base.update(kw)
    return Trip(**base)


class _FakeActions:
    def __init__(self, interrupt=True, lease=True, notify=True, raise_on=None):
        self._interrupt = interrupt
        self._lease = lease
        self._notify = notify
        self._raise_on = raise_on or set()
        self.calls = []

    def interrupt(self, session_id, reason):
        self.calls.append(("interrupt", session_id, reason))
        if "interrupt" in self._raise_on:
            raise RuntimeError("boom")
        return self._interrupt

    def release_lease(self, session_id):
        self.calls.append(("release_lease", session_id))
        if "release_lease" in self._raise_on:
            raise RuntimeError("boom")
        return self._lease

    def notify(self, text):
        self.calls.append(("notify", text))
        if "notify" in self._raise_on:
            raise RuntimeError("boom")
        return self._notify


def test_enforce_requests_stop_then_notifies_without_releasing_lease():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    kinds = [c[0] for c in actions.calls]
    assert kinds == ["interrupt", "notify"]
    assert result.stop_requested is True
    assert result.interrupted is True
    assert result.lease_released is False
    assert result.notified is True
    assert result.killed is False
    assert "Interrupt request accepted: yes" in result.report
    assert "Lease: retained until generation-safe gateway unwind" in result.report
    assert not result.errors


def test_enforce_reports_even_when_interrupt_fails():
    actions = _FakeActions(raise_on={"interrupt"})
    result = GuardEnforcer(actions).enforce(_trip())
    # Interrupt raised, so the lease stays held; notification still runs.
    assert any(c[0] == "notify" for c in actions.calls)
    assert not any(c[0] == "release_lease" for c in actions.calls)
    assert result.notified is True
    assert any("interrupt failed" in e for e in result.errors)


def test_enforce_never_raises_on_notify_failure():
    actions = _FakeActions(raise_on={"notify"})
    result = GuardEnforcer(actions).enforce(_trip())  # must not raise
    assert result.interrupted is True
    assert result.notified is False
    assert any("notify failed" in e for e in result.errors)


def test_unconfirmed_interrupt_cannot_release_lease_or_claim_kill():
    actions = _FakeActions(interrupt=False, lease=True)
    result = GuardEnforcer(actions).enforce(_trip())
    assert result.interrupted is False
    assert result.lease_released is False
    assert result.killed is False
    assert not any(c[0] == "release_lease" for c in actions.calls)


def test_notify_receives_the_formatted_report():
    actions = _FakeActions()
    result = GuardEnforcer(actions).enforce(_trip())
    notify_text = [c[1] for c in actions.calls if c[0] == "notify"][0]
    assert notify_text == result.report
    assert "20260725_001655_7eff2a" not in notify_text
    assert "Safety stop requested" in notify_text


# -- report formatter ---------------------------------------------------------


def test_report_contains_truthful_request_receipt_without_raw_state():
    r = format_kill_report(_trip())
    assert "Safety stop requested" in r
    assert "20260725_001655_7eff2a" not in r
    assert "wall_clock_runtime_exceeded" in r
    assert "Model calls: 1800" in r
    assert "Runtime seconds: 46800.0" in r
    assert "xai" in r and "grok-4.5" in r and "effort=max" in r
    assert "Usage provenance: unknown" in r
    assert "frozen:terminal" not in r
    assert "lease released" not in r.lower()


def test_report_keeps_usage_dimensions_and_cost_status_separate():
    report = format_kill_report(
        _trip(
            usage_quality="measured",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=300,
            cache_write_tokens=40,
            reasoning_tokens=50,
            cost=1.25,
            cost_status="estimated",
            cost_source="test",
        )
    )
    assert "Input tokens: 1000" in report
    assert "Output tokens: 200" in report
    assert "Cache read tokens: 300" in report
    assert "Cache write tokens: 40" in report
    assert "Reasoning tokens: 50" in report
    assert "Cost: 1.250000 (estimated; test)" in report


def test_report_omits_last_state_when_absent():
    r = format_kill_report(_trip(last_state=None))
    assert "last state" not in r


def test_continuation_notice_only_notifies():
    actions = _FakeActions()
    trip = _trip(
        outcome=GuardOutcome.CONTINUATION_NOTICE,
        is_hard_stop=False,
        notice_text="Extension checkpoint",
        extension_grant_size=100,
        extension_revision=1,
    )

    result = GuardEnforcer(actions).enforce(trip)

    assert [call[0] for call in actions.calls] == ["notify"]
    assert result.stop_requested is False
    assert result.interrupted is False
    assert result.lease_released is False
    assert result.notified is True
    assert "Extension checkpoint" in result.report
    assert "Continuing by default" in result.report
