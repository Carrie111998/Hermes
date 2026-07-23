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
    monkeypatch.setattr(
        config,
        "load_env",
        lambda: {"WHATSAPP_ENABLED": "false"},
    )
    monkeypatch.setattr(
        config,
        "read_raw_config",
        lambda: {"platforms": {"whatsapp": {"enabled": False}}},
    )
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"platforms": {"whatsapp": {"enabled": False}}},
    )

    setup.persist_whatsapp_enabled(False)

    assert writes == [
        ("yaml", "whatsapp", "enabled", False),
        ("env", "WHATSAPP_ENABLED", "false"),
    ]


def test_enable_config_failure_never_writes_true_env(monkeypatch):
    from hermes_cli import config
    from hermes_cli import whatsapp_setup as setup

    env_writes = []
    monkeypatch.setattr(
        config,
        "save_env_value",
        lambda key, value: env_writes.append((key, value)),
    )
    monkeypatch.setattr(
        config,
        "write_platform_config_field",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("config is read-only")
        ),
    )

    try:
        setup.persist_whatsapp_enabled(True)
    except RuntimeError as exc:
        assert "could not be persisted as enabled" in str(exc)
    else:
        raise AssertionError("expected activation persistence failure")

    assert ("WHATSAPP_ENABLED", "true") not in env_writes


def test_enable_env_failure_rolls_config_back_to_disabled(monkeypatch):
    from hermes_cli import config
    from hermes_cli import whatsapp_setup as setup

    config_writes = []
    env_writes = []

    monkeypatch.setattr(
        config,
        "write_platform_config_field",
        lambda platform, field, value: config_writes.append(
            (platform, field, value)
        ),
    )

    def save_env(key, value):
        env_writes.append((key, value))
        if value == "true":
            raise PermissionError("env is read-only")

    monkeypatch.setattr(config, "save_env_value", save_env)

    try:
        setup.persist_whatsapp_enabled(True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected activation persistence failure")

    assert config_writes == [
        ("whatsapp", "enabled", True),
        ("whatsapp", "enabled", False),
    ]
    assert env_writes == [
        ("WHATSAPP_ENABLED", "true"),
        ("WHATSAPP_ENABLED", "false"),
    ]


def test_persist_whatsapp_enabled_refuses_unapplied_managed_write(monkeypatch):
    from hermes_cli import config
    from hermes_cli import managed_scope
    from hermes_cli import whatsapp_setup as setup

    monkeypatch.setattr(config, "save_env_value", lambda key, value: None)
    monkeypatch.setattr(
        config,
        "write_platform_config_field",
        lambda platform, field, value: None,
    )
    monkeypatch.setattr(
        config,
        "load_env",
        lambda: {"WHATSAPP_ENABLED": "false"},
    )
    monkeypatch.setattr(
        config,
        "read_raw_config",
        lambda: {"platforms": {"whatsapp": {"enabled": False}}},
    )
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"platforms": {"whatsapp": {"enabled": True}}},
    )
    monkeypatch.setattr(
        managed_scope,
        "load_managed_env",
        lambda: {"WHATSAPP_ENABLED": "true"},
    )

    try:
        setup.persist_whatsapp_enabled(False)
    except RuntimeError as exc:
        assert "pairing was stopped before touching the session" in str(exc)
    else:
        raise AssertionError("expected managed write refusal")


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


def test_prepare_system_gateway_preflight_runs_before_disable(monkeypatch):
    from hermes_cli import whatsapp_setup as setup

    events = []
    monkeypatch.setattr(
        setup,
        "_preflight_gateway_restart",
        lambda profile: (_ for _ in ()).throw(RuntimeError("requires root")),
    )
    monkeypatch.setattr(
        setup,
        "persist_whatsapp_enabled",
        lambda enabled: events.append(("enabled", enabled)),
    )

    try:
        setup.prepare_whatsapp_pairing(profile="default")
    except RuntimeError as exc:
        assert "requires root" in str(exc)
    else:
        raise AssertionError("expected system-service preflight failure")
    assert events == []


def test_system_service_without_root_is_rejected_before_config_write(
    monkeypatch,
):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(status, "get_running_pid", lambda path=None: None)
    monkeypatch.setattr(setup, "_active_system_gateway_pid", lambda: 123)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 501)
    writes = []
    monkeypatch.setattr(
        setup,
        "persist_whatsapp_enabled",
        lambda enabled: writes.append(enabled),
    )

    try:
        setup.prepare_whatsapp_pairing(profile="default")
    except RuntimeError as exc:
        assert "sudo hermes gateway stop --system" in str(exc)
    else:
        raise AssertionError("expected system-scope authority failure")
    assert writes == []


def test_root_system_service_restart_uses_system_scope(monkeypatch):
    from gateway import status
    from hermes_cli import whatsapp_setup as setup

    captured = {}
    monkeypatch.setattr(status, "get_running_pid", lambda path=None: 123)
    monkeypatch.setattr(
        setup,
        "_raise_if_system_gateway_requires_root",
        lambda old_pid: 123,
    )
    monkeypatch.setattr(setup, "_active_system_gateway_pid", lambda: 456)

    class FakeProc:
        def poll(self):
            return None

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen)

    assert setup.restart_gateway_if_running(profile="default") is True
    assert captured["args"][-3:] == ["gateway", "restart", "--system"]


def test_default_profile_restart_command_is_explicit():
    from hermes_cli import whatsapp_setup as setup

    assert setup._gateway_restart_command("default") == [
        setup.sys.executable,
        "-m",
        "hermes_cli.main",
        "-p",
        "default",
        "gateway",
        "restart",
    ]


def test_multiplexed_secondary_profile_resolves_to_default_owner(
    monkeypatch,
    tmp_path,
):
    from gateway import status
    from gateway import config as gateway_config
    from hermes_cli import profiles
    from hermes_cli import whatsapp_setup as setup

    default_home = tmp_path / ".hermes"
    monkeypatch.setattr(
        profiles,
        "get_profile_dir",
        lambda name: default_home if name == "default" else default_home / "profiles" / name,
    )
    monkeypatch.setattr(status, "get_running_pid", lambda path=None: 123)
    monkeypatch.setattr(
        status,
        "read_runtime_status",
        lambda path=None: {"served_profiles": ["default", "work"]},
    )
    monkeypatch.setattr(
        gateway_config,
        "load_gateway_config",
        lambda: type("Config", (), {"multiplex_profiles": True})(),
    )

    assert setup.resolve_whatsapp_gateway_profile("work") == "default"


def test_stale_served_profiles_do_not_override_live_nonmultiplex_config(
    monkeypatch,
    tmp_path,
):
    from gateway import config as gateway_config
    from gateway import status
    from hermes_cli import profiles
    from hermes_cli import whatsapp_setup as setup

    default_home = tmp_path / ".hermes"
    monkeypatch.setattr(
        profiles,
        "get_profile_dir",
        lambda name: default_home if name == "default" else default_home / "profiles" / name,
    )
    monkeypatch.setattr(status, "get_running_pid", lambda path=None: 123)
    monkeypatch.setattr(
        status,
        "read_runtime_status",
        lambda path=None: {"served_profiles": ["default", "work"]},
    )
    monkeypatch.setattr(
        gateway_config,
        "load_gateway_config",
        lambda: type("Config", (), {"multiplex_profiles": False})(),
    )

    assert setup.resolve_whatsapp_gateway_profile("work") == "work"


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
