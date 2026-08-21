"""Tests for hermes_cli.copilot_auth — Copilot token validation and resolution."""

import pytest
from unittest.mock import patch


class TestTokenValidation:
    """Token type validation."""

    def test_classic_pat_rejected(self):
        from hermes_cli.copilot_auth import validate_copilot_token
        valid, msg = validate_copilot_token("ghp_abcdefghijklmnop1234")
        assert valid is False
        assert "Classic Personal Access Tokens" in msg
        assert "ghp_" in msg


class TestResolveToken:
    """Token resolution with env var priority."""


    def test_gh_token_second_priority(self, monkeypatch):
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_gh_second")
        monkeypatch.setenv("GITHUB_TOKEN", "gho_github_third")
        token, source = resolve_copilot_token()
        assert token == "gho_gh_second"
        assert source == "GH_TOKEN"




    def test_gh_cli_classic_pat_raises(self, monkeypatch):
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("hermes_cli.copilot_auth._try_gh_cli_token", return_value="ghp_classic"):
            with pytest.raises(ValueError, match="classic PAT"):
                resolve_copilot_token()

    def test_invalid_env_var_skips_gh_cli_fallback(self, monkeypatch):
        """When an env var is set but holds an unsupported classic PAT,
        resolve_copilot_token must NOT fall back to ``gh auth token``.

        The user explicitly exported a token; silently substituting one
        from the gh CLI credential store is surprising and the subprocess
        call adds up to 5s of latency on Windows cold starts (#60800).
        Only fall back to the CLI when NO Copilot env var is set at all.
        """
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_classic_pat_nope")
        with patch("hermes_cli.copilot_auth._try_gh_cli_token") as mock_cli:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        mock_cli.assert_not_called()

    def test_all_env_vars_invalid_skips_gh_cli_fallback(self, monkeypatch):
        """All three env vars set to classic PATs → no gh CLI call."""
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_one")
        monkeypatch.setenv("GH_TOKEN", "ghp_two")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_three")
        with patch("hermes_cli.copilot_auth._try_gh_cli_token") as mock_cli:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        mock_cli.assert_not_called()


class TestGhCliTokenCache:
    """The gh-CLI probe result is cached — a miss must not re-spawn gh.

    Regression: /api/model/options ran `gh auth token` four times per build;
    with no gh credential store each probe blocked its full 5s timeout, so the
    Desktop Models/Providers settings pages took 20s per open and exceeded the
    renderer's 15s IPC budget (Aug 2026 desktop audit).
    """

    def _reset(self):
        from hermes_cli.copilot_auth import _invalidate_gh_cli_token_cache
        _invalidate_gh_cli_token_cache()

    def test_miss_is_cached_and_probe_runs_once(self):
        from hermes_cli import copilot_auth
        self._reset()
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value=None) as probe:
            assert copilot_auth._try_gh_cli_token() is None
            assert copilot_auth._try_gh_cli_token() is None
            assert copilot_auth._try_gh_cli_token() is None
        assert probe.call_count == 1
        self._reset()

    def test_hit_is_cached(self):
        from hermes_cli import copilot_auth
        self._reset()
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value="gho_cached") as probe:
            assert copilot_auth._try_gh_cli_token() == "gho_cached"
            assert copilot_auth._try_gh_cli_token() == "gho_cached"
        assert probe.call_count == 1
        self._reset()

    def test_ttl_expiry_reprobes(self, monkeypatch):
        from hermes_cli import copilot_auth
        self._reset()
        clock = {"now": 1000.0}
        monkeypatch.setattr(copilot_auth.time, "monotonic", lambda: clock["now"])
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value=None) as probe:
            copilot_auth._try_gh_cli_token()
            clock["now"] += copilot_auth._GH_CLI_TOKEN_CACHE_TTL_SECONDS + 1
            copilot_auth._try_gh_cli_token()
        assert probe.call_count == 2
        self._reset()

    def test_invalidate_forces_reprobe(self):
        from hermes_cli import copilot_auth
        self._reset()
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value=None) as probe:
            copilot_auth._try_gh_cli_token()
            copilot_auth._invalidate_gh_cli_token_cache()
            copilot_auth._try_gh_cli_token()
        assert probe.call_count == 2
        self._reset()


class TestDeviceOAuthIdentity:
    def test_vscode_client_id_flow_does_not_claim_cli_identity(self):
        import urllib.parse
        import hermes_cli.copilot_auth as mod

        with patch("urllib.request.urlopen", side_effect=OSError("offline")) as urlopen:
            assert mod.copilot_device_code_login() is None

        request = urlopen.call_args.args[0]
        form = urllib.parse.parse_qs(request.data.decode())
        assert form["client_id"] == [mod.COPILOT_OAUTH_CLIENT_ID]
        assert request.get_header("User-agent") == "HermesAgent/1.0"
        assert not request.get_header("User-agent").startswith("copilot/")


