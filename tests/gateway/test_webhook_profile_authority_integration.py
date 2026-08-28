"""Adversarial integration tests for durable webhook profile authority."""

import asyncio
import base64
import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import CIMultiDict

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    WebhookTargetDeliveryDisposition,
    _PROFILE_AUTHORITY_INCARNATION_FILENAME,
)
from gateway.platforms.webhook_auth import WebhookLocalBypassReceipt
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_ledger import (
    OperationState,
    TargetState,
    WebhookLedgerError,
    WebhookLedgerTransitionError,
)


@pytest.fixture(autouse=True)
def _isolated_ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _adapter() -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {},
                "idempotency_max_entries": 8,
            },
        )
    )


def _grant_snapshot(adapter: WebhookAdapter, envelope: WebhookEnvelope) -> dict:
    try:
        profile_generation = adapter._current_profile_authority_generation(
            envelope.authority_profile,
            route_name=envelope.route.name,
        )
    except Exception:
        # Removed-profile tests construct the carrier after modeling removal;
        # this stands in for the non-empty incarnation captured beforehand.
        profile_generation = "test-prior-profile-incarnation"
    return {
        "v": 1,
        "toolsets": [],
        "profile_generation": profile_generation,
    }


def _stage_delivery(
    adapter: WebhookAdapter,
    *,
    profile: str,
    target: dict,
    suffix: str,
    relinquish: bool = False,
):
    raw_body = f'{{"delivery":"{suffix}"}}'.encode()
    route = WebhookRouteConfig.bind(
        f"profile-{suffix}",
        {"provider": "generic", "profile": profile},
        headers={},
        request_profile=profile,
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id=f"trace-{suffix}",
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    source = adapter._source_for_envelope(envelope)
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "direct",
            "text": f"durable output {suffix}",
            "payload": {"delivery": suffix},
            "message_id": envelope.delivery_id,
            "source": source.to_dict(),
        },
        target_snapshot=target,
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    assert adapter._operation_ledger.mark_running(prepared)
    staged = adapter._stage_exact_delivery(
        prepared,
        f"durable output {suffix}",
        {"v": 1, "kind": "direct"},
    )
    assert staged.state is OperationState.DELIVERY_READY
    if relinquish:
        assert adapter._operation_ledger.relinquish_recovery_claim(staged)
    return staged


