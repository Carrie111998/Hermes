"""Tests for the optional truststore OS-trust-store injection in
``agent.process_bootstrap``.

The injection makes Python's ``ssl`` module consult the OS trust store
(Windows cert store, macOS keychain) on top of the bundled certifi bundle,
so corporate / MDM-deployed root CAs are honoured on TLS-inspecting proxy
networks.  It is opt-in via the optional ``truststore`` extra and must
silently no-op whenever the package is absent, the platform is Linux, or the
operator sets the ``HERMES_DISABLE_TRUSTSTORE`` escape hatch.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


def _reload_process_bootstrap():
    """Force a fresh import so the module-level ``_maybe_inject_truststore()``
    call re-runs under the current monkeypatch state.
    """
    sys.modules.pop("agent.process_bootstrap", None)
    return importlib.import_module("agent.process_bootstrap")


def test_truststore_not_installed_silent_noop(monkeypatch):
    """When the ``truststore`` package is absent, import still succeeds and
    ``_TRUSTSTORE_INJECTED`` stays False.  No exception, no warning.
    """
    real_import = builtins.__import__

    def _block_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_truststore)
    monkeypatch.delenv("HERMES_DISABLE_TRUSTSTORE", raising=False)
    # Force win32 path even on Linux CI so the platform gate opens.
    monkeypatch.setattr(sys, "platform", "win32")

    mod = _reload_process_bootstrap()
    assert mod._TRUSTSTORE_INJECTED is False


def test_truststore_disabled_env_var_skips_injection(monkeypatch):
    """``HERMES_DISABLE_TRUSTSTORE=1`` must short-circuit before importing
    truststore, even on Windows / macOS.
    """
    monkeypatch.setenv("HERMES_DISABLE_TRUSTSTORE", "1")
    monkeypatch.setattr(sys, "platform", "win32")

    called = {"import": False}
    real_import = builtins.__import__

    def _spy_import(name, *args, **kwargs):
        if name == "truststore":
            called["import"] = True
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _spy_import)
    mod = _reload_process_bootstrap()

    assert mod._TRUSTSTORE_INJECTED is False
    assert called["import"] is False, "truststore must not be imported when disabled"


@pytest.mark.parametrize("bad_value", ["1", "true", "yes", "on", "TRUE"])
def test_truststore_disabled_env_var_variants(monkeypatch, bad_value):
    """All documented truthy values for the escape hatch must be honoured."""
    monkeypatch.setenv("HERMES_DISABLE_TRUSTSTORE", bad_value)
    monkeypatch.setattr(sys, "platform", "win32")
    mod = _reload_process_bootstrap()
    assert mod._TRUSTSTORE_INJECTED is False


def test_truststore_skipped_on_linux(monkeypatch):
    """On Linux the OS trust store is already wired via ca-certificates.crt
    and certifi; truststore is a no-op there, so we skip the import entirely.
    """
    monkeypatch.delenv("HERMES_DISABLE_TRUSTSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    called = {"import": False}
    real_import = builtins.__import__

    def _spy_import(name, *args, **kwargs):
        if name == "truststore":
            called["import"] = True
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _spy_import)
    mod = _reload_process_bootstrap()

    assert mod._TRUSTSTORE_INJECTED is False
    assert called["import"] is False, "truststore must not be imported on Linux"


def test_truststore_injection_success_marks_injected(monkeypatch):
    """When ``truststore`` is importable and inject_into_ssl() succeeds, the
    ``_TRUSTSTORE_INJECTED`` flag flips to True.
    """
    monkeypatch.delenv("HERMES_DISABLE_TRUSTSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    injected = {"called": False}

    class _FakeTruststoreModule:
        @staticmethod
        def inject_into_ssl():
            injected["called"] = True

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _FakeTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    mod = _reload_process_bootstrap()

    assert mod._TRUSTSTORE_INJECTED is True
    assert injected["called"] is True


def test_truststore_injection_failure_silent_noop(monkeypatch):
    """If ``inject_into_ssl()`` raises (e.g. truststore installed but the OS
    store is inaccessible), the bootstrap must swallow it and leave the
    certifi default in place.  ssl_guard surfaces a clear error later.
    """
    monkeypatch.delenv("HERMES_DISABLE_TRUSTSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    class _FailingTruststoreModule:
        @staticmethod
        def inject_into_ssl():
            raise RuntimeError("OS trust store unavailable")

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _FailingTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    mod = _reload_process_bootstrap()

    assert mod._TRUSTSTORE_INJECTED is False


def test_maybe_inject_truststore_is_idempotent(monkeypatch):
    """A second call to ``_maybe_inject_truststore()`` after a successful
    injection must not re-import / re-inject.
    """
    monkeypatch.delenv("HERMES_DISABLE_TRUSTSTORE", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    inject_count = {"n": 0}

    class _CountingTruststoreModule:
        @staticmethod
        def inject_into_ssl():
            inject_count["n"] += 1

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _CountingTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    mod = _reload_process_bootstrap()
    assert inject_count["n"] == 1

    # Second call — should be a no-op.
    mod._maybe_inject_truststore()
    assert inject_count["n"] == 1, "inject_into_ssl must not run twice"
