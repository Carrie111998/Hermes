"""Focused tests for the Impossibl AI API model-provider plugin."""

from __future__ import annotations

import contextlib
import io
import sys
import types
from argparse import Namespace
from unittest.mock import patch

import pytest

from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    resolve_api_key_provider_credentials,
    resolve_provider,
)
from hermes_cli.models import CANONICAL_PROVIDERS, provider_model_ids
from hermes_cli.model_normalize import normalize_model_for_provider
from hermes_cli.model_switch import switch_model
from hermes_cli.provider_catalog import provider_catalog_by_slug
from hermes_cli.providers import resolve_provider_full
from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


@pytest.fixture(autouse=True)
def _clear_impossibl_env(monkeypatch):
    monkeypatch.delenv("IMPOSSIBL_API_KEY", raising=False)


def test_profile_registers_with_expected_identity_and_catalog():
    profile = get_provider_profile("impossibl")

    assert profile is not None
    assert get_provider_profile("imp") is profile
    assert profile.display_name == "Impossibl AI API"
    assert profile.description == "Impossibl AI API — one API for models across providers"
    assert profile.signup_url == "https://impossibl.com/"
    assert profile.env_vars == ("IMPOSSIBL_API_KEY",)
    assert profile.base_url == "https://api.impossibl.com/v1"
    assert profile.models_url == "https://api.impossibl.com/v1/models"
    assert profile.auth_type == "api_key"
    assert profile.default_aux_model in profile.fallback_models


def test_alias_and_credentials_resolve_through_generated_registry(monkeypatch):
    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-test-key")

    assert resolve_provider("imp") == "impossibl"

    registry_entry = PROVIDER_REGISTRY["impossibl"]
    assert registry_entry.name == "Impossibl AI API"
    assert registry_entry.api_key_env_vars == ("IMPOSSIBL_API_KEY",)
    assert registry_entry.inference_base_url == "https://api.impossibl.com/v1"

    credentials = resolve_api_key_provider_credentials("impossibl")
    assert credentials == {
        "provider": "impossibl",
        "api_key": "imp-test-key",
        "base_url": "https://api.impossibl.com/v1",
        "source": "IMPOSSIBL_API_KEY",
    }


def test_runtime_uses_openai_compatible_chat_transport(monkeypatch):
    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-runtime-key")

    with patch("hermes_cli.runtime_provider._get_model_config", return_value={}):
        runtime = resolve_runtime_provider(requested="imp")

    assert runtime["provider"] == "impossibl"
    assert runtime["requested_provider"] == "imp"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["base_url"] == "https://api.impossibl.com/v1"
    assert runtime["api_key"] == "imp-runtime-key"


def test_auxiliary_client_uses_the_profile_default(monkeypatch):
    from agent.auxiliary_client import resolve_provider_client

    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-aux-key")
    with patch("agent.auxiliary_client.OpenAI") as mock_openai:
        mock_openai.return_value = object()
        client, model = resolve_provider_client("impossibl")

    assert client is not None
    assert model == "moonshotai/kimi-k3"
    assert mock_openai.call_args.kwargs["api_key"] == "imp-aux-key"
    assert mock_openai.call_args.kwargs["base_url"] == "https://api.impossibl.com/v1"


def test_model_catalog_merges_static_agentic_fallbacks_with_live_models(monkeypatch):
    profile = get_provider_profile("impossibl")
    assert profile is not None
    live = [
        "openai/gpt-5.4-mini",
        "anthropic/claude-sonnet-5",
    ]

    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-catalog-key")
    monkeypatch.setattr(profile, "fetch_models", lambda **_kwargs: live)

    models = provider_model_ids("impossibl")

    assert models[: len(profile.fallback_models)] == list(profile.fallback_models)
    assert set(profile.fallback_models) | set(live) <= set(models)
    assert models.count("openai/gpt-5.4-mini") == 1


