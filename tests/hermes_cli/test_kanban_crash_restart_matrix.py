from __future__ import annotations

import multiprocessing as mp
import json
import os
import signal
import sqlite3
import sys
import time

import pytest

from hermes_cli.kanban_delivery_outbox import (
    due_children,
    init_schema,
    lease_child,
    mark_sending,
    mark_sent,
    materialize_parent,
    recover_expired,
)


def _source(event: str) -> dict:
    return {
        "board_uuid": "board",
        "event_id": event,
        "subscription_id": "sub",
        "destination": "telegram:1",
        "payload_version": "v1",
    }


def _push() -> dict:
    return {
        "version": "route-capability-v1",
        "adapter_type": "telegram",
        "adapter_version": "1",
        "route_kind": "push",
        "supports_async_delivery": True,
        "creator_wake_applicable": False,
        "artifact_transport": "telegram",
        "artifact_policy_version": "artifact-policy-v1",
    }


def _non_push() -> dict:
    capability = _push()
    capability.update(
        adapter_type="api_server",
        route_kind="non_push",
        supports_async_delivery=False,
        creator_wake_applicable=True,
        creator_session_id="session-1",
        artifact_transport="creator_session",
    )
    return capability


def _authority_holder(state_root: str, ready) -> None:
    os.environ["HERMES_STATE_ROOT"] = state_root
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("crash-holder")
    ready.send(acquired.state.value)
    if acquired.lease is None:
        return
    while True:
        time.sleep(10)


def _materialize_then_crash(db_path: str, state_root: str, ready) -> None:
    os.environ["HERMES_STATE_ROOT"] = state_root
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("capability-crash")
    if acquired.lease is None:
        ready.send(("authority_failed", None))
        return
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    parent_id = materialize_parent(
        conn,
        source=_source("c1"),
        capability=_non_push(),
        text="must-not-send",
    )
    conn.close()
    ready.send(("materialized", parent_id))
    os.kill(os.getpid(), signal.SIGKILL)


def _pinned_worker(ready) -> None:
    keys = (
        "HERMES_PROFILE",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_TOKEN",
        "HERMES_MODEL",
        "HERMES_PROVIDER",
        "HERMES_ENABLED_TOOLSETS",
    )
    ready.send({key: os.environ.get(key) for key in keys})
    while True:
        time.sleep(10)


def _crash_after_sending(db_path: str, child_id: str, ready) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    token = lease_child(conn, child_id, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, child_id, token, now=10)
    conn.close()
    ready.send("sending")
    os.kill(os.getpid(), signal.SIGKILL)


def _crash_mid_artifacts(db_path: str, first: str, second: str, ready) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    token1 = lease_child(conn, first, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, first, token1, now=10)
    mark_sent(conn, first, token1, receipt="safe:first", now=10)
    token2 = lease_child(conn, second, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, second, token2, now=10)
    conn.close()
    ready.send("middle")
    os.kill(os.getpid(), signal.SIGKILL)


def _spawn_scoped_worker_then_crash(
    task,
    workspace: str,
    state_root: str,
    hermes_home: str,
    hermes_executable: str,
    systemd_run: str,
    systemctl: str,
    ready,
) -> None:
    os.environ["HERMES_STATE_ROOT"] = state_root
    os.environ["HERMES_HOME"] = hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher
    from hermes_cli import worker_scope

    kb._resolve_hermes_argv = lambda: [sys.executable, hermes_executable]
    original_which = worker_scope.shutil.which
    worker_scope.shutil.which = lambda name: {
        "systemd-run": systemd_run,
        "systemctl": systemctl,
    }.get(name, original_which(name))

    acquired = acquire_machine_dispatcher("production-spawn-crash")
    if acquired.lease is None:
        ready.send(("authority_failed", None))
        return
    pid = kb._invoke_worker_spawn(
        acquired.lease, None, task, workspace, board="default"
    )
    ready.send(("spawned", pid))
    os.kill(os.getpid(), signal.SIGKILL)


def test_g_authority_sigkill_releases_lock_and_restart_gets_fresh_lease(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    state_root = str(tmp_path / "state")
    monkeypatch.setenv("HERMES_STATE_ROOT", state_root)
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_authority_holder, args=(state_root, child))
    proc.start()
    assert parent.recv() == "acquired"
    denied = acquire_machine_dispatcher("contender")
    assert denied.state is AcquireState.CONTENDED
    assert proc.pid is not None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL
    restarted = acquire_machine_dispatcher("restart")
    assert restarted.state is AcquireState.ACQUIRED
    assert restarted.lease is not None
    restarted.lease.release()


