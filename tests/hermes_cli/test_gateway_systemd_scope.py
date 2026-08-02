"""Focused contract tests for YAML-scoped Hermes systemd gateway names."""

import sys

import pytest
import yaml

from hermes_cli import gateway
from hermes_cli import profiles as profiles_cli
from hermes_cli import uninstall as uninstall_cli


_MISSING = object()


def _configure_home(monkeypatch, tmp_path, *, profile=None, scope=_MISSING):
    root = tmp_path / "hermes-root"
    home = root / "profiles" / profile if profile else root
    home.mkdir(parents=True)
    config = {"gateway": {}}
    if scope is not _MISSING:
        config["gateway"]["systemd_scope"] = scope
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return root, home


def _write_unit(path, owner=_MISSING):
    path.parent.mkdir(parents=True, exist_ok=True)
    environment = "" if owner is _MISSING else f'Environment="HERMES_HOME={owner}"\n'
    path.write_text(f"[Service]\n{environment}", encoding="utf-8")


def _block(monkeypatch, target, attribute):
    calls = []
    monkeypatch.setattr(
        target, attribute, lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def _assert_owner_refusal(call, *, path=None, calls=None):
    original = path.read_bytes() if path is not None else None
    with pytest.raises(RuntimeError, match="HERMES_HOME"):
        call()
    if path is not None:
        assert path.read_bytes() == original
    if calls is not None:
        assert calls == []


@pytest.mark.parametrize(
    ("profile", "scope", "expected"),
    [
        (None, _MISSING, "hermes-gateway"),
        ("personal-main", None, "hermes-gateway-personal-main"),
        (None, "team_a", "hermes-gateway-team_a"),
        ("personal-main", "team_a", "hermes-gateway-team_a-personal-main"),
        (None, "yaml-scope", "hermes-gateway-yaml-scope"),
        (None, "a" * 128, f"hermes-gateway-{'a' * 128}"),
    ],
)
def test_service_name_composes_scope_and_profile(
    monkeypatch, tmp_path, profile, scope, expected
):
    _configure_home(monkeypatch, tmp_path, profile=profile, scope=scope)
    monkeypatch.setenv("HERMES_GATEWAY_SYSTEMD_SCOPE", "environment-scope")
    assert gateway.get_service_name() == expected


@pytest.mark.parametrize(
    "scope",
    [42, True, "", " ", "UPPER", "a.b", "../escape", "a/b", "-leading", "a" * 129],
)
def test_invalid_systemd_scope_is_rejected(scope, monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path, scope=scope)
    with pytest.raises(ValueError, match=r"gateway\.systemd_scope"):
        gateway.get_service_name()


def test_install_validates_scope_before_legacy_cleanup_or_mutation(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path, scope="../invalid")
    legacy_dir = tmp_path / "legacy-user-units"
    legacy_dir.mkdir()
    legacy_unit = legacy_dir / "hermes.service"
    legacy_unit.write_text(
        "[Service]\nExecStart=python -m hermes_cli.main gateway run\n", encoding="utf-8"
    )
    before, unit_path = legacy_unit.read_bytes(), tmp_path / "current.service"
    monkeypatch.setattr(
        gateway, "_legacy_unit_search_paths", lambda: [(False, legacy_dir)]
    )
    monkeypatch.setattr(
        gateway, "get_systemd_unit_path", lambda system=False: unit_path
    )
    calls = _block(monkeypatch, gateway, "_run_systemctl")
    with pytest.raises(ValueError, match=r"gateway\.systemd_scope"):
        gateway.systemd_install(force=True, non_interactive=True)
    assert legacy_unit.read_bytes() == before and not unit_path.exists() and calls == []


@pytest.mark.parametrize(
    "owner_case", ["matching", "mismatched", "missing", "symlink", "dangling"]
)
def test_scoped_unit_owner_uses_canonical_persisted_home(
    monkeypatch, tmp_path, owner_case
):
    if sys.platform == "win32" and owner_case == "symlink":
        pytest.skip("symlink canonicalization is POSIX-only")
    _, active_home = _configure_home(monkeypatch, tmp_path, scope="shared")
    if owner_case == "symlink":
        link_home = tmp_path / "active-link"
        link_home.symlink_to(active_home, target_is_directory=True)
        monkeypatch.setenv("HERMES_HOME", str(link_home))
    path = tmp_path / "systemd/user" / "hermes-gateway-shared.service"
    if owner_case == "dangling":
        target = tmp_path / "missing.service"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
    else:
        owner = active_home if owner_case in {"matching", "symlink"} else _MISSING
        if owner_case == "mismatched":
            owner = tmp_path / "other-root/profiles/personal-main"
        _write_unit(path, owner)
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: path)
    if owner_case == "dangling":
        calls = _block(monkeypatch, gateway, "_run_systemctl")
        with pytest.raises(RuntimeError, match="HERMES_HOME"):
            gateway.systemd_install(force=True, non_interactive=True)
        assert path.is_symlink() and not target.exists() and calls == []
    elif owner_case in {"matching", "symlink"}:
        assert gateway._assert_scoped_systemd_unit_owner() is None
    else:
        _assert_owner_refusal(gateway._assert_scoped_systemd_unit_owner, path=path)