class TestRequestHeaders:
    """Copilot API header generation."""

    def test_default_headers_include_openai_intent(self):
        from hermes_cli.copilot_auth import (
            COPILOT_INTEGRATION_ID,
            copilot_request_headers,
        )
        headers = copilot_request_headers()
        assert headers["Openai-Intent"] == "conversation-edits"
        assert headers["Copilot-Integration-Id"] == COPILOT_INTEGRATION_ID
        assert COPILOT_INTEGRATION_ID == "copilot-developer-cli"
        assert "Editor-Version" in headers


    def test_no_vision_header_by_default(self):
        from hermes_cli.copilot_auth import copilot_request_headers
        headers = copilot_request_headers()
        assert "Copilot-Vision-Request" not in headers


    def test_inference_uses_resolved_cli_identity(self, monkeypatch):
        from hermes_cli.copilot_auth import (
            CopilotClientIdentity,
            COPILOT_INTEGRATION_ID,
            copilot_request_headers,
        )
        identity = CopilotClientIdentity(
            version="9.8.7",
            user_agent="copilot/9.8.7 (linux v24.0.0) term/test-terminal",
            editor_version="copilot/9.8.7",
        )
        monkeypatch.setattr(
            "hermes_cli.copilot_auth.resolve_copilot_client_identity",
            lambda: identity,
        )

        headers = copilot_request_headers()
        assert headers["Copilot-Integration-Id"] == COPILOT_INTEGRATION_ID
        assert headers["User-Agent"] == identity.user_agent
        assert headers["Editor-Version"] == identity.editor_version


class TestCopilotClientIdentity:
    def test_installed_version_comes_from_copilot_executable(self, monkeypatch):
        import hermes_cli.copilot_auth as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/bin/copilot" if name == "copilot" else None,
        )
        monkeypatch.setattr(
            mod,
            "_command_version",
            lambda command: "GitHub Copilot CLI 2.3.4-5.\nRun 'copilot update'",
        )
        assert mod._installed_copilot_cli_version() == "2.3.4-5"

    def test_resolves_installed_cli_version_and_authentic_shape(self, monkeypatch):
        import hermes_cli.copilot_auth as mod

        mod.resolve_copilot_client_identity.cache_clear()
        monkeypatch.setattr(mod, "_installed_copilot_cli_version", lambda: "1.2.3-4")
        monkeypatch.setattr(mod, "_installed_node_version", lambda: "v24.1.0")
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setenv("TERM_PROGRAM", "tmux")

        identity = mod.resolve_copilot_client_identity()
        assert identity.version == "1.2.3-4"
        assert identity.editor_version == "copilot/1.2.3-4"
        assert identity.user_agent == "copilot/1.2.3-4 (linux v24.1.0) term/tmux"
        mod.resolve_copilot_client_identity.cache_clear()

    def test_fallback_is_truthfully_hermes_when_cli_is_absent(self, monkeypatch):
        import hermes_cli.copilot_auth as mod

        mod.resolve_copilot_client_identity.cache_clear()
        monkeypatch.setattr(mod, "_installed_copilot_cli_version", lambda: None)
        identity = mod.resolve_copilot_client_identity()

        assert identity.version is None
        assert identity.editor_version.startswith("hermes-cli/")
        assert identity.user_agent.startswith(identity.editor_version)
        assert "copilot/" not in identity.user_agent.lower()
        mod.resolve_copilot_client_identity.cache_clear()


class TestCopilotDefaultHeaders:
    """The models.py copilot_default_headers uses copilot_auth."""


    def test_agent_turn_explicit(self):
        """Explicitly passing is_agent_turn=True sets x-initiator to 'agent'."""
        from hermes_cli.models import copilot_default_headers
        headers = copilot_default_headers(is_agent_turn=True)
        assert headers["x-initiator"] == "agent"

    def test_param_passthrough_both_values(self):
        """is_agent_turn param correctly maps to x-initiator for both True and False."""
        from hermes_cli.models import copilot_default_headers
        for is_agent, expected in [(True, "agent"), (False, "user")]:
            headers = copilot_default_headers(is_agent_turn=is_agent)
            assert headers["x-initiator"] == expected, (
                f"is_agent_turn={is_agent} should produce x-initiator={expected!r}, "
                f"got {headers['x-initiator']!r}"
            )


class TestApiModeSelection:
    """API mode selection matching opencode's shouldUseCopilotResponsesApi."""

    def test_gpt5_uses_responses(self):
        from hermes_cli.models import _should_use_copilot_responses_api
        assert _should_use_copilot_responses_api("gpt-5.4") is True
        assert _should_use_copilot_responses_api("gpt-5.4-mini") is True
        assert _should_use_copilot_responses_api("gpt-5.3-codex") is True
        assert _should_use_copilot_responses_api("gpt-5.2-codex") is True
        assert _should_use_copilot_responses_api("gpt-5.2") is True
        assert _should_use_copilot_responses_api("gpt-5.1-codex-max") is True

    def test_gpt5_mini_excluded(self):
        from hermes_cli.models import _should_use_copilot_responses_api
        assert _should_use_copilot_responses_api("gpt-5-mini") is False


class TestEnvVarOrder:
    """PROVIDER_REGISTRY has correct env var order."""

    def test_copilot_env_vars_include_copilot_github_token(self):
        from hermes_cli.auth import PROVIDER_REGISTRY
        copilot = PROVIDER_REGISTRY["copilot"]
        assert "COPILOT_GITHUB_TOKEN" in copilot.api_key_env_vars
        # COPILOT_GITHUB_TOKEN should be first
        assert copilot.api_key_env_vars[0] == "COPILOT_GITHUB_TOKEN"

