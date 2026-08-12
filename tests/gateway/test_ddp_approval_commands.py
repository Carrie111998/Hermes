"""Tests for fail-closed, durable DDP human approval commands."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from devflow_delegation.contract import parse_request
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import transition
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from tests.gateway.hang_guards import HANG_GUARD_S

# How long a deliberately parked ledger read stays parked before giving up.
# Not an assertion and not a speed claim: under the correct code the test
# releases it immediately, and it exists only so that a regression (the read
# back on the event loop, which cannot reach the release) FAILS the test
# rather than wedging the file until pytest-timeout kills the process.
_PARKED_READ_MAX_S = 5


def _make_source(user_id: str = "admin-1", *, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id="chat-1",
        user_name="operator",
        chat_type=chat_type,
    )


def _make_event(text: str, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(text=text, source=source or _make_source(), message_id="message-1")


def _make_request():
    return parse_request({
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "test:ddp-approval:v1",
        "source": {"agent": "tester", "kind": "explicit", "finding_id": "T-1"},
        "kind": "feature",
        "title": "DDP approval fixture",
        "problem_statement": "Exercise the authenticated DDP approval gateway.",
        "evidence": [{"kind": "manual", "ref": "test", "summary": "fixture"}],
        "target": {"repo": "hermes", "subsystem": "test"},
        "severity": "medium",
        "priority": "P2",
        "confidence": 0.9,
        "acceptance_criteria": ["n/a"],
        "safety_notes": [],
    })


def _make_runner(ledger: DelegationLedger, *, admin_ids=("admin-1",)):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="***",
                extra={"allow_admin_from": list(admin_ids)},
            )
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._ddp_ledger = ledger
    runner._ddp_bus = None
    return runner


def _seed_triaged(ledger: DelegationLedger) -> str:
    request = _make_request()
    ledger.insert_request(request)
    transition(ledger, None, request.request_id, "TRIAGED", actor="triage")
    return request.request_id


@pytest.mark.asyncio
async def test_ddp_stage_read_runs_off_event_loop_thread(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    event_loop_thread = threading.get_ident()
    observed_threads = []
    original = ledger.get_request

    def capture_thread(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "get_request", capture_thread)

    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "ddp-approve-confirm" in result
    assert observed_threads and all(thread_id != event_loop_thread for thread_id in observed_threads)


@pytest.mark.asyncio
async def test_ddp_complete_confirm_transaction_runs_off_event_loop_thread(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    event_loop_thread = threading.get_ident()
    observed_threads = []
    original = ledger.record_human_decision

    def capture_thread(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "record_human_decision", capture_thread)

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "approved" in result.lower()
    assert observed_threads and all(thread_id != event_loop_thread for thread_id in observed_threads)
    assert ledger.get_request(request_id)["state"] == "PLANNED"


@pytest.mark.asyncio
async def test_ddp_post_commit_telemetry_runs_off_event_loop_and_sees_commit(tmp_path):
    class InspectingBus:
        def __init__(self, ledger, request_id):
            self.ledger = ledger
            self.request_id = request_id
            self.thread_id = None
            self.commit_visible = False

        def emit(self, **_kwargs):
            self.thread_id = threading.get_ident()
            self.commit_visible = self.ledger.get_request(self.request_id)["state"] == "PLANNED"

    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    bus = InspectingBus(ledger, request_id)
    runner._ddp_bus = bus
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    event_loop_thread = threading.get_ident()

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "approved" in result.lower()
    assert bus.thread_id != event_loop_thread
    assert bus.commit_visible is True


@pytest.mark.asyncio
async def test_ddp_blocking_stage_read_keeps_event_loop_responsive(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    entered = threading.Event()
    release = threading.Event()
    # Set for exactly as long as the ledger read is parked. This is the
    # ordering primitive the test turns on: "the loop ran a callback WHILE the
    # read was in flight" is a question about state, not about elapsed time.
    read_in_progress = threading.Event()
    original = ledger.get_request

    def blocking_read(*args, **kwargs):
        read_in_progress.set()
        entered.set()
        try:
            # Bounded so that a REGRESSION fails the test instead of wedging
            # the whole file — the return value is not an assertion. Under the
            # correct code the release below arrives immediately.
            release.wait(timeout=_PARKED_READ_MAX_S)
        finally:
            read_in_progress.clear()
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "get_request", blocking_read)
    command = asyncio.create_task(runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    ))
    try:
        await asyncio.wait_for(
            asyncio.to_thread(entered.wait, HANG_GUARD_S), timeout=HANG_GUARD_S
        )
        # The read is parked right now. Schedule a loop callback and wait for
        # it: if the read were running ON the event loop, this callback could
        # not run until the read finished — and by then ``read_in_progress`` is
        # clear, which is what the assertion below catches. The bound is a hang
        # guard; the assertion after it is the test.
        ticked = asyncio.Event()
        asyncio.get_running_loop().call_soon(ticked.set)
        await asyncio.wait_for(ticked.wait(), timeout=HANG_GUARD_S)
        assert read_in_progress.is_set(), (
            "the event loop only ticked after the ledger read had finished — "
            "the blocking read is back on the event loop"
        )
    finally:
        release.set()

    assert "ddp-approve-confirm" in await command


@pytest.mark.asyncio
async def test_ddp_approve_is_fail_closed_when_admin_policy_is_disabled(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=())

    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "not enabled" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_secondary_profile_uses_its_own_admin_policy(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("default-admin",))
    runner.config.multiplex_profiles = True
    secondary_config = GatewayConfig(
        multiplex_profiles=True,
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="***",
                extra={"allow_admin_from": ["secondary-admin"]},
            )
        },
    )
    runner._profile_gateway_configs = {"secondary": secondary_config}
    secondary_source = _make_source("secondary-admin")
    secondary_source.profile = "secondary"
    default_source = _make_source("default-admin")
    default_source.profile = "secondary"

    denied = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", default_source)
    )
    staged = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", secondary_source)
    )
    token = staged.rsplit(" ", 1)[-1].strip("`.")
    applied = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", secondary_source)
    )

    assert "not enabled" in denied.lower()
    assert "approved" in applied.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for(request_id, "telegram:secondary-admin") is not None


@pytest.mark.asyncio
async def test_ddp_secondary_profile_cold_and_busy_dispatch_use_its_policy(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("default-admin",))
    runner.config.multiplex_profiles = True
    runner._profile_gateway_configs = {
        "secondary": GatewayConfig(
            multiplex_profiles=True,
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    token="***",
                    extra={"allow_admin_from": ["secondary-admin"]},
                )
            },
        )
    }
    default_source = _make_source("default-admin")
    default_source.profile = "secondary"
    secondary_source = _make_source("secondary-admin")
    secondary_source.profile = "secondary"

    cold_denied = await runner._handle_message(
        _make_event(f"/ddp-approve {request_id} operator reviewed", default_source)
    )
    runner._running_agents[build_session_key(secondary_source, profile="secondary")] = MagicMock()
    runner._running_agents_ts[build_session_key(secondary_source, profile="secondary")] = 0
    busy_prompt = await runner._handle_message(
        _make_event(f"/ddp-approve {request_id} operator reviewed", secondary_source)
    )

    assert "admin-only" in cold_denied.lower()
    assert "ddp-approve-confirm" in busy_prompt
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_unknown_secondary_profile_fails_closed(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("admin-1",))
    runner.config.multiplex_profiles = True
    runner._profile_gateway_configs = {}
    source = _make_source("admin-1")
    source.profile = "unknown-secondary"

    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )

    assert "not enabled" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_approve_requires_explicit_admin(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)

    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", _make_source("not-admin"))
    )

    assert "admin" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_group_scope_uses_its_own_explicit_admin_list(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("dm-admin",))
    runner.config.platforms[Platform.TELEGRAM].extra["group_allow_admin_from"] = ["group-admin"]
    dm_admin_in_group = _make_source("dm-admin", chat_type="group")
    group_admin = _make_source("group-admin", chat_type="group")

    denied = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", dm_admin_in_group)
    )
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", group_admin)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    approved = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", group_admin)
    )

    assert "not enabled" in denied.lower()
    assert "approved" in approved.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for(request_id, "telegram:group-admin") is not None


@pytest.mark.asyncio
async def test_ddp_approve_fails_closed_when_decision_lookup_is_unavailable(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("ledger busy")

    monkeypatch.setattr(ledger, "human_decision_for_request", unavailable)
    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "unavailable" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert not runner._ddp_decision_service().has_pending()


@pytest.mark.asyncio
async def test_ddp_approve_returns_one_time_confirmation_without_mutating(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)

    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "ddp-approve-confirm" in result
    assert request_id in result
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_approve_confirm_records_decision_and_advances_to_planned(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()

    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "approved" in result.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    decision = ledger.human_decision_for(request_id, "telegram:admin-1")
    assert decision is not None
    assert decision["decision"] == "approve"


@pytest.mark.asyncio
async def test_ddp_approve_confirm_replay_does_not_add_a_transition(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")

    await runner._handle_ddp_approve_confirm_command(_make_event(f"/ddp-approve-confirm {token}", source))
    replay = await runner._handle_ddp_approve_confirm_command(_make_event(f"/ddp-approve-confirm {token}", source))

    assert "already" in replay.lower()
    assert [item["to_state"] for item in ledger.transitions_for(request_id)] == ["TRIAGED", "PLANNED"]


@pytest.mark.asyncio
async def test_ddp_unconfirmed_token_is_invalid_after_runner_restart_and_can_be_restaged(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    source = _make_source()
    runner_a = _make_runner(ledger)
    runner_b = _make_runner(ledger)

    prompt_a = await runner_a._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    old_token = prompt_a.rsplit(" ", 1)[-1].strip("`.")
    stale_confirmation = await runner_b._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {old_token}", source)
    )

    assert "unknown" in stale_confirmation.lower() or "expired" in stale_confirmation.lower()
    assert ledger.human_decision_for_request(request_id) is None
    assert ledger.get_request(request_id)["state"] == "TRIAGED"

    prompt_b = await runner_b._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    new_token = prompt_b.rsplit(" ", 1)[-1].strip("`.")
    confirmed = await runner_b._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {new_token}", source)
    )

    assert new_token != old_token
    assert "approved" in confirmed.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for_request(request_id)["decision"] == "approve"


@pytest.mark.asyncio
async def test_ddp_confirmation_failure_preserves_a_retryable_token(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")

    original = ledger.record_human_decision
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("temporary ledger failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "record_human_decision", fail_once)

    failed = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )
    retried = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "not applied" in failed.lower()
    assert "approved" in retried.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert attempts == 2


@pytest.mark.asyncio
async def test_ddp_confirmation_commits_before_telemetry_failure(tmp_path):
    class RaisingBus:
        def emit(self, **_kwargs):
            raise RuntimeError("telemetry sink down")

    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    runner._ddp_bus = RaisingBus()
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "approved" in result.lower()
    assert "telemetry" in result.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for(request_id, "telegram:admin-1") is not None
    assert runner._ddp_decision_service().pending(token) is None


@pytest.mark.asyncio
async def test_ddp_confirm_uses_durable_token_guard_for_concurrent_delivery(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")

    original = ledger.record_human_decision
    calls = 0

    def claim_then_report_replay(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert original(*args, **kwargs)
            return False
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "record_human_decision", claim_then_report_replay)

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "already decided" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert len(ledger.transitions_for(request_id)) == 1
    assert runner._ddp_decision_service().pending(token) is None


@pytest.mark.asyncio
async def test_competing_admin_approve_and_decline_has_one_deterministic_winner(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("admin-1", "admin-2"))
    admin_1 = _make_source("admin-1")
    admin_2 = _make_source("admin-2")

    approve_prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} reviewed", admin_1)
    )
    decline_prompt = await runner._handle_ddp_decline_command(
        _make_event(f"/ddp-decline {request_id} not ready", admin_2)
    )
    approve_token = approve_prompt.rsplit(" ", 1)[-1].strip("`.")
    decline_token = decline_prompt.rsplit(" ", 1)[-1].strip("`.")

    approved = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {approve_token}", admin_1)
    )
    declined = await runner._handle_ddp_decline_confirm_command(
        _make_event(f"/ddp-decline-confirm {decline_token}", admin_2)
    )

    assert "approved" in approved.lower()
    assert "already decided" in declined.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for_request(request_id)["decision"] == "approve"
    assert [item["to_state"] for item in ledger.transitions_for(request_id)] == [
        "TRIAGED", "PLANNED"
    ]


@pytest.mark.asyncio
async def test_ddp_decline_confirm_records_decision_and_transitions_to_declined(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()

    prompt = await runner._handle_ddp_decline_command(
        _make_event(f"/ddp-decline {request_id} missing acceptance evidence", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    result = await runner._handle_ddp_decline_confirm_command(
        _make_event(f"/ddp-decline-confirm {token}", source)
    )

    assert "declined" in result.lower()
    assert ledger.get_request(request_id)["state"] == "DECLINED"
    decision = ledger.human_decision_for(request_id, "telegram:admin-1")
    assert decision is not None
    assert decision["decision"] == "decline"


@pytest.mark.asyncio
async def test_ddp_confirmation_is_bound_to_initiating_actor(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=("admin-1", "admin-2"))
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", _make_source("admin-1"))
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", _make_source("admin-2"))
    )

    assert "same admin" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_confirmation_rejects_missing_unknown_and_wrong_decision_tokens(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()

    missing = await runner._handle_ddp_approve_confirm_command(
        _make_event("/ddp-approve-confirm", source)
    )
    unknown = await runner._handle_ddp_approve_confirm_command(
        _make_event("/ddp-approve-confirm unknown-token", source)
    )
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    wrong_decision = await runner._handle_ddp_decline_confirm_command(
        _make_event(f"/ddp-decline-confirm {token}", source)
    )

    assert "usage" in missing.lower()
    assert "unknown" in unknown.lower()
    assert "different" in wrong_decision.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert ledger.human_decision_for(request_id, "telegram:admin-1") is None
    assert runner._ddp_decision_service().pending(token) is not None


@pytest.mark.asyncio
async def test_ddp_confirmation_expiry_removes_pending_token_without_mutating(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed", source)
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    monkeypatch.setattr(
        runner._ddp_decision_service(),
        "_monotonic",
        lambda: runner._ddp_decision_service().pending(token).expires_at_monotonic + 1,
    )

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "expired" in result.lower()
    assert runner._ddp_decision_service().pending(token) is None
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert ledger.human_decision_for(request_id, "telegram:admin-1") is None


@pytest.mark.asyncio
async def test_ddp_rejects_unknown_and_non_triaged_requests(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    runner = _make_runner(ledger)

    unknown = await runner._handle_ddp_approve_command(
        _make_event("/ddp-approve dwr_unknown operator reviewed")
    )
    assert "unknown" in unknown.lower()

    request_id = _seed_triaged(ledger)
    assert ledger.record_human_decision(
        request_id, "fixture", "approve", "fixture setup", "token-non-triaged"
    )
    transition(ledger, None, request_id, "PLANNED", actor="fixture")
    non_triaged = await runner._handle_ddp_decline_command(
        _make_event(f"/ddp-decline {request_id} no longer relevant")
    )
    assert "triaged" in non_triaged.lower()


@pytest.mark.asyncio
async def test_ddp_requires_request_id_and_evidence(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    runner = _make_runner(ledger)

    assert "usage" in (await runner._handle_ddp_approve_command(_make_event("/ddp-approve"))).lower()
    assert "evidence" in (await runner._handle_ddp_decline_command(_make_event("/ddp-decline dwr_any"))).lower()


@pytest.mark.asyncio
async def test_cold_dispatch_routes_ddp_commands(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)

    result = await runner._handle_message(
        _make_event(f"/ddp_approve {request_id} operator reviewed")
    )

    assert "ddp-approve-confirm" in result
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_busy_dispatch_routes_ddp_commands(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)
    source = _make_source()
    runner._running_agents[build_session_key(source)] = MagicMock()
    runner._running_agents_ts[build_session_key(source)] = 0

    result = await runner._handle_message(
        _make_event(f"/ddp-decline {request_id} operator reviewed", source)
    )

    assert "ddp-decline-confirm" in result
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_cold_dispatch_remains_fail_closed_when_slash_access_is_disabled(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger, admin_ids=())

    result = await runner._handle_message(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "not enabled" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


@pytest.mark.asyncio
async def test_ddp_confirmation_does_not_construct_or_invoke_an_executor(tmp_path, monkeypatch):
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _seed_triaged(ledger)
    runner = _make_runner(ledger)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("DDP approval must not construct an executor")

    monkeypatch.setattr("devflow_delegation.executor.run_executor_tick", forbidden)
    prompt = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )
    token = prompt.rsplit(" ", 1)[-1].strip("`.")
    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}")
    )

    assert "approved" in result.lower()
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert [item["to_state"] for item in ledger.transitions_for(request_id)] == ["TRIAGED", "PLANNED"]
    assert "MERGED" not in [item["to_state"] for item in ledger.transitions_for(request_id)]
    assert "DEPLOYED" not in [item["to_state"] for item in ledger.transitions_for(request_id)]


@pytest.mark.asyncio
async def test_ddp_lazy_emitter_uses_fixture_control_plane_paths(tmp_path, monkeypatch):
    """A production runner without injected test doubles resolves the canonical
    DDP emitter lazily, entirely under the configured Hermes root."""
    from events import paths
    from gateway.run import GatewayRunner

    runner = _make_runner(DelegationLedger(tmp_path / "injected-ledger.db"))
    del runner._ddp_ledger
    del runner._ddp_bus
    monkeypatch.setattr(paths, "get_default_hermes_root", lambda: tmp_path)

    ledger, bus = runner._ddp_ledger_and_bus()
    try:
        assert ledger.db_path == tmp_path / "devflow" / "delegation_ledger.db"
        assert bus.db_path == tmp_path / "events" / "event_bus.db"
        assert not (tmp_path / "devflow" / ".autonomy_enabled").exists()
    finally:
        ledger.close()
        bus.close()


@pytest.mark.asyncio
async def test_devflow_login_mints_redeemable_code_for_admin(tmp_path):
    from gateway.devflow_auth import DevflowLoginGrantStore

    ledger = DelegationLedger(tmp_path / "ledger.db")
    try:
        runner = _make_runner(ledger)
        store = DevflowLoginGrantStore(
            secret=b"pepper",
            token_factory=iter(("login-grant", "opaque-subject")).__next__,
        )
        runner.adapters[Platform.API_SERVER] = SimpleNamespace(
            _get_devflow_grant_store=lambda: store
        )

        result = await runner._handle_devflow_login_command(_make_event("/devflow-login"))

        # The one-time code is delivered with paste instructions.
        assert "login-grant" in result
        assert "localhost:3040/auth" in result
        # It is redeemable via the same store the browser redeem path uses.
        redeemed = store.redeem(grant="login-grant", audience="devflow-mission-control")
        assert redeemed.subject == "opaque-subject"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_devflow_login_denies_non_admin_caller(tmp_path):
    from gateway.devflow_auth import DevflowLoginGrantStore

    ledger = DelegationLedger(tmp_path / "ledger.db")
    try:
        runner = _make_runner(ledger, admin_ids=("someone-else",))
        runner.adapters[Platform.API_SERVER] = SimpleNamespace(
            _get_devflow_grant_store=lambda: DevflowLoginGrantStore(
                secret=b"pepper", token_factory=iter(("x", "y")).__next__
            )
        )

        result = await runner._handle_devflow_login_command(_make_event("/devflow-login"))

        assert "not enabled for this caller" in result
    finally:
        ledger.close()
