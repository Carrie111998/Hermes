"""``key_cmd``: derive a provider API key by running a command.

Gateways that issue short-lived bearers (SSO/OIDC brokers, cloud IAM, internal
auth proxies) make a stored key go stale mid-session. These tests pin the three
behaviours that make the feature work:

* resolution yields a CALLABLE (invoked per request) rather than a resolved
  string, so a long session never sends a stale token;
* the token is cached until shortly before expiry, so the command is not run
  once per request;
* a failure never leaks the helper's output or the command string, either of
  which can contain a credential.
"""

from __future__ import annotations

import pytest

from agent.command_token_source import (
    CommandTokenError,
    CommandTokenSource,
    build_command_token_provider,
)


class TestMinting:
    def test_bare_token_stdout(self):
        source = CommandTokenSource("printf 'tok-abc'", "dbx")
        assert source() == "tok-abc"

    def test_json_access_token(self):
        """The OAuth 2.0 token-endpoint response shape."""
        source = CommandTokenSource(
            """printf '{"access_token":"tok-json","expires_in":3600}'""", "dbx"
        )
        assert source() == "tok-json"

    def test_trailing_newline_is_stripped(self):
        """A raw newline in the credential would corrupt the auth header."""
        assert CommandTokenSource("echo tok-nl", "dbx")() == "tok-nl"

    def test_multiline_output_is_rejected_not_guessed(self):
        """Only the token may land on stdout.

        Silently taking the first line turns a misconfigured helper (banner,
        warning, two tokens) into a corrupt-credential 401 that is much harder
        to diagnose than an explicit refusal.
        """
        source = CommandTokenSource("printf 'banner\\ntok-real'", "dbx")
        with pytest.raises(CommandTokenError, match="multiple lines"):
            source()

    def test_json_without_access_token_is_an_error(self):
        source = CommandTokenSource("""printf '{"nope":1}'""", "dbx")
        with pytest.raises(CommandTokenError, match="access_token"):
            source()

    def test_empty_output_is_an_error(self):
        with pytest.raises(CommandTokenError, match="no output"):
            CommandTokenSource("true", "dbx")()

    def test_nonzero_exit_is_an_error(self):
        with pytest.raises(CommandTokenError, match="exited 3"):
            CommandTokenSource("exit 3", "dbx")()

    def test_failure_message_is_actionable_without_echoing_the_command(self):
        """Actionable, but never echoes the command (it may embed a secret)."""
        secret_cmd = "print-token --client-secret=SENTINEL-SECRET; exit 1"
        with pytest.raises(CommandTokenError) as excinfo:
            CommandTokenSource(secret_cmd, "dbx")()
        message = str(excinfo.value)
        assert "SENTINEL-SECRET" not in message
        assert "dbx" in message          # names the provider to fix
        assert "exited" in message       # states what happened


class TestNoCredentialLeak:
    def test_failure_message_excludes_command_output(self):
        """A failing auth helper may print a token — it must not be surfaced."""
        source = CommandTokenSource(
            "printf 'SENTINEL-SECRET'; printf 'stderr-SENTINEL' >&2; exit 1",
            "dbx",
        )
        with pytest.raises(CommandTokenError) as excinfo:
            source()
        assert "SENTINEL" not in str(excinfo.value)


