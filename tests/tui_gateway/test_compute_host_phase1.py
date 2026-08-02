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


def test_compute_host_does_not_publish_turn_end_for_uncertain_worker_outcome(
    monkeypatch,
):
    from tui_gateway import server

    class _JoinedThread:
        def join(self):
            return None

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    session = {
        "history_lock": threading.RLock(),
        "running": False,
        "history_version": 0,
        "history": [],
        "session_key": "uncertain-key",
        "agent": None,
    }

    monkeypatch.setattr(host, "_ensure_server_session", lambda *_args: session)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *_args: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})

    def _uncertain_submit(request_id, _sid, target, _text):
        target["_prompt_terminal_outcome"] = {
            "request_id": request_id,
            "disposition": "uncertain",
        }
        target["_run_thread"] = _JoinedThread()

    monkeypatch.setattr(server, "_run_prompt_submit", _uncertain_submit)

    try:
        host._run_real_turn(
            {
                "sid": "uncertain-sid",
                "request_id": "uncertain-turn",
                "text": "do work",
            }
        )
        frame = next(
            candidate
            for candidate in _json_lines(out)
            if candidate.get("request_id") == "uncertain-turn"
            and candidate.get("type") in {"turn.end", "turn.error"}
        )
    finally:
        host.close()

    assert frame["type"] == "turn.error"
    assert frame["execution_state"] == "ambiguous"


def test_compute_host_labels_preexecution_rejection_as_not_started():
    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    try:
        host._run_real_turn({"sid": "", "request_id": "missing-sid"})
        frame = next(
            candidate
            for candidate in _json_lines(out)
            if candidate.get("request_id") == "missing-sid"
        )
    finally:
        host.close()

    assert frame["type"] == "turn.error"
    assert frame["reason"] == "not_started"
    assert frame["execution_state"] == "not_started"


def test_compute_host_marks_existing_server_session_as_non_queue_owner():
    from tui_gateway import server

    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    session = {"transport": None}
    server._sessions["worker-owner"] = session
    try:
        resolved = host._ensure_server_session(
            server,
            {"sid": "worker-owner", "session_key": "durable-session"},
        )
        assert resolved is session
        assert resolved["_compute_host_worker"] is True
    finally:
        server._sessions.pop("worker-owner", None)
        host.close()


@pytest.mark.parametrize("explicit_profile_home", [True, False])
def test_respawn_rehydrates_authoritative_history_from_effective_profile_db(
    tmp_path,
    monkeypatch,
    explicit_profile_home,
):
    from tui_gateway.compute_host import ComputeHost

    effective_home = tmp_path / "effective-profile"
    effective_home.mkdir()
    persisted_history = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "answer one"},
    ]
    opened_db_paths = []

    class FakeSessionDB:
        def __init__(self, db_path):
            self.db_path = Path(db_path)
            opened_db_paths.append(self.db_path)

        def get_messages_as_conversation(self, session_id, repair_alternation=False):
            assert session_id == "durable-key"
            assert repair_alternation is True
            return list(persisted_history)

    import hermes_state
    import hermes_constants

    monkeypatch.setattr(hermes_state, "SessionDB", FakeSessionDB)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: effective_home)

    class FakeServer:
        def __init__(self):
            self._sessions = {}

        def _make_agent(self, *_args, **_kwargs):
            return object()

        def _init_session(self, sid, key, agent, history, **kwargs):
            self._sessions[sid] = {
                "agent": agent,
                "session_key": key,
                "history": list(history),
                "history_lock": threading.Lock(),
                "transport": None,
                **kwargs,
            }

    host = ComputeHost.__new__(ComputeHost)
    host._transport = object()
    server = FakeServer()
    session = host._ensure_server_session(
        server,
        {
            "sid": "live-sid",
            "session_key": "durable-key",
            "profile_home": str(effective_home) if explicit_profile_home else "",
            "history": [{"role": "user", "content": "stale parent copy"}],
            "history_version": 7,
            "cols": 80,
        },
    )

    assert opened_db_paths == [effective_home / "state.db"]
    assert session["history"] == persisted_history
    assert session["history_version"] == 7
    assert session["profile_home"] == str(effective_home)
