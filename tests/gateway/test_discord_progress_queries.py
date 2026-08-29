"""Discord pre-router contract for read-only project progress recall."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.specialist_handoff import HandoffResult
from gateway.specialist_routing import RouteKind, SpecialistRouteDecision


@pytest.fixture
def adapter(monkeypatch):
    import plugins.platforms.discord.adapter as discord_platform
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(discord_platform.discord, "DMChannel", type("DMChannel", (), {}), raising=False)
    config = PlatformConfig(
        enabled=True,
        token="fake-token",
        extra={
            "specialist_routing": {
                "enabled": True,
                "board": "exampleproject-burndown",
                "model": "fast-router",
            }
        },
    )
    value = DiscordAdapter(config)
    value._client = SimpleNamespace(user=SimpleNamespace(id=999))
    value.send = AsyncMock()
    return value


def _event(text="How did the burndown go and what else do we need to do?"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        message_id="message-1",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="example-project",
            chat_type="group",
            user_id="operator-1",
        ),
    )


def test_progress_pre_router_answers_before_model_specialist_classifier(adapter, monkeypatch):
    from gateway.progress_queries import ProgressQueryResult

    captured = {}

    def fake_resolve(request, *, source, board):
        captured.update(request=request, source=source, board=board)
        return ProgressQueryResult(True, "Burndown: 1 completed; next: acceptance.", "resolved")

    monkeypatch.setattr("gateway.progress_queries.resolve_progress_query", fake_resolve)
    adapter._classify_specialist_event = AsyncMock()

    handled = asyncio.run(adapter._maybe_answer_progress_event(_event()))

    assert handled is True
    assert captured["request"] == "How did the burndown go and what else do we need to do?"
    assert captured["source"].platform == "discord"
    assert captured["source"].chat_id == "example-project"
    assert not hasattr(captured["source"], "session_id")
    assert captured["board"] == "exampleproject-burndown"
    adapter._classify_specialist_event.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "example-project", content="Burndown: 1 completed; next: acceptance.", reply_to="message-1"
    )


@pytest.mark.parametrize("reason", ["unavailable", "no_match"])
def test_unhandled_progress_lookup_falls_through_without_send_or_model(
    adapter, monkeypatch, reason
):
    from gateway.progress_queries import ProgressQueryResult

    monkeypatch.setattr(
        "gateway.progress_queries.resolve_progress_query",
        lambda *args, **kwargs: ProgressQueryResult(False, "", reason),
    )

    handled = asyncio.run(adapter._maybe_answer_progress_event(_event()))

    assert handled is False
    adapter.send.assert_not_awaited()


def test_specialist_routing_configuration_requires_nonempty_explicit_board(adapter):
    settings = adapter._specialist_routing_settings()

    assert settings["board"] == "exampleproject-burndown"

    adapter.config.extra["specialist_routing"]["board"] = ""
    assert adapter._specialist_routing_settings()["enabled"] is False


def _specialist_decision() -> SpecialistRouteDecision:
    return SpecialistRouteDecision(
        kind=RouteKind.SPECIALIST,
        profile="market-data-authority-auditor",
        confidence=0.95,
        reason="market-data audit",
        title="Audit market-data evidence",
        audit_reason="specialist",
    )


def test_discord_specialist_ingress_passes_fixed_local_signature_and_registry(adapter, monkeypatch):
    captured = {}

    def fake_handoff(**kwargs):
        captured.update(kwargs)
        return HandoffResult(False, reason="registry_unavailable")

    adapter._classify_specialist_event = AsyncMock(return_value=_specialist_decision())
    monkeypatch.setattr("gateway.specialist_handoff.create_specialist_handoff", fake_handoff)

    handled = asyncio.run(adapter._maybe_route_specialist_event(_event("Audit market data.")))

    assert handled is False
    assert captured["board"] == "exampleproject-burndown"
    assert captured["signature"].domain == "market-data"
    assert captured["signature"].actions == ("audit", "inspect", "read", "review", "validate")
    assert captured["signature"].requested_permissions == ("market-data:read",)
    assert captured["registry"]._board == "exampleproject-burndown"
    adapter.send.assert_not_awaited()


def test_discord_specialist_registry_unavailable_falls_through_to_normal_chat(adapter, monkeypatch):
    from gateway.capability_registry import RegistryResolution

    adapter._classify_specialist_event = AsyncMock(return_value=_specialist_decision())
    monkeypatch.setattr(
        "gateway.specialist_handoff.resolve_registry",
        lambda signature, registry: RegistryResolution(
            status="unavailable", profile=None, reason="database unavailable"
        ),
    )

    handled = asyncio.run(adapter._maybe_route_specialist_event(_event("Audit market data.")))

    assert handled is False
    adapter.send.assert_not_awaited()
