"""Focused profile-bound root-action delivery tests."""

from hashlib import sha256
import hmac
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.root_action_approval import (
    PendingRootAction,
    RootActionApprovalStore,
    RootActionProtocolError,
    RootActionProposal,
)
from plugins.platforms.telegram import adapter as telegram_adapter


def _pending() -> PendingRootAction:
    proposal = RootActionProposal(
        action_id="act-profile",
        parameter_digest="a" * 64,
        preview="profile preview",
        expires_at="2099-01-01T00:00:00Z",
        created_at="2026-08-02T00:00:00Z",
    )
    return PendingRootAction(
        proposal=proposal,
        callback_url="https://pythia.internal/root-action/callback",
        callback_secret="secret",
        chat_id="-100123",
    )


class _Sender:
    def __init__(self):
        self.calls = []

    async def send_root_action_proposal(self, chat_id, preview, *, pending):
        self.calls.append((chat_id, preview, pending.proposal.action_id))
        return SendResult(success=True, message_id="telegram-message")


@pytest.mark.asyncio
async def test_profile_route_uses_only_selected_profile_telegram_adapter():
    selected = _Sender()
    primary = _Sender()
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: primary},
        _profile_adapters={"ops": {Platform.TELEGRAM: selected}},
    )
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    result = await adapter._deliver_root_action_proposal(
        _pending(),
        {"deliver_extra": {"chat_id": "-100123"}},
        profile="ops",
    )
    assert result.success is True
    assert selected.calls and not primary.calls


@pytest.mark.asyncio
async def test_active_named_profile_selects_primary_adapter():
    primary = _Sender()
    secondary = _Sender()
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: primary},
        _profile_adapters={"ops": {Platform.TELEGRAM: secondary}},
        _authorization_adapter=lambda platform, profile: (
            primary if profile == "ops" else None
        ),
        _active_profile_name=lambda: "ops",
    )
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    result = await adapter._deliver_root_action_proposal(
        _pending(),
        {"deliver_extra": {"chat_id": "-100123"}},
        profile="ops",
    )
    assert result.success is True
    assert primary.calls and not secondary.calls

@pytest.mark.asyncio
async def test_profile_route_fails_closed_without_selected_telegram_adapter():
    primary = _Sender()
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: primary},
        _profile_adapters={"ops": {}},
    )
    adapter = object.__new__(WebhookAdapter)
    adapter.gateway_runner = runner
    result = await adapter._deliver_root_action_proposal(
        _pending(),
        {"deliver_extra": {"chat_id": "-100123"}},
        profile="ops",
    )
    assert result.success is False
    assert not primary.calls



def _route_config() -> dict:
    return {
        "root_action_proposal": True,
        "deliver_extra": {"chat_id": "-100123"},
        "pythia_callback_url": "https://pythia.internal/root-action/callback",
        "pythia_callback_secret": "secret",
    }


@pytest.mark.asyncio
async def test_duplicate_stale_proposal_redelivers_before_accepting(
    tmp_path,
):
    store = RootActionApprovalStore(tmp_path / "approvals.json")
    pending = _pending()
    assert store.put(pending) is True
    adapter = object.__new__(WebhookAdapter)
    adapter._root_action_store = store
    calls = []

    async def _redeliver(existing, route_config, *, profile):
        calls.append((existing.proposal.action_id, profile))
        return SendResult(success=True, message_id="replayed-message")

    adapter._deliver_root_action_proposal = _redeliver
    response = await adapter._handle_root_action_proposal(
        _route_config(), pending.proposal.payload(), profile="ops"
    )
    assert response.status == 202
    assert calls == [(pending.proposal.action_id, "ops")]
    assert store.get(pending.proposal.action_id).message_id == "replayed-message"


