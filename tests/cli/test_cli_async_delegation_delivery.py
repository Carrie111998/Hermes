"""Regression coverage for CLI async-delegation completion ownership."""

import queue
import threading

import pytest

import cli as cli_module
import tools.async_delegation as ad
from cli import (
    HermesCLI,
    _INTERNAL_DELIVERY_RETRY_MIN_SECONDS,
    _InternalPendingTurn,
)


@pytest.fixture(autouse=True)
def _reset_async_delivery_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


class _FakeTimer:
    instances = []
    fail_start = False

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        if self.__class__.fail_start:
            raise RuntimeError("timer start failed")
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


def _retry_cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "owner"
    cli._session_db = None
    cli._pending_input = queue.Queue()
    cli._should_exit = False
    cli._internal_delivery_retry_lock = threading.Lock()
    cli._internal_delivery_retry_timers = {}
    return cli


def _persist_terminal_completion(home, delegation_id):
    # Durable delivery captures the profile directory's filesystem identity;
    # model a real initialized profile instead of relying on SQLite to create
    # only its parent as an incidental side effect.
    home.mkdir(parents=True, exist_ok=True)
    record = {
        "delegation_id": delegation_id,
        "session_key": "owner",
        "origin_ui_session_id": "",
        "parent_session_id": "owner",
        "dispatched_at": 1.0,
    }
    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "owner",
        "origin_ui_session_id": "",
        "parent_session_id": "owner",
        "status": "completed",
        "summary": "done",
        "completed_at": 2.0,
        "runtime_effect": None,
    }
    with ad._delivery_home_scope(home):
        ad._persist_dispatch(record)
        ad._persist_completion(
            event,
            {"status": "completed", "summary": "done"},
        )


def _restore_turn(home, delegation_id):
    _persist_terminal_completion(home, delegation_id)
    restored = queue.Queue()
    assert (
        ad.restore_undelivered_completions(
            restored,
            hermes_home=home,
        )
        == 1
    )
    event = restored.get_nowait()
    assert event["delegation_id"] == delegation_id
    return _InternalPendingTurn(
        text=f"completion {delegation_id}",
        runtime_effect=None,
        delivery_event=event,
    )


@pytest.fixture
def fake_timers(monkeypatch):
    _FakeTimer.instances = []
    _FakeTimer.fail_start = False
    monkeypatch.setattr(cli_module.threading, "Timer", _FakeTimer)
    return _FakeTimer


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            calls.append((session_key, owns_event(event)))
            return [(event, "completion payload")]

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    pending = cli._pending_input.get_nowait()
    assert isinstance(pending, _InternalPendingTurn)
    assert pending.text == "completion payload"
    assert pending.runtime_effect is None
    assert pending.delivery_event == event
    # Claim/ACK are deliberately deferred until process_loop starts and the
    # resulting turn returns through chat's durable boundary.
    assert claimed == []
    assert completed == []


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )


def test_cli_malformed_runtime_effect_drops_through_event_store(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-invalid",
        "session_key": "visible-session",
        "runtime_effect": {"schema": "forged"},
    }

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            assert session_key == "visible-session"
            assert owns_event(event)
            return [(event, "must not enter the model")]

    dropped = []
    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.drop_event_delivery",
        lambda evt, claim: dropped.append((evt, claim)) or True,
    )

    cli._drain_process_notifications("cli-idle")

    assert dropped == [(event, "claim-token")]
    assert cli._pending_input.empty()


def test_cli_foreign_fresh_claim_retains_delayed_carrier(
    tmp_path,
    fake_timers,
):
    turn = _restore_turn(
        tmp_path / "profile",
        "deleg-cli-foreign-claim",
    )
    foreign_claim = ad.claim_event_delivery(
        turn.delivery_event,
        "foreign-consumer",
    )
    assert foreign_claim
    cli = _retry_cli()

    # The CLI has already dequeued the turn.  Failing to acquire its durable
    # claim must retain a delayed carrier instead of losing it until restart.
    assert (
        cli._claim_internal_delivery_turn(turn, "cli-turn")
        is None
    )
    assert len(fake_timers.instances) == 1
    timer = fake_timers.instances[0]
    assert timer.delay > 299.0
    assert cli._pending_input.empty()

    assert ad.release_event_delivery(
        turn.delivery_event,
        foreign_claim,
    )
    timer.fire()
    assert cli._pending_input.get_nowait() is turn
    assert cli._internal_delivery_retry_timers == {}