def test_root_ledger_isolates_same_provider_delivery_id_by_physical_profile(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "shared-root"
    alpha_home = root / "profiles" / "alpha"
    beta_home = root / "profiles" / "beta"
    alpha_home.mkdir(parents=True)
    beta_home.mkdir(parents=True)
    message_id = "same-provider-delivery"
    timestamp = str(int(time.time()))

    def build(profile: str, secret: str, body: bytes):
        monkeypatch.setenv(
            "HERMES_HOME",
            str(root / "profiles" / profile),
        )
        adapter = WebhookAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "127.0.0.1",
                    "port": 0,
                    "routes": {
                        "shared": {
                            "provider": "standard_webhooks",
                            "secret": secret,
                        }
                    },
                },
            )
        )
        adapter._bind_route_authentication_authorities(adapter._routes)
        signed = f"{message_id}.{timestamp}.".encode() + body
        signature = (
            "v1,"
            + base64.b64encode(
                hmac.new(secret.encode(), signed, hashlib.sha256).digest()
            ).decode()
        )
        headers = CIMultiDict({
            "Content-Type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        })
        bound = WebhookRouteConfig.bind(
            "shared",
            adapter._routes["shared"],
            headers=headers,
            request_profile="default",
        )
        receipt = adapter._verify_signature_receipt(
            SimpleNamespace(
                headers=headers,
                match_info={"route_name": "shared"},
            ),
            body,
            secret,
            bound,
        )
        assert receipt is not None
        envelope = WebhookEnvelope.from_receipt(
            receipt,
            raw_body=body,
            media_type="application/json",
            authority_profile=str(
                adapter._authenticated_route_bundles["shared"].authority[0]
            ),
        )
        return adapter, envelope

    alpha, alpha_envelope = build("alpha", "alpha-secret", b'{"owner":"alpha"}')
    alpha_admission = alpha._operation_ledger.admit(alpha_envelope)
    assert alpha_admission.authority is not None
    beta, beta_envelope = build("beta", "beta-secret", b'{"owner":"beta"}')
    beta_admission = beta._operation_ledger.admit(beta_envelope)
    assert beta_admission.authority is not None

    assert alpha_envelope.replay_id == beta_envelope.replay_id
    assert alpha_envelope.authority_profile == "alpha"
    assert beta_envelope.authority_profile == "beta"
    assert (
        alpha_admission.authority.operation_id != beta_admission.authority.operation_id
    )
    assert beta._operation_ledger.count() == 2
    assert alpha._operation_ledger.settle_no_effect(
        alpha_admission.authority,
        "alpha complete",
    )
    assert beta._operation_ledger.settle_no_effect(
        beta_admission.authority,
        "beta complete",
    )
    assert alpha._operation_ledger.admit(alpha_envelope).authority.state is (
        OperationState.SETTLED
    )
    assert beta._operation_ledger.admit(beta_envelope).authority.state is (
        OperationState.SETTLED
    )


@pytest.mark.asyncio
async def test_single_profile_recovery_switchback_claims_only_exact_physical_profile(
    tmp_path,
    monkeypatch,
):
    """A shared root never lets the active named profile borrow peer work."""

    root = tmp_path / "shared-root"
    alpha_home = root / "profiles" / "alpha"
    beta_home = root / "profiles" / "beta"
    alpha_home.mkdir(parents=True)
    beta_home.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(alpha_home))
    alpha_predecessor = _adapter()
    staged = _stage_delivery(
        alpha_predecessor,
        profile="alpha",
        target={"v": 1, "kind": "log", "profile": "alpha"},
        suffix="alpha-switchback",
        relinquish=True,
    )
    initial = alpha_predecessor._operation_ledger.lookup_session(staged.session_key)
    assert initial is not None
    assert initial.state is OperationState.DELIVERY_READY
    initial_owner = initial.owner_instance
    initial_generation = initial.generation

    monkeypatch.setenv("HERMES_HOME", str(beta_home))
    beta = _adapter()
    assert beta._operation_ledger.db_path == root / "state.db"
    beta_invoke = AsyncMock(wraps=beta._invoke_staged_target)
    monkeypatch.setattr(beta, "_invoke_staged_target", beta_invoke)

    assert await beta.recover_pending_operations(trigger="beta-active") == 0
    await asyncio.sleep(0)

    untouched = beta._operation_ledger.lookup_session(staged.session_key)
    assert untouched is not None
    assert untouched.state is OperationState.DELIVERY_READY
    assert untouched.owner_instance == initial_owner
    assert untouched.generation == initial_generation
    assert tuple(beta._background_tasks) == ()
    beta_invoke.assert_not_awaited()

    monkeypatch.setenv("HERMES_HOME", str(alpha_home))
    alpha = _adapter()
    assert alpha._operation_ledger.db_path == beta._operation_ledger.db_path
    alpha_invoke = AsyncMock(wraps=alpha._invoke_staged_target)
    monkeypatch.setattr(alpha, "_invoke_staged_target", alpha_invoke)

    assert await alpha.recover_pending_operations(trigger="alpha-restored") == 1
    pending = tuple(alpha._background_tasks)
    if pending:
        await asyncio.gather(*pending)
    await asyncio.sleep(0)

    settled = alpha._operation_ledger.lookup_session(staged.session_key)
    assert settled is not None
    assert settled.state is OperationState.SETTLED
    assert settled.target_state is TargetState.SUPPRESSED
    assert settled.generation == initial_generation + 1
    alpha_invoke.assert_awaited_once()

    assert await alpha.recover_pending_operations(trigger="alpha-repeat") == 0
    await asyncio.sleep(0)
    alpha_invoke.assert_awaited_once()
    repeated = alpha._operation_ledger.lookup_session(staged.session_key)
    assert repeated is not None
    assert repeated.state is OperationState.SETTLED
    assert repeated.generation == initial_generation + 1


@pytest.mark.asyncio
async def test_default_target_does_not_borrow_active_named_profile_adapter():
    """Persisted ``default`` authority must select its exact adapter registry."""

    from gateway.run import GatewayRunner

    adapter = _adapter()
    named_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="wrong"))
    )
    default_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="right"))
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._active_profile_name = lambda: "dev"
    runner.adapters = {
        Platform.WEBHOOK: adapter,
        Platform.TELEGRAM: named_adapter,
    }
    runner._profile_adapters = {
        "default": {Platform.TELEGRAM: default_adapter},
    }
    adapter.gateway_runner = runner

    staged = _stage_delivery(
        adapter,
        profile="default",
        target={
            "v": 1,
            "kind": "platform",
            "profile": "default",
            "platform": "telegram",
            "chat_id": "default-chat",
        },
        suffix="default-egress",
    )

    result = await adapter._invoke_staged_target(staged)

    assert result.success is True
    default_adapter.send.assert_awaited_once_with(
        "default-chat",
        "durable output default-egress",
        metadata=None,
    )
    named_adapter.send.assert_not_awaited()
    restored = adapter._operation_ledger.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.SETTLED
    assert restored.target_state is TargetState.CONFIRMED


