import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tui_gateway import compute_host, server
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


@pytest.mark.parametrize("fallback", [False, True])
def test_compute_host_reconstructs_workspace_ownership(monkeypatch, tmp_path, fallback):
    host = ComputeHost(stdout=io.StringIO(), max_workers=1, heartbeat_secs=0)
    sid = f"ownership-{'fallback' if fallback else 'native'}"
    agent = object()
    monkeypatch.setattr(server, "_make_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *_args: False)

    def init_session(init_sid, key, init_agent, history, **kwargs):
        if fallback:
            raise RuntimeError("boom")
        server._sessions[init_sid] = {
            "agent": init_agent,
            "cwd": kwargs["cwd"],
            "cwd_owned": kwargs["cwd_owned"],
            "explicit_cwd": kwargs["explicit_cwd"],
            "history": history,
            "history_lock": threading.Lock(),
            "session_key": key,
        }

    monkeypatch.setattr(server, "_init_session", init_session)

    frame = {
        "cols": 80,
        "cwd": str(tmp_path),
        "cwd_owned": False,
        "explicit_cwd": False,
        "history": [],
        "session_key": "detached-host",
        "sid": sid,
        "source": "desktop",
    }
    try:
        session = host._ensure_server_session(server, frame)
        assert session["cwd_owned"] is False
        assert session["explicit_cwd"] is False
        assert session["_defer_compute_host_session_info"] is True
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_fallback_preserves_absent_legacy_cwd_ownership(monkeypatch, tmp_path):
    host = ComputeHost(stdout=io.StringIO(), max_workers=1, heartbeat_secs=0)
    sid = "legacy-fallback-ownership"
    monkeypatch.setattr(server, "_make_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *_args: False)
    monkeypatch.setattr(
        server,
        "_init_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    frame = {
        "cols": 80,
        "cwd": str(tmp_path),
        "explicit_cwd": False,
        "history": [],
        "session_key": "legacy-tui-host",
        "sid": sid,
        "source": "tui",
    }
    try:
        session = host._ensure_server_session(server, frame)
        assert "cwd_owned" not in session
        assert server._session_owns_cwd(session) is True
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_reused_session_refreshes_actual_terminal_cwd(
    monkeypatch, tmp_path
):
    """A newer parent topology must reach the host's real terminal resolver."""
    from tools import terminal_tool

    old_cwd = tmp_path / "old-workspace"
    new_cwd = tmp_path / "new-workspace"
    old_cwd.mkdir()
    new_cwd.mkdir()
    sid = "reused-host-cwd"
    session_key = "stored-reused-host-cwd"
    session = {
        "agent": object(),
        "cwd": str(old_cwd),
        "cwd_owned": True,
        "explicit_cwd": True,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "session_key": session_key,
        "_workspace_topology_generation": 0,
    }
    host = ComputeHost(stdout=io.StringIO(), max_workers=1, heartbeat_secs=0)

    # Keep this production-path terminal invocation hermetic while still using
    # the real LocalEnvironment and foreground cwd-resolution chain.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(old_cwd))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    server._sessions[sid] = session
    server._register_session_cwd(session)
    assert terminal_tool.get_session_cwd(session_key) == str(old_cwd)

    try:
        before_update = json.loads(
            terminal_tool.terminal_tool(command="pwd", task_id=session_key)
        )
        assert before_update["exit_code"] == 0, before_update
        assert os.path.realpath(
            before_update["output"].strip()
        ) == os.path.realpath(str(old_cwd))

        updated = host._ensure_server_session(
            server,
            {
                "sid": sid,
                "session_key": session_key,
                "cwd": str(new_cwd),
                "cwd_owned": True,
                "explicit_cwd": True,
                "cwd_from_settle": False,
                "workspace_topology_generation": 1,
            },
        )
        assert updated["cwd"] == str(new_cwd)
        assert updated["_workspace_topology_generation"] == 1

        result = json.loads(
            terminal_tool.terminal_tool(command="pwd", task_id=session_key)
        )
        assert result["exit_code"] == 0, result
        assert os.path.realpath(result["output"].strip()) == os.path.realpath(
            str(new_cwd)
        )

        transient = json.loads(
            terminal_tool.terminal_tool(
                command="pwd", task_id=session_key, workdir=str(old_cwd)
            )
        )
        assert transient["exit_code"] == 0, transient
        assert os.path.realpath(transient["output"].strip()) == os.path.realpath(
            str(old_cwd)
        )
        after_transient = json.loads(
            terminal_tool.terminal_tool(command="pwd", task_id=session_key)
        )
        assert after_transient["exit_code"] == 0, after_transient
        assert os.path.realpath(
            after_transient["output"].strip()
        ) == os.path.realpath(str(new_cwd))
    finally:
        terminal_tool.cleanup_all_environments()
        terminal_tool.clear_task_env_overrides(session_key)
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_reused_session_rejects_stale_topology_and_skips_noop_reset(
    monkeypatch, tmp_path
):
    from tools import terminal_tool

    current_cwd = tmp_path / "current-workspace"
    stale_cwd = tmp_path / "stale-workspace"
    shell_cwd = tmp_path / "shell-subdirectory"
    current_cwd.mkdir()
    stale_cwd.mkdir()
    shell_cwd.mkdir()
    sid = "host-topology-ordering"
    session = {
        "agent": object(),
        "cwd": str(current_cwd),
        "cwd_owned": True,
        "explicit_cwd": True,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "session_key": "stored-host-topology-ordering",
        "_workspace_topology_generation": 2,
    }
    host = ComputeHost(stdout=io.StringIO(), max_workers=1, heartbeat_secs=0)
    resets = []
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        server,
        "_finish_explicit_session_cwd_change",
        lambda current, resolved, *, persist: resets.append(
            (current["cwd"], resolved, persist)
        ),
    )
    server._sessions[sid] = session
    server._register_session_cwd(session)
    terminal_tool.record_session_cwd(session["session_key"], str(shell_cwd))

    try:
        for stale_generation in (1, 2):
            host._ensure_server_session(
                server,
                {
                    "sid": sid,
                    "session_key": session["session_key"],
                    "cwd": str(stale_cwd),
                    "cwd_owned": False,
                    "explicit_cwd": False,
                    "cwd_from_settle": True,
                    "workspace_topology_generation": stale_generation,
                },
            )
            assert session["cwd"] == str(current_cwd)
            assert session["cwd_owned"] is True
            assert session["explicit_cwd"] is True
            assert session["cwd_from_settle"] is False
            assert session["_workspace_topology_generation"] == 2
            assert terminal_tool.get_session_cwd(session["session_key"]) == str(
                shell_cwd
            )

        # A causally newer explicit move re-anchors an active shell even when
        # its topology tuple is idempotent, but does not tear down/reseed the
        # host environment because its workspace/backend topology is unchanged.
        host._ensure_server_session(
            server,
            {
                "sid": sid,
                "session_key": session["session_key"],
                "cwd": str(current_cwd),
                "cwd_owned": True,
                "explicit_cwd": True,
                "cwd_from_settle": False,
                "workspace_topology_generation": 3,
            },
        )
        assert session["_workspace_topology_generation"] == 3
        assert terminal_tool.get_session_cwd(session["session_key"]) == str(
            current_cwd
        )
        assert resets == []
    finally:
        terminal_tool.clear_task_env_overrides(session["session_key"])
        server._sessions.pop(sid, None)
        host.close()


@pytest.mark.parametrize(
    ("source", "frame_extra", "expected_owned"),
    [
        ("tui", {}, True),
        (
            "desktop",
            {
                "cwd_owned": False,
                "workspace_topology_generation": 1,
            },
            False,
        ),
    ],
    ids=["legacy-absent-cwd-owned", "detached-neutral"],
)
def test_compute_host_reused_session_preserves_workspace_ownership_registration(
    monkeypatch, tmp_path, source, frame_extra, expected_owned
):
    from tools import terminal_tool

    old_cwd = tmp_path / f"{source}-old"
    new_cwd = tmp_path / f"{source}-new"
    old_cwd.mkdir()
    new_cwd.mkdir()
    sid = f"host-{source}-ownership"
    session_key = f"stored-host-{source}-ownership"
    session = {
        "agent": object(),
        "cwd": str(old_cwd),
        "explicit_cwd": False,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "session_key": session_key,
        "source": source,
    }
    if source == "desktop":
        session["cwd_owned"] = False
        session["_workspace_topology_generation"] = 0

    host = ComputeHost(stdout=io.StringIO(), max_workers=1, heartbeat_secs=0)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    server._sessions[sid] = session
    server._register_session_cwd(session)

    try:
        host._ensure_server_session(
            server,
            {
                "sid": sid,
                "session_key": session_key,
                "cwd": str(new_cwd),
                "explicit_cwd": False,
                **frame_extra,
            },
        )
        assert ("cwd_owned" in session) is (source == "desktop")
        assert server._session_owns_cwd(session) is expected_owned
        assert terminal_tool.get_session_cwd(session_key) == str(new_cwd)
        assert terminal_tool.resolve_task_overrides(session_key) == {
            "cwd": str(new_cwd),
            "cwd_source": "session",
        }
    finally:
        terminal_tool.clear_task_env_overrides(session_key)
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_turn_end_returns_host_workspace_topology(monkeypatch, tmp_path):
    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    settled_cwd = tmp_path / "settled-project"
    settled_cwd.mkdir()
    session = {
        "agent": object(),
        "cwd": str(tmp_path),
        "cwd_owned": False,
        "explicit_cwd": False,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "session_key": "host-topology",
    }
    monkeypatch.setattr(host, "_ensure_server_session", lambda _server, _frame: session)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)

    def run_prompt(*_args, **_kwargs):
        with session["history_lock"]:
            session.update(
                {
                    "cwd": str(settled_cwd),
                    "cwd_owned": True,
                    "explicit_cwd": True,
                    "cwd_from_settle": True,
                    "running": False,
                }
            )
            server._bump_workspace_topology_generation_locked(session)

    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session: {
            "cwd": _session["cwd"],
            "cwd_owned": _session["cwd_owned"],
        },
    )

    try:
        host._run_real_turn(
            {
                "request_id": "turn-topology",
                "sid": "host-topology",
                "text": "settle",
                "workspace_topology_generation": 0,
            }
        )
        turn_end = next(
            frame
            for frame in _json_lines(out)
            if frame.get("type") == "turn.end"
        )
        assert turn_end["session_info"] == {
            "cwd": str(settled_cwd),
            "cwd_owned": True,
            "explicit_cwd": True,
            "cwd_from_settle": True,
        }
        assert turn_end["workspace_topology_base_generation"] == 0
        assert turn_end["workspace_topology_generation"] == 1
        assert turn_end["session_info_emitted"] is False
    finally:
        host.close()


