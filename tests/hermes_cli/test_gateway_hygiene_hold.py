from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway_hygiene as hygiene_mod
from gateway import status
from hermes_cli import gateway as gateway_cli
from hermes_cli.gateway_hygiene import (
    harden_hygiene_unit_definition,
    harden_hygiene_watchdog_script,
    migrate_gateway_hygiene_hold_support,
    rollback_gateway_hygiene_migration,
)
from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE


LIVE_UNIT_FIXTURE = """[Unit]
Description=Hermes Gateway Hygiene Watchdog
After=hermes-gateway.service
Wants=hermes-gateway.service network-online.target
Requires=hermes-gateway.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/ed/.hermes/scripts/gateway-hygiene-watchdog.py
"""


def _watchdog_fixture(home: Path) -> str:
    return f'''from pathlib import Path
import argparse

HOME = Path({str(home)!r})
SERVICE = "hermes-gateway.service"
CALLED = False

def pending_restart_unit():
    return False

def schedule_restart(reason: str, dry_run: bool) -> tuple[bool, str]:
    global CALLED
    CALLED = True
    return True, "scheduled"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    schedule_restart("inactive", args.dry_run)
    return 0
'''


def test_unit_migration_removes_only_implicit_gateway_start_edges():
    hardened = harden_hygiene_unit_definition(LIVE_UNIT_FIXTURE)

    assert "After=hermes-gateway.service" in hardened
    assert "Wants=network-online.target" in hardened
    assert "Wants=hermes-gateway.service" not in hardened
    assert "Requires=hermes-gateway.service" not in hardened
    assert "ExecStart=" in hardened


def test_hardened_watchdog_exits_and_cannot_schedule_when_owner_hold_exists(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".gateway-owner-hold.json").write_text(
        '{"schema_version": 1, "state": "held"}\n',
        encoding="utf-8",
    )
    hardened = harden_hygiene_watchdog_script(_watchdog_fixture(tmp_path))
    compile(hardened, "gateway-hygiene-watchdog.py", "exec")
    namespace = {"__name__": "watchdog_fixture"}
    exec(hardened, namespace)

    monkeypatch.setattr(sys, "argv", ["gateway-hygiene-watchdog.py"])
    assert namespace["main"]() == 0
    assert namespace["CALLED"] is False
    assert namespace["schedule_restart"]("inactive", False) == (
        False,
        "owner hold active; restart suppressed",
    )
    assert namespace["CALLED"] is False


def test_migration_backs_up_readbacks_is_idempotent_and_rolls_back(tmp_path):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    original_script = _watchdog_fixture(tmp_path)
    script.write_text(original_script, encoding="utf-8")
    reloads: list[str] = []

    receipt = migrate_gateway_hygiene_hold_support(
        unit_path=unit,
        script_path=script,
        backup_root=backups,
        daemon_reload=lambda: reloads.append("reload"),
    )

    assert receipt.changed_paths == (unit, script)
    assert reloads == ["reload"]
    assert (backups / f"original-{unit.name}").read_text(
        encoding="utf-8"
    ) == LIVE_UNIT_FIXTURE
    assert (backups / f"original-{script.name}").read_text(
        encoding="utf-8"
    ) == original_script
    assert receipt.before_sha256[str(unit)] != receipt.after_sha256[str(unit)]
    assert receipt.before_sha256[str(script)] != receipt.after_sha256[str(script)]

    second = migrate_gateway_hygiene_hold_support(
        unit_path=unit,
        script_path=script,
        backup_root=backups,
        daemon_reload=lambda: reloads.append("unexpected"),
    )
    assert second.changed_paths == ()
    assert reloads == ["reload"]

    restored = rollback_gateway_hygiene_migration(
        unit_path=unit,
        script_path=script,
        backup_root=backups,
        receipt=receipt,
        daemon_reload=lambda: reloads.append("rollback"),
    )
    assert restored == (unit, script)
    assert unit.read_text(encoding="utf-8") == LIVE_UNIT_FIXTURE
    assert script.read_text(encoding="utf-8") == original_script
    assert reloads == ["reload", "rollback"]


def test_migration_precomputes_all_transforms_before_first_write(tmp_path):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    script.write_text("print('missing watchdog anchors')\n", encoding="utf-8")
    original_unit = unit.read_bytes()
    original_script = script.read_bytes()

    with pytest.raises(ValueError, match="SERVICE declaration"):
        migrate_gateway_hygiene_hold_support(
            unit_path=unit,
            script_path=script,
            backup_root=backups,
        )

    assert unit.read_bytes() == original_unit
    assert script.read_bytes() == original_script
    assert not backups.exists()


