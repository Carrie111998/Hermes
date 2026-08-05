"""Tests for AgentRouter first-class provider wiring.

AgentRouter is unusual among Hermes providers: one host, one API key, two wire
protocols. ``https://agentrouter.org/v1`` speaks OpenAI Chat Completions and
``https://agentrouter.org`` speaks Anthropic Messages. Neither URL carries the
``/anthropic`` suffix that ``host_mandated_api_mode`` keys off, so the transport
is pinned entirely by the HERMES_OVERLAYS entries checked here — get those wrong
and Claude traffic silently goes out on the OpenAI wire (or vice versa).
"""

from __future__ import annotations

import sys
import types


if "dotenv" not in sys.modules:
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = fake_dotenv


class TestAgentRouterResolver:
    """resolve_provider_full must recognise both routes.

    When it returns None, an explicit ``provider: agentrouter`` in config.yaml
    is discarded and resolution falls through to env auto-detect.
    """

    def test_resolves_openai_route(self):
        from hermes_cli.providers import resolve_provider_full

        pdef = resolve_provider_full("agentrouter", {}, [])
        assert pdef is not None
        assert pdef.id == "agentrouter"
        assert pdef.base_url == "https://agentrouter.org/v1"
        assert "AGENTROUTER_API_KEY" in pdef.api_key_env_vars

    def test_resolves_anthropic_route(self):
        from hermes_cli.providers import resolve_provider_full

        pdef = resolve_provider_full("agentrouter-anthropic", {}, [])
        assert pdef is not None
        assert pdef.id == "agentrouter-anthropic"
        assert pdef.base_url == "https://agentrouter.org"
        assert "AGENTROUTER_API_KEY" in pdef.api_key_env_vars


class TestAgentRouterOverlays:
    def test_openai_overlay(self):
        from hermes_cli.providers import HERMES_OVERLAYS

        overlay = HERMES_OVERLAYS["agentrouter"]
        assert overlay.transport == "openai_chat"
        assert overlay.extra_env_vars == ("AGENTROUTER_API_KEY",)
        assert overlay.base_url_override == "https://agentrouter.org/v1"
        assert overlay.base_url_env_var == "AGENTROUTER_BASE_URL"
        assert overlay.is_aggregator

    def test_anthropic_overlay(self):
        from hermes_cli.providers import HERMES_OVERLAYS

        overlay = HERMES_OVERLAYS["agentrouter-anthropic"]
        assert overlay.transport == "anthropic_messages"
        assert overlay.base_url_override == "https://agentrouter.org"
        assert overlay.base_url_env_var == "AGENTROUTER_ANTHROPIC_BASE_URL"
        assert overlay.is_aggregator

    def test_labels(self):
        from hermes_cli.providers import get_label

        assert get_label("agentrouter") == "AgentRouter"
        assert get_label("agentrouter-anthropic") == "AgentRouter (Claude)"


class TestAgentRouterApiMode:
    """The Claude route must land on anthropic_messages.

    ``https://agentrouter.org`` matches none of host_mandated_api_mode's URL
    heuristics (no api.anthropic.com host, no /anthropic suffix), so without the
    overlay this would default to chat_completions.
    """

    def test_anthropic_route_selects_messages_transport(self):
        from hermes_cli.providers import determine_api_mode

        mode = determine_api_mode(
            "agentrouter-anthropic",
            base_url="https://agentrouter.org",
            model="claude-opus-4-6",
        )
        assert mode == "anthropic_messages"

    def test_openai_route_stays_on_chat_completions(self):
        from hermes_cli.providers import determine_api_mode

        mode = determine_api_mode(
            "agentrouter",
            base_url="https://agentrouter.org/v1",
            model="gpt-5.5",
        )
        assert mode == "chat_completions"


class TestAgentRouterAliases:
    def test_auth_resolver_aliases(self):
        from hermes_cli.auth import resolve_provider

        assert resolve_provider("agent-router") == "agentrouter"
        assert resolve_provider("agentrouter-openai") == "agentrouter"
        assert resolve_provider("agentrouter-claude") == "agentrouter-anthropic"

    def test_provider_registry_entries(self):
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["agentrouter"].auth_type == "api_key"
        assert (
            PROVIDER_REGISTRY["agentrouter"].inference_base_url
            == "https://agentrouter.org/v1"
        )
        # Both routes authenticate with the same key.
        assert PROVIDER_REGISTRY["agentrouter-anthropic"].api_key_env_vars == (
            "AGENTROUTER_API_KEY",
        )
        assert (
            PROVIDER_REGISTRY["agentrouter-anthropic"].inference_base_url
            == "https://agentrouter.org"
        )


class TestAgentRouterEnvCatalog:
    """The dashboard Providers page lists OPTIONAL_ENV_VARS keys whose category
    is "provider". Without these entries AGENTROUTER_API_KEY never reaches the
    frontend and AgentRouter stays invisible there, even though EnvPage.tsx has
    a matching PROVIDER_GROUPS prefix.
    """

    def test_optional_env_vars_include_agentrouter(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "AGENTROUTER_API_KEY" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["AGENTROUTER_API_KEY"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["AGENTROUTER_API_KEY"]["password"] is True

        assert "AGENTROUTER_BASE_URL" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["AGENTROUTER_BASE_URL"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["AGENTROUTER_BASE_URL"]["password"] is False


class TestAgentRouterInPicker:
    """CANONICAL_PROVIDERS auto-extends from the plugin registry; both routes
    are api-key providers so both must show up in `hermes model`.
    """

    def test_both_routes_are_listed(self):
        from hermes_cli.models import CANONICAL_PROVIDERS

        slugs = {p.slug for p in CANONICAL_PROVIDERS}
        assert "agentrouter" in slugs
        assert "agentrouter-anthropic" in slugs
