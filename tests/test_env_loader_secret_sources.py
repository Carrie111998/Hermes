"""Tests for the secret-source tracking in ``hermes_cli.env_loader``.

These cover the small public surface that lets `hermes model` / `hermes setup`
label detected credentials with their origin ("from Bitwarden") so users
don't see an unexplained "credentials ✓" line when their .env is empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import env_loader  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sources():
    """Each test starts with a clean source map."""
    env_loader._SECRET_SOURCES.clear()
    env_loader._SECRET_VALUES.clear()
    yield
    env_loader._SECRET_SOURCES.clear()
    env_loader._SECRET_VALUES.clear()


def test_get_secret_source_returns_none_for_untracked_var():
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_get_secret_source_returns_label_for_tracked_var():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"


def test_format_secret_source_suffix_empty_for_untracked():
    # Credentials from .env or the shell shouldn't add noise — the
    # implicit case stays unlabeled.
    assert env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY") == ""


def test_format_secret_source_suffix_bitwarden_uses_proper_name():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_format_secret_source_suffix_generic_label_for_future_sources():
    # Future-proofing: a new secret source (e.g. "vault") should still
    # produce a sensible label without needing to edit every call site.
    env_loader._SECRET_SOURCES["OPENAI_API_KEY"] = "vault"
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from vault)"
    )


def test_apply_external_secret_sources_records_bitwarden_origin(tmp_path, monkeypatch):
    """End-to-end: when ``apply_bitwarden_secrets`` returns applied keys,
    they end up in ``_SECRET_SOURCES`` so the UI can label them."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    # Stub apply_bitwarden_secrets to return a synthetic FetchResult.
    from agent.secret_sources.bitwarden import FetchResult

    fake_result = FetchResult(
        secrets={"ANTHROPIC_API_KEY": "sk-ant-test"},
        applied=["ANTHROPIC_API_KEY"],
    )

    def _fake_apply(**_kwargs):
        return fake_result

    # The import inside _apply_external_secret_sources is lazy, so we
    # patch the *module attribute* it will pull in.
    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "apply_bitwarden_secrets", _fake_apply)

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_apply_external_secret_sources_noop_when_disabled(tmp_path, monkeypatch):
    """Disabled Bitwarden config must not touch the source map."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_apply_external_secret_sources_records_onepassword_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    item: Hermes\n",
        encoding="utf-8",
    )

    import agent.secret_sources.onepassword as op_module

    monkeypatch.setattr(
        op_module, "apply_onepassword_secrets",
        lambda *a, **kw: ({"SOME_KEY": "***"}, []),
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("SOME_KEY") == "onepassword"


def test_onepassword_local_override_is_not_clobbered_on_next_sync(tmp_path, monkeypatch):
    """Regression test for the "preserve local overrides after a managed
    refresh" fix: once an operator's .env value diverges from what
    1Password last injected, the stale 'onepassword' label must be
    dropped so the next sync's refresh-without-override_existing logic
    doesn't treat it as still-managed and clobber the override."""
    import os

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    item: Hermes\n",
        encoding="utf-8",
    )

    import agent.secret_sources.onepassword as op_module

    calls = []

    def _fake_apply(config, home_path, previously_managed=None):
        calls.append(set(previously_managed or set()))
        os.environ["SOME_KEY"] = "op-value-1"
        return {"SOME_KEY": "***"}, []

    monkeypatch.setattr(op_module, "apply_onepassword_secrets", _fake_apply)

    # First sync: nothing previously managed, 1Password sets SOME_KEY.
    env_loader._apply_external_secret_sources(tmp_path)
    assert calls[-1] == set()
    assert env_loader.get_secret_source("SOME_KEY") == "onepassword"

    # Operator edits .env to override it locally.
    monkeypatch.setenv("SOME_KEY", "operator-override")

    # Second sync: the label must be dropped BEFORE previously_managed is
    # computed, since the env value no longer matches what 1Password set.
    env_loader._apply_external_secret_sources(tmp_path)
    assert calls[-1] == set()  # SOME_KEY must NOT appear as previously_managed
    monkeypatch.delenv("SOME_KEY", raising=False)


def test_onepassword_removal_clears_source_tracking(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    item: Hermes\n",
        encoding="utf-8",
    )

    env_loader._SECRET_SOURCES["GONE_KEY"] = "onepassword"
    env_loader._SECRET_VALUES["GONE_KEY"] = "old-value"

    import agent.secret_sources.onepassword as op_module

    monkeypatch.setattr(
        op_module, "apply_onepassword_secrets",
        lambda *a, **kw: ({}, ["GONE_KEY"]),
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("GONE_KEY") is None
    assert "GONE_KEY" not in env_loader._SECRET_VALUES
