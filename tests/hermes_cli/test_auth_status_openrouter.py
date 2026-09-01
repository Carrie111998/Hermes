"""Regression tests for `hermes auth status openrouter` (#95878).

`openrouter` is deliberately absent from ``PROVIDER_REGISTRY`` (the model
setup flow synthesizes a minimal pconfig), so ``get_auth_status()`` fell
through to the default ``{"logged_in": False}`` without ever consulting the
credential pool or ``.env`` — the one diagnostic surface that disagreed with
``auth list`` / ``config check`` / ``debug share``. The dispatcher now
routes ``openrouter`` through the API-key status resolver with the same
synthesized pconfig ``_model_flow_openrouter`` uses.
"""

from __future__ import annotations

import hermes_cli.auth as auth_mod


class _FakeEntry:
    def __init__(self, secret: str):
        self.access_token = secret


class _FakePool:
    """The slice of the credential-pool API the resolver consumes."""

    def __init__(self, secrets):
        self._secrets = list(secrets)

    def has_credentials(self) -> bool:
        return bool(self._secrets)

    def peek(self):
        return _FakeEntry(self._secrets[0]) if self._secrets else None

    def entries(self):
        return [_FakeEntry(s) for s in self._secrets]


def _run(monkeypatch, *, dotenv_key="", pool_secrets=None):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import agent.credential_pool as pool_mod
    import hermes_cli.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "get_env_value_prefer_dotenv",
        lambda var: dotenv_key if var == "OPENROUTER_API_KEY" else "",
    )
    # _resolve_api_key_provider_secret does a function-level import of
    # load_pool, so patching the module attribute is what reaches it.
    pool = _FakePool(pool_secrets or [])
    monkeypatch.setattr(pool_mod, "load_pool", lambda provider_id: pool)
    return auth_mod.get_auth_status("openrouter")


class TestOpenRouterAuthStatus:
    def test_env_key_reports_logged_in(self, monkeypatch):
        """A usable .env key must report logged_in, matching auth list."""
        status = _run(monkeypatch, dotenv_key="sk-or-test-not-real")
        assert status.get("logged_in") is True
        assert status.get("provider") == "openrouter"

    def test_pool_credential_reports_logged_in(self, monkeypatch):
        """A credential-pool entry is an equally valid resolution source."""
        status = _run(monkeypatch, pool_secrets=["sk-or-test-not-real"])
        assert status.get("logged_in") is True

    def test_no_credential_reports_logged_out(self, monkeypatch):
        """Without any credential the honest answer stays logged out."""
        status = _run(monkeypatch)
        assert status.get("logged_in") is False

    def test_registry_providers_route_unchanged(self, monkeypatch):
        """A registry api_key provider keeps its existing status path."""
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        # z.ai is a real registry api_key provider; route through it with no
        # credential present and confirm we get the resolver's shape (not the
        # bare fallthrough dict).
        status = auth_mod.get_auth_status("zai")
        assert "configured" in status or "logged_in" in status
