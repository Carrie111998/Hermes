"""Out-of-cgroup updater-owner restart verification regressions."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from hermes_cli import update_owner_restart


SERVICE = "hermes-gateway-coding_lead"
OLD_STATE = {
    "active_state": "active",
    "sub_state": "running",
    "main_pid": 4321,
    "exec_start": 101,
    "active_enter": 102,
    "restart": "on-failure",
}
NEW_STATE = {
    "active_state": "active",
    "sub_state": "running",
    "main_pid": 9876,
    "exec_start": 201,
    "active_enter": 202,
    "restart": "on-failure",
}


def _prepare(
    home: Path,
    *,
    restart: str = "on-failure",
    timeout_seconds: float = 0.03,
    nonce: str = "a" * 32,
) -> dict:
    old_state = {**OLD_STATE, "restart": restart}
    return update_owner_restart.prepare_owner_restart_request(
        home,
        scope="user",
        service=SERVICE,
        old_state=old_state,
        final_exit_code=0,
        timeout_seconds=timeout_seconds,
        nonce=nonce,
    )


def test_request_accepts_bounded_full_gateway_exit_wait(tmp_path):
    request = _prepare(tmp_path, timeout_seconds=1800.0)

    assert request["timeout_seconds"] == 1800.0

    with pytest.raises(ValueError, match="owner restart timeout"):
        _prepare(tmp_path, timeout_seconds=1800.1)


def _result(home: Path) -> dict:
    return json.loads(
        (home / update_owner_restart.OWNER_RESTART_RESULT_FILE).read_text()
    )


def test_signal_accepted_without_owner_transition_persists_failure(
    tmp_path, monkeypatch
):
    request = _prepare(tmp_path)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: dict(OLD_STATE),
    )
    monkeypatch.setattr(
        update_owner_restart.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert (
        update_owner_restart.verify_owner_restart(
            tmp_path, request["nonce"], poll_interval=0.001
        )
        == 1
    )

    assert signals == [(4321, signal.SIGUSR1)]
    assert (tmp_path / ".update_exit_code").read_text() == "1"
    assert _result(tmp_path)["verified"] is False
    assert "timed out" in _result(tmp_path)["reason"]
    assert "✓ Update complete!" not in (
        tmp_path / ".update_output.txt"
    ).read_text()


def test_changed_pid_and_generation_require_matching_health_ack(tmp_path, monkeypatch):
    request = _prepare(tmp_path)
    states = iter([dict(OLD_STATE), dict(NEW_STATE)])
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, dict(NEW_STATE)),
    )
    monkeypatch.setattr(update_owner_restart.os, "kill", lambda _pid, _sig: None)
    assert update_owner_restart.acknowledge_owner_restart_ready(
        tmp_path,
        current_service=SERVICE,
        current_pid=9876,
        now_ns=request["requested_at_ns"] + 1,
    )

    assert (
        update_owner_restart.verify_owner_restart(
            tmp_path, request["nonce"], poll_interval=0.001
        )
        == 0
    )

    result = _result(tmp_path)
    assert result["verified"] is True
    assert result["observed_state"]["main_pid"] == 9876
    assert result["observed_state"]["exec_start"] == 201
    assert (tmp_path / ".update_exit_code").read_text() == "0"
    assert "✓ Update complete!" in (tmp_path / ".update_output.txt").read_text()


def test_restart_no_is_started_once_outside_owner_cgroup(tmp_path, monkeypatch):
    request = _prepare(tmp_path, restart="no")
    old_no_restart = {**OLD_STATE, "restart": "no"}
    inactive = {
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "exec_start": 101,
        "active_enter": 102,
        "restart": "no",
    }
    new_no_restart = {**NEW_STATE, "restart": "no"}
    states = iter([old_no_restart, inactive, new_no_restart])
    starts: list[tuple[str, str]] = []

    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, new_no_restart),
    )
    monkeypatch.setattr(update_owner_restart.os, "kill", lambda _pid, _sig: None)

    def fake_start(scope, service):
        starts.append((scope, service))
        assert update_owner_restart.acknowledge_owner_restart_ready(
            tmp_path,
            current_service=SERVICE,
            current_pid=9876,
            now_ns=request["requested_at_ns"] + 1,
        )
        return True

    monkeypatch.setattr(update_owner_restart, "start_inactive_service", fake_start)

    assert (
        update_owner_restart.verify_owner_restart(
            tmp_path, request["nonce"], poll_interval=0.001
        )
        == 0
    )
    assert starts == [("user", SERVICE)]


def test_restart_no_start_failure_is_actionable(tmp_path, monkeypatch):
    request = _prepare(tmp_path, restart="no")
    old_no_restart = {**OLD_STATE, "restart": "no"}
    inactive = {
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "exec_start": 101,
        "active_enter": 102,
        "restart": "no",
    }
    states = iter([old_no_restart, inactive])
    starts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, dict(inactive)),
    )
    monkeypatch.setattr(update_owner_restart.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(
        update_owner_restart,
        "start_inactive_service",
        lambda scope, service: starts.append((scope, service)) or False,
    )

    assert (
        update_owner_restart.verify_owner_restart(
            tmp_path, request["nonce"], poll_interval=0.001
        )
        == 1
    )
    assert starts == [("user", SERVICE)]
    assert "could not start" in _result(tmp_path)["reason"]


def test_changed_owner_without_health_ack_times_out_as_failure(tmp_path, monkeypatch):
    request = _prepare(tmp_path)
    states = iter([dict(OLD_STATE), dict(NEW_STATE)])
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, dict(NEW_STATE)),
    )
    monkeypatch.setattr(update_owner_restart.os, "kill", lambda _pid, _sig: None)

    assert (
        update_owner_restart.verify_owner_restart(
            tmp_path, request["nonce"], poll_interval=0.001
        )
        == 1
    )
    assert _result(tmp_path)["verified"] is False
    assert "readiness acknowledgement" in _result(tmp_path)["reason"]


def test_stale_and_foreign_requests_are_not_acknowledged(tmp_path):
    stale = _prepare(tmp_path, timeout_seconds=0.01)
    assert not update_owner_restart.acknowledge_owner_restart_ready(
        tmp_path,
        current_service=SERVICE,
        current_pid=9876,
        now_ns=stale["deadline_ns"] + 1,
    )
    assert not (tmp_path / update_owner_restart.OWNER_RESTART_ACK_FILE).exists()

    foreign = _prepare(tmp_path, nonce="b" * 32)
    assert not update_owner_restart.acknowledge_owner_restart_ready(
        tmp_path,
        current_service="hermes-gateway-other",
        current_pid=9876,
        now_ns=foreign["requested_at_ns"] + 1,
    )
    assert not (tmp_path / update_owner_restart.OWNER_RESTART_ACK_FILE).exists()


def test_foreign_nonce_is_rejected_without_signalling(tmp_path, monkeypatch):
    _prepare(tmp_path)
    monkeypatch.setattr(
        update_owner_restart.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    assert update_owner_restart.verify_owner_restart(tmp_path, "f" * 32) == 1
    assert not (tmp_path / ".update_exit_code").exists()


def test_duplicate_verifier_invocation_is_idempotent(tmp_path, monkeypatch):
    request = _prepare(tmp_path)
    states = iter([dict(OLD_STATE), dict(NEW_STATE)])
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, dict(NEW_STATE)),
    )
    monkeypatch.setattr(
        update_owner_restart.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    assert update_owner_restart.acknowledge_owner_restart_ready(
        tmp_path,
        current_service=SERVICE,
        current_pid=9876,
        now_ns=request["requested_at_ns"] + 1,
    )

    assert update_owner_restart.verify_owner_restart(tmp_path, request["nonce"]) == 0
    first_output = (tmp_path / ".update_output.txt").read_text()
    assert update_owner_restart.verify_owner_restart(tmp_path, request["nonce"]) == 0

    assert signals == [(4321, signal.SIGUSR1)]
    assert (tmp_path / ".update_output.txt").read_text() == first_output


def test_atomic_result_remains_terminal_when_legacy_marker_write_fails(
    tmp_path, monkeypatch
):
    request = _prepare(tmp_path)
    states = iter([dict(OLD_STATE), dict(NEW_STATE)])
    monkeypatch.setattr(
        update_owner_restart,
        "read_systemd_service_state",
        lambda _scope, _service: next(states, dict(NEW_STATE)),
    )
    monkeypatch.setattr(update_owner_restart.os, "kill", lambda _pid, _sig: None)
    assert update_owner_restart.acknowledge_owner_restart_ready(
        tmp_path,
        current_service=SERVICE,
        current_pid=9876,
        now_ns=request["requested_at_ns"] + 1,
    )
    monkeypatch.setattr(
        update_owner_restart,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only marker")),
    )

    assert update_owner_restart.verify_owner_restart(tmp_path, request["nonce"]) == 0
    assert update_owner_restart.verify_owner_restart(tmp_path, request["nonce"]) == 0
    assert update_owner_restart.read_owner_restart_result_exit_code(tmp_path) == 0
    assert not (tmp_path / ".update_exit_code").exists()
