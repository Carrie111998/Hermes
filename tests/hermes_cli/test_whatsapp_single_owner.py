from __future__ import annotations


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
    monkeypatch.setattr(
        setup,
        "restart_gateway_if_running",
        lambda **kwargs: events.append(("restart", kwargs)) or True,
    )

    assert setup.prepare_whatsapp_pairing() is True
    assert events == [("enabled", False), ("restart", {"profile": None})]


def test_restart_gateway_if_running_is_noop_when_stopped(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    monkeypatch.setattr(status, "get_running_pid", lambda pid_path=None: None)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("spawned")))

    assert setup.restart_gateway_if_running() is False


def test_restart_gateway_if_running_is_detached_profile_aware_and_scrubs_gateway_marker(
    monkeypatch,
):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    captured = {}
    pids = iter([123, 123, 456])
    monkeypatch.setattr(status, "get_running_pid", lambda pid_path=None: next(pids))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    class FakeProc:
        def poll(self):
            return None

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)

    assert setup.restart_gateway_if_running(profile="Work Profile", timeout=15) is True
    assert captured["args"] == [
        setup.sys.executable,
        "-m",
        "hermes_cli.main",
        "-p",
        "work profile",
        "gateway",
        "restart",
    ]
    assert "_HERMES_GATEWAY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["HERMES_NONINTERACTIVE"] == "1"
    assert captured["kwargs"]["stdin"] is setup.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is setup.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is setup.subprocess.DEVNULL


def test_restart_gateway_checks_the_requested_profile_pid(monkeypatch, tmp_path):
    from gateway import status
    from hermes_cli import profiles
    from hermes_cli import whatsapp_setup as setup

    profile_dir = tmp_path / "profiles" / "work"
    pid_paths = []
    pids = iter([123, 456])

    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: profile_dir)
    monkeypatch.setattr(
        status,
        "get_running_pid",
        lambda pid_path=None: pid_paths.append(pid_path) or next(pids),
    )

    class FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_a, **_kw: FakeProc())

    assert setup.restart_gateway_if_running(profile="work") is True
    assert pid_paths == [
        profile_dir / "gateway.pid",
        profile_dir / "gateway.pid",
    ]


def test_restart_gateway_retries_windows_without_breakaway(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    pids = iter([123, 456])
    calls = []
    monkeypatch.setattr(status, "get_running_pid", lambda pid_path=None: next(pids))
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(
        setup,
        "windows_detach_popen_kwargs",
        lambda: {"creationflags": 99},
    )
    monkeypatch.setattr(
        setup,
        "windows_detach_flags_without_breakaway",
        lambda: 42,
    )

    class FakeProc:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise PermissionError("breakaway denied")
        return FakeProc()

    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen)

    assert setup.restart_gateway_if_running() is True
    assert calls[0]["creationflags"] == 99
    assert calls[1]["creationflags"] == 42


def test_restart_gateway_timeout_does_not_kill_detached_replacement(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    class FakeProc:
        def poll(self):
            return None

    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(status, "get_running_pid", lambda pid_path=None: 123)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_a, **_kw: FakeProc())
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)

    try:
        setup.restart_gateway_if_running(timeout=0.5)
    except RuntimeError as exc:
        assert "detached restart was left running" in str(exc)
    else:
        raise AssertionError("expected restart handoff timeout")