def test_migration_write_failure_compensates_every_changed_file(
    tmp_path,
    monkeypatch,
):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    script.write_text(_watchdog_fixture(tmp_path), encoding="utf-8")
    original_unit = unit.read_bytes()
    original_script = script.read_bytes()
    original_replace = hygiene_mod._atomic_replace_text

    def fail_script_replace(path: Path, content: str) -> None:
        if path == script:
            raise OSError("injected script replace failure")
        original_replace(path, content)

    monkeypatch.setattr(
        hygiene_mod,
        "_atomic_replace_text",
        fail_script_replace,
    )

    with pytest.raises(OSError, match="injected script replace failure"):
        migrate_gateway_hygiene_hold_support(
            unit_path=unit,
            script_path=script,
            backup_root=backups,
        )

    assert unit.read_bytes() == original_unit
    assert script.read_bytes() == original_script


def test_migration_reload_failure_restores_sources_and_reloads_original(
    tmp_path,
):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    script.write_text(_watchdog_fixture(tmp_path), encoding="utf-8")
    original_unit = unit.read_bytes()
    original_script = script.read_bytes()
    reload_calls = 0

    def fail_first_reload() -> None:
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 1:
            raise OSError("injected daemon reload failure")

    with pytest.raises(OSError, match="injected daemon reload failure"):
        migrate_gateway_hygiene_hold_support(
            unit_path=unit,
            script_path=script,
            backup_root=backups,
            daemon_reload=fail_first_reload,
        )

    assert reload_calls == 2
    assert unit.read_bytes() == original_unit
    assert script.read_bytes() == original_script


def test_rollback_refuses_later_edit_without_changing_either_file(tmp_path):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    script.write_text(_watchdog_fixture(tmp_path), encoding="utf-8")
    receipt = migrate_gateway_hygiene_hold_support(
        unit_path=unit,
        script_path=script,
        backup_root=backups,
    )
    migrated_script = script.read_bytes()
    unit.write_text(unit.read_text(encoding="utf-8") + "# later owner edit\n")
    later_unit = unit.read_bytes()

    with pytest.raises(RuntimeError, match="protected state changed"):
        rollback_gateway_hygiene_migration(
            unit_path=unit,
            script_path=script,
            backup_root=backups,
            receipt=receipt,
        )

    assert unit.read_bytes() == later_unit
    assert script.read_bytes() == migrated_script


def test_rollback_write_failure_compensates_to_all_migrated_bytes(
    tmp_path,
    monkeypatch,
):
    unit = tmp_path / "hermes-gateway-hygiene.service"
    script = tmp_path / "gateway-hygiene-watchdog.py"
    backups = tmp_path / "backups"
    unit.write_text(LIVE_UNIT_FIXTURE, encoding="utf-8")
    original_script = _watchdog_fixture(tmp_path)
    script.write_text(original_script, encoding="utf-8")
    receipt = migrate_gateway_hygiene_hold_support(
        unit_path=unit,
        script_path=script,
        backup_root=backups,
    )
    migrated_unit = unit.read_bytes()
    migrated_script = script.read_bytes()
    original_script_bytes = (backups / f"original-{script.name}").read_bytes()
    original_replace = hygiene_mod._atomic_replace_text

    def fail_second_restore(path: Path, content: str) -> None:
        if path == script and content.encode("utf-8") == original_script_bytes:
            raise OSError("injected second restore failure")
        original_replace(path, content)

    monkeypatch.setattr(
        hygiene_mod,
        "_atomic_replace_text",
        fail_second_restore,
    )

    with pytest.raises(OSError, match="injected second restore failure"):
        rollback_gateway_hygiene_migration(
            unit_path=unit,
            script_path=script,
            backup_root=backups,
            receipt=receipt,
        )

    assert unit.read_bytes() == migrated_unit
    assert script.read_bytes() == migrated_script


def test_systemd_stop_holds_then_stops_hygiene_before_gateway(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_sync_hermes_home_from_systemd_unit",
        lambda **_kwargs: calls.append("sync"),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_migrate_gateway_hygiene_hold_support",
        lambda **_kwargs: calls.append("migrate"),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_stop_gateway_hygiene_units",
        lambda **_kwargs: calls.append("stop_hygiene"),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_run_systemctl",
        lambda args, **_kwargs: calls.append(tuple(args)),
    )
    monkeypatch.setattr(status, "get_running_pid", lambda **_kwargs: 4242)
    monkeypatch.setattr(
        status,
        "write_gateway_owner_hold",
        lambda **kwargs: calls.append(("hold", kwargs["target_pid"])),
    )
    monkeypatch.setattr(
        status,
        "write_planned_stop_marker",
        lambda pid: calls.append(("planned", pid)),
    )

    gateway_cli.systemd_stop()

    assert calls == [
        "sync",
        ("hold", 4242),
        "migrate",
        "stop_hygiene",
        ("planned", 4242),
        ("stop", gateway_cli.get_service_name()),
    ]


def test_systemd_stop_reaches_gateway_when_every_hygiene_stop_raises(
    monkeypatch,
):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_sync_hermes_home_from_systemd_unit",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_migrate_gateway_hygiene_hold_support",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(status, "get_running_pid", lambda **_kwargs: None)
    monkeypatch.setattr(
        status,
        "write_gateway_owner_hold",
        lambda **_kwargs: None,
    )

    def fail_hygiene_only(args, **_kwargs):
        call = tuple(args)
        if call[1] != gateway_cli.get_service_name():
            raise ValueError("injected hygiene stop failure")
        calls.append(call)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway_cli, "_run_systemctl", fail_hygiene_only)

    gateway_cli.systemd_stop()

    assert calls == [("stop", gateway_cli.get_service_name())]


