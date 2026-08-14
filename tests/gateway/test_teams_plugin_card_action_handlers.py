"""Tests for plugin-registered Teams Adaptive Card Action.Execute handlers."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_teams_mock() -> None:
    if "microsoft_teams" in sys.modules and hasattr(sys.modules["microsoft_teams"], "__file__"):
        return

    microsoft_teams = types.ModuleType("microsoft_teams")
    microsoft_teams_apps = types.ModuleType("microsoft_teams.apps")
    microsoft_teams_api = types.ModuleType("microsoft_teams.api")
    microsoft_teams_api_activities = types.ModuleType("microsoft_teams.api.activities")
    microsoft_teams_api_activities_typing = types.ModuleType(
        "microsoft_teams.api.activities.typing"
    )
    microsoft_teams_api_activities_invoke = types.ModuleType(
        "microsoft_teams.api.activities.invoke"
    )
    microsoft_teams_api_activities_invoke_adaptive_card = types.ModuleType(
        "microsoft_teams.api.activities.invoke.adaptive_card"
    )
    microsoft_teams_common = types.ModuleType("microsoft_teams.common")
    microsoft_teams_common_http = types.ModuleType("microsoft_teams.common.http")
    microsoft_teams_common_http_client = types.ModuleType(
        "microsoft_teams.common.http.client"
    )
    microsoft_teams_api_models = types.ModuleType("microsoft_teams.api.models")
    microsoft_teams_api_models_adaptive_card = types.ModuleType(
        "microsoft_teams.api.models.adaptive_card"
    )
    microsoft_teams_api_models_invoke_response = types.ModuleType(
        "microsoft_teams.api.models.invoke_response"
    )
    microsoft_teams_cards = types.ModuleType("microsoft_teams.cards")
    microsoft_teams_apps_http = types.ModuleType("microsoft_teams.apps.http")
    microsoft_teams_apps_http_adapter = types.ModuleType(
        "microsoft_teams.apps.http.adapter"
    )

    class MockApp:
        def __init__(self, **kwargs):
            self._client_id = kwargs.get("client_id")

        @property
        def id(self):
            return self._client_id

        def on_message(self, func):
            return func

        def on_card_action(self, func):
            return func

        async def initialize(self):
            pass

        async def send(self, conversation_id, activity):
            result = MagicMock()
            result.id = "sent-activity-id"
            return result

    class MockAdaptiveCard:
        def with_version(self, *_a, **_k):
            return self

        def with_body(self, *_a, **_k):
            return self

        def with_actions(self, *_a, **_k):
            return self

    class MockInvokeResponse:
        def __init__(self, status=200, body=None):
            self.status = status
            self.body = body

    class MockMessageResponse:
        def __init__(self, value=""):
            self.value = value

    class MockCardResponse:
        def __init__(self, value=None):
            self.value = value

    microsoft_teams_apps.App = MockApp
    microsoft_teams_apps.ActivityContext = object
    microsoft_teams_common_http_client.ClientOptions = MagicMock
    microsoft_teams_api.MessageActivity = object
    microsoft_teams_api.ConversationReference = object
    microsoft_teams_api_activities_typing.TypingActivityInput = MagicMock
    microsoft_teams_api_activities_invoke_adaptive_card.AdaptiveCardInvokeActivity = object
    microsoft_teams_api_models_adaptive_card.AdaptiveCardActionCardResponse = MockCardResponse
    microsoft_teams_api_models_adaptive_card.AdaptiveCardActionMessageResponse = (
        MockMessageResponse
    )
    microsoft_teams_api_models_invoke_response.InvokeResponse = MockInvokeResponse
    microsoft_teams_api_models_invoke_response.AdaptiveCardInvokeResponse = object
    microsoft_teams_apps_http_adapter.HttpMethod = object
    microsoft_teams_apps_http_adapter.HttpRequest = object
    microsoft_teams_apps_http_adapter.HttpResponse = object
    microsoft_teams_apps_http_adapter.HttpRouteHandler = object
    microsoft_teams_cards.AdaptiveCard = MockAdaptiveCard
    microsoft_teams_cards.ExecuteAction = MagicMock
    microsoft_teams_cards.TextBlock = MagicMock

    for name, mod in [
        ("microsoft_teams", microsoft_teams),
        ("microsoft_teams.apps", microsoft_teams_apps),
        ("microsoft_teams.api", microsoft_teams_api),
        ("microsoft_teams.api.activities", microsoft_teams_api_activities),
        ("microsoft_teams.api.activities.typing", microsoft_teams_api_activities_typing),
        ("microsoft_teams.api.activities.invoke", microsoft_teams_api_activities_invoke),
        (
            "microsoft_teams.api.activities.invoke.adaptive_card",
            microsoft_teams_api_activities_invoke_adaptive_card,
        ),
        ("microsoft_teams.common", microsoft_teams_common),
        ("microsoft_teams.common.http", microsoft_teams_common_http),
        ("microsoft_teams.common.http.client", microsoft_teams_common_http_client),
        ("microsoft_teams.api.models", microsoft_teams_api_models),
        (
            "microsoft_teams.api.models.adaptive_card",
            microsoft_teams_api_models_adaptive_card,
        ),
        (
            "microsoft_teams.api.models.invoke_response",
            microsoft_teams_api_models_invoke_response,
        ),
        ("microsoft_teams.cards", microsoft_teams_cards),
        ("microsoft_teams.apps.http", microsoft_teams_apps_http),
        ("microsoft_teams.apps.http.adapter", microsoft_teams_apps_http_adapter),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())
    sys.modules.setdefault("aiohttp.web", MagicMock())


_ensure_teams_mock()

from gateway.config import PlatformConfig  # noqa: E402
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest  # noqa: E402
from plugins.platforms.teams import adapter as teams_mod  # noqa: E402

teams_mod.TEAMS_SDK_AVAILABLE = True
teams_mod.AIOHTTP_AVAILABLE = True
teams_mod.App = sys.modules["microsoft_teams.apps"].App
teams_mod.InvokeResponse = sys.modules[
    "microsoft_teams.api.models.invoke_response"
].InvokeResponse
teams_mod.AdaptiveCardActionMessageResponse = sys.modules[
    "microsoft_teams.api.models.adaptive_card"
].AdaptiveCardActionMessageResponse
teams_mod.AdaptiveCardActionCardResponse = sys.modules[
    "microsoft_teams.api.models.adaptive_card"
].AdaptiveCardActionCardResponse
teams_mod.AdaptiveCard = sys.modules["microsoft_teams.cards"].AdaptiveCard
teams_mod.TextBlock = sys.modules["microsoft_teams.cards"].TextBlock
teams_mod.ExecuteAction = sys.modules["microsoft_teams.cards"].ExecuteAction


def _make_ctx(name: str = "test_plugin") -> tuple[PluginManager, PluginContext]:
    mgr = PluginManager()
    manifest = PluginManifest(name=name, version="0.1.0", description="test")
    return mgr, PluginContext(manifest=manifest, manager=mgr)


class TestRegisterTeamsCardActionHandlerAPI:
    def test_string_action_is_queued(self):
        mgr, ctx = _make_ctx()

        async def cb(*, ctx, data, adapter):  # pragma: no cover
            return "ok"

        ctx.register_teams_card_action_handler("claim_referral", cb)
        handlers = mgr.get_teams_card_action_handlers()
        assert len(handlers) == 1
        assert handlers[0][0] == "claim_referral"
        assert handlers[0][2] == "test_plugin"

    def test_empty_action_rejected(self):
        _, ctx = _make_ctx()

        async def cb(*, ctx, data, adapter):  # pragma: no cover
            return None

        with pytest.raises(ValueError, match="empty hermes_action"):
            ctx.register_teams_card_action_handler("  ", cb)


@pytest.mark.asyncio
async def test_card_action_dispatches_plugin_handler(monkeypatch):
    monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
    mgr, pctx = _make_ctx("laborde")
    seen = {}

    async def cb(*, ctx, data, adapter):
        seen["pnc"] = data.get("pnc_id")
        return f"claimed {data.get('pnc_id')}"

    pctx.register_teams_card_action_handler("claim_referral", cb)

    adapter = teams_mod.TeamsAdapter(
        PlatformConfig(enabled=True, extra={"client_id": "x", "client_secret": "y", "tenant_id": "z"})
    )
    adapter._conv_refs = {}

    activity = SimpleNamespace(
        value=SimpleNamespace(
            action=SimpleNamespace(
                data={"hermes_action": "claim_referral", "pnc_id": "PNC-1"}
            )
        ),
        conversation=SimpleNamespace(id="19:channel@thread.tacv2"),
        from_=SimpleNamespace(aad_object_id="user-1", id="user-1"),
    )
    ctx = SimpleNamespace(activity=activity, conversation_ref="ref-1")

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=mgr):
        resp = await adapter._on_card_action(ctx)

    assert seen["pnc"] == "PNC-1"
    assert resp.body.value == "claimed PNC-1"
    assert adapter._conv_refs["19:channel@thread.tacv2"] == "ref-1"


@pytest.mark.asyncio
async def test_card_action_plugin_exception_is_soft(monkeypatch):
    monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
    mgr, pctx = _make_ctx("boom")

    async def cb(*, ctx, data, adapter):
        raise RuntimeError("nope")

    pctx.register_teams_card_action_handler("claim_referral", cb)
    adapter = teams_mod.TeamsAdapter(
        PlatformConfig(enabled=True, extra={"client_id": "x", "client_secret": "y", "tenant_id": "z"})
    )
    activity = SimpleNamespace(
        value=SimpleNamespace(
            action=SimpleNamespace(data={"hermes_action": "claim_referral", "pnc_id": "PNC-1"})
        ),
        conversation=SimpleNamespace(id="c1"),
        from_=SimpleNamespace(aad_object_id="u", id="u"),
    )
    ctx = SimpleNamespace(activity=activity, conversation_ref="r")
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=mgr):
        resp = await adapter._on_card_action(ctx)
    assert "Action failed" in resp.body.value
