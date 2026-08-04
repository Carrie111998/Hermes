"""Contract tests for explicit, effective, read-only config inspection."""

from __future__ import annotations

from pathlib import Path

import pytest


def _reset_config_caches() -> None:
    from hermes_cli import config, managed_scope

    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    managed_scope.invalidate_managed_cache()


def test_explicit_effective_loader_normalizes_legacy_root_and_expands_env(tmp_path, monkeypatch):
    """An explicit read-only path keeps behavioral config transforms intact."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "provider: ${D2_C03_EFFECTIVE_PROVIDER}\n"
        "model: claude-sonnet-4.6\n"
        "fallback_providers:\n"
        "  - provider: ${D2_C03_EFFECTIVE_FALLBACK}\n"
        "    model: claude-haiku-4.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("D2_C03_EFFECTIVE_PROVIDER", "anthropic")
    monkeypatch.setenv("D2_C03_EFFECTIVE_FALLBACK", "claude-code")
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches()
    from hermes_cli.config import load_effective_config_readonly

    config = load_effective_config_readonly(config_path)

    assert config["model"]["provider"] == "anthropic"
    assert config["fallback_providers"][0]["provider"] == "claude-code"
    assert not (home / "SOUL.md").exists()


def test_explicit_effective_loader_applies_explicit_managed_overlay(tmp_path, monkeypatch):
    """The supported managed-dir overlay wins for an explicit inspected path."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n"
        "fallback_providers:\n"
        "  - provider: claude-code\n"
        "    model: claude-haiku-4.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches()
    from hermes_cli.config import load_effective_config_readonly

    config = load_effective_config_readonly(config_path)

    assert config["model"]["provider"] == "anthropic"
    assert config["fallback_providers"][0]["provider"] == "claude-code"


def test_explicit_effective_loader_does_not_provision_missing_home(tmp_path, monkeypatch):
    """Auditing an absent config cannot create a Hermes home or seed SOUL.md."""
    home = tmp_path / "never-provisioned"
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches()
    from hermes_cli.config import load_effective_config_readonly

    config = load_effective_config_readonly(home / "config.yaml")

    assert config["model"] == ""
    assert not home.exists()


def test_explicit_effective_loader_propagates_malformed_config(tmp_path, monkeypatch):
    """Audit callers can fail closed instead of accepting runtime fallbacks."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: [anthropic\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    _reset_config_caches()
    from hermes_cli.config import load_effective_config_readonly

    with pytest.raises(Exception):
        load_effective_config_readonly(config_path)


def test_pure_provider_selection_preserves_runtime_precedence():
    """Configured provider wins; HERMES_INFERENCE_PROVIDER fills only a gap."""
    from hermes_cli.provider_selection import resolve_requested_provider_from_model_config

    assert (
        resolve_requested_provider_from_model_config(
            {"provider": "openrouter"},
            getenv=lambda _name, _default="": "anthropic",
        )
        == "openrouter"
    )
    assert (
        resolve_requested_provider_from_model_config(
            {"default": "claude-sonnet-4.6"},
            getenv=lambda _name, _default="": "claude-code",
        )
        == "claude-code"
    )


@pytest.mark.parametrize("failure", ("malformed", "unreadable", "unstatable"))
def test_explicit_effective_loader_rejects_explicit_managed_scope_failures(
    tmp_path, monkeypatch, failure
):
    """Strict audit reads cannot silently erase an explicit managed overlay."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    managed_config = managed / "config.yaml"
    if failure == "malformed":
        managed_config.write_text("model: [anthropic\n", encoding="utf-8")
    else:
        managed_config.write_text(
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches()
    from hermes_cli.config import load_effective_config_readonly

    if failure == "unreadable":
        import builtins

        real_open = builtins.open

        def deny_managed_config(path, *args, **kwargs):
            if Path(path) == managed_config:
                raise PermissionError("managed-audit-unreadable")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", deny_managed_config)
    elif failure == "unstatable":
        real_stat = Path.stat

        def deny_managed_directory(path, *args, **kwargs):
            if path == managed:
                raise PermissionError("managed-audit-unstatable")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_managed_directory)

    with pytest.raises(Exception):
        load_effective_config_readonly(config_path)


def test_explicit_effective_loader_applies_valid_audit_overlay_once(tmp_path, monkeypatch):
    """A valid strict managed overlay is read once and merged once."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches()
    from hermes_cli import managed_scope
    from hermes_cli.config import load_effective_config_readonly

    real_load = managed_scope.load_managed_config_for_audit
    calls = 0

    def counted_load():
        nonlocal calls
        calls += 1
        return real_load()

    monkeypatch.setattr(managed_scope, "load_managed_config_for_audit", counted_load)

    config = load_effective_config_readonly(config_path)

    assert config["model"]["provider"] == "anthropic"
    assert calls == 1


def test_normal_runtime_loader_keeps_managed_scope_fail_open(tmp_path, monkeypatch):
    """The audit-only strict path must not alter normal startup behavior."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: [anthropic\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _reset_config_caches()
    from hermes_cli.config import load_config

    config = load_config()

    assert config["model"]["provider"] == "openrouter"
