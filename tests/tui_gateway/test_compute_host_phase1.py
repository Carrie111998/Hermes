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


def test_compute_host_turn_frame_carries_typed_one_turn_route_intent():
    from tui_gateway import server

    model_target = {
        "kind": "model",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }
    moa_target = {"kind": "moa", "preset": "deep"}
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "session_key": "key",
        "one_turn_route_target": model_target,
        "one_turn_moa_target": moa_target,
    }

    frame = server._compute_host_turn_frame("rid", "sid", session, "hello")

    assert frame["one_turn_route_target"] == model_target
    assert frame["one_turn_moa_target"] == moa_target


def test_compute_host_successful_handoff_consumes_parent_one_turn_intent(monkeypatch):
    from tui_gateway import server

    captured = {}

    class _Supervisor:
        def submit_turn(self, frame, *, on_complete):
            captured.update(frame)

    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "session_key": "key",
        "one_turn_route_target": {
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        "one_turn_moa_target": {"kind": "moa", "preset": "deep"},
    }
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_args: _Supervisor())

    response = server._submit_prompt_to_compute_host("rid", "sid", session, "hello")

    assert response["result"]["turn_isolation"] is True
    assert captured["one_turn_route_target"]["model"] == "gpt-5.6-sol"
    assert "one_turn_route_target" not in session
    assert "one_turn_moa_target" not in session


def test_compute_host_hydrates_and_clears_one_turn_intent_per_frame():
    from tui_gateway import server

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    session = {
        "agent": object(),
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "transport": host._transport,
    }
    server._sessions["sid-route"] = session
    try:
        hydrated = host._ensure_server_session(
            server,
            {
                "sid": "sid-route",
                "one_turn_route_target": {
                    "kind": "model",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                },
            },
        )
        assert hydrated["one_turn_route_target"]["model"] == "gpt-5.6-sol"

        cleared = host._ensure_server_session(server, {"sid": "sid-route"})
        assert "one_turn_route_target" not in cleared
        assert "one_turn_moa_target" not in cleared
    finally:
        server._sessions.pop("sid-route", None)
        host.close()


def test_compute_host_rebuilds_a_quarantined_agent_without_dropping_session_state(
    monkeypatch,
):
    from agent.turn_routing_runtime import TurnRoutingSessionState
    from tui_gateway import server

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    old_agent = object()
    new_agent = object()
    ready = threading.Event()
    ready.set()
    state = TurnRoutingSessionState(
        affinity_route="deep",
        affinity_target={"kind": "moa", "preset": "deep"},
        affinity_remaining=2,
        consecutive_failures=1,
        fail_off=False,
    )
    session = {
        "agent": old_agent,
        "agent_ready": ready,
        "session_key": "host-key",
        "history_lock": threading.Lock(),
        "history": [{"role": "user", "content": "preserved"}],
        "history_version": 1,
        "attached_images": [],
        "running": False,
        "turn_routing_state": state,
        "transport": host._transport,
    }
    server._sessions["sid-quarantine"] = session
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: new_agent)
    try:
        server._quarantine_turn_routing_agent(
            "sid-quarantine",
            session,
            old_agent,
            "route_restore_failed",
        )
        assert session["agent"] is None
        assert ready.is_set() is False

        rebuilt = host._ensure_server_session(
            server,
            {
                "sid": "sid-quarantine",
                "session_key": "host-key",
                "history": [],
            },
        )
    finally:
        server._sessions.pop("sid-quarantine", None)
        host.close()

    assert rebuilt["agent"] is new_agent
    assert rebuilt["history"] == [{"role": "user", "content": "preserved"}]
    assert rebuilt["turn_routing_state"] is state
    assert rebuilt["turn_routing_state"].affinity_route == "deep"
    assert rebuilt["agent_ready"].is_set() is True
    assert "routing_quarantined" not in rebuilt


