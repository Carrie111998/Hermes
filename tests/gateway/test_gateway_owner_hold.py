from __future__ import annotations

import os

from gateway import status


def test_gateway_owner_hold_is_durable_targeted_and_explicitly_cleared(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)

    record = status.write_gateway_owner_hold(
        target_pid=os.getpid(),
        owner="Ed",
        reason="Direct Operations release hold",
    )

    assert record["state"] == "held"
    assert status.gateway_owner_hold_active() is True
    assert status.gateway_owner_hold_targets_self() is True
    assert status.read_gateway_owner_hold() == record
    assert status.get_gateway_owner_hold_path().read_text(encoding="utf-8")

    assert status.clear_gateway_owner_hold() is True
    assert status.gateway_owner_hold_active() is False
    assert status.get_gateway_owner_hold_path().exists() is False


def test_gateway_owner_hold_for_other_pid_does_not_freeze_this_process(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)

    status.write_gateway_owner_hold(target_pid=os.getpid() + 10_000)

    assert status.gateway_owner_hold_active() is True
    assert status.gateway_owner_hold_targets_self() is False


def test_malformed_owner_hold_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    status.get_gateway_owner_hold_path().write_text(
        '{"schema_version": 1, "state": "unknown"}\n',
        encoding="utf-8",
    )

    assert status.read_gateway_owner_hold() is None
    assert status.gateway_owner_hold_active() is True
    assert status.gateway_owner_hold_targets_self() is True


def test_owner_hold_with_malformed_target_pid_targets_self_fail_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: tmp_path)
    status.get_gateway_owner_hold_path().write_text(
        (
            '{"schema_version": 1, "state": "held", '
            '"target_pid": "not-a-pid"}\n'
        ),
        encoding="utf-8",
    )

    assert status.read_gateway_owner_hold() is not None
    assert status.gateway_owner_hold_active() is True
    assert status.gateway_owner_hold_targets_self() is True
