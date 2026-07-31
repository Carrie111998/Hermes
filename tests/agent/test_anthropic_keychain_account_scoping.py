"""Regression tests: the macOS Keychain lookup must disambiguate by account.

macOS permits several generic-password items to share one service name, and
Claude Code does exactly that. Alongside the login credential it stores its
MCP-server OAuth state under the same ``Claude Code-credentials`` service, as a
separate item whose account is ``unknown`` and whose payload contains only an
``mcpOAuth`` key — no ``claudeAiOauth`` at all.

``security find-generic-password`` returns the FIRST matching item. An unscoped
lookup therefore often wins the MCP item, parses fine, finds no
``claudeAiOauth``, and yields None — so Hermes reports "No Anthropic credentials
found" while the user is fully logged in to Claude Code. Nothing errors; the
lookup succeeds and returns the wrong item.

These tests pin the two properties that prevent that:

  1. the account-scoped read is attempted, and
  2. an item that parses but lacks ``claudeAiOauth`` does not abort the search.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.anthropic_adapter import _read_claude_code_credentials_from_keychain

# Exercises the reader with explicit platform/subprocess mocks; never touches a
# real Keychain, so it opts out of the suite-wide guard like its sibling module.
pytestmark = pytest.mark.allow_macos_keychain


MCP_ONLY_PAYLOAD = json.dumps(
    {"mcpOAuth": {"posthog|abc123": {"serverName": "posthog", "accessToken": "mcp-tok"}}}
)
LOGIN_PAYLOAD = json.dumps(
    {
        "mcpOAuth": {"posthog|abc123": {"serverName": "posthog"}},
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-real-login-token",
            "refreshToken": "sk-ant-ort01-refresh",
            "expiresAt": 4102444800000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "team",
        },
    }
)


def _result(stdout: str, code: int = 0) -> MagicMock:
    r = MagicMock()
    r.returncode = code
    r.stdout = stdout
    return r


class TestKeychainAccountScoping:
    def test_account_scoped_lookup_is_attempted(self):
        """The reader must pass ``-a <username>``, not only ``-s <service>``."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", return_value="alice"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   return_value=_result(LOGIN_PAYLOAD)) as run:
            creds = _read_claude_code_credentials_from_keychain()

        assert creds is not None
        assert creds["accessToken"] == "sk-ant-oat01-real-login-token"

        argv = run.call_args_list[0][0][0]
        assert "-a" in argv, f"account flag missing — got {argv}"
        assert argv[argv.index("-a") + 1] == "alice"
        assert "-s" in argv and argv[argv.index("-s") + 1] == "Claude Code-credentials"

    def test_mcp_only_first_item_does_not_shadow_the_login_credential(self):
        """THE BUG: an mcpOAuth-only item must not end the search.

        Models the real machine: the account-scoped read hits an item without
        ``claudeAiOauth``, and the unscoped fallback returns the login payload.
        A reader that gave up on the first parse-without-claudeAiOauth returns
        None here and reports "no credentials" to a logged-in user.
        """
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", return_value="alice"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=[_result(MCP_ONLY_PAYLOAD), _result(LOGIN_PAYLOAD)]) as run:
            creds = _read_claude_code_credentials_from_keychain()

        assert run.call_count == 2, "must keep probing after an mcpOAuth-only item"
        assert creds is not None, "login credential was shadowed by the MCP item"
        assert creds["accessToken"] == "sk-ant-oat01-real-login-token"
        assert creds["source"] == "macos_keychain"

    def test_unscoped_fallback_preserved_when_username_unavailable(self):
        """If the username cannot be determined, the historical read still runs."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", side_effect=OSError("no login name")), \
             patch("agent.anthropic_adapter.subprocess.run",
                   return_value=_result(LOGIN_PAYLOAD)) as run:
            creds = _read_claude_code_credentials_from_keychain()

        assert creds is not None
        argv = run.call_args_list[0][0][0]
        assert "-a" not in argv, "must not pass an empty account"

    def test_returns_none_when_no_item_has_claude_oauth(self):
        """All candidates lacking claudeAiOauth still resolves to None."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", return_value="alice"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   return_value=_result(MCP_ONLY_PAYLOAD)):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_nonzero_exit_on_scoped_read_falls_through_to_unscoped(self):
        """No item for this account is not a failure — try the unscoped read."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", return_value="alice"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=[_result("", code=44), _result(LOGIN_PAYLOAD)]):
            creds = _read_claude_code_credentials_from_keychain()

        assert creds is not None
        assert creds["accessToken"] == "sk-ant-oat01-real-login-token"

    def test_malformed_json_does_not_abort_remaining_candidates(self):
        """A corrupt item must not mask a good one behind it."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("getpass.getuser", return_value="alice"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=[_result("not json at all"), _result(LOGIN_PAYLOAD)]):
            creds = _read_claude_code_credentials_from_keychain()

        assert creds is not None
        assert creds["accessToken"] == "sk-ant-oat01-real-login-token"

    def test_non_darwin_short_circuits_without_subprocess(self):
        """Linux/Windows must not shell out to ``security`` at all."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Linux"), \
             patch("agent.anthropic_adapter.subprocess.run") as run:
            assert _read_claude_code_credentials_from_keychain() is None
        run.assert_not_called()