@pytest.mark.parametrize("route_name", ["slash.prompt", "session.compress"])
def test_compute_host_control_session_info_carries_topology_generations(
    monkeypatch, route_name
):
    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    sid = f"control-topology-{route_name}"
    session = {
        "agent": object(),
        "cwd": "/host/workspace",
        "cwd_owned": True,
        "explicit_cwd": True,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 4,
        "session_key": "stored-control-topology",
        "_workspace_topology_generation": 3,
    }
    server._sessions[sid] = session
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session: {
            "cwd": _session["cwd"],
            "cwd_owned": _session["cwd_owned"],
        },
    )
    monkeypatch.setattr(server, "_mirror_slash_side_effects", lambda *_args: "ok")
    if route_name == "session.compress":
        monkeypatch.setitem(
            server._methods,
            "session.compress",
            lambda _rid, _params: {"result": {"status": "compressed"}},
        )

    try:
        host._handle_control(
            {
                "type": "control",
                "sid": sid,
                "request_id": "control-1",
                "route_name": route_name,
                "command": "/prompt concise",
                "workspace_topology_generation": 2,
            }
        )
        ack = next(
            frame
            for frame in _json_lines(out)
            if frame.get("type") == "control.ack"
        )
        assert ack["workspace_topology_base_generation"] == 2
        assert ack["workspace_topology_generation"] == 3
        assert ack["session_info"] == {
            "cwd": "/host/workspace",
            "cwd_owned": True,
            "explicit_cwd": True,
            "cwd_from_settle": False,
        }
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_compute_host_control_defers_direct_session_info_until_parent_ack(
    monkeypatch,
):
    out = io.StringIO()
    host = ComputeHost(stdout=out, max_workers=1, heartbeat_secs=0)
    sid = "stale-control-session-info"
    session = {
        "agent": object(),
        "cwd": "/child/old",
        "cwd_owned": False,
        "explicit_cwd": False,
        "cwd_from_settle": False,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 1,
        "session_key": "stored-stale-control",
        "transport": host._transport,
        "_workspace_topology_generation": 0,
        "_defer_compute_host_session_info": True,
    }
    server._sessions[sid] = session
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, current: {
            "cwd": current["cwd"],
            "cwd_owned": current["cwd_owned"],
        },
    )

    def emit_before_ack(*_args):
        server._emit(
            "session.info",
            sid,
            {"cwd": "/child/old", "cwd_owned": False},
        )
        return "ok"

    monkeypatch.setattr(server, "_mirror_slash_side_effects", emit_before_ack)

    try:
        host._handle_control(
            {
                "type": "control",
                "sid": sid,
                "request_id": "control-stale",
                "route_name": "slash.prompt",
                "command": "/prompt concise",
                "workspace_topology_generation": 1,
            }
        )
        frames = _json_lines(out)
        assert not [frame for frame in frames if frame.get("type") == "rpc"]
        ack = next(frame for frame in frames if frame.get("type") == "control.ack")
        assert ack["workspace_topology_base_generation"] == 1
        assert ack["workspace_topology_generation"] == 0
    finally:
        server._sessions.pop(sid, None)
        host.close()


