"""Tests for service-singleton lifecycle: atexit handler, idempotent shutdown.

These cover the exit-cleanup behavior added to plug the language-server
process leak — without the atexit hook, ``hermes chat`` exits while
pyright/gopls/etc. are still alive on the host.
"""
from __future__ import annotations

import atexit
from unittest.mock import MagicMock

import pytest

from agent import lsp as lsp_module


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Force a clean module state before each test.

    Tests in this file share process-global state (the lazy
    singleton + atexit registration flag); reset both before and
    after every test so order doesn't matter.
    """
    lsp_module._service = None
    lsp_module._service_scope = None
    lsp_module._services.clear()
    lsp_module._atexit_registered = False
    yield
    lsp_module._service = None
    lsp_module._service_scope = None
    lsp_module._services.clear()
    lsp_module._atexit_registered = False


def test_get_service_registers_atexit_handler_once(monkeypatch):
    """First call to ``get_service`` must register an atexit handler;
    subsequent calls must NOT register another one (Python's ``atexit``
    runs every registered callable, so a duplicate would shutdown
    twice — harmless but wasteful)."""
    fake_svc = MagicMock()
    fake_svc.is_active.return_value = True
    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(lambda cls: fake_svc)
    )

    registrations = []

    def fake_register(fn):
        registrations.append(fn)

    monkeypatch.setattr(atexit, "register", fake_register)

    a = lsp_module.get_service()
    b = lsp_module.get_service()
    c = lsp_module.get_service()

    assert a is fake_svc
    assert b is fake_svc
    assert c is fake_svc
    assert len(registrations) == 1
    # The registered callable must be our internal shutdown wrapper.
    assert registrations[0] is lsp_module._atexit_shutdown




def test_atexit_shutdown_swallows_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("server already dead")

    monkeypatch.setattr(lsp_module, "_shutdown_all_services", boom)
    # Must not raise.
    lsp_module._atexit_shutdown()


def test_shutdown_service_idempotent(monkeypatch):
    """Calling shutdown twice must be safe — first call cleans up,
    second call no-ops (nothing to shut down)."""
    fake_svc = MagicMock()
    fake_svc.is_active.return_value = True
    fake_svc.shutdown = MagicMock()
    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(lambda cls: fake_svc)
    )
    monkeypatch.setattr(atexit, "register", lambda fn: None)

    lsp_module.get_service()
    lsp_module.shutdown_service()
    lsp_module.shutdown_service()  # must not raise

    assert fake_svc.shutdown.call_count == 1


def test_shutdown_service_only_stops_active_profile(monkeypatch, tmp_path):
    """An explicit profile restart must not stop another profile's service."""
    services: list[MagicMock] = []

    def make_service(cls):
        fake = MagicMock()
        fake.is_active.return_value = True
        services.append(fake)
        return fake

    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(make_service)
    )
    monkeypatch.setattr(atexit, "register", lambda fn: None)
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    service_a = lsp_module.get_service()
    scope_a = lsp_module._current_scope()
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    service_b = lsp_module.get_service()
    scope_b = lsp_module._current_scope()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    lsp_module.shutdown_service()

    services[0].shutdown.assert_called_once()
    services[1].shutdown.assert_not_called()
    assert service_a is not None
    assert service_b is not None
    assert scope_a not in lsp_module._services
    assert lsp_module._services[scope_b] is service_b
    assert len(services) == 2


def test_get_service_isolated_by_hermes_home(monkeypatch, tmp_path):
    """Profile-scoped service accessors must not share client state."""
    services = []

    def make_service(cls):
        fake = MagicMock()
        fake.is_active.return_value = True
        services.append(fake)
        return fake

    monkeypatch.setattr(
        lsp_module.LSPService, "create_from_config", classmethod(make_service)
    )
    monkeypatch.setattr(atexit, "register", lambda fn: None)
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    service_a = lsp_module.get_service()
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    service_b = lsp_module.get_service()

    assert service_a is not service_b
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert lsp_module.get_service() is service_a
    assert len(services) == 2