@pytest.mark.asyncio
async def test_failed_post_effect_settlement_invokes_target_once_and_fences_intake(
    monkeypatch,
):
    """A failed confirmed-write becomes one durable unknown, never a retry."""

    from gateway.run import GatewayRunner

    adapter = _adapter()
    target_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="sent-once"))
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._active_profile_name = lambda: "default"
    runner.adapters = {
        Platform.WEBHOOK: adapter,
        Platform.TELEGRAM: target_adapter,
    }
    runner._profile_adapters = {}
    adapter.gateway_runner = runner
    staged = _stage_delivery(
        adapter,
        profile="default",
        target={
            "v": 1,
            "kind": "platform",
            "profile": "default",
            "platform": "telegram",
            "chat_id": "target-chat",
        },
        suffix="settlement-write-failure",
    )

    real_settle = adapter._operation_ledger.settle_target
    calls = {"count": 0}

    def fail_first_settlement(attempt, settlement):
        calls["count"] += 1
        if calls["count"] == 1:
            raise WebhookLedgerError("injected post-effect settlement failure")
        return real_settle(attempt, settlement)

    monkeypatch.setattr(
        adapter._operation_ledger,
        "settle_target",
        fail_first_settlement,
    )
    result = await adapter._invoke_staged_target(staged)

    assert result.disposition is WebhookTargetDeliveryDisposition.INDETERMINATE
    target_adapter.send.assert_awaited_once()
    assert calls["count"] == 2
    assert adapter._accepting_webhooks is False
    restored = adapter._operation_ledger.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE
    assert restored.target_state is TargetState.INDETERMINATE


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["platform", "github_comment"])
async def test_removed_profile_recovery_never_borrows_live_authority(
    carrier, monkeypatch
):
    """A removed durable profile is rejected before any recovered effect."""

    from agent import secret_scope
    from gateway.run import GatewayRunner
    from gateway.platforms import webhook_route_authority

    adapter = _adapter()
    primary_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="primary"))
    )
    retired_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="retired"))
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._active_profile_name = lambda: "dev"
    runner.adapters = {
        Platform.WEBHOOK: adapter,
        Platform.TELEGRAM: primary_adapter,
    }
    # Model a removal race where the live registry has not yet discarded its
    # old adapter. The real profile resolver must remain the final authority.
    runner._profile_adapters = {
        "retired": {Platform.TELEGRAM: retired_adapter},
    }
    adapter.gateway_runner = runner

    token_lookup = MagicMock(
        side_effect=AssertionError("removed profile borrowed a live token")
    )
    monkeypatch.setattr(secret_scope, "get_secret", token_lookup)
    executable_lookup = MagicMock(
        side_effect=AssertionError("removed profile resolved an external carrier")
    )
    monkeypatch.setattr(
        webhook_route_authority.shutil,
        "which",
        executable_lookup,
    )
    external_carrier = MagicMock(
        side_effect=AssertionError("removed profile invoked an external carrier")
    )
    monkeypatch.setattr(adapter, "_run_github_comment", external_carrier)

    if carrier == "platform":
        target = {
            "v": 1,
            "kind": "platform",
            "profile": "retired",
            "platform": "telegram",
            "chat_id": "retired-chat",
        }
    else:
        target = {
            "v": 1,
            "kind": "github_comment",
            "profile": "retired",
            "repo": "owner/repo",
            "pr_number": 7,
        }
    staged = _stage_delivery(
        adapter,
        profile="retired",
        target=target,
        suffix=f"removed-{carrier}",
        relinquish=True,
    )

    assert await adapter.recover_pending_operations(trigger="profile-removal") == 0
    pending = tuple(adapter._background_tasks)
    assert pending == ()

    restored = adapter._operation_ledger.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.DELIVERY_READY
    assert restored.owner_instance != adapter._operation_ledger.instance_id
    primary_adapter.send.assert_not_awaited()
    retired_adapter.send.assert_not_awaited()
    token_lookup.assert_not_called()
    executable_lookup.assert_not_called()
    external_carrier.assert_not_called()


