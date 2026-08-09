"""Tests for fail-closed, durable DDP human approval commands."""

from __future__ import annotations

import sqlite3
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

    monkeypatch.setattr(ledger, "human_decision_for", unavailable)
    result = await runner._handle_ddp_approve_command(
        _make_event(f"/ddp-approve {request_id} operator reviewed")
    )

    assert "unavailable" in result.lower()
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert not runner._ddp_confirmation_store()


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
    assert token not in runner._ddp_confirmation_store()


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
    assert token not in runner._ddp_confirmation_store()


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
    assert token in runner._ddp_confirmation_store()


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
    runner._ddp_confirmation_store()[token]["created_at"] = 0
    monkeypatch.setattr("gateway.slash_commands.time.monotonic", lambda: runner._DDP_CONFIRM_TTL_SECONDS + 1)

    result = await runner._handle_ddp_approve_confirm_command(
        _make_event(f"/ddp-approve-confirm {token}", source)
    )

    assert "expired" in result.lower()
    assert token not in runner._ddp_confirmation_store()
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