def test_supervisor_still_forwards_ordinary_child_rpc(tmp_path):
    forwarded = []
    supervisor = HostSupervisor(
        registry_path=tmp_path / "compute-host.json",
        rpc_sink=forwarded.append,
        autostart=False,
    )
    message = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": "sid",
            "payload": {"text": "still forwarded"},
        },
    }

    supervisor._handle_host_frame({"type": "rpc", "message": message})

    assert forwarded == [message]


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


def _record_finalize(monkeypatch, events: list[str], *sids: str) -> None:
    """Give ``flush_all_sessions`` sessions and record which ones finalize."""
    keys = sids or ("s1",)
    monkeypatch.setattr(
        server,
        "_sessions",
        {sid: {"session_key": sid} for sid in keys},
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_finalize_session",
        lambda _session, end_reason="tui_close": events.append(
            f"finalize:{_session['session_key']}:{end_reason}"
        ),
        raising=False,
    )


def _register_turn(host: ComputeHost, fn, sid: str = "s1") -> None:
    """Submit a turn exactly the way ``_handle_turn_start`` does."""
    host._track_turn_future(host._executor.submit(fn), sid)


def test_shutdown_drains_in_flight_turn_before_finalizing_sessions(monkeypatch):
    events: list[str] = []
    _record_finalize(monkeypatch, events)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    running = threading.Event()

    def _turn() -> None:
        running.set()
        time.sleep(0.3)
        events.append("turn_end")

    _register_turn(host, _turn, sid="s1")
    assert running.wait(timeout=5.0)

    host.shutdown(reason="sigterm", wait=3.0)

    # ``_finalize_session`` latches on ``session["_finalized"]``, so its single
    # run has to observe the finished turn or the tail is unpersistable. A turn
    # that *did* drain must still finalize — the live-turn skip must not
    # over-reach into sessions whose work is done.
    assert events == ["turn_end", "finalize:s1:compute_host_sigterm"]

    # The done-callback still has to remove the entry now that the container is
    # a dict: ``set.discard`` was a valid bare callback, ``dict.pop`` is not.
    deadline = time.monotonic() + 2.0
    while host._turn_futures and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host._turn_futures == {}, "in-flight turns must not accumulate"


