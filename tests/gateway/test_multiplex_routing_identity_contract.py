"""Current-main integration contract for issue #88715.

These tests are copied into the Hermes checkout by ``apply_current_main.py`` and
run after the exact-blob transformations land.  They intentionally exercise the
real SessionSource/SessionEntry, adapter registry, authz mixin, and TurnContext
rather than re-testing only the leaf routing-identity module.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.routing_identity import (
    RoutingIdentity,
    attach_identity_to_source,
    current_routing_identity,
    identity_from_source,
    routing_identity_scope,
)
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.turn_context import TurnContext


class _StubAdapter(BasePlatformAdapter):
    pass


_StubAdapter.__abstractmethods__ = frozenset()  # type: ignore[attr-defined]


def _adapter(platform: Platform, runner: object) -> _StubAdapter:
    adapter = _StubAdapter.__new__(_StubAdapter)
    adapter.platform = platform
    adapter.gateway_runner = runner
    return adapter


def test_build_session_key_honors_stamped_runtime_profile_without_explicit_arg():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat",
        chat_type="dm",
        profile="finance",
    )
    assert build_session_key(source).startswith("agent:finance:")

    source.profile = None
    assert build_session_key(source).startswith("agent:main:")


@pytest.mark.asyncio
async def test_direct_multiplex_source_without_transport_binding_fails_closed():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.multiplex_profile_allowlist = ["finance"]
    runner._primary_profile_name = "default"
    runner.adapters = {}
    runner._profile_adapters = {"finance": {}}
    runner._profile_name_for_source = lambda _source: "finance"

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="direct-chat",
        chat_type="dm",
        profile="finance",
    )
    event = MessageEvent(text="must not run", source=source)

    with patch(
        "hermes_cli.profiles.profiles_to_serve",
        return_value=[
            ("default", Path("/profiles/default")),
            ("finance", Path("/profiles/finance")),
        ],
    ):
        result = await GatewayRunner._handle_message(runner, event)

    assert result is None
    assert source.profile_route_rejected is True


def test_session_source_keeps_owner_off_wire_and_dataclass_replace_preserves_it():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    identity = RoutingIdentity("default", "finance", "finance")
    attach_identity_to_source(source, identity)

    copied = replace(source)
    assert identity_from_source(copied) == identity
    assert copied.routing_identity == identity.to_persistence_dict()
    assert "routing_identity" not in copied.to_dict()
    assert "transport_profile" not in copied.to_dict()


def test_session_entry_round_trip_restores_trusted_transport_owner():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    identity = RoutingIdentity("default", "finance", "finance")
    attach_identity_to_source(source, identity)
    now = datetime.now()
    entry = SessionEntry(
        session_key="agent:finance:telegram:dm:chat",
        session_id="sid",
        created_at=now,
        updated_at=now,
        origin=source,
    )

    payload = entry.to_dict()
    assert payload["routing_identity"] == identity.to_persistence_dict()

    restored = SessionEntry.from_dict(payload)
    assert restored.origin is not None
    assert identity_from_source(restored.origin) == identity
    assert restored.routing_identity == identity.to_persistence_dict()


def test_secondary_credential_owner_is_stamped_before_adapter_batch_keying():
    runner = MagicMock(spec=GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = []
    runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(
        runner
    )
    runner._primary_profile_name = "default"

    adapter = _adapter(Platform.TELEGRAM, runner)
    adapter._multiplex_profile_name = "career-ops"
    source = adapter.build_source(chat_id="chat", chat_type="dm", user_id="user")

    identity = identity_from_source(source, credential_owner="career-ops")
    assert source.profile == "career-ops"
    assert identity == RoutingIdentity("career-ops", "career-ops", "career-ops")
    assert build_session_key(source).startswith("agent:career-ops:")


def test_shared_primary_credential_can_route_to_named_runtime_without_losing_owner():
    runner = MagicMock(spec=GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.profile_routes = []
    runner._primary_profile_name = "default"
    runner._profile_name_for_source = MagicMock(return_value="finance")

    adapter = _adapter(Platform.TELEGRAM, runner)
    with patch(
        "hermes_cli.profiles.profiles_to_serve",
        return_value=[
            ("default", Path("/profiles/default")),
            ("finance", Path("/profiles/finance")),
        ],
    ):
        source = adapter.build_source(chat_id="route-chat", chat_type="dm")

    identity = identity_from_source(source, credential_owner="default")
    assert identity == RoutingIdentity("default", "finance", "finance")
    assert source.profile == "finance"
    assert build_session_key(source).startswith("agent:finance:")


def _bare_runner_with_adapters(default_adapter: object, finance_adapter: object):
    runner = object.__new__(GatewayRunner)
    runner._primary_profile_name = "default"
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    runner._profile_adapters = {
        "finance": {Platform.TELEGRAM: finance_adapter},
    }
    return runner


def test_outbound_adapter_uses_durable_transport_owner_not_runtime_profile():
    default_adapter = object()
    finance_adapter = object()
    runner = _bare_runner_with_adapters(default_adapter, finance_adapter)

    shared_credential_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat",
        profile="finance",
        routing_identity=RoutingIdentity(
            "default", "finance", "finance"
        ).to_persistence_dict(),
    )
    assert runner._adapter_for_source(shared_credential_source) is default_adapter

    per_credential_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat",
        profile="finance",
        routing_identity=RoutingIdentity(
            "finance", "finance", "finance"
        ).to_persistence_dict(),
    )
    assert runner._adapter_for_source(per_credential_source) is finance_adapter


def test_turn_context_captures_identity_for_callbacks_and_worker_threads():
    identity = RoutingIdentity("default", "finance", "finance")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    with routing_identity_scope(identity):
        context = TurnContext(source=source)
        assert current_routing_identity() == identity
    assert context.routing_identity == identity


@pytest.mark.asyncio
async def test_adapter_handle_message_scopes_identity_before_session_keying():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.multiplex_profile_allowlist = ["finance"]
    runner._primary_profile_name = "default"
    runner._profile_adapters = {"finance": {}}
    runner._profile_name_for_source = lambda _source: "finance"
    runner._resolve_profile_home_for_source = lambda _source: Path(
        "/profiles/finance"
    )

    adapter = _adapter(Platform.TELEGRAM, runner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    adapter.config = GatewayConfig(multiplex_profiles=True)
    adapter.config.extra = {}
    adapter._message_handler = object()
    adapter._topic_recovery_fn = None
    adapter._session_store = None
    adapter._active_sessions = {}

    captured = {}

    def start_processing(event, session_key):
        captured["identity"] = current_routing_identity()
        captured["profile"] = event.source.profile
        captured["session_key"] = session_key
        return True

    adapter._start_session_processing = start_processing
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="route-chat", chat_type="dm"
    )
    event = MessageEvent(text="hello", source=source)

    @contextmanager
    def fake_profile_scope(_home):
        yield

    with (
        patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[
                ("default", Path("/profiles/default")),
                ("finance", Path("/profiles/finance")),
            ],
        ),
        patch("gateway.run._profile_runtime_scope", fake_profile_scope),
    ):
        await adapter.handle_message(event)

    assert captured == {
        "identity": RoutingIdentity("default", "finance", "finance"),
        "profile": "finance",
        "session_key": "agent:finance:telegram:dm:route-chat",
    }
    assert current_routing_identity() is None