@pytest.mark.asyncio
async def test_duplicate_locked_proposal_restarts_callback_delivery(
    tmp_path,
):
    store = RootActionApprovalStore(tmp_path / "approvals.json")
    pending = _pending()
    assert store.put(pending) is True
    store.consume(
        pending.proposal.action_id,
        decision="approve",
        principal="telegram:42",
        chat_id="-100123",
    )
    starts = []
    adapter = object.__new__(WebhookAdapter)
    adapter._root_action_store = store
    adapter._telegram_adapter_for_profile = lambda profile: SimpleNamespace(
        _start_root_action_delivery=starts.append
    )
    response = await adapter._handle_root_action_proposal(
        _route_config(), pending.proposal.payload(), profile="ops"
    )
    assert response.status == 202
    assert starts == [pending.proposal.action_id]

def test_root_route_signature_requires_timestamp_bound_v2():
    adapter = object.__new__(WebhookAdapter)
    body = b'{"action_id":"act"}'
    secret = "webhook-secret"
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, sha256
    ).hexdigest()
    request = MagicMock()
    request.headers = {
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    }
    assert adapter._validate_generic_v2_signature(request, body, secret) is True
    legacy = MagicMock()
    legacy.headers = {
        "X-Webhook-Signature": hmac.new(secret.encode(), body, sha256).hexdigest()
    }
    assert adapter._validate_generic_v2_signature(legacy, body, secret) is False


@pytest.mark.asyncio
async def test_locked_callback_retries_exact_outbox_until_ack(tmp_path, monkeypatch):
    store = RootActionApprovalStore(tmp_path / "approvals.json")
    pending = _pending()
    store.put(pending)
    locked = store.consume(
        pending.proposal.action_id,
        decision="approve",
        principal="telegram:42",
        chat_id="-100123",
    )
    calls = []

    def _post(current, **kwargs):
        calls.append(
            (
                current.proposal.action_id,
                kwargs["decision"],
                kwargs["principal"],
                kwargs["chat_id"],
                kwargs["decided_at"],
            )
        )
        if len(calls) == 1:
            raise RootActionProtocolError("temporary transport failure")
        return 204

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(telegram_adapter, "post_signed_decision", _post)
    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", _no_sleep)
    worker = object.__new__(telegram_adapter.TelegramAdapter)
    worker.platform = Platform.TELEGRAM
    worker._root_action_store = store
    await worker._deliver_root_action_until_ack(locked.proposal.action_id)
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert store.get(locked.proposal.action_id).acknowledged is True


@pytest.mark.asyncio
async def test_terminal_callback_4xx_is_persisted_and_not_retried(
    tmp_path, monkeypatch
):
    store = RootActionApprovalStore(tmp_path / "approvals.json")
    pending = _pending()
    store.put(pending)
    store.set_message_id(pending.proposal.action_id, "message-1")
    locked = store.consume(
        pending.proposal.action_id,
        decision="approve",
        principal="telegram:42",
        chat_id="-100123",
    )
    calls = []

    def _post(_current, **_kwargs):
        calls.append(True)
        raise RootActionProtocolError(
            "Pythia rejected decision", status=400, terminal=True
        )

    async def _unexpected_sleep(_delay):
        raise AssertionError("terminal callback must not retry")

    edited = []

    async def _edit_message_text(**kwargs):
        edited.append(kwargs)

    monkeypatch.setattr(telegram_adapter, "post_signed_decision", _post)
    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", _unexpected_sleep)
    worker = object.__new__(telegram_adapter.TelegramAdapter)
    worker.platform = Platform.TELEGRAM
    worker._root_action_store = store
    worker._bot = SimpleNamespace(edit_message_text=_edit_message_text)
    await worker._deliver_root_action_until_ack(locked.proposal.action_id)
    terminal = store.get(locked.proposal.action_id)
    assert terminal.terminal_failure is True
    assert edited and "permanently" in edited[0]["text"]
    restarted = RootActionApprovalStore(tmp_path / "approvals.json")
    persisted = restarted.get(locked.proposal.action_id)
    assert persisted.terminal_failure is True
    assert restarted.pending_deliveries() == []