@pytest.mark.parametrize("operation", ["refresh", "stop", "uninstall"])
def test_gateway_lifecycle_checks_scoped_owner_before_side_effects(
    monkeypatch, tmp_path, operation
):
    _configure_home(monkeypatch, tmp_path, scope="shared")
    path = tmp_path / "systemd/user" / "hermes-gateway-shared.service"
    _write_unit(path, tmp_path / "other-root/profiles/personal-main")
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: path)
    calls = _block(monkeypatch, gateway, "_run_systemctl")
    if operation == "refresh":
        monkeypatch.setattr(
            gateway,
            "systemd_unit_is_current",
            lambda system=False: pytest.fail("ownership must precede comparison"),
        )
        call = gateway.refresh_systemd_unit_if_needed
    elif operation == "stop":
        call = gateway.systemd_stop
    else:
        call = gateway.systemd_uninstall
    _assert_owner_refusal(
        call, path=path if operation == "uninstall" else None, calls=calls
    )


def test_second_root_cannot_overwrite_a_scoped_unit(monkeypatch, tmp_path):
    _, first = _configure_home(
        monkeypatch, tmp_path / "first", profile="personal-main", scope="shared"
    )
    path = tmp_path / "systemd/user" / "hermes-gateway-shared-personal-main.service"
    _write_unit(path, first)
    _, second = _configure_home(
        monkeypatch, tmp_path / "second", profile="personal-main", scope="shared"
    )
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: path)
    monkeypatch.setattr(
        gateway,
        "generate_systemd_unit",
        lambda **kwargs: '[Service]\nEnvironment="HERMES_HOME=/home/second"\n',
    )
    calls = _block(monkeypatch, gateway, "_run_systemctl")
    _assert_owner_refusal(
        lambda: gateway.systemd_install(force=True, non_interactive=True),
        path=path,
        calls=calls,
    )
    assert second != first


@pytest.mark.parametrize("cleanup", ["profile", "full"])
def test_direct_cleanup_checks_scoped_owner_before_systemctl_calls(
    monkeypatch, tmp_path, cleanup
):
    if cleanup == "profile":
        _, active = _configure_home(
            monkeypatch, tmp_path, profile="personal-main", scope="shared"
        )
        path = (
            tmp_path
            / "host-home/.config/systemd/user"
            / "hermes-gateway-shared-personal-main.service"
        )
        monkeypatch.setattr(profiles_cli.Path, "home", lambda: tmp_path / "host-home")
        monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: path)
        _write_unit(path, tmp_path / "other-root/profiles/personal-main")
        calls = _block(monkeypatch, profiles_cli.subprocess, "run")
        profiles_cli._cleanup_gateway_service("personal-main", active)
    else:
        _configure_home(monkeypatch, tmp_path, scope="shared")
        path = tmp_path / "systemd/user" / "hermes-gateway-shared.service"
        _write_unit(path, tmp_path / "other-root/profiles/personal-main")
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kwargs: [])
        monkeypatch.setattr(
            gateway, "_discover_service_pids", lambda: (set(), True)
        )
        monkeypatch.setattr(
            gateway,
            "get_systemd_unit_path",
            lambda system=False: (
                path if not system else tmp_path / "absent-system.service"
            ),
        )
        calls = _block(monkeypatch, uninstall_cli.subprocess, "run")
        assert uninstall_cli.uninstall_gateway_service() is False
    assert calls == []


