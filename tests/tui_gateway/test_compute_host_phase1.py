import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tui_gateway.compute_host import ComputeHost, _default_workers
from tui_gateway.host_supervisor import (
    MUTATOR_ROUTE_TABLE,
    HostSupervisor,
    append_log_record,
)


def _json_lines(out: io.StringIO) -> list[dict]:
    frames = []
    for line in out.getvalue().splitlines():
        if line.strip():
            frames.append(json.loads(line))
    return frames


def _wait_for_frame(out: io.StringIO, predicate, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in _json_lines(out):
            if predicate(frame):
                return frame
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for frame; saw={_json_lines(out)}")


def test_compute_host_workers_inherit_tui_pool_env_or_8(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_RPC_POOL_WORKERS", raising=False)
    monkeypatch.delenv("HERMES_COMPUTE_HOST_WORKERS", raising=False)
    assert _default_workers() == 8

    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "11")
    assert _default_workers() == 11

    # Dead-RC tombstone: malformed env falls back to 8, not the old except-branch 4.
    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "not-an-int")
    assert _default_workers() == 8


def test_mutator_route_table_matches_prd_inventory():
    assert MUTATOR_ROUTE_TABLE == {
        "prompt.submit": "turn-path",
        "session.interrupt": "turn-path",
        "reload.mcp": "run-concurrent",
        "session.save": "run-concurrent",
        "session.compress": "idle-gated",
        "prompt.submit.truncate": "idle-gated",
        "slash.model": "idle-gated",
        "slash.personality": "idle-gated",
        "slash.prompt": "idle-gated",
        "slash.compress": "idle-gated",
        "session.reset": "idle-gated",
        "session.history.reload": "idle-gated",
        "slash.retry": "idle-gated",
    }


def test_append_log_record_single_write_lines(tmp_path):
    path = tmp_path / "agent.log"

    def writer(i: int) -> None:
        append_log_record(path, f"line-{i:03d}-" + ("x" * 2000))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 32
    assert sorted(line.split("-", 2)[1] for line in lines) == [f"{i:03d}" for i in range(32)]
    assert all(line.endswith("x" * 2000) for line in lines)


def test_supervisor_startup_reconcile_pid_reuse_guard(tmp_path, monkeypatch):
    registry = tmp_path / "dashboard-compute-host.json"
    registry.write_text(json.dumps({"host_pid": os.getpid(), "boot_id": "stale"}), encoding="utf-8")

    killed: list[int] = []
    supervisor = HostSupervisor(registry_path=registry, argv=[sys.executable, "-c", ""], autostart=False)
    monkeypatch.setattr(supervisor, "_pid_matches_compute_host", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_terminate_pid", lambda pid, **_kw: killed.append(pid))

    result = supervisor.reconcile_startup_orphan()

    assert result == "pid-reuse-ignored"
    assert killed == []
    assert not registry.exists()


def test_compute_host_reports_and_interrupts_process_local_subagents(tmp_path):
    from unittest.mock import MagicMock

    from tools.delegate_tool import _register_subagent, _unregister_subagent

    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    agent = MagicMock()
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    _register_subagent(
        {
            "subagent_id": "sa-compute-local",
            "goal": "process local child",
            "status": "running",
            "agent": agent,
            "_profile_home": str(profile_home),
        }
    )
    try:
        host.handle_frame(
            {
                "type": "subagent.status",
                "request_id": "status-1",
                "profile_home": str(profile_home),
            }
        )
        host.handle_frame(
            {
                "type": "subagent.interrupt",
                "request_id": "interrupt-1",
                "profile_home": str(profile_home),
                "subagent_id": "sa-compute-local",
            }
        )
    finally:
        _unregister_subagent("sa-compute-local")
        host.close()

    frames = _json_lines(out)
    status = next(frame for frame in frames if frame.get("request_id") == "status-1")
    interrupted = next(frame for frame in frames if frame.get("request_id") == "interrupt-1")
    assert status["type"] == "subagent.status.ack"
    assert [row["subagent_id"] for row in status["active"]] == ["sa-compute-local"]
    assert interrupted == {
        **{key: interrupted[key] for key in ("host_ns",)},
        "type": "subagent.interrupt.ack",
        "request_id": "interrupt-1",
        "interrupted": True,
    }
    agent.interrupt.assert_called_once_with(
        "Interrupted via TUI (sa-compute-local)"
    )


def test_supervisor_subagent_query_round_trip(tmp_path, monkeypatch):
    supervisor = HostSupervisor(
        registry_path=tmp_path / "registry.json",
        argv=[sys.executable, "-c", ""],
        autostart=False,
    )
    monkeypatch.setattr(supervisor, "start", lambda: None)

    def respond(frame):
        supervisor._handle_host_frame(
            {
                "type": f"{frame['type']}.ack",
                "request_id": frame["request_id"],
                "active": [{"subagent_id": "sa-host-round-trip"}],
                "interrupted": True,
            }
        )

    monkeypatch.setattr(supervisor, "_send_frame", respond)

    status = supervisor.subagent_status("/profiles/default")
    interrupted = supervisor.interrupt_subagent(
        "sa-host-round-trip",
        profile_home="/profiles/default",
    )

    assert status["active"] == [{"subagent_id": "sa-host-round-trip"}]
    assert interrupted is True


def test_supervisor_subagent_query_timeout_is_explicit(tmp_path, monkeypatch):
    supervisor = HostSupervisor(
        registry_path=tmp_path / "registry.json",
        argv=[sys.executable, "-c", ""],
        autostart=False,
    )
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "_send_frame", lambda _frame: None)

    with pytest.raises(TimeoutError, match=r"subagent\.status timed out after 0\.01s"):
        supervisor.subagent_status("/profiles/default", timeout=0.01)


def test_supervisor_subagent_query_crosses_real_compute_host_process(tmp_path):
    supervisor = HostSupervisor(
        registry_path=tmp_path / "registry.json",
        heartbeat_secs=0,
        autostart=False,
    )
    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    try:
        response = supervisor.subagent_status(str(profile_home), timeout=5.0)
        interrupted = supervisor.interrupt_subagent(
            "sa-does-not-exist",
            profile_home=str(profile_home),
            timeout=5.0,
        )
    finally:
        supervisor.shutdown()

    assert response["type"] == "subagent.status.ack"
    assert response["active"] == []
    assert interrupted is False


def test_compute_host_subagent_query_rejects_unscoped_frame():
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "subagent.status", "request_id": "unscoped"})
    finally:
        host.close()

    frame = next(item for item in _json_lines(out) if item.get("request_id") == "unscoped")
    assert frame["type"] == "subagent.status.error"
    assert frame["message"] == "profile_home required"


def _make_compress_host_session(events: list) -> dict:
    class _Agent:
        model = "host-model"
        provider = "host-provider"
        tools = []
        _cached_system_prompt = ""
        session_input_tokens = 1
        session_output_tokens = 1
        session_prompt_tokens = 1
        session_completion_tokens = 1
        session_total_tokens = 2
        session_api_calls = 1
        session_id = "rotated-id"

    agent = _Agent()
    agent.context_compressor = type("ContextEngineStub", (), {})()
    agent.context_compressor.on_session_start = (
        lambda *_args, **_kwargs: events.append("notify")
    )
    return {
        "agent": agent,
        "session_key": "before-key",
        "history": [
            {"role": "user", "content": "before"},
            {"role": "assistant", "content": "before"},
        ],
        "history_lock": threading.Lock(),
        "history_version": 2,
        "running": False,
        "manual_compression_lock": threading.Lock(),
    }


