from __future__ import annotations

from types import SimpleNamespace


def test_persist_whatsapp_enabled_updates_env_and_yaml(monkeypatch):
    from hermes_cli import config
    from hermes_cli import whatsapp_setup as setup

    writes: list[tuple] = []
    monkeypatch.setattr(config, "save_env_value", lambda key, value: writes.append(("env", key, value)))
    monkeypatch.setattr(
        config,
        "write_platform_config_field",
        lambda platform, field, value: writes.append(("yaml", platform, field, value)),
    )

    setup.persist_whatsapp_enabled(False)

    assert writes == [
        ("env", "WHATSAPP_ENABLED", "false"),
        ("yaml", "whatsapp", "enabled", False),
    ]


def test_prepare_disables_before_gateway_restart(monkeypatch):
    from hermes_cli import whatsapp_setup as setup

    events: list[object] = []
    monkeypatch.setattr(setup, "persist_whatsapp_enabled", lambda enabled: events.append(("enabled", enabled)))
    monkeypatch.setattr(setup, "restart_gateway_if_running", lambda: events.append("restart") or True)

    assert setup.prepare_whatsapp_pairing() is True
    assert events == [("enabled", False), "restart"]


def test_restart_gateway_if_running_is_noop_when_stopped(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    monkeypatch.setattr(status, "get_running_pid", lambda: None)
    monkeypatch.setattr(setup.subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("spawned")))

    assert setup.restart_gateway_if_running() is False


def test_restart_gateway_if_running_uses_current_python(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    captured = {}
    monkeypatch.setattr(status, "get_running_pid", lambda: 123)

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="restarted")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    assert setup.restart_gateway_if_running(timeout=15) is True
    assert captured["args"] == [
        setup.sys.executable,
        "-m",
        "hermes_cli.main",
        "gateway",
        "restart",
    ]
    assert captured["kwargs"]["timeout"] == 15