class TestCaching:
    def test_token_is_cached_between_calls(self):
        """Without caching the command would run on every request."""
        # A command whose output changes each run: equal results prove caching.
        source = CommandTokenSource("date +%s%N", "dbx")
        assert source() == source()

    def test_expired_token_is_reminted(self):
        source = CommandTokenSource(
            """printf '{"access_token":"tok-%s","expires_in":3600}' $RANDOM""", "dbx"
        )
        first = source()
        # Force the cache past its expiry.
        source._expires_at = 0.0
        assert source() != first

    def test_no_advertised_ttl_caches_indefinitely(self):
        """No TTL means trust the token and refresh on 401.

        Inventing a synthetic expiry would re-run the command on a schedule
        the issuer never asked for.
        """
        source = CommandTokenSource("date +%s%N", "dbx")
        source()
        assert source._expires_at is None
        assert source() == source()

    def test_advertised_ttl_sets_an_expiry(self):
        source = CommandTokenSource(
            """printf '{"access_token":"tok","expires_in":3600}'""", "dbx"
        )
        source()
        assert source._expires_at is not None

    def test_ttl_shorter_than_the_leeway_still_caches_briefly(self):
        """A leeway larger than the TTL must not disable caching entirely."""
        source = CommandTokenSource(
            """printf '{"access_token":"tok","expires_in":1}'""", "dbx"
        )
        source()
        assert source._expires_at is not None
        assert source._expires_at > 0.0


class TestBuilder:
    def test_returns_none_when_unset(self):
        assert build_command_token_provider("") is None
        assert build_command_token_provider("   ") is None

    def test_returns_callable_when_set(self):
        provider = build_command_token_provider("printf tok", "dbx")
        assert callable(provider)
        assert provider() == "tok"


class TestResolutionYieldsACallable:
    """The integration contract: a callable reaches the wire client."""

    def test_key_cmd_entry_resolves_to_a_callable(self, monkeypatch):
        from hermes_cli import runtime_provider as rp

        config = {
            "providers": {
                "dbx": {
                    "base_url": "https://example.invalid/v1",
                    "api_mode": "chat_completions",
                    "model": "m1",
                    "key_cmd": "printf minted-token",
                }
            }
        }
        monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)

        runtime = rp.resolve_runtime_provider(requested="custom:dbx")
        api_key = runtime["api_key"]
        assert callable(api_key), "key_cmd must resolve to a per-request callable"
        assert api_key() == "minted-token"

    def test_explicit_api_key_still_wins(self, monkeypatch):
        """``--api-key`` stays the one-off recovery escape hatch."""
        from hermes_cli import runtime_provider as rp

        config = {
            "providers": {
                "dbx": {
                    "base_url": "https://example.invalid/v1",
                    "api_mode": "chat_completions",
                    "model": "m1",
                    "key_cmd": "printf minted-token",
                }
            }
        }
        monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)

        runtime = rp.resolve_runtime_provider(
            requested="custom:dbx", explicit_api_key="sk-explicit-override"
        )
        assert runtime["api_key"] == "sk-explicit-override"


class TestCallableKeyGetsBearerAuth:
    """A callable api_key must reach the Anthropic bearer-hook client path.

    This is why key_cmd needs no per-vendor auth wiring: a static string is
    sent as ``x-api-key`` (which OAuth-gated gateways reject with 401), while a
    callable routes through the per-request ``Authorization: Bearer`` hook the
    Entra ID path already established. Verified against a live gateway with the
    SAME token value: static -> 401, callable -> 200.
    """

    def test_callable_takes_the_bearer_hook_path(self, monkeypatch):
        import agent.anthropic_adapter as aa

        seen = {}

        def _fake_hook(api_key, base_url, timeout, **kw):
            seen["callable"] = callable(api_key)
            return object()

        monkeypatch.setattr(
            aa, "_build_anthropic_client_with_bearer_hook", _fake_hook
        )
        aa.build_anthropic_client(
            lambda: "minted-token", "https://gateway.invalid/anthropic"
        )
        assert seen.get("callable") is True

    def test_output_cap_error_phrasing_is_recognized(self):
        """Some gateways name the cap in prose, not as ``max_tokens``.

        Misclassified, this deterministic 400 is routed into the compression
        loop, which re-sends the same oversized cap until the session dies.
        """
        from agent.model_metadata import is_output_cap_error

        assert is_output_cap_error(
            "The maximum tokens you requested exceeds the model limit of 128000"
        )
        # A genuine input overflow still routes to compression.
        assert not is_output_cap_error(
            "prompt is too long: 210000 tokens > 200000 maximum"
        )
