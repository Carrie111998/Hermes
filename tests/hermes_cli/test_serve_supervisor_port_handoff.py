"""Supervisor-owned fixed-port Hermes backend handoff contracts."""

from unittest.mock import patch


def test_launchd_waits_for_existing_hermes_backend(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.dashboard")
    conflicts = iter((True, False))
    monkeypatch.setattr(web_server, "_port_bind_conflict", lambda *_: next(conflicts))
    monkeypatch.setattr(web_server, "_probe_hermes_backend", lambda *_: True)

    with patch("time.sleep") as sleep:
        assert web_server._wait_for_supervised_backend_holder("127.0.0.1", 9119)

    assert sleep.call_count == 1


def test_manual_or_foreign_port_holder_is_not_adopted(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(web_server, "_probe_hermes_backend", lambda *_: True)
    assert not web_server._wait_for_supervised_backend_holder("127.0.0.1", 9119)

    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.dashboard")
    monkeypatch.setattr(web_server, "_probe_hermes_backend", lambda *_: False)
    assert not web_server._wait_for_supervised_backend_holder("127.0.0.1", 9119)