def test_model_catalog_falls_back_without_credentials():
    profile = get_provider_profile("impossibl")
    assert profile is not None

    assert provider_model_ids("impossibl") == list(profile.fallback_models)


def test_provider_is_automatically_wired_to_model_and_provider_menus():
    model_entry = next(entry for entry in CANONICAL_PROVIDERS if entry.slug == "impossibl")
    descriptor = provider_catalog_by_slug()["impossibl"]

    assert model_entry.label == "Impossibl AI API"
    assert model_entry.tui_desc == "Impossibl AI API — one API for models across providers"
    assert descriptor.label == model_entry.label
    assert descriptor.description == model_entry.tui_desc
    assert descriptor.tab == "keys"
    assert descriptor.api_key_env_vars == ("IMPOSSIBL_API_KEY",)
    assert descriptor.signup_url == "https://impossibl.com/"


@pytest.mark.parametrize("provider_name", ["impossibl", "imp"])
def test_shared_provider_def_resolver_supports_canonical_and_alias(provider_name):
    resolved = resolve_provider_full(provider_name)

    assert resolved is not None
    assert resolved.id == "impossibl"
    assert resolved.name == "Impossibl AI API"
    assert resolved.api_key_env_vars == ("IMPOSSIBL_API_KEY",)
    assert resolved.base_url == "https://api.impossibl.com/v1"
    assert resolved.is_aggregator is True


def test_model_normalization_uses_impossibl_vendor_slugs():
    assert (
        normalize_model_for_provider("claude-sonnet-4.6", "impossibl")
        == "anthropic/claude-sonnet-4.6"
    )
    assert (
        normalize_model_for_provider(
            "deepseek/deepseek-v4-flash", "impossibl"
        )
        == "deepseek/deepseek-v4-flash"
    )


def test_switch_model_preserves_current_impossibl_for_vendor_slug(monkeypatch):
    model_id = "openai/gpt-5.4-mini"
    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-switch-key")

    with (
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch("hermes_cli.models.fetch_api_models", return_value=[model_id]),
    ):
        result = switch_model(
            raw_input=model_id,
            current_provider="impossibl",
            current_model="deepseek/deepseek-v4-flash",
            current_base_url="https://api.impossibl.com/v1",
            current_api_key="imp-switch-key",
        )

    assert result.success is True
    assert result.target_provider == "impossibl"
    assert result.new_model == model_id
    assert result.base_url == "https://api.impossibl.com/v1"
    assert result.api_key == "imp-switch-key"


def test_switch_model_accepts_explicit_impossibl_for_vendor_slug(monkeypatch):
    model_id = "openai/gpt-5.4-mini"
    monkeypatch.setenv("IMPOSSIBL_API_KEY", "imp-switch-key")

    with (
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch("hermes_cli.models.fetch_api_models", return_value=[model_id]),
    ):
        result = switch_model(
            raw_input=model_id,
            current_provider="openrouter",
            current_model="anthropic/claude-sonnet-4.6",
            explicit_provider="impossibl",
        )

    assert result.success is True
    assert result.target_provider == "impossibl"
    assert result.new_model == model_id
    assert result.base_url == "https://api.impossibl.com/v1"
    assert result.api_key == "imp-switch-key"


def test_doctor_accepts_impossibl_vendor_slug(monkeypatch, tmp_path):
    from hermes_cli import doctor as doctor_mod

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: impossibl\n"
        "  default: deepseek/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    monkeypatch.setitem(
        sys.modules,
        "model_tools",
        types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        ),
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False, ack=None))
    output = buf.getvalue()

    assert "model.provider 'impossibl' is not a recognised provider" not in output
    assert "model.provider 'impossibl' is unknown" not in output
    assert (
        "model.default 'deepseek/deepseek-v4-flash' uses a vendor/model slug "
        "but provider is 'impossibl'"
        not in output
    )
    assert "Either set model.provider to 'openrouter', or drop the vendor prefix." not in output
