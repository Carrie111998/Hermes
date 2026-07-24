"""Tests for the out-of-tree, SDK-backed ``compresr`` plugin shim.

These pin the two things this repo is responsible for (the SDK owns the rest):

1. The shim wires Compresr's cache subdir into the generic out-of-tree cache
   surface so a recovery path is agent-visible on non-Local backends. This is
   the fix for "tool-output compression dead on Docker/Modal/SSH" that an
   out-of-tree plugin cannot achieve without the widened core, exercised
   against the REAL translator (not the SDK test suite's faked one).
2. The shim fails open when the ``compresr`` package is absent, and delegates
   to the SDK's ``register`` when present.
"""

import importlib
import sys
import types

import pytest

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


# --- 1. cache-visibility wiring (the C1 fix) --------------------------------

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
    # Must not raise even though the SDK is absent.
    shim.register(object())
    # Wiring still happened (it precedes the SDK import).
    assert any(new == "cache/compresr/tool-output" for new, _ in cf._CACHE_DIRS)


def _blocking_import(blocked_prefix, real):
    def _imp(name, *args, **kwargs):
        if name == blocked_prefix or name.startswith(blocked_prefix + "."):
            raise ImportError(f"blocked: {name}")
        return real(name, *args, **kwargs)

    return _imp