def test_compute_host_restart_rehydrates_parent_mirrored_routing_state(monkeypatch):
    from agent.turn_routing_runtime import TurnRoutingSessionState
    from tui_gateway import server

    parent_session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 3,
        "attached_images": [],
        "session_key": "host-key",
    }
    server._apply_compute_host_metadata_mirror(
        parent_session,
        {
            "turn_routing_state": {
                "affinity_route": "deep",
                "affinity_target": {"kind": "moa", "preset": "deep"},
                "affinity_remaining": 1,
                "consecutive_failures": 3,
                "fail_off": True,
                "fail_off_reason": "automatic_failure_limit",
                "turn_sequence": 7,
                "affinity_window": 2,
                "failure_limit": 3,
            }
        },
    )
    frame = server._compute_host_turn_frame(
        "rid-after-host-restart",
        "sid-after-host-restart",
        parent_session,
        "next turn",
    )
    assert frame["turn_routing_state"]["fail_off"] is True
    assert frame["turn_routing_state"]["turn_sequence"] == 7

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    new_agent = object()

    def _init_session(sid, key, agent, history, **_kwargs):
        server._sessions[sid] = {
            "agent": agent,
            "session_key": key,
            "history": list(history),
            "history_lock": threading.Lock(),
            "history_version": 0,
            "attached_images": [],
            "running": False,
        }

    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: new_agent)
    monkeypatch.setattr(server, "_init_session", _init_session)
    try:
        child_session = host._ensure_server_session(server, frame)
    finally:
        server._sessions.pop("sid-after-host-restart", None)
        host.close()

    child_state = child_session["turn_routing_state"]
    assert isinstance(child_state, TurnRoutingSessionState)
    assert child_state.affinity_route == "deep"
    assert child_state.affinity_remaining == 1
    assert child_state.consecutive_failures == 3
    assert child_state.fail_off is True
    assert child_state.fail_off_reason == "automatic_failure_limit"
    assert child_state.turn_sequence == 7


def test_compute_host_real_turn_reaches_shared_prompt_path_with_typed_intent(monkeypatch):
    from tui_gateway import server

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    captured = {}
    session = {
        "agent": object(),
        "session_key": "host-key",
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "running": False,
        "transport": host._transport,
    }
    server._sessions["sid-host-route"] = session

    def _run_prompt(_rid, _sid, host_session, _text):
        captured["target"] = dict(host_session["one_turn_route_target"])
        host_session.pop("one_turn_route_target", None)
        host_session["running"] = False

    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_run_prompt_submit", _run_prompt)
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session: {})
    try:
        host._run_real_turn(
            {
                "type": "turn.start",
                "sid": "sid-host-route",
                "request_id": "turn-host-route",
                "session_key": "host-key",
                "text": "hello",
                "one_turn_route_target": {
                    "kind": "model",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                },
            }
        )
    finally:
        server._sessions.pop("sid-host-route", None)
        host.close()

    assert captured["target"] == {
        "kind": "model",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }


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
        "route.command": "idle-gated",
    }


def test_compute_host_route_control_reads_and_resets_provider_owned_state(monkeypatch):
    from agent.turn_routing_runtime import TurnRoutingSessionState
    from tui_gateway import server

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    state = TurnRoutingSessionState(
        affinity_route="deep",
        affinity_remaining=2,
        fail_off=True,
    )
    session = {
        "agent": None,
        "session_key": "host-route-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "turn_routing_state": state,
    }
    server._sessions["host-route-sid"] = session
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"routing": {"mode": "observe", "budget": {}}},
    )
    try:
        host.handle_frame(
            {
                "type": "control",
                "sid": "host-route-sid",
                "request_id": "route-status",
                "route_name": "route.command",
                "argument": "status",
            }
        )
        status = _wait_for_frame(
            out,
            lambda frame: frame.get("request_id") == "route-status",
        )
        host.handle_frame(
            {
                "type": "control",
                "sid": "host-route-sid",
                "request_id": "route-reset",
                "route_name": "route.command",
                "argument": "reset",
            }
        )
        reset = _wait_for_frame(
            out,
            lambda frame: frame.get("request_id") == "route-reset",
        )
    finally:
        server._sessions.pop("host-route-sid", None)
        host.close()

    assert "Affinity: deep (2 turns remaining)" in status["result"]["output"]
    assert reset["result"]["output"].startswith("Session routing state reset")
    assert state.affinity_route is None
    assert state.fail_off is False


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


