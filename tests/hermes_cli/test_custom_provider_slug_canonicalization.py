"""The ``custom:<name>`` slug must never brick a config it gets written into.

The pickers and the desktop settings hand around namespaced provider slugs.
For declared ``providers:`` / ``custom_providers:`` entries those resolve to a
durable id — but the BARE custom endpoint (``model.provider: custom`` +
base_url, the shape Agent Command's deploy writes) declares no entry, so its
UI row slug is ``custom:custom`` and resolves to nothing. Persisting that
string verbatim made the next agent init die with
"Unknown provider 'custom:custom'" (2026-08-12, live): the desktop's provider
settings saved it through POST /api/model/set — into EVERY auxiliary slot,
because that branch did no normalization at all.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_cli.auth import AuthError, resolve_provider
from hermes_cli.providers import canonicalize_provider_slug


CUSTOM_PROVIDERS = [
    {"name": "Qwen Lab", "base_url": "https://qwen.lab/v1", "key_env": "QWEN_LAB_KEY"},
]


class TestCanonicalizeProviderSlug:
    def test_non_custom_slugs_pass_through(self):
        assert canonicalize_provider_slug("openrouter") == "openrouter"
        assert canonicalize_provider_slug("zai", {}) == "zai"
        assert canonicalize_provider_slug("") == ""
        assert canonicalize_provider_slug("custom") == "custom"

    def test_bare_endpoint_row_slug_collapses_to_custom(self):
        # The live failure: no declared entries at all, slug from the UI row.
        assert canonicalize_provider_slug("custom:custom", {}) == "custom"
        assert canonicalize_provider_slug("custom:custom", None) == "custom"

    def test_declared_custom_provider_resolves_to_durable_slug(self):
        cfg = {"custom_providers": CUSTOM_PROVIDERS}
        assert canonicalize_provider_slug("custom:qwen-lab", cfg) == "custom:qwen-lab"

    def test_declared_user_provider_keyed_by_slug_resolves(self):
        cfg = {"providers": {"custom:mybox": {"base_url": "https://box.lab/v1"}}}
        assert canonicalize_provider_slug("custom:mybox", cfg) == "custom:mybox"

    def test_unresolvable_slug_with_unrelated_entries_collapses(self):
        cfg = {"custom_providers": CUSTOM_PROVIDERS}
        assert canonicalize_provider_slug("custom:something-else", cfg) == "custom"


class TestResolveProviderCustomNamespace:
    def test_namespaced_custom_slug_resolves_to_custom_family(self):
        # Raising here bricked agent init on a config a settings UI wrote.
        assert resolve_provider("custom:custom") == "custom"
        assert resolve_provider("custom:qwen-lab") == "custom"

    def test_genuinely_unknown_provider_still_raises(self):
        with pytest.raises(AuthError):
            resolve_provider("definitely-not-a-provider")


class TestModelSetCanonicalization:
    def _patch_config(self, monkeypatch, cfg, saved):
        from hermes_cli import web_server

        monkeypatch.setattr(web_server, "load_config", lambda: cfg)
        monkeypatch.setattr(web_server, "save_config", lambda c: saved.update(c))

    def test_main_assignment_never_persists_unresolved_slug(self, monkeypatch):
        from hermes_cli import web_server

        # _normalize_main_model_assignment loads config itself (module import).
        monkeypatch.setattr(web_server, "load_config", lambda: {})
        provider, model = web_server._normalize_main_model_assignment(
            "custom:custom", "ornith-1.0-35b"
        )
        assert provider == "custom"
        assert model == "ornith-1.0-35b"

    def test_main_assignment_keeps_declared_custom_provider(self, monkeypatch):
        from hermes_cli import web_server

        monkeypatch.setattr(
            web_server, "load_config", lambda: {"custom_providers": CUSTOM_PROVIDERS}
        )
        provider, _ = web_server._normalize_main_model_assignment(
            "custom:qwen-lab", "qwen3"
        )
        assert provider == "custom:qwen-lab"

    def test_auxiliary_assignment_canonicalizes_every_slot(self, monkeypatch):
        from hermes_cli import web_server

        saved: dict = {}
        self._patch_config(monkeypatch, {}, saved)
        result = web_server._apply_model_assignment_sync(
            "auxiliary", "custom:custom", "ornith-1.0-35b", "", "", ""
        )
        assert result["ok"] is True
        aux = saved.get("auxiliary")
        assert isinstance(aux, dict) and aux, "expected every aux slot written"
        for slot, slot_cfg in aux.items():
            assert slot_cfg["provider"] == "custom", (
                f"slot {slot} persisted an unresolvable provider slug"
            )


class TestValidateCustomEndpointKeyProbe:
    """OPENAI_API_KEY on a custom-provider config must probe the configured
    base_url, not api.openai.com — the table probe false-rejected lab keys."""

    class _FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

    def _run_validate(self, monkeypatch, cfg, status_code):
        from hermes_cli import web_server

        seen = {}

        fake_response = self._FakeResponse(status_code)

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None, params=None):
                seen["url"] = url
                seen["headers"] = headers or {}
                return fake_response

        monkeypatch.setattr("httpx.Client", _FakeClient)
        monkeypatch.setattr(web_server, "load_config", lambda: cfg)
        monkeypatch.setattr(web_server, "_require_token", lambda request: None)

        body = web_server.EnvVarUpdate(key="OPENAI_API_KEY", value="lab-key-123")
        result = asyncio.run(web_server.validate_provider_credential(body, request=None))
        return result, seen

    def test_probes_configured_base_url_with_bearer(self, monkeypatch):
        cfg = {"model": {"provider": "custom", "base_url": "https://vllm.lab/v1"}}
        result, seen = self._run_validate(monkeypatch, cfg, 200)
        assert seen["url"] == "https://vllm.lab/v1/models"
        assert seen["headers"].get("Authorization") == "Bearer lab-key-123"
        assert result["ok"] is True

    def test_rejection_from_the_real_endpoint_still_reports(self, monkeypatch):
        cfg = {"model": {"provider": "custom", "base_url": "https://vllm.lab/v1"}}
        result, seen = self._run_validate(monkeypatch, cfg, 401)
        assert seen["url"] == "https://vllm.lab/v1/models"
        assert result["ok"] is False and result["reachable"] is True

    def test_non_custom_provider_keeps_the_table_probe(self, monkeypatch):
        cfg = {"model": {"provider": "openrouter"}}
        _, seen = self._run_validate(monkeypatch, cfg, 200)
        assert seen["url"] == "https://api.openai.com/v1/models"
