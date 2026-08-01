"""Behavioral coverage for the gateway systemd unit-name override."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway


UNIT_NAME = "hermes-gateway-worktree-docs-l2-topology-guide-personal-main"
PRIVATE_UNIT_ENV = "_HERMES_GATEWAY_SYSTEMD_UNIT"


@pytest.fixture(autouse=True)
def _isolate_gateway_config(monkeypatch, tmp_path):
    monkeypatch.delenv(PRIVATE_UNIT_ENV, raising=False)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield
    os.environ.pop(PRIVATE_UNIT_ENV, None)


def _write_gateway_config(unit_name: object) -> None:
    hermes_home = gateway.get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    value = "null" if unit_name is None else repr(unit_name)
    (hermes_home / "config.yaml").write_text(
        f"gateway:\n  systemd_unit_name: {value}\n",
        encoding="utf-8",
    )


def _patch_stable_unit_inputs(monkeypatch):
    monkeypatch.setattr(
        gateway, "get_python_path", lambda: "/opt/hermes/venv/bin/python"
    )
    monkeypatch.setattr(gateway, "_detect_venv_dir", lambda: Path("/opt/hermes/venv"))
    monkeypatch.setattr(
        gateway, "_build_service_path_dirs", lambda: ["/opt/hermes/venv/bin"]
    )
    monkeypatch.setattr(gateway.shutil, "which", lambda executable: None)
    monkeypatch.setattr(
        gateway, "_build_user_local_paths", lambda home, path_entries: []
    )
    monkeypatch.setattr(gateway, "_build_wsl_interop_paths", lambda path_entries: [])
    monkeypatch.setattr(
        gateway, "_stable_service_working_dir", lambda: "/var/lib/hermes"
    )
    monkeypatch.setattr(gateway, "_get_restart_drain_timeout", lambda: 30)
    monkeypatch.setattr(
        gateway,
        "_system_service_identity",
        lambda run_as_user=None: ("service", "service", "/home/service"),
    )
    monkeypatch.setattr(
        gateway,
        "_hermes_home_for_target_user",
        lambda home_dir: f"{home_dir}/.hermes",
    )


def _capture_systemctl(monkeypatch):
    calls: list[tuple[list[str], bool]] = []

    def fake_run(args, system=False, **kwargs):
        calls.append((list(args), system))
        if args[0] == "show" and any("FragmentPath" in arg for arg in args):
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=not-found\nFragmentPath=\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr(gateway, "_run_systemctl", fake_run)
    return calls


def _prepare_target_user_config(monkeypatch, tmp_path, unit_name=UNIT_NAME):
    root_home = tmp_path / "root"
    service_home = tmp_path / "service"
    root_hermes = root_home / ".hermes"
    service_hermes = service_home / ".hermes"
    root_hermes.mkdir(parents=True)
    service_hermes.mkdir(parents=True)
    (service_hermes / "config.yaml").write_text(
        f"gateway:\n  systemd_unit_name: {unit_name}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: root_home))
    monkeypatch.setenv("HERMES_HOME", str(root_hermes))
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        gateway,
        "_system_service_identity",
        lambda run_as_user=None: ("service", "service", str(service_home)),
    )
    monkeypatch.setattr(
        gateway,
        "_hermes_home_for_target_user",
        lambda home_dir: str(service_hermes),
    )
    return service_home, service_hermes


def _prepare_persisted_system_unit(monkeypatch, tmp_path):
    _, service_hermes = _prepare_target_user_config(monkeypatch, tmp_path)
    system_dir = tmp_path / "system"
    system_dir.mkdir()
    unit_path = system_dir / f"{UNIT_NAME}.service"
    unit_path.write_text(
        "[Service]\n"
        "ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run\n"
        f'Environment="HERMES_HOME={service_hermes}"\n'
        f'Environment="{PRIVATE_UNIT_ENV}={UNIT_NAME}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gateway,
        "_systemd_unit_directory",
        lambda system=False: system_dir,
        raising=False,
    )
    monkeypatch.setattr(
        gateway,
        "get_systemd_unit_path",
        lambda system=False, hermes_home=None, default_root=None: (
            system_dir
            / f"{gateway.get_service_name(hermes_home, default_root)}.service"
        ),
    )
    return service_hermes, unit_path


def test_default_service_name_is_unchanged_without_override(monkeypatch):
    monkeypatch.setattr(gateway, "_profile_suffix", lambda: "")

    assert gateway.get_service_name() == "hermes-gateway"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (UNIT_NAME, UNIT_NAME),
        (f"{UNIT_NAME}.service", UNIT_NAME),
        ("hermes@worker:blue", "hermes@worker:blue"),
    ],
)
def test_configured_unit_name_is_validated_and_normalized(configured, expected):
    _write_gateway_config(configured)

    assert gateway.get_service_name() == expected


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "   ",
        "/",
        "..",
        "../hermes-gateway-evil",
        "hermes/gateway/evil",
        "hermes..gateway",
        "-hermes-gateway",
        "hermes gateway",
        "hermes-gateway-\nmalicious",
        "h" * 129,
    ],
)
def test_configured_unit_name_rejects_invalid_and_traversal_like_values(configured):
    _write_gateway_config(configured)

    with pytest.raises(ValueError, match="gateway.systemd_unit_name"):
        gateway.get_service_name()


def test_override_selects_user_and_system_unit_paths():
    _write_gateway_config(f"{UNIT_NAME}.service")

    assert gateway.get_systemd_unit_path(system=False) == (
        Path.home() / ".config" / "systemd" / "user" / f"{UNIT_NAME}.service"
    )
    assert gateway.get_systemd_unit_path(system=True) == (
        Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
    )


@pytest.mark.parametrize("system", [False, True])
def test_generated_units_persist_resolved_name(monkeypatch, system):
    _patch_stable_unit_inputs(monkeypatch)
    _write_gateway_config(f"{UNIT_NAME}.service")
    if system:
        monkeypatch.setattr(
            gateway,
            "_hermes_home_for_target_user",
            lambda home_dir: str(gateway.get_hermes_home()),
        )

    unit = gateway.generate_systemd_unit(
        system=system,
        run_as_user="service" if system else None,
    )

    assert f'Environment="{PRIVATE_UNIT_ENV}={UNIT_NAME}"' in unit


def test_unit_name_expands_environment_reference(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_SYSTEMD_UNIT", UNIT_NAME)
    _write_gateway_config("${HERMES_TEST_SYSTEMD_UNIT}")

    assert gateway.get_service_name() == UNIT_NAME


def test_persisted_child_name_takes_precedence_over_changed_config(monkeypatch):
    _patch_stable_unit_inputs(monkeypatch)
    _write_gateway_config("new-config-name")
    monkeypatch.setenv(PRIVATE_UNIT_ENV, f"{UNIT_NAME}.service")

    assert gateway.get_service_name() == UNIT_NAME
    assert f'Environment="{PRIVATE_UNIT_ENV}={UNIT_NAME}"' in (
        gateway.generate_systemd_unit()
    )


@pytest.mark.parametrize("managed", [True, False])
def test_profile_cleanup_only_removes_managed_custom_unit(
    monkeypatch, tmp_path, managed
):
    import platform

    from hermes_cli import profiles

    current_name = "current-hermes-service"
    target_name = "target-hermes-service"
    current_home = tmp_path / "current" / ".hermes"
    target_home = tmp_path / "target" / ".hermes"
    user_units = tmp_path / ".config" / "systemd" / "user"
    current_home.mkdir(parents=True)
    target_home.mkdir(parents=True)
    user_units.mkdir(parents=True)
    (target_home / "config.yaml").write_text(
        f"gateway:\n  systemd_unit_name: {target_name}\n",
        encoding="utf-8",
    )
    current_unit = user_units / f"{current_name}.service"
    target_unit = user_units / f"{target_name}.service"
    current_unit.write_text("[Unit]\n", encoding="utf-8")
    target_unit.write_text(
        (
            "[Service]\n"
            "ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run\n"
            f'Environment="{PRIVATE_UNIT_ENV}={target_name}"\n'
            if managed
            else "[Service]\nExecStart=/usr/bin/unrelated-application\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HERMES_HOME", str(current_home))
    monkeypatch.setenv(PRIVATE_UNIT_ENV, current_name)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        profiles.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append(list(args)) or SimpleNamespace(returncode=0)
        ),
    )

    profiles._cleanup_gateway_service("target", target_home)

    assert current_unit.exists()
    assert target_unit.exists() is not managed
    assert (["systemctl", "--user", "stop", target_name] in calls) is managed


@pytest.mark.parametrize("managed", [True, False])
def test_discovery_only_includes_owned_custom_unit(monkeypatch, tmp_path, managed):
    _write_gateway_config("existing-application")
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    unit_path = unit_dir / "existing-application.service"
    unit_path.write_text(
        (
            "[Service]\n"
            "ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run\n"
            f'Environment="{PRIVATE_UNIT_ENV}=existing-application"\n'
            if managed
            else "[Service]\nExecStart=/usr/bin/existing-application\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gateway, "_systemd_unit_directory", lambda system=False: unit_dir
    )

    patterns = gateway._systemd_gateway_unit_patterns()

    assert ("existing-application.service" in patterns) is managed


def test_system_install_uses_target_user_override_for_path_and_enable(
    monkeypatch, tmp_path
):
    _prepare_target_user_config(monkeypatch, tmp_path)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    calls = _capture_systemctl(monkeypatch)
    resolved_paths: list[Path] = []

    def fake_unit_path(system=False, hermes_home=None, default_root=None):
        path = tmp_path / (
            f"{gateway.get_service_name(hermes_home, default_root)}.service"
        )
        resolved_paths.append(path)
        return path

    monkeypatch.setattr(gateway, "get_systemd_unit_path", fake_unit_path)
    monkeypatch.setattr(gateway, "generate_systemd_unit", lambda **kwargs: "[Unit]\n")
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *args: False)
    monkeypatch.setattr(gateway, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gateway, "print_legacy_unit_warning", lambda: None)

    gateway.systemd_install(force=True, system=True, run_as_user="service")

    assert resolved_paths[0].name == f"{UNIT_NAME}.service"
    assert (["enable", UNIT_NAME], True) in calls


def test_system_status_adopts_persisted_custom_system_unit(monkeypatch, tmp_path):
    service_hermes, _ = _prepare_persisted_system_unit(monkeypatch, tmp_path)
    calls = _capture_systemctl(monkeypatch)
    monkeypatch.setattr(gateway, "has_conflicting_systemd_units", lambda: False)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway, "systemd_unit_is_current", lambda system=False: True)

    gateway.systemd_status(system=True)

    assert (["status", UNIT_NAME, "--no-pager"], True) in calls
    assert (["is-active", UNIT_NAME], True) in calls
    assert gateway.get_hermes_home() == service_hermes


def test_system_scope_uses_sudo_user_config_when_multiple_custom_units_exist(
    monkeypatch, tmp_path
):
    import pwd

    service_hermes, unit_path = _prepare_persisted_system_unit(monkeypatch, tmp_path)
    other_name = "other-hermes-service"
    other_home = tmp_path / "other" / ".hermes"
    other_home.mkdir(parents=True)
    (unit_path.parent / f"{other_name}.service").write_text(
        "[Service]\n"
        "ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run\n"
        f'Environment="HERMES_HOME={other_home}"\n'
        f'Environment="{PRIVATE_UNIT_ENV}={other_name}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway, "_default_system_service_user", lambda: "service")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda username: SimpleNamespace(pw_dir=str(service_hermes.parent)),
    )

    assert gateway._select_systemd_scope(system=True) is True
    assert gateway.get_service_name() == UNIT_NAME
    assert gateway.get_hermes_home() == service_hermes


def test_system_uninstall_adopts_persisted_custom_system_unit(monkeypatch, tmp_path):
    _, unit_path = _prepare_persisted_system_unit(monkeypatch, tmp_path)
    calls = _capture_systemctl(monkeypatch)

    gateway.systemd_uninstall(system=True)

    assert (["stop", UNIT_NAME], True) in calls
    assert (["disable", UNIT_NAME], True) in calls
    assert not unit_path.exists()


@pytest.mark.parametrize("subcommand", ["stop", "restart"])
def test_command_dispatch_adopts_persisted_custom_system_unit(
    monkeypatch, tmp_path, subcommand
):
    _prepare_persisted_system_unit(monkeypatch, tmp_path)
    dispatched: list[tuple[str, bool, str]] = []

    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr(
        gateway,
        f"systemd_{subcommand}",
        lambda system=False: dispatched.append((
            subcommand,
            system,
            gateway.get_service_name(),
        )),
    )
    monkeypatch.setattr(
        gateway,
        "stop_profile_gateway",
        lambda: pytest.fail("fell through to manual PID stop"),
    )
    monkeypatch.setattr(
        gateway,
        "run_gateway",
        lambda **kwargs: pytest.fail("fell through to manual gateway start"),
    )

    gateway.gateway_command(
        SimpleNamespace(gateway_command=subcommand, system=True, all=False)
    )

    assert dispatched == [(subcommand, True, UNIT_NAME)]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_custom_name_refuses_to_manage_unrelated_existing_unit(
    monkeypatch, tmp_path, operation
):
    _write_gateway_config("existing-application")
    unit_path = tmp_path / "existing-application.service"
    original = "[Service]\nExecStart=/usr/bin/existing-application\n"
    unit_path.write_text(original, encoding="utf-8")
    calls = _capture_systemctl(monkeypatch)
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False, **kwargs: unit_path
    )
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *args: False)

    with pytest.raises(SystemExit) as exc_info:
        if operation == "install":
            gateway.systemd_install(force=True)
        else:
            gateway.systemd_uninstall()

    assert exc_info.value.code == 1
    assert unit_path.read_text(encoding="utf-8") == original
    assert calls == []


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("system", [False, True])
def test_custom_name_refuses_manager_visible_unit_outside_writable_path(
    monkeypatch, tmp_path, operation, system
):
    if system:
        _, service_hermes = _prepare_target_user_config(
            monkeypatch, tmp_path, "existing-application"
        )
        if operation == "uninstall":
            monkeypatch.setenv("HERMES_HOME", str(service_hermes))
    else:
        _write_gateway_config("existing-application")

    writable_path = tmp_path / "writable" / "existing-application.service"
    vendor_path = tmp_path / "vendor" / "existing-application.service"
    vendor_path.parent.mkdir()
    original = "[Service]\nExecStart=/usr/bin/existing-application\n"
    vendor_path.write_text(original, encoding="utf-8")
    calls: list[tuple[list[str], bool]] = []

    def fake_run(args, system=False, **kwargs):
        calls.append((list(args), system))
        if args[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout=f"LoadState=loaded\nFragmentPath={vendor_path}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway, "_run_systemctl", fake_run)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway, "_adopt_persisted_systemd_unit", lambda system: None)
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False, **kwargs: writable_path
    )

    with pytest.raises(SystemExit) as exc_info:
        if operation == "install":
            gateway.systemd_install(
                force=True,
                system=system,
                run_as_user="service" if system else None,
            )
        else:
            gateway.systemd_uninstall(system=system)

    assert exc_info.value.code == 1
    assert vendor_path.read_text(encoding="utf-8") == original
    assert calls == [
        (
            [
                "show",
                "existing-application",
                "--no-pager",
                "--property",
                "LoadState,FragmentPath",
            ],
            system,
        )
    ]


@pytest.mark.parametrize("system", [False, True])
@pytest.mark.parametrize(
    "probe_outcome",
    ["exception", "timeout", "nonzero", "malformed", "empty", "transient"],
)
def test_custom_name_install_refuses_failed_manager_probe(
    monkeypatch, tmp_path, system, probe_outcome
):
    if system:
        _prepare_target_user_config(monkeypatch, tmp_path, "existing-application")
    else:
        _write_gateway_config("existing-application")

    unit_path = tmp_path / "writable" / "existing-application.service"

    def fake_run(args, system=False, **kwargs):
        if args[0] != "show":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if probe_outcome == "exception":
            raise OSError("systemd manager unavailable")
        if probe_outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 10)
        if probe_outcome == "nonzero":
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")
        if probe_outcome == "malformed":
            stdout = "FragmentPath=\n"
        elif probe_outcome == "transient":
            stdout = "LoadState=loaded\nFragmentPath=\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(gateway, "_run_systemctl", fake_run)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False, **kwargs: unit_path
    )
    monkeypatch.setattr(gateway, "generate_systemd_unit", lambda **kwargs: "[Unit]\n")
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *args: False)

    with pytest.raises(SystemExit) as exc_info:
        gateway.systemd_install(
            force=True,
            system=system,
            run_as_user="service" if system else None,
        )

    assert exc_info.value.code == 1
    assert not unit_path.exists()


@pytest.mark.parametrize("system", [False, True])
def test_custom_name_install_accepts_confirmed_absent_manager_unit(
    monkeypatch, tmp_path, system
):
    if system:
        _prepare_target_user_config(monkeypatch, tmp_path, "available-application")
    else:
        _write_gateway_config("available-application")

    unit_path = tmp_path / "writable" / "available-application.service"

    def fake_run(args, system=False, **kwargs):
        if args[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="LoadState=not-found\nFragmentPath=\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway, "_run_systemctl", fake_run)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False, **kwargs: unit_path
    )
    monkeypatch.setattr(gateway, "generate_systemd_unit", lambda **kwargs: "[Unit]\n")
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *args: False)
    monkeypatch.setattr(gateway, "_ensure_linger_enabled", lambda: None)

    gateway.systemd_install(
        force=True,
        system=system,
        run_as_user="service" if system else None,
        enable_on_startup=False,
    )

    assert unit_path.read_text(encoding="utf-8") == "[Unit]\n"


def test_refresh_refuses_unrelated_custom_name_collision(monkeypatch, tmp_path):
    _write_gateway_config("existing-application")
    unit_path = tmp_path / "existing-application.service"
    original = "[Service]\nExecStart=/usr/bin/existing-application\n"
    unit_path.write_text(original, encoding="utf-8")
    calls = _capture_systemctl(monkeypatch)
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False: unit_path
    )
    monkeypatch.setattr(gateway, "systemd_unit_is_current", lambda system=False: False)
    monkeypatch.setattr(gateway, "generate_systemd_unit", lambda **kwargs: "new unit\n")

    assert gateway.refresh_systemd_unit_if_needed() is False
    assert unit_path.read_text(encoding="utf-8") == original
    assert calls == []


def test_system_install_refuses_target_user_custom_name_collision(
    monkeypatch, tmp_path
):
    _prepare_target_user_config(monkeypatch, tmp_path, "existing-application")
    unit_path = tmp_path / "existing-application.service"
    original = "[Service]\nExecStart=/usr/bin/existing-application\n"
    unit_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda **kwargs: unit_path)
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *args: False)

    with pytest.raises(SystemExit) as exc_info:
        gateway.systemd_install(force=True, system=True, run_as_user="service")

    assert exc_info.value.code == 1
    assert unit_path.read_text(encoding="utf-8") == original


def test_full_uninstall_adopts_persisted_custom_system_unit(monkeypatch, tmp_path):
    import platform

    from hermes_cli import uninstall

    _, unit_path = _prepare_persisted_system_unit(monkeypatch, tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert uninstall.uninstall_gateway_service() is True
    assert not unit_path.exists()


def test_full_uninstall_ignores_unrelated_custom_name_collision(monkeypatch, tmp_path):
    import platform

    from hermes_cli import uninstall

    _write_gateway_config("existing-application")
    unit_path = tmp_path / "existing-application.service"
    unit_path.write_text(
        "[Service]\nExecStart=/usr/bin/existing-application\n", encoding="utf-8"
    )
    missing_system_unit = tmp_path / "missing-system.service"
    calls: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
    monkeypatch.setattr(
        gateway,
        "get_systemd_unit_path",
        lambda system=False: missing_system_unit if system else unit_path,
    )
    monkeypatch.setattr(gateway, "_adopt_persisted_systemd_unit", lambda system: None)
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append(list(args)) or SimpleNamespace(returncode=0)
        ),
    )

    assert uninstall.uninstall_gateway_service() is False
    assert unit_path.exists()
    assert calls == []


@pytest.mark.parametrize("system", [False, True])
def test_refresh_preserves_persisted_name_after_config_change(
    monkeypatch, tmp_path, system
):
    _patch_stable_unit_inputs(monkeypatch)
    monkeypatch.setenv(PRIVATE_UNIT_ENV, UNIT_NAME)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: Path("/var/lib/hermes"))
    monkeypatch.setattr(
        gateway, "_hermes_home_for_target_user", lambda home: "/var/lib/hermes"
    )
    unit_path = tmp_path / f"{UNIT_NAME}.service"
    unit_path.write_text(
        "[Service]\n"
        "ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run\n"
        'Environment="HERMES_HOME=/var/lib/hermes"\n'
        f'Environment="{PRIVATE_UNIT_ENV}={UNIT_NAME}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False: unit_path
    )
    monkeypatch.setattr(gateway, "systemd_unit_is_current", lambda system=False: False)
    monkeypatch.setattr(gateway, "_run_systemctl", lambda *args, **kwargs: None)

    assert gateway.refresh_systemd_unit_if_needed(system=system) is True
    assert f'Environment="{PRIVATE_UNIT_ENV}={UNIT_NAME}"' in unit_path.read_text(
        encoding="utf-8"
    )


def test_system_unit_remains_current_after_only_configured_name_changes(
    monkeypatch, tmp_path
):
    _, service_hermes = _prepare_target_user_config(monkeypatch, tmp_path)
    _patch_stable_unit_inputs(monkeypatch)
    monkeypatch.setattr(
        gateway, "_hermes_home_for_target_user", lambda home: str(service_hermes)
    )
    unit_path = tmp_path / f"{UNIT_NAME}.service"
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False: unit_path
    )
    unit_path.write_text(
        gateway.generate_systemd_unit(system=True, run_as_user="service"),
        encoding="utf-8",
    )
    (service_hermes / "config.yaml").write_text(
        "gateway:\n  systemd_unit_name: renamed-service\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PRIVATE_UNIT_ENV, UNIT_NAME)

    assert gateway.systemd_unit_is_current(system=True) is True


def test_service_pid_discovery_returns_pid_for_custom_unit(monkeypatch):
    monkeypatch.setenv(PRIVATE_UNIT_ENV, UNIT_NAME)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)

    def fake_run(args, **kwargs):
        if "list-units" in args and "--user" in args:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{UNIT_NAME}.service loaded active running Hermes\n",
                stderr="",
            )
        if "show" in args:
            return SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    assert gateway._get_service_pids() == {4321}
