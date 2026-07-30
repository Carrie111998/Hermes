from unittest.mock import patch

from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status
from hermes_cli.models import (
    PROVIDER_GROUPS,
    _PROVIDER_MODELS,
    normalize_provider,
    provider_label,
)


def test_claude_cli_is_distinct_from_direct_anthropic():
    assert normalize_provider("claude-cli") == "claude-cli"
    assert normalize_provider("anthropic") == "anthropic"
    assert normalize_provider("claude-code") == "claude-cli"
    assert provider_label("claude-cli") == "Claude Code (subscription)"
    assert PROVIDER_REGISTRY["claude-cli"].auth_type == "external_process"


def test_claude_group_offers_subscription_and_direct_api_routes():
    assert PROVIDER_GROUPS["anthropic"][2] == ["claude-cli", "anthropic"]
    assert _PROVIDER_MODELS["claude-cli"] == ["opus", "sonnet", "haiku"]


def test_claude_cli_status_maps_first_party_subscription_without_credentials():
    live = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }
    with patch(
        "agent.claude_cli_process.ClaudeCLIProcessRunner.auth_status",
        return_value=live,
    ), patch(
        "agent.claude_cli_process.ClaudeCLIProcessRunner.version",
        return_value="2.1.220 (Claude Code)",
    ):
        status = get_auth_status("claude-cli")

    assert status == {
        "configured": True,
        "logged_in": True,
        "provider": "claude-cli",
        "name": "Claude Code (subscription)",
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
        "subscription_type": "max",
        "version": "2.1.220 (Claude Code)",
    }


def test_anthropic_picker_description_is_direct_api_only():
    from hermes_cli.models import CANONICAL_PROVIDERS

    entry = next(item for item in CANONICAL_PROVIDERS if item.slug == "anthropic")
    assert "direct API" in entry.tui_desc
    assert "Claude Code" not in entry.tui_desc
