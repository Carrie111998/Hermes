"""Focused tests for ai& (aiand) first-class provider wiring.

These tests pin the wiring that makes ai& a real provider — alias
resolution through both CLI resolvers, config/doctor/overlay registration,
and credential/base-URL resolution — without any live network calls.
"""

from __future__ import annotations

import contextlib
import io
import sys
import types
from argparse import Namespace

import pytest

if "dotenv" not in sys.modules:
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = fake_dotenv

from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    resolve_api_key_provider_credentials,
    resolve_provider,
)
from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS, normalize_provider


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for key in ("AIAND_API_KEY",):
        monkeypatch.delenv(key, raising=False)


class TestAiandAliases:
    """Both CLI resolvers must map the aliases — the plugin's aliases= tuple is
    NOT consulted by these static maps, so they need explicit coverage."""

    @pytest.mark.parametrize("alias", ["aiand", "ai&", "ai-and", "AI&", " Ai-And "])
    def test_models_normalize_provider(self, alias):
        assert normalize_provider(alias) == "aiand"

    @pytest.mark.parametrize("alias", ["aiand", "ai&", "ai-and"])
    def test_providers_normalize_provider(self, alias):
        from hermes_cli.providers import normalize_provider as normalize_in_providers

        assert normalize_in_providers(alias) == "aiand"

    @pytest.mark.parametrize("alias", ["aiand", "ai&", "ai-and"])
    def test_auth_resolve_provider(self, alias):
        assert resolve_provider(alias) == "aiand"


class TestAiandRegistry:
    """Auto-registration from the ProviderProfile should expose aiand."""

    def test_in_provider_registry(self):
        assert "aiand" in PROVIDER_REGISTRY
        pconfig = PROVIDER_REGISTRY["aiand"]
        assert pconfig.id == "aiand"
        assert pconfig.name == "ai&"
        assert pconfig.auth_type == "api_key"
        assert pconfig.inference_base_url == "https://api.aiand.com/v1"
        assert pconfig.api_key_env_vars == ("AIAND_API_KEY",)

    def test_present_in_canonical_providers(self):
        slugs = [p.slug for p in CANONICAL_PROVIDERS]
        assert "aiand" in slugs

    def test_has_a_label(self):
        assert _PROVIDER_LABELS.get("aiand") == "ai&"

    def test_auto_detects_aiand_key(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "test-aiand-key")
        assert resolve_provider("auto") == "aiand"


class TestAiandConfigRegistry:
    def test_optional_env_vars_include_aiand(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "AIAND_API_KEY" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["AIAND_API_KEY"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["AIAND_API_KEY"]["password"] is True

        assert "AIAND_BASE_URL" not in OPTIONAL_ENV_VARS


class TestAiandOverlay:
    def test_overlay_exists(self):
        from hermes_cli.providers import HERMES_OVERLAYS

        assert "aiand" in HERMES_OVERLAYS
        overlay = HERMES_OVERLAYS["aiand"]
        assert overlay.transport == "openai_chat"
        assert overlay.base_url_override == "https://api.aiand.com/v1"
        assert not overlay.base_url_env_var
        assert not overlay.is_aggregator


class TestAiandDoctor:
    def test_provider_env_hints_include_aiand(self):
        from hermes_cli.doctor import _PROVIDER_ENV_HINTS

        assert "AIAND_API_KEY" in _PROVIDER_ENV_HINTS

    def test_slash_form_model_is_not_flagged_as_vendor_prefixed(self, monkeypatch, tmp_path):
        """ai&'s native model IDs are vendor-prefixed slugs
        (deepseek-ai/deepseek-v4-flash, moonshotai/kimi-k2.7-code, …), so
        doctor must NOT warn that provider should be 'openrouter' / the prefix
        dropped — that heuristic is for aggregator vendor slugs only."""
        from hermes_cli import doctor as doctor_mod

        home = tmp_path / ".hermes"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "model:\n"
            "  provider: aiand\n"
            "  default: deepseek-ai/deepseek-v4-flash\n"
            "memory: {}\n",
            encoding="utf-8",
        )
        (home / ".env").write_text("AIAND_API_KEY=aiand_test\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setenv("AIAND_API_KEY", "aiand_test")

        # Keep the run offline and cheap.
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: types.SimpleNamespace(status_code=200))
        monkeypatch.setitem(
            sys.modules,
            "model_tools",
            types.SimpleNamespace(check_tool_availability=lambda *a, **k: ([], []), TOOLSET_REQUIREMENTS={}),
        )
        with contextlib.suppress(Exception):
            from hermes_cli import auth as _auth_mod

            monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})

        buf = io.StringIO()
        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
            doctor_mod.run_doctor(Namespace(fix=False))
        out = buf.getvalue()

        assert "vendor-prefixed" not in out
        assert "vendor/model slug" not in out


class TestAiandCredentials:
    def test_resolves_default_base_url(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "aiand_test_key")
        creds = resolve_api_key_provider_credentials("aiand")
        assert creds["api_key"] == "aiand_test_key"
        assert creds["base_url"] == "https://api.aiand.com/v1"
        assert creds["source"] == "AIAND_API_KEY"


class TestAiandRuntime:
    def test_runtime_provider_resolution(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "aiand-key")
        from hermes_cli.runtime_provider import resolve_runtime_provider

        result = resolve_runtime_provider(requested="aiand")
        assert result["provider"] == "aiand"
        assert result["api_mode"] == "chat_completions"
        assert result["api_key"] == "aiand-key"
        assert result["base_url"] == "https://api.aiand.com/v1"


class TestAiandAuxiliary:
    """resolve_provider_client wires the BYOK key and cheap aux model."""

    def _resolve(self, name):
        from unittest.mock import patch

        from agent.auxiliary_client import resolve_provider_client

        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = object()
            client, model = resolve_provider_client(name)
        return client, model, mock_openai.call_args.kwargs

    def test_client_points_at_aiand_endpoint(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "aiand_test_key")
        client, model, kwargs = self._resolve("aiand")
        assert client is not None
        assert kwargs["base_url"] == "https://api.aiand.com/v1"

    def test_aux_model_is_cheap_flash_model(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "aiand_test_key")
        _, model, _ = self._resolve("aiand")
        assert model == "deepseek-ai/deepseek-v4-flash"

    def test_alias_resolves_through_aux_client(self, monkeypatch):
        monkeypatch.setenv("AIAND_API_KEY", "aiand_test_key")
        client, _, _ = self._resolve("ai-and")
        assert client is not None


class TestAiandModelMetadata:
    def test_url_infers_aiand(self):
        from agent.model_metadata import _infer_provider_from_url

        assert _infer_provider_from_url("https://api.aiand.com/v1") == "aiand"