def test_shutdown_retains_a_live_turns_session_when_the_drain_deadline_expires(monkeypatch):
    wait = 1.0
    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="sigterm", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    # ``_finalize_session`` is one-shot, and the ``shutdown(wait=False)`` that
    # follows does not join the turn. Spending "live"'s single latch mid-turn
    # would leave it permanently un-finalizable and release its active-session
    # lease out from under running work — the same lifecycle race the drain
    # exists to close, just moved past the deadline. It is retained unfinalized
    # for recovery instead. A turn outliving the window must not cost the flush
    # for anyone else, so "idle" still finalizes in the same pass.
    assert events == ["finalize:idle:compute_host_sigterm"]
    assert elapsed < wait


def test_shutdown_retains_live_sessions_within_the_stdin_closed_budget(monkeypatch):
    """The tightest real budget any caller uses is ``wait=2.0``.

    ``run_host`` finalizes through ``host.shutdown(reason="stdin_closed",
    wait=2.0)``, which is where the reserve — ``wait`` minus
    ``min(_FLUSH_RESERVE_SECS, wait / 2)`` — has the least room to work with.
    The retain-live-sessions rule must hold there without costing the flush for
    idle sessions and without pushing the call past the budget the supervisor's
    kill escalation is timed against.
    """
    wait = 2.0
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="stdin_closed", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_stdin_closed"]
    assert elapsed >= drain_budget - 1e-6, "the drain must use its full window"
    assert elapsed < wait


def test_shutdown_drain_sleep_never_overshoots_the_reserve(monkeypatch):
    """The drain's per-tick sleep must be bounded by the time left to it.

    A flat tick overshoots the drain deadline by up to one tick, eating the
    reserve held back for ``flush_all_sessions``; for a small ``wait`` that is
    the whole reserve. Asserting on the *requested* sleep totals rather than on
    wall-clock keeps this deterministic: each sleep is clamped to the remaining
    time, so the sum can never exceed the drain budget however the scheduler
    interleaves.
    """
    wait = 0.34
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "idle")

    slept: list[float] = []
    real_sleep = time.sleep

    def _recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(compute_host.time, "sleep", _recording_sleep)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        host.shutdown(reason="sigterm", wait=wait)
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_sigterm"]
    assert slept, "the drain loop should have ticked at least once"
    assert sum(slept) <= drain_budget + 1e-6