@pytest.mark.asyncio
async def test_existing_but_unserved_profile_cannot_recover_agent_work(
    tmp_path, monkeypatch
):
    """Allowlist revocation wins even while the profile directory remains."""

    from gateway.run import GatewayRunner

    adapter = _adapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        multiplex_profile_allowlist=["dev"],
    )
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner._profile_adapters = {}
    adapter.gateway_runner = runner
    adapter.handle_message = AsyncMock(
        side_effect=AssertionError("revoked profile reached agent dispatch")
    )

    ops_home = tmp_path / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name == "ops",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: ops_home if name == "ops" else tmp_path / name,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda **_kwargs: [("dev", tmp_path / "profiles" / "dev")],
    )

    raw_body = b'{"event":"revoked"}'
    route = WebhookRouteConfig.bind(
        "revoked-agent",
        {"provider": "generic", "profile": "ops"},
        headers={},
        request_profile="ops",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="revoked-agent-trace",
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    source = adapter._source_for_envelope(envelope)
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "agent",
            "text": "do not run",
            "payload": {"event": "revoked"},
            "message_id": envelope.delivery_id,
            "source": source.to_dict(),
        },
        target_snapshot={"v": 1, "kind": "log", "profile": "ops"},
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    assert adapter._operation_ledger.relinquish_recovery_claim(prepared)

    assert await adapter.recover_pending_operations(trigger="allowlist-revocation") == 0
    pending = tuple(adapter._background_tasks)
    assert pending == ()

    restored = adapter._operation_ledger.lookup_session(envelope.session_key)
    assert restored is not None
    assert restored.state is OperationState.READY
    assert restored.owner_instance != adapter._operation_ledger.instance_id
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recreated_same_name_profile_cannot_execute_prior_incarnation(
    tmp_path,
    monkeypatch,
):
    """A new physical profile incarnation never inherits old replay-safe work."""

    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    runner = SimpleNamespace(
        config=GatewayConfig(
            multiplex_profiles=True,
            multiplex_profile_allowlist=["ops"],
        ),
        adapters={},
        _profile_adapters={},
        _resolve_profile_home_for_source=lambda source: (
            profile_home if source.profile == "ops" else tmp_path
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda **_kwargs: [("default", tmp_path), ("ops", profile_home)],
    )

    prior = _adapter()
    prior.gateway_runner = runner
    raw_body = b'{"event":"prior-incarnation"}'
    route = WebhookRouteConfig.bind(
        "incarnation",
        {"provider": "generic", "profile": "ops"},
        headers={},
        request_profile="ops",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="prior-incarnation",
        authority_profile="ops",
    )
    admitted = prior._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    authority = prior._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "direct",
            "text": "must not run",
            "payload": {"event": "prior-incarnation"},
            "message_id": envelope.delivery_id,
            "source": prior._source_for_envelope(envelope).to_dict(),
        },
        target_snapshot={"v": 1, "kind": "log", "profile": "ops"},
        grant_snapshot=_grant_snapshot(prior, envelope),
    )
    assert prior._operation_ledger.relinquish_recovery_claim(authority)

    token_path = profile_home / _PROFILE_AUTHORITY_INCARNATION_FILENAME
    token_path.unlink()

    replacement = _adapter()
    replacement.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = replacement
    assert (
        await replacement.recover_pending_operations(trigger="same-name-recreated") == 0
    )
    assert tuple(replacement._background_tasks) == ()

    restored = replacement._operation_ledger.lookup_session(envelope.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE


@pytest.mark.asyncio
async def test_profile_generation_is_rechecked_at_agent_running_gate(tmp_path):
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
        _resolve_profile_home_for_source=lambda source: (
            profile_home if source.profile == "ops" else tmp_path
        ),
    )
    adapter = _adapter()
    adapter.gateway_runner = runner
    raw_body = b'{"event":"agent-gate"}'
    route = WebhookRouteConfig.bind(
        "agent-gate",
        {"provider": "generic", "profile": "ops"},
        headers={},
        request_profile="ops",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="agent-gate",
        authority_profile="ops",
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "agent",
            "text": "must not run",
            "payload": {"event": "agent-gate"},
            "message_id": envelope.delivery_id,
            "source": adapter._source_for_envelope(envelope).to_dict(),
        },
        target_snapshot={"v": 1, "kind": "log", "profile": "ops"},
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    (profile_home / _PROFILE_AUTHORITY_INCARNATION_FILENAME).unlink()

    with pytest.raises(
        WebhookLedgerTransitionError,
        match="profile authority changed",
    ):
        await adapter.on_processing_start(SimpleNamespace(webhook_authority=prepared))

    restored = adapter._operation_ledger.lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE


