"""PID-incarnation regressions for the fleet update proof surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gateway.status as gateway_status
import hermes_cli.update_inventory as update_inventory
import hermes_cli.update_receipt as update_receipt
import pytest


_EXPECTED_SHA = "a" * 40
_MISSING = object()
_RAISE = object()


def _write_gateway_state(home: Path, *, start_time: Any = _MISSING) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 4242,
        "gateway_state": "running",
        "code_sha": _EXPECTED_SHA,
        "code_version": "1.0",
    }
    if start_time is not _MISSING:
        payload["start_time"] = start_time
    (home / "gateway_state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _bind_one_profile(
    monkeypatch,
    tmp_path: Path,
    *,
    recorded_start_time: Any = 111,
    live_start_time: Any = 111,
    pid_is_live: bool = True,
) -> Path:
    home = tmp_path / "home"
    _write_gateway_state(home, start_time=recorded_start_time)

    monkeypatch.setattr(
        "hermes_cli.profiles._get_default_hermes_home", lambda: home
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles"
    )
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda pid: pid == 4242 and pid_is_live,
    )

    def probe_start_time(pid: int):
        assert pid == 4242
        if live_start_time is _RAISE:
            raise OSError("start-time probe unavailable")
        return live_start_time

    monkeypatch.setattr("gateway.status._get_process_start_time", probe_start_time)
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway", lambda profile_home: None
    )
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {
            "sha": _EXPECTED_SHA,
            "short_sha": _EXPECTED_SHA[:8],
            "version": "1.0",
            "source": "git",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.gateway._get_service_pids", lambda all_profiles=False: set()
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes", lambda: []
    )
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *args, **kwargs: "git"
    )
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.recommended_update_command_for_method",
        lambda method: "hermes update",
    )
    return home


def _assert_excluded_from_both_proof_surfaces() -> None:
    assert update_receipt.collect_fleet_versions() == []
    assert update_inventory.collect_runtime_inventory().runtimes == []


def test_recycled_pid_is_excluded_from_both_fleet_proof_surfaces(
    monkeypatch, tmp_path
):
    _bind_one_profile(monkeypatch, tmp_path, live_start_time=222)

    _assert_excluded_from_both_proof_surfaces()
    # A recycled live PID is not the stopped pre-update process and must not
    # be transformed into a misleading DOWN row either.
    assert update_receipt.collect_fleet_versions(pre_restart_pids=[4242]) == []


def test_matching_pid_incarnation_is_admitted_by_both_fleet_proof_surfaces(
    monkeypatch, tmp_path
):
    _bind_one_profile(monkeypatch, tmp_path, live_start_time=111)

    fleet = update_receipt.collect_fleet_versions()
    assert [(entry["pid"], entry["state"]) for entry in fleet] == [(4242, "current")]

    inventory = update_inventory.collect_runtime_inventory()
    assert [(runtime.pid, runtime.code_sha) for runtime in inventory.runtimes] == [
        (4242, _EXPECTED_SHA)
    ]


def test_missing_recorded_start_time_fails_closed_on_both_proof_surfaces(
    monkeypatch, tmp_path
):
    home = _bind_one_profile(
        monkeypatch,
        tmp_path,
        recorded_start_time=_MISSING,
    )
    record = gateway_status.read_runtime_status(home / "gateway_state.json")

    # Preserve the intentionally permissive UI/legacy contract.
    assert gateway_status.runtime_status_pid_is_live(record) is True
    assert gateway_status.runtime_status_pid_incarnation_is_live(record) is False
    _assert_excluded_from_both_proof_surfaces()


def test_missing_live_start_time_fails_closed_on_both_proof_surfaces(
    monkeypatch, tmp_path
):
    home = _bind_one_profile(monkeypatch, tmp_path, live_start_time=None)
    record = gateway_status.read_runtime_status(home / "gateway_state.json")

    # Legacy readers still accept PID-only liveness when the OS fingerprint
    # is unavailable; update proof surfaces deliberately do not.
    assert gateway_status.runtime_status_pid_is_live(record) is True
    assert gateway_status.runtime_status_pid_incarnation_is_live(record) is False
    _assert_excluded_from_both_proof_surfaces()


def test_start_time_probe_exception_fails_closed_on_both_proof_surfaces(
    monkeypatch, tmp_path
):
    home = _bind_one_profile(monkeypatch, tmp_path, live_start_time=_RAISE)
    record = gateway_status.read_runtime_status(home / "gateway_state.json")

    assert gateway_status.runtime_status_pid_incarnation_is_live(record) is False
    _assert_excluded_from_both_proof_surfaces()


def test_pid_liveness_probe_exception_fails_closed_without_down_row(
    monkeypatch, tmp_path
):
    home = _bind_one_profile(monkeypatch, tmp_path)

    def fail_liveness_probe(pid: int) -> bool:
        raise OSError(f"cannot inspect pid {pid}")

    monkeypatch.setattr("gateway.status._pid_exists", fail_liveness_probe)
    record = gateway_status.read_runtime_status(home / "gateway_state.json")

    assert gateway_status.runtime_status_pid_incarnation_is_live(record) is False
    _assert_excluded_from_both_proof_surfaces()
    assert update_receipt.collect_fleet_versions(pre_restart_pids=[4242]) == []


def test_only_provably_dead_pre_restart_pid_becomes_down(monkeypatch, tmp_path):
    _bind_one_profile(
        monkeypatch,
        tmp_path,
        live_start_time=None,
        pid_is_live=False,
    )

    assert update_inventory.collect_runtime_inventory().runtimes == []
    assert update_receipt.collect_fleet_versions() == []
    fleet = update_receipt.collect_fleet_versions(pre_restart_pids=[4242])
    assert [(entry["pid"], entry["state"]) for entry in fleet] == [
        (4242, "down")
    ]


@pytest.mark.parametrize(
    "recorded_start_time",
    [None, True, -1, 111.0, "111"],
)
def test_invalid_recorded_start_time_is_not_strict_proof(
    monkeypatch, tmp_path, recorded_start_time
):
    home = _bind_one_profile(
        monkeypatch,
        tmp_path,
        recorded_start_time=recorded_start_time,
    )
    record = gateway_status.read_runtime_status(home / "gateway_state.json")

    assert gateway_status.runtime_status_pid_incarnation_is_live(record) is False