def test_n_crash_after_sending_recovers_same_child_identity(tmp_path):
    db = tmp_path / "outbox.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    parent_id = materialize_parent(conn, source=_source("n1"), capability=_push(), text="done")
    child_id = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='primary_text'",
        (parent_id,),
    ).fetchone()[0]
    conn.close()

    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_crash_after_sending, args=(str(db), child_id, child))
    proc.start()
    assert parent.recv() == "sending"
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL

    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    assert recover_expired(conn, now=12) == [child_id]
    assert [row["child_id"] for row in due_children(conn, parent_id=parent_id, now=12)] == [child_id]
    assert conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=?", (parent_id,)
    ).fetchone()[0] == child_id
    conn.close()


def test_n_partial_artifact_batch_preserves_sent_sibling_and_retries_only_incomplete(tmp_path):
    db = tmp_path / "artifacts.db"
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    import hashlib

    artifacts = [
        {"manifest_id": "a", "path": str(a), "sha256": hashlib.sha256(b"a").hexdigest()},
        {"manifest_id": "b", "path": str(b), "sha256": hashlib.sha256(b"b").hexdigest()},
    ]
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    parent_id = materialize_parent(
        conn, source=_source("n2"), capability=_push(), text="done", artifacts=artifacts
    )
    rows = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='artifact_upload' ORDER BY ordinal",
        (parent_id,),
    ).fetchall()
    first, second = rows[0][0], rows[1][0]
    text_child = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='primary_text'",
        (parent_id,),
    ).fetchone()[0]
    text_token = lease_child(conn, text_child, "setup", now=1, lease_seconds=5)
    mark_sending(conn, text_child, text_token, now=1)
    mark_sent(conn, text_child, text_token, receipt="safe:text", now=1)
    conn.close()

    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_crash_mid_artifacts, args=(str(db), first, second, child))
    proc.start()
    assert parent.recv() == "middle"
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL

    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    assert recover_expired(conn, now=12) == [second]
    states = dict(
        conn.execute(
            "SELECT child_id,state FROM kanban_delivery_children WHERE child_id IN (?,?)",
            (first, second),
        ).fetchall()
    )
    assert states == {first: "sent", second: "failed"}
    assert [row["child_id"] for row in due_children(conn, parent_id=parent_id, now=12)] == [second]
    conn.close()


def test_c_capability_shape_survives_sigkill_restart_and_route_drift(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    db = tmp_path / "capability.db"
    state_root = str(tmp_path / "state")
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_materialize_then_crash, args=(str(db), state_root, child))
    proc.start()
    state, original_parent = parent.recv()
    assert state == "materialized"
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL

    monkeypatch.setenv("HERMES_STATE_ROOT", state_root)
    restarted = acquire_machine_dispatcher("capability-restart")
    assert restarted.lease is not None
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    from hermes_cli.kanban_delivery_outbox import CapabilityError

    with pytest.raises(CapabilityError, match="snapshot"):
        materialize_parent(
            conn,
            source=_source("c1"),
            capability=_push(),
            text="route-drifted",
        )
    rows = conn.execute(
        "SELECT kind FROM kanban_delivery_children WHERE parent_id=? ORDER BY ordinal",
        (original_parent,),
    ).fetchall()
    assert [row["kind"] for row in rows] == ["creator_wake"]
    conn.close()
    restarted.lease.release()