def test_cli_unpersisted_turn_release_regains_carrier(
    tmp_path,
    fake_timers,
):
    turn = _restore_turn(
        tmp_path / "profile",
        "deleg-cli-unpersisted-turn",
    )
    claim = ad.claim_event_delivery(
        turn.delivery_event,
        "cli-turn",
    )
    assert claim
    stopped = []
    cli = _retry_cli()

    assert cli._settle_internal_delivery_turn(
        turn,
        claim,
        succeeded=False,
        stop_renewal=lambda: stopped.append(True),
    )
    assert stopped == [True]
    assert len(fake_timers.instances) == 1
    timer = fake_timers.instances[0]
    assert timer.delay == pytest.approx(
        _INTERNAL_DELIVERY_RETRY_MIN_SECONDS
    )

    timer.fire()
    assert cli._pending_input.get_nowait() is turn
    assert cli._internal_delivery_retry_timers == {}


def test_cli_retry_callback_does_not_requeue_terminal_row(
    tmp_path,
    fake_timers,
):
    turn = _restore_turn(
        tmp_path / "profile",
        "deleg-cli-terminal-retry",
    )
    cli = _retry_cli()
    assert cli._schedule_internal_delivery_retry(turn)
    timer = fake_timers.instances[0]

    claim = ad.claim_event_delivery(
        turn.delivery_event,
        "terminal-consumer",
    )
    assert claim
    assert ad.complete_event_delivery(
        turn.delivery_event,
        claim,
    )
    timer.fire()

    assert cli._pending_input.empty()
    assert cli._internal_delivery_retry_timers == {}


def test_cli_retry_callback_rechecks_session_ownership(
    tmp_path,
    fake_timers,
):
    turn = _restore_turn(
        tmp_path / "profile",
        "deleg-cli-old-session",
    )
    cli = _retry_cli()
    assert cli._schedule_internal_delivery_retry(turn)
    timer = fake_timers.instances[0]

    cli.session_id = "new-session"
    timer.fire()

    assert cli._pending_input.empty()
    assert cli._internal_delivery_retry_timers == {}


def test_cli_retry_identity_separates_same_id_in_two_stores(
    tmp_path,
    fake_timers,
):
    delegation_id = "deleg-cli-same-id"
    alpha_turn = _restore_turn(
        tmp_path / "alpha",
        delegation_id,
    )
    beta_turn = _restore_turn(
        tmp_path / "beta",
        delegation_id,
    )
    cli = _retry_cli()

    assert cli._schedule_internal_delivery_retry(alpha_turn)
    assert cli._schedule_internal_delivery_retry(beta_turn)
    assert len(cli._internal_delivery_retry_timers) == 2
    assert len(fake_timers.instances) == 2


def test_cli_failed_replacement_timer_preserves_existing_carrier(
    tmp_path,
    fake_timers,
):
    turn = _restore_turn(
        tmp_path / "profile",
        "deleg-cli-retry-start-failure",
    )
    foreign_claim = ad.claim_event_delivery(
        turn.delivery_event,
        "foreign-consumer",
    )
    assert foreign_claim
    cli = _retry_cli()
    assert cli._schedule_internal_delivery_retry(turn)
    original = fake_timers.instances[0]

    assert ad.release_event_delivery(
        turn.delivery_event,
        foreign_claim,
    )
    fake_timers.fail_start = True
    assert not cli._schedule_internal_delivery_retry(turn)
    identity = cli._internal_delivery_retry_identity(turn)
    assert cli._internal_delivery_retry_timers[identity] is original
    assert original.cancelled is False

    original.fire()
    assert cli._pending_input.get_nowait() is turn
    assert cli._internal_delivery_retry_timers == {}