def test_systemd_start_migrates_then_explicitly_unholds_and_resumes_hygiene(
    monkeypatch,
):
    calls: list[object] = []
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_migrate_gateway_hygiene_hold_support",
        lambda **_kwargs: calls.append("migrate"),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_run_systemctl",
        lambda args, **_kwargs: calls.append(tuple(args)),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_start_gateway_hygiene_timer_if_installed",
        lambda **_kwargs: calls.append("start_hygiene"),
    )
    monkeypatch.setattr(
        status,
        "clear_gateway_owner_hold",
        lambda: calls.append("unhold"),
    )

    gateway_cli.systemd_start()

    assert calls == [
        "migrate",
        "unhold",
        ("start", gateway_cli.get_service_name()),
        "start_hygiene",
    ]


def test_failed_systemd_start_restores_prior_owner_hold(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    prior = status.write_gateway_owner_hold(
        target_pid=4242,
        owner="owner stop",
        reason="keep stopped",
    )
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_migrate_gateway_hygiene_hold_support",
        lambda **_kwargs: None,
    )

    def fail_start(args, **_kwargs):
        if args[0] == "is-active":
            return SimpleNamespace(
                returncode=3,
                stdout="activating\n",
                stderr="",
            )
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(gateway_cli, "_run_systemctl", fail_start)

    with pytest.raises(subprocess.TimeoutExpired):
        gateway_cli.systemd_start()

    restored = status.read_gateway_owner_hold()
    assert restored is not None
    assert restored["target_pid"] == prior["target_pid"]
    assert restored["owner"] == prior["owner"]
    assert restored["reason"] == prior["reason"]


def test_failed_systemd_restart_restores_prior_owner_hold(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    prior = status.write_gateway_owner_hold(
        target_pid=None,
        owner="owner stop",
        reason="keep stopped",
    )
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_main_pid",
        lambda **_kwargs: 4242,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_migrate_gateway_hygiene_hold_support",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_restart_after_owner_unhold",
        lambda _system: (_ for _ in ()).throw(
            TimeoutError("restart outcome unknown")
        ),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_active_main_pid",
        lambda **_kwargs: None,
    )

    with pytest.raises(TimeoutError, match="restart outcome unknown"):
        gateway_cli.systemd_restart()

    restored = status.read_gateway_owner_hold()
    assert restored is not None
    assert restored["target_pid"] == prior["target_pid"]
    assert restored["owner"] == prior["owner"]
    assert restored["reason"] == prior["reason"]


def test_systemd_restart_keeps_hold_when_old_process_remains_active(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    prior = status.write_gateway_owner_hold(
        target_pid=4242,
        owner="owner stop",
        reason="keep stopped",
    )
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_main_pid",
        lambda **_kwargs: 4242,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_restart_after_owner_unhold",
        lambda _system: None,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_active_main_pid",
        lambda **_kwargs: 4242,
    )

    gateway_cli.systemd_restart()

    restored = status.read_gateway_owner_hold()
    assert restored is not None
    assert restored["target_pid"] == 4242
    assert restored["owner"] == prior["owner"]
    assert restored["reason"] == prior["reason"]


def test_systemd_restart_unknown_replacement_applies_global_hold_without_prior_hold(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_main_pid",
        lambda **_kwargs: 4242,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_restart_after_owner_unhold",
        lambda _system: (_ for _ in ()).throw(
            TimeoutError("restart replacement unknown")
        ),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_active_main_pid",
        lambda **_kwargs: None,
    )

    with pytest.raises(TimeoutError, match="restart replacement unknown"):
        gateway_cli.systemd_restart()

    hold = status.read_gateway_owner_hold()
    assert hold is not None
    assert hold["target_pid"] is None
    assert hold["owner"] == "hermes gateway restart rollback"


def test_systemd_restart_releases_hold_only_for_proven_replacement_pid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    status.write_gateway_owner_hold(
        target_pid=4242,
        owner="owner stop",
        reason="keep stopped",
    )
    monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
    monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda *a, **k: None)
    monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_main_pid",
        lambda **_kwargs: 4242,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_restart_after_owner_unhold",
        lambda _system: None,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_systemd_active_main_pid",
        lambda **_kwargs: 5252,
    )

    gateway_cli.systemd_restart()

    assert status.read_gateway_owner_hold() is None


def test_implicit_gateway_run_is_refused_while_owner_hold_exists(monkeypatch):
    monkeypatch.setattr(status, "gateway_owner_hold_active", lambda: True)

    with pytest.raises(SystemExit) as exc:
        gateway_cli.run_gateway()

    assert exc.value.code == GATEWAY_FATAL_CONFIG_EXIT_CODE
