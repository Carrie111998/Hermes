"""Separate process ownership from parent notification routing."""

import json

import tools.process_registry as process_registry_module
from tools.process_registry import ProcessRegistry, ProcessSession


def _child_session(**overrides):
    values = {
        "id": "proc_childroute01",
        "command": "python child_job.py",
        "task_id": "sa-0-childroute",
        "session_key": "sa-0-childroute",
        "parent_session_id": "root-session",
        "notification_session_key": "root-session",
        "started_at": 1234.5,
    }
    values.update(overrides)
    return ProcessSession(**values)


def test_completion_routes_to_root_without_changing_process_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        process_registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json"
    )
    registry = ProcessRegistry()
    session = _child_session(
        exited=True,
        exit_code=7,
        output_buffer="ERROR_VISIBILITY_CANARY\n",
        notify_on_complete=True,
    )
    registry._running[session.id] = session

    registry._move_to_finished(session)

    event = registry.completion_queue.get_nowait()
    assert event["session_key"] == "root-session"
    assert event["parent_session_id"] == "root-session"
    assert event["exit_code"] == 7
    assert registry.get(session.id).session_key == "sa-0-childroute"


def test_running_process_remains_owned_only_by_child_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json"
    )
    registry = ProcessRegistry()
    session = _child_session()
    registry._running[session.id] = session

    child_rows = registry.list_sessions(session_key="sa-0-childroute")
    parent_rows = registry.list_sessions(session_key="root-session")

    assert [row["session_id"] for row in child_rows] == [session.id]
    assert parent_rows == []


def test_watch_event_uses_root_notification_route(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json"
    )
    registry = ProcessRegistry()
    session = _child_session(watch_patterns=["FAIL"])

    registry._check_watch_patterns(session, "FAIL deterministic marker")

    event = registry.completion_queue.get_nowait()
    assert event["type"] == "watch_match"
    assert event["session_key"] == "root-session"
    assert event["parent_session_id"] == "root-session"
    assert session.session_key == "sa-0-childroute"


def test_checkpoint_roundtrip_preserves_notification_and_process_scopes(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)
    writer = ProcessRegistry()
    session = _child_session(
        pid=12345,
        host_start_time=99,
        watcher_interval=5,
        notify_on_complete=True,
    )
    writer._running[session.id] = session
    writer._write_checkpoint()

    stored = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert stored[0]["session_key"] == "sa-0-childroute"
    assert stored[0]["notification_session_key"] == "root-session"
    assert stored[0]["parent_session_id"] == "root-session"

    reader = ProcessRegistry()
    monkeypatch.setattr(reader, "_host_pid_is_ours", lambda pid, started: True)
    assert reader.recover_from_checkpoint() == 1

    recovered = reader.get(session.id)
    assert recovered is not None
    assert recovered.session_key == "sa-0-childroute"
    assert recovered.notification_session_key == "root-session"
    assert recovered.parent_session_id == "root-session"
    assert reader.pending_watchers[0]["session_key"] == "root-session"
    assert reader.pending_watchers[0]["parent_session_id"] == "root-session"


def test_watch_disabled_event_stamps_parent_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json"
    )
    registry = ProcessRegistry()
    session = _child_session(watch_patterns=["FAIL"])

    registry._emit_lifetime_watch_disabled(session)

    event = registry.completion_queue.get_nowait()
    assert event["type"] == "watch_disabled"
    assert event["session_key"] == "root-session"
    assert event["parent_session_id"] == "root-session"


def test_spawn_local_checkpoint_includes_notify_flags_before_exit(
    tmp_path, monkeypatch
):
    import sys
    import time

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        command=f'{sys.executable} -c "import time; time.sleep(60)"',
        cwd=str(tmp_path),
        task_id="sa-0-spawnckpt",
        session_key="sa-0-spawnckpt",
        notification_session_key="root-session",
        parent_session_id="root-session",
        notify_on_complete=True,
        watcher_interval=5,
        watcher_platform="telegram",
        watcher_chat_id="123",
    )
    try:
        stored = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert stored[0]["notify_on_complete"] is True
        assert stored[0]["watcher_interval"] == 5
        assert stored[0]["watcher_platform"] == "telegram"
        assert stored[0]["parent_session_id"] == "root-session"
        assert stored[0]["notification_session_key"] == "root-session"
        assert stored[0]["session_key"] == "sa-0-spawnckpt"
        deadline = time.time() + 5
        while time.time() < deadline and not session.exited:
            if stored[0]["notify_on_complete"] is True:
                break
            time.sleep(0.05)
    finally:
        registry.kill_process(session.id)


def test_fast_nonzero_exit_queues_completion_when_notify_set_at_spawn(
    tmp_path, monkeypatch
):
    import time

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        command="exit 7",
        cwd=str(tmp_path),
        task_id="sa-0-fastfail",
        session_key="sa-0-fastfail",
        notification_session_key="root-session",
        parent_session_id="root-session",
        notify_on_complete=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline and not session.exited:
        time.sleep(0.05)
    assert session.exited is True
    assert session.exit_code == 7

    events = []
    while not registry.completion_queue.empty():
        events.append(registry.completion_queue.get_nowait())
    completions = [evt for evt in events if evt.get("type") == "completion"]
    assert len(completions) == 1
    assert completions[0]["exit_code"] == 7
    assert completions[0]["parent_session_id"] == "root-session"
    assert completions[0]["session_key"] == "root-session"


def test_should_surface_failed_child_but_suppress_successful_child_noise(
    monkeypatch,
):
    monkeypatch.setattr(
        ProcessRegistry,
        "_surface_child_process_notifications",
        staticmethod(lambda: False),
    )
    failed = {
        "type": "completion",
        "task_id": "sa-0-child",
        "exit_code": 7,
        "completion_reason": "exited",
    }
    success = {
        "type": "completion",
        "task_id": "sa-0-child",
        "exit_code": 0,
        "completion_reason": "exited",
    }
    watch = {
        "type": "watch_match",
        "task_id": "sa-0-child",
        "pattern": "FAIL",
    }
    assert ProcessRegistry.should_surface_process_notification(failed) is True
    assert ProcessRegistry.should_surface_process_notification(success) is False
    assert ProcessRegistry.should_surface_process_notification(watch) is False