def test_d_dashboard_status_only_maps_dispatcher_crash_to_503_without_board_state(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from plugins.kanban.dashboard import plugin_api

    state_root = str(tmp_path / "state")
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_STATE_ROOT", state_root)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_authority_holder, args=(state_root, child))
    proc.start()
    assert parent.recv() == "acquired"

    try:
        plugin_api.dispatch(board="must-not-resolve")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["dispatch_performed"] is False
    else:
        raise AssertionError("status-only dashboard endpoint unexpectedly returned success")

    assert proc.pid is not None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL
    try:
        plugin_api.dispatch(board="must-not-resolve")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail["dispatch_nudge_accepted"] is False
    else:
        raise AssertionError("crashed dispatcher status unexpectedly returned success")
    assert not hermes_home.exists()


def test_p_pinned_worker_survives_dispatcher_sigkill_and_restart(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    state_root = str(tmp_path / "state")
    monkeypatch.setenv("HERMES_STATE_ROOT", state_root)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "specialist"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n    - file\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("kanban: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    observation = tmp_path / "worker-observation.json"
    stop_file = tmp_path / "stop-worker"
    monkeypatch.setenv("P_OBSERVATION", str(observation))
    monkeypatch.setenv("P_STOP_FILE", str(stop_file))
    fake_hermes = tmp_path / "fake_hermes.py"
    fake_hermes.write_text(
        """import json, os, sys, time
keys = [
 'HERMES_PROFILE', 'HERMES_HOME', 'HERMES_KANBAN_TASK',
 'HERMES_KANBAN_WORKSPACE', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_DB',
 'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_KANBAN_RUN_ID',
 'HERMES_KANBAN_CLAIM_LOCK', 'HERMES_KANBAN_MODEL',
 'HERMES_KANBAN_PROVIDER', 'HERMES_KANBAN_TOOLSETS', 'TERMINAL_CWD',
]
payload = {'pid': os.getpid(), 'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in keys}}
with open(os.environ['P_OBSERVATION'], 'w', encoding='utf-8') as fh:
 json.dump(payload, fh, sort_keys=True)
while not os.path.exists(os.environ['P_STOP_FILE']):
 time.sleep(0.02)
""",
        encoding="utf-8",
    )
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\nprintf 'running\\n'\n", encoding="utf-8")
    fake_systemctl.chmod(0o755)
    fake_systemd_run = tmp_path / "systemd-run"
    fake_systemd_run.write_text(
        """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
unit = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--unit=')), '')
if unit.startswith('hermes-kanban-isolation-probe-'):
 print(f'0::/user.slice/user@1000.service/app.slice/{unit}.scope')
 raise SystemExit(0)
for arg in args:
 if arg.startswith('--setenv='):
  key, value = arg[len('--setenv='):].split('=', 1)
  os.environ[key] = value
command = args[args.index('--') + 1:]
os.execvpe(command[0], command, os.environ)
""",
        encoding="utf-8",
    )
    fake_systemd_run.chmod(0o755)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_exact",
        title="production P",
        body=None,
        assignee="specialist",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=str(workspace),
        claim_lock="opaque-claim",
        claim_expires=999,
        tenant=None,
        current_run_id=77,
        model_override="exact-model",
        provider_override="exact-provider",
    )
    dispatch_parent, dispatch_child = mp.Pipe(duplex=False)
    dispatcher = mp.Process(
        target=_spawn_scoped_worker_then_crash,
        args=(
            task,
            str(workspace),
            state_root,
            str(root),
            str(fake_hermes),
            str(fake_systemd_run),
            str(fake_systemctl),
            dispatch_child,
        ),
    )
    dispatcher.start()
    status, worker_pid = dispatch_parent.recv()
    assert status == "spawned"
    dispatcher.join(timeout=5)
    assert dispatcher.exitcode == -signal.SIGKILL

    deadline = time.time() + 5
    while not observation.exists() and time.time() < deadline:
        time.sleep(0.02)
    observed = json.loads(observation.read_text(encoding="utf-8"))
    assert observed["pid"] == worker_pid
    expected_env = {
        "HERMES_PROFILE": "specialist",
        "HERMES_HOME": str(profile),
        "HERMES_KANBAN_TASK": "t_exact",
        "HERMES_KANBAN_WORKSPACE": str(workspace),
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_RUN_ID": "77",
        "HERMES_KANBAN_CLAIM_LOCK": "opaque-claim",
        "HERMES_KANBAN_MODEL": "exact-model",
        "HERMES_KANBAN_PROVIDER": "exact-provider",
        "HERMES_KANBAN_TOOLSETS": "bfl,file,kanban,terminal",
        "TERMINAL_CWD": str(workspace),
    }
    for key, value in expected_env.items():
        assert observed["env"][key] == value
    assert observed["env"]["HERMES_KANBAN_DB"] == str(root / "kanban.db")
    assert observed["env"]["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
        root / "kanban" / "workspaces"
    )
    assert observed["argv"] == [
        "-p", "specialist", "--cli", "--accept-hooks",
        "-m", "exact-model", "--provider", "exact-provider",
        "--toolsets", "bfl,file,kanban,terminal", "chat", "-q",
        "work kanban task t_exact",
    ]
    os.kill(worker_pid, 0)

    restarted = acquire_machine_dispatcher("post-worker-crash-restart")
    assert restarted.lease is not None
    os.kill(worker_pid, 0)
    restarted.lease.release()
    stop_file.write_text("stop", encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
