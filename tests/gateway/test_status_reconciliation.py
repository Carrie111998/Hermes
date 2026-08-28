"""Residual restart-status coverage for the latest-main reconciliation."""

from __future__ import annotations

import json

import pytest

import gateway.status as status


def _seed_platform_status(monkeypatch, *, pid: int, start_time: float) -> dict:
    monkeypatch.setattr(status.os, "getpid", lambda: pid)
    monkeypatch.setattr(status, "_get_process_start_time", lambda value: start_time)
    status.write_runtime_status(gateway_state="running", exit_reason=None)
    status.write_runtime_status(
        platform="discord",
        platform_state="connected",
        error_code=None,
        error_message=None,
    )
    payload = status.read_runtime_status()
    assert payload is not None
    return payload


@pytest.mark.parametrize(
    ("predecessor_pid", "predecessor_start", "successor_pid", "successor_start"),
    [
        pytest.param(4242, 111.0, 9999, 222.0, id="pid-mismatch"),
        pytest.param(4242, 111.0, 4242, 222.0, id="pid-reuse"),
    ],
)
def test_successor_clears_predecessor_platforms_before_identity_stamp(
    tmp_path,
    monkeypatch,
    predecessor_pid,
    predecessor_start,
    successor_pid,
    successor_start,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    predecessor = _seed_platform_status(
        monkeypatch, pid=predecessor_pid, start_time=predecessor_start
    )
    assert predecessor["platforms"]["discord"]["writer_pid"] == predecessor_pid
    assert predecessor["platforms"]["discord"]["writer_start_time"] == predecessor_start

    monkeypatch.setattr(status.os, "getpid", lambda: successor_pid)
    monkeypatch.setattr(
        status, "_get_process_start_time", lambda value: successor_start
    )
    status.write_runtime_status(
        gateway_state="starting",
        exit_reason=None,
        clear_predecessor_platforms=True,
        clear_profile_platforms=True,
    )

    successor = status.read_runtime_status()
    assert successor is not None
    assert successor["pid"] == successor_pid
    assert successor["start_time"] == successor_start
    assert successor["platforms"] == {}


def test_combined_startup_cleanup_preserves_current_primary_platform(
    tmp_path, monkeypatch
):
    """Main's multiplex cleanup remains active when the process is unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status.os, "getpid", lambda: 4242)
    monkeypatch.setattr(status, "_get_process_start_time", lambda value: 111.0)
    (tmp_path / "gateway_state.json").write_text(
        json.dumps({
            "pid": 4242,
            "start_time": 111.0,
            "platforms": {
                "discord": {"state": "connected"},
                "reviewer:slack": {"state": "fatal"},
            },
        }),
        encoding="utf-8",
    )

    status.write_runtime_status(
        gateway_state="starting",
        clear_predecessor_platforms=True,
        clear_profile_platforms=True,
    )

    payload = status.read_runtime_status()
    assert payload is not None
    assert payload["platforms"] == {"discord": {"state": "connected"}}
