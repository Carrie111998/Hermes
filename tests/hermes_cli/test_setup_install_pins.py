"""Setup-time optional installs must resolve pinned versions.

`hermes setup` installs optional SDKs (modal, daytona, neutts) and the Matrix
plugin installs mautrix. Every one of those runs on a user machine at setup
time, so an unpinned spec lets whatever the index serves at that moment become
executing code. These tests lock in:

  1. the pins come from the single source of truth, tools/lazy_deps.LAZY_DEPS;
  2. the hardcoded fallbacks (used when lazy_deps is unimportable) don't drift
     away from that table;
  3. the install still goes through hermes_cli.tools_config._pip_install, so
     the uv → pip → ensurepip fallback ladder is retained.
"""

import pytest

from hermes_cli.tools_config import _pinned_specs, _pip_install
from tools.lazy_deps import LAZY_DEPS


# ── 1. Pins resolve from LAZY_DEPS ────────────────────────────────────────────

@pytest.mark.parametrize("feature", ["terminal.modal", "terminal.daytona", "platform.matrix"])
def test_pinned_specs_reads_lazy_deps(feature):
    assert _pinned_specs(feature, ("sentinel",)) == list(LAZY_DEPS[feature])


@pytest.mark.parametrize("feature", ["terminal.modal", "terminal.daytona", "platform.matrix"])
def test_lazy_deps_entries_are_version_pinned(feature):
    """Every spec setup installs must carry an exact version."""
    for spec in LAZY_DEPS[feature]:
        assert "==" in spec, f"{feature} spec {spec!r} is not pinned"


def test_pinned_specs_falls_back_when_feature_unknown():
    assert _pinned_specs("no.such.feature", ("modal==1.3.4",)) == ["modal==1.3.4"]


def test_pinned_specs_falls_back_when_lazy_deps_unimportable(monkeypatch):
    """Stripped installs have no tools.lazy_deps — the fallback must hold."""
    import builtins

    real_import = builtins.__import__

    def _blow_up(name, *args, **kwargs):
        if name == "tools.lazy_deps":
            raise ImportError("simulated stripped install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blow_up)
    assert _pinned_specs("terminal.modal", ("modal==1.3.4",)) == ["modal==1.3.4"]


# ── 2. Hardcoded fallbacks must not drift from LAZY_DEPS ──────────────────────

def _fallback_literals(path, marker):
    """Extract the fallback tuple passed to _pinned_specs at *marker*."""
    import ast

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_pinned_specs"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == marker
        ):
            return tuple(e.value for e in node.args[1].elts)
    raise AssertionError(f"no _pinned_specs({marker!r}, ...) call found in {path}")


@pytest.mark.parametrize(
    "module_name, feature",
    [
        ("hermes_cli.setup", "terminal.modal"),
        ("hermes_cli.setup", "terminal.daytona"),
        ("plugins.platforms.matrix.adapter", "platform.matrix"),
    ],
)
def test_hardcoded_fallbacks_match_lazy_deps(module_name, feature):
    import importlib
    from pathlib import Path

    module = importlib.import_module(module_name)
    fallback = _fallback_literals(Path(module.__file__), feature)
    assert fallback == LAZY_DEPS[feature], (
        f"{module_name} fallback for {feature} drifted from LAZY_DEPS"
    )


# ── 3. neutts is bounded ──────────────────────────────────────────────────────

def test_neutts_spec_has_upper_bound():
    """neutts is installed with -U (no exact pin), so it needs a major bound."""
    from hermes_cli.setup import NEUTTS_SPEC

    assert NEUTTS_SPEC.startswith("neutts[all]")
    assert "<" in NEUTTS_SPEC, "unbounded -U install: a hostile major installs itself"


def test_neutts_install_passes_bounded_spec(monkeypatch):
    """_install_neutts_deps must hand the bounded spec to _pip_install."""
    import subprocess

    import hermes_cli.setup as setup_mod
    import hermes_cli.tools_config as tools_config

    calls = []

    def _fake_pip_install(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_mod, "_check_espeak_ng", lambda: True)
    monkeypatch.setattr(tools_config, "_pip_install", _fake_pip_install)

    assert setup_mod._install_neutts_deps() is True
    assert len(calls) == 1
    assert setup_mod.NEUTTS_SPEC in calls[0]
    assert "neutts[all]" not in calls[0], "bare unbounded spec leaked through"


# ── 4. The fallback ladder is retained ────────────────────────────────────────

def test_pip_install_is_the_shared_installer():
    """The salvaged pins ride on tools_config._pip_install, not a new installer.

    Guards against reintroducing a private installer that skips the
    uv → pip → ensurepip ladder (pip-less `uv venv` installs depend on it).
    """
    import inspect

    source = inspect.getsource(_pip_install)
    assert "ensurepip" in source
    assert "uv" in source

    for module_name in ("hermes_cli.setup", "plugins.platforms.matrix.adapter"):
        import importlib
        from pathlib import Path

        mod_source = Path(importlib.import_module(module_name).__file__).read_text(
            encoding="utf-8"
        )
        assert "_pip_install" in mod_source