def test_full_uninstall_kills_standalone_but_not_service_managed_gateway(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path, scope="shared")
    managed_pid, standalone_pid, late_managed_pid = 4101, 4102, 4103
    service_pid_checks = 0

    def _service_pids():
        nonlocal service_pid_checks
        service_pid_checks += 1
        if service_pid_checks == 1:
            return {managed_pid}, True
        if service_pid_checks == 2:
            return {late_managed_pid}, True
        return set(), True

    monkeypatch.setattr(gateway, "_discover_service_pids", _service_pids)
    monkeypatch.setattr(
        gateway,
        "_scan_gateway_pids",
        lambda *args, **kwargs: [late_managed_pid, standalone_pid],
    )
    killed = []
    monkeypatch.setattr(
        gateway,
        "terminate_pid",
        lambda pid, force=False: killed.append(pid),
    )
    monkeypatch.setenv("TERMUX_VERSION", "1")

    assert uninstall_cli.uninstall_gateway_service() is True
    assert killed == [standalone_pid]
    assert service_pid_checks == 2


def test_standalone_only_aborts_when_service_pid_discovery_times_out(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path, scope="shared")
    candidate_pid = 4201

    def _timeout(command, **kwargs):
        raise gateway.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)
    monkeypatch.setattr(gateway.subprocess, "run", _timeout)
    monkeypatch.setattr(
        gateway,
        "_scan_gateway_pids",
        lambda *args, **kwargs: [candidate_pid],
    )
    killed = []
    monkeypatch.setattr(
        gateway,
        "terminate_pid",
        lambda pid, force=False: killed.append(pid),
    )

    assert gateway._get_service_pids() == set()
    assert gateway.kill_gateway_processes(standalone_only=True) == 0
    assert killed == []


def test_full_uninstall_rejects_scoped_system_unit_before_mutation(
    monkeypatch, tmp_path
):
    _, active_home = _configure_home(monkeypatch, tmp_path, scope="shared")
    user_path = tmp_path / "absent-user.service"
    system_path = tmp_path / "system/hermes-gateway-shared.service"
    _write_unit(system_path, active_home)
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kwargs: [])
    monkeypatch.setattr(gateway, "_discover_service_pids", lambda: (set(), True))
    monkeypatch.setattr(
        gateway,
        "get_systemd_unit_path",
        lambda system=False: system_path if system else user_path,
    )
    monkeypatch.setattr(uninstall_cli.os, "geteuid", lambda: 0)
    calls = _block(monkeypatch, uninstall_cli.subprocess, "run")

    assert uninstall_cli.uninstall_gateway_service() is False
    assert system_path.exists()
    assert calls == []


def test_scoped_systemd_install_rejects_explicit_system_scope(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path, scope="shared")
    calls = _block(monkeypatch, gateway, "_run_systemctl")
    with pytest.raises(gateway.ScopedSystemdRequiresUserError, match="user-systemd"):
        gateway.systemd_install(system=True)
    assert calls == []


@pytest.mark.parametrize("selection", ["explicit", "automatic"])
def test_scoped_systemd_selector_rejects_system_scope(monkeypatch, tmp_path, selection):
    _configure_home(monkeypatch, tmp_path, scope="shared")
    system_path, user_path = tmp_path / "system.service", tmp_path / "user.service"
    if selection == "automatic":
        system_path.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: system_path if system else user_path)  # fmt: skip
    with pytest.raises(gateway.ScopedSystemdRequiresUserError, match="user-systemd"):
        gateway._select_systemd_scope(system=selection == "explicit")


def test_gateway_command_presents_scoped_systemd_error(monkeypatch, capsys):
    def fail(_args):
        raise gateway.ScopedSystemdRequiresUserError("user-systemd")

    monkeypatch.setattr(gateway, "_gateway_command_inner", fail)
    with pytest.raises(SystemExit) as exc_info:
        gateway.gateway_command(object())
    assert exc_info.value.code == 1 and "user-systemd" in capsys.readouterr().out
