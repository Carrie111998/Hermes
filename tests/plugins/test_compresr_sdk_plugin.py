"""Tests for the ``compresr`` plugin shim — the two things this repo owns
(the SDK owns the rest): cache-subdir wiring, exercised against the real
translator, and fail-open/delegation around the optional SDK import.
"""

import importlib
import sys
import types

import pytest

import hermes_cli.plugins_cmd as pc
import tools.credential_files as cf


@pytest.fixture
def isolated_cache_dirs():
    """Snapshot/restore the module-global cache registry around a test."""
    saved = list(cf._CACHE_DIRS)
    try:
        yield
    finally:
        cf._CACHE_DIRS[:] = saved


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point cache-dir resolution at a temp HERMES_HOME with the compresr dir."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    cache_dir = tmp_path / "cache" / "compresr" / "tool-output"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path, cache_dir


def _load_shim():
    """Import the plugin shim fresh (it's a plain module with register())."""
    sys.modules.pop("plugins.compresr", None)
    return importlib.import_module("plugins.compresr")


# --- 1. cache-visibility wiring ---------------------------------------------

def test_translator_returns_none_without_registration(isolated_cache_dirs, hermes_home):
    """Counterfactual: with no registration the compresr cache is invisible."""
    _home, cache_dir = hermes_home
    host_file = str(cache_dir / "abc123.txt")
    assert cf.map_cache_path_to_container(host_file, "/root/.hermes") is None


def test_register_cache_dir_makes_path_visible(isolated_cache_dirs, hermes_home):
    """After register_cache_dir the SDK's cache path translates on Docker."""
    _home, cache_dir = hermes_home
    cf.register_cache_dir("cache/compresr/tool-output")

    assert any(
        new == "cache/compresr/tool-output" for new, _old in cf._CACHE_DIRS
    )
    mounts = cf.get_cache_directory_mounts("/root/.hermes")
    assert any(
        m["container_path"] == "/root/.hermes/cache/compresr/tool-output"
        for m in mounts
    )
    host_file = str(cache_dir / "abc123.txt")
    translated = cf.map_cache_path_to_container(host_file, "/root/.hermes")
    assert translated == "/root/.hermes/cache/compresr/tool-output/abc123.txt"


def test_shim_register_wires_cache_dir(isolated_cache_dirs, hermes_home, monkeypatch):
    """The shim's register() performs the cache-dir wiring itself."""
    # Provide a fake `compresr` so delegation doesn't error; we only assert wiring.
    delegated = {}
    fake_pkg = types.ModuleType("compresr")
    fake_hermes = types.ModuleType("compresr.integrations.hermes")
    fake_plugin = types.ModuleType("compresr.integrations.hermes.plugin")
    fake_plugin.register = lambda ctx: delegated.setdefault("ctx", ctx)
    fake_integrations = types.ModuleType("compresr.integrations")
    monkeypatch.setitem(sys.modules, "compresr", fake_pkg)
    monkeypatch.setitem(sys.modules, "compresr.integrations", fake_integrations)
    monkeypatch.setitem(sys.modules, "compresr.integrations.hermes", fake_hermes)
    monkeypatch.setitem(sys.modules, "compresr.integrations.hermes.plugin", fake_plugin)

    shim = _load_shim()
    ctx = object()
    shim.register(ctx)

    assert any(new == "cache/compresr/tool-output" for new, _ in cf._CACHE_DIRS)
    assert delegated.get("ctx") is ctx  # delegated to the SDK register


# --- 2. fail-open / delegation ---------------------------------------------

def test_shim_fails_open_without_sdk(isolated_cache_dirs, hermes_home, monkeypatch, caplog):
    """No `compresr` package installed -> register() returns without raising."""
    for name in list(sys.modules):
        if name == "compresr" or name.startswith("compresr."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    # Block importability of `compresr`.
    monkeypatch.setattr(
        "builtins.__import__",
        _blocking_import("compresr", real=__import__),
    )
    shim = _load_shim()
    shim.register(object())
    # Wiring still happened (it precedes the SDK import).
    assert any(new == "cache/compresr/tool-output" for new, _ in cf._CACHE_DIRS)


def _blocking_import(blocked_prefix, real):
    def _imp(name, *args, **kwargs):
        if name == blocked_prefix or name.startswith(blocked_prefix + "."):
            raise ImportError(f"blocked: {name}")
        return real(name, *args, **kwargs)

    return _imp


# ---------------------------------------------------------------------------
# requires_env resolution + prompt-on-enable (hermes_cli.plugins_cmd)
# ---------------------------------------------------------------------------


def test_plugin_requires_env_from_entrypoint_module(monkeypatch):
    """Entrypoint plugins expose requires_env via a module-level REQUIRES_ENV."""
    mod = types.ModuleType("fake_ep_plugin")
    mod.REQUIRES_ENV = [{"name": "FAKE_KEY", "secret": True}]
    monkeypatch.setitem(sys.modules, "fake_ep_plugin", mod)
    assert pc._plugin_requires_env("entrypoint", "fake_ep_plugin") == [
        {"name": "FAKE_KEY", "secret": True}
    ]


def test_plugin_requires_env_from_bundled_manifest(tmp_path):
    """Bundled/user plugins read requires_env from their plugin.yaml."""
    (tmp_path / "plugin.yaml").write_text(
        "name: x\nrequires_env:\n  - name: X_KEY\n    secret: true\n"
    )
    assert pc._plugin_requires_env("user", str(tmp_path)) == [
        {"name": "X_KEY", "secret": True}
    ]


def test_plugin_requires_env_swallows_errors():
    """A bad/empty locator yields [] rather than raising."""
    assert pc._plugin_requires_env("entrypoint", "no.such.module.exists") == []
    assert pc._plugin_requires_env("entrypoint", None) == []


def test_prompt_requires_env_for_key_forwards_resolved_env(monkeypatch):
    """Resolves a plugin's requires_env by key and forwards it to the prompt."""
    monkeypatch.setattr(
        pc,
        "_discover_all_plugins",
        lambda: [("compresr", "1", "d", "entrypoint", "fake_ep2", "compresr")],
    )
    mod = types.ModuleType("fake_ep2")
    mod.REQUIRES_ENV = [{"name": "COMPRESR_API_KEY", "secret": True}]
    monkeypatch.setitem(sys.modules, "fake_ep2", mod)
    seen = {}
    monkeypatch.setattr(
        pc, "_prompt_plugin_env_vars", lambda manifest, console: seen.update(manifest)
    )
    pc._prompt_requires_env_for_key("compresr", console=None)
    assert seen.get("name") == "compresr"
    assert seen.get("requires_env") == [{"name": "COMPRESR_API_KEY", "secret": True}]


def test_prompt_requires_env_for_key_no_env_no_prompt(monkeypatch):
    """A plugin with no requires_env triggers no prompt call."""
    monkeypatch.setattr(
        pc,
        "_discover_all_plugins",
        lambda: [("plain", "1", "d", "bundled", None, "plain")],
    )
    called = []
    monkeypatch.setattr(
        pc, "_prompt_plugin_env_vars", lambda *a, **k: called.append(1)
    )
    pc._prompt_requires_env_for_key("plain", console=None)
    assert called == []


def test_prompt_requires_env_for_key_swallows_errors(monkeypatch):
    """A failure while resolving requires_env must never propagate."""

    def _boom():
        raise RuntimeError("discovery blew up")

    monkeypatch.setattr(pc, "_discover_all_plugins", _boom)
    pc._prompt_requires_env_for_key("compresr", console=None)  # must not raise
