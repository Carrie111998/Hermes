"""Regression tests: oauth_minimax auth_type must route to the MiniMax OAuth
auxiliary client instead of falling through to the unhandled-auth_type
(None, None) return (#45241, #45242, #58231).

The minimax-oauth provider uses the Anthropic Messages wire with a
callable MiniMax OAuth token provider. Before the fix, the dispatch block
at resolve_provider_client only handled oauth_device_code / oauth_external,
so oauth_minimax fell through to 'unhandled auth_type' and every auxiliary
task (compression, vision, title_generation, session_search, ...) returned
(None, None).
"""

from unittest.mock import patch

import pytest


def _import_resolve():
    from agent.auxiliary_client import resolve_provider_client

    return resolve_provider_client


class TestOauthMinimaxAuthTypeDispatch:
    """oauth_minimax auth_type routes to the minimax-oauth builder."""

    @pytest.fixture(autouse=True)
    def _import(self):
        self.resolve = _import_resolve()

    def _make_pconfig(self, provider="minimax-oauth", auth_type="oauth_minimax"):
        class _PConfig:
            def __init__(self, provider, auth_type):
                self.provider = provider
                self.auth_type = auth_type
                self.base_url = "https://api.minimax.io/anthropic"
                self.api_key = None
                self.model = "minimax/minimax-m2.7"
                self.api_base = None
                self.extra_headers = None
                self.timeout = None
                self.max_retries = None
                self.aws_region = None
                self.aws_access_key_id = None
                self.aws_secret_access_key = None
                self.aws_session_token = None
                self.external_process = None

        return _PConfig(provider, auth_type)

    def test_oauth_minimax_dispatch_returns_client(self, monkeypatch):
        """resolve_provider_client(minimax-oauth, ...) must build a client via
        _build_minimax_oauth_aux_client, not return (None, None)."""
        class _FakeClient:
            def __init__(self, model):
                self.model = model

        with patch(
            "agent.auxiliary_client._build_minimax_oauth_aux_client",
            return_value=(_FakeClient("minimax-m2.7"), "minimax-m2.7"),
        ) as mock_build:
            client, model = self.resolve("minimax-oauth", "minimax/minimax-m2.7")
        mock_build.assert_called_once()
        assert client is not None
        assert model == "minimax/minimax-m2.7"

    def test_oauth_minimax_unauthenticated_returns_none_none(self, monkeypatch):
        """No valid token -> (None, None), matching the other OAuth branches."""
        with patch(
            "agent.auxiliary_client._build_minimax_oauth_aux_client",
            return_value=(None, None),
        ):
            client, model = self.resolve("minimax-oauth", "minimax/minimax-m2.7")
        assert client is None
        assert model is None

    def test_auth_type_set_includes_oauth_minimax(self):
        """The dispatch set must contain oauth_minimax (the #45241 symptom)."""
        import agent.auxiliary_client as ac

        src = open(ac.__file__, encoding="utf-8").read()
        assert '"oauth_device_code", "oauth_external", "oauth_minimax"' in src or (
            '"oauth_device_code", "oauth_external", "oauth_minimax"'
        ) in src, "oauth_minimax must be in the auth_type dispatch set"