@pytest.mark.asyncio
async def test_profile_generation_is_rechecked_at_recovered_direct_running_gate(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter()
    raw_body = b'{"event":"recovered-direct-gate"}'
    route = WebhookRouteConfig.bind(
        "recovered-direct-gate",
        {"provider": "generic"},
        headers={},
        request_profile="default",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="recovered-direct-gate",
    )
    admitted = adapter._operation_ledger.admit(envelope)
    assert admitted.authority is not None
    prepared = adapter._operation_ledger.prepare(
        admitted.authority,
        event_snapshot={
            "v": 1,
            "mode": "direct",
            "text": "must not deliver",
            "payload": {"event": "recovered-direct-gate"},
            "message_id": envelope.delivery_id,
            "source": adapter._source_for_envelope(envelope).to_dict(),
        },
        target_snapshot={"v": 1, "kind": "log", "profile": "default"},
        grant_snapshot=_grant_snapshot(adapter, envelope),
    )
    mark_running = MagicMock(wraps=adapter._operation_ledger.mark_running)
    monkeypatch.setattr(adapter._operation_ledger, "mark_running", mark_running)
    (tmp_path / _PROFILE_AUTHORITY_INCARNATION_FILENAME).unlink()

    await adapter._recover_event_ready(prepared)

    restored = adapter._operation_ledger.lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE
    mark_running.assert_not_called()


@pytest.mark.asyncio
async def test_profile_generation_is_rechecked_at_target_attempt_gate(tmp_path):
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    target_adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="wrong"))
    )
    runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
        adapters={},
        _profile_adapters={"ops": {Platform.TELEGRAM: target_adapter}},
        _resolve_profile_home_for_source=lambda source: (
            profile_home if source.profile == "ops" else tmp_path
        ),
    )
    adapter = _adapter()
    adapter.gateway_runner = runner
    staged = _stage_delivery(
        adapter,
        profile="ops",
        target={
            "v": 1,
            "kind": "platform",
            "profile": "ops",
            "platform": "telegram",
            "chat_id": "must-not-send",
        },
        suffix="target-generation-gate",
    )
    (profile_home / _PROFILE_AUTHORITY_INCARNATION_FILENAME).unlink()

    result = await adapter._invoke_staged_target(staged)

    assert result.disposition is WebhookTargetDeliveryDisposition.INDETERMINATE
    target_adapter.send.assert_not_awaited()
    restored = adapter._operation_ledger.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE
