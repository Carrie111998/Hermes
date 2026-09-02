"""Tests for `hermes memory setup` provider discovery failure reporting.

`_scan_providers` used to collapse every failure into an empty list, so a
broken `plugins.memory` import or a provider whose load raised was reported
as "No memory provider plugins detected" — advising a reinstall of plugins
that were already installed and discovered. These tests pin the three
outcomes the wizard must distinguish: discovery crashed, providers dropped
at load, and genuinely nothing installed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hermes_cli.memory_setup as memory_setup
from hermes_cli.memory_setup import _CANCELLED, _curses_select


class FakeProvider:
    def __init__(self):
        self.save_config = MagicMock()

    def get_config_schema(self):
        return [{
            "key": "mode",
            "description": "Mode",
            "default": "one",
            "choices": ["one", "two"],
        }]


def test_cmd_setup_generic_choice_cancel_writes_nothing(tmp_path, monkeypatch):
    provider = FakeProvider()
    selections = iter([0, _CANCELLED])
    save_config = MagicMock()
    install_dependencies = MagicMock()

    monkeypatch.setattr(memory_setup, "_scan_providers", lambda: ([("fake", "local", provider)], []))
    monkeypatch.setattr(memory_setup, "_curses_select", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(memory_setup, "_install_dependencies", install_dependencies)
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {}})
    monkeypatch.setattr("hermes_cli.config.save_config", save_config)

    memory_setup.cmd_setup(SimpleNamespace())

    install_dependencies.assert_called_once_with("fake")
    save_config.assert_not_called()
    provider.save_config.assert_not_called()
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Discovery failure reporting
# ---------------------------------------------------------------------------


def test_cmd_setup_reports_discovery_crash_instead_of_empty(capsys, monkeypatch):
    """A plugins.memory import failure must surface the real error, not the
    install-a-plugin advice — the plugins are installed and discovered."""
    def boom():
        raise ModuleNotFoundError("No module named 'yaml'")

    monkeypatch.setattr(memory_setup, "_scan_providers", boom)

    memory_setup.cmd_setup(SimpleNamespace())

    out = capsys.readouterr().out
    assert "Failed to discover memory provider plugins" in out
    assert "No module named 'yaml'" in out
    assert "Install a plugin" not in out


def test_cmd_setup_reports_dropped_providers_when_none_load(capsys, monkeypatch):
    """Every discovered provider failing to load is a broken-install report
    naming each failure, not "no plugins detected"."""
    monkeypatch.setattr(
        memory_setup,
        "_scan_providers",
        lambda: ([], ["mem0: cannot import name 'X'", "honcho: boom"]),
    )

    memory_setup.cmd_setup(SimpleNamespace())

    out = capsys.readouterr().out
    assert "No memory provider plugins could be loaded" in out
    assert "mem0: cannot import name 'X'" in out
    assert "honcho: boom" in out
    assert "Install a plugin" not in out


def test_cmd_setup_lists_skipped_providers_alongside_picker(capsys, monkeypatch):
    """A partial failure must not hide the dropped provider from the user
    even when others load fine."""
    provider = FakeProvider()
    monkeypatch.setattr(memory_setup, "_scan_providers", lambda: ([("fake", "local", provider)], ["mem0: boom"]))
    monkeypatch.setattr(memory_setup, "_curses_select", lambda *a, **k: _CANCELLED)

    memory_setup.cmd_setup(SimpleNamespace())

    out = capsys.readouterr().out
    assert "1 installed provider(s) could not be loaded" in out
    assert "mem0: boom" in out
    assert "Cancelled" in out


def test_cmd_setup_provider_reports_discovery_crash(capsys, monkeypatch):
    """`hermes memory setup <name>` hits the same wall and must not claim
    the provider is merely not found."""
    def boom():
        raise ImportError("cannot import name 'X' from 'some.dep'")

    monkeypatch.setattr(memory_setup, "_scan_providers", boom)

    memory_setup.cmd_setup_provider("mem0")

    out = capsys.readouterr().out
    assert "Failed to discover memory provider plugins" in out
    assert "cannot import name 'X'" in out
    assert "not found" not in out


def test_cmd_setup_provider_names_dropped_provider(capsys, monkeypatch):
    """When the requested provider was discovered but its load failed, the
    not-found message must point at the load failure, not at the picker."""
    monkeypatch.setattr(
        memory_setup,
        "_scan_providers",
        lambda: ([("other", "local", FakeProvider())], ["mem0: cannot import name 'X'"]),
    )

    memory_setup.cmd_setup_provider("mem0")

    out = capsys.readouterr().out
    assert "Memory provider 'mem0' not found" in out
    assert "could not be loaded" in out
    assert "mem0: cannot import name 'X'" in out


def test_scan_providers_collects_load_failure_reasons(monkeypatch):
    """The scan itself records why each provider was dropped and keeps going."""
    import plugins.memory as pm

    good = FakeProvider()

    def fake_load(name):
        if name == "broken":
            raise ImportError("cannot import name 'X' from 'some.dep'")
        if name == "empty":
            return None
        return good

    monkeypatch.setattr(pm, "discover_memory_providers", lambda: [
        ("good", "a good provider", True),
        ("broken", "raises on load", True),
        ("empty", "module exposes no instance", True),
    ], raising=False)
    monkeypatch.setattr(pm, "load_memory_provider", fake_load, raising=False)

    providers, skipped = memory_setup._scan_providers()

    assert [name for name, _, _ in providers] == ["good"]
    assert skipped == [
        "broken: cannot import name 'X' from 'some.dep'",
        "empty: provider module did not expose an instance",
    ]


def test_scan_providers_propagates_discovery_crash(monkeypatch):
    """An import failure of plugins.memory itself must propagate so callers
    can report it — it is not "no plugins detected"."""
    import builtins
    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "plugins.memory":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)

    with pytest.raises(ModuleNotFoundError):
        memory_setup._scan_providers()


# ---------------------------------------------------------------------------
# _provider_pip_dependencies — mode-aware dep expansion (#70636)
# ---------------------------------------------------------------------------


def test_install_dependencies_force_reinstalls_versioned_specs(tmp_path, monkeypatch):
    """force=True hands every declared spec (version ranges intact) to pip,
    so a downgraded/stripped bridge package is restored on hermes update."""
    import yaml as _yaml

    plugin_dir = tmp_path / "mem0"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["mem0ai>=2.0.10,<3"]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )

    installed = []

    def fake_install_specs(specs, timeout=120):
        installed.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    memory_setup._install_dependencies("mem0", force=True)

    assert installed, "force=True must reach the install step"
    assert any("mem0ai>=2.0.10,<3" in specs for specs in installed)


def test_cmd_status_memory_tool_gate_disabled(capsys, monkeypatch):
    """When both memory stores are disabled, Memory status reports memory tool as disabled."""
    _cfg = {"memory": {"memory_enabled": False, "user_profile_enabled": False}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _cfg)
    # check_memory_requirements() reads the readonly loader, not load_config.
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: _cfg, raising=False
    )
    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [])

    memory_setup.cmd_status(SimpleNamespace())

    captured = capsys.readouterr().out
    assert "Memory tool:        disabled ✗" in captured
    assert "Memory injection:   disabled ✗" in captured
    assert "User profile:       disabled ✗" in captured


def test_cmd_status_memory_tool_gate_enabled(capsys, monkeypatch):
    """When at least one memory store is enabled, Memory status reports memory tool as enabled."""
    _cfg = {"memory": {"memory_enabled": True, "user_profile_enabled": False}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _cfg)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: _cfg, raising=False
    )
    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [])

    memory_setup.cmd_status(SimpleNamespace())

    captured = capsys.readouterr().out
    assert "Memory tool:        enabled ✓" in captured
    assert "Memory injection:   enabled ✓" in captured
    assert "User profile:       disabled ✗" in captured

