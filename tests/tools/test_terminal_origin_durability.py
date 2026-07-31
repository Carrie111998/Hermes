"""Durable immutable origins for terminal(background=True)."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

from tools.process_registry import ProcessRegistry, ProcessSession


def _watcher_config():
    return {
        "platform": "feishu",
        "chat_id": "oc_chat",
        "user_id": "ou_user",
        "user_name": "User",
        "thread_id": "",
        "origin_message_id": "om_origin",
        "origin_source": {
            "platform": "feishu",
            "chat_id": "oc_chat",
            "chat_type": "dm",
            "user_id": "ou_user",
            "message_id": "om_origin",
            "profile": "coder",
        },
        "origin_profile": "coder",
        "parent_session_id": "sess_parent",
        "check_interval": 5,
        "notify_on_complete": True,
        "watch_patterns": [],
    }


def test_first_spawn_checkpoint_contains_immutable_origin(tmp_path):
    registry = ProcessRegistry()
    checkpoint = tmp_path / "processes.json"
    fake_proc = MagicMock(pid=4242, stdout=MagicMock())
    fake_thread = MagicMock()
    config = _watcher_config()

    with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint), \
         patch("tools.process_registry.subprocess.Popen", return_value=fake_proc), \
         patch("tools.process_registry.threading.Thread", return_value=fake_thread), \
         patch.object(registry, "_safe_host_start_time", return_value=99):
        session = registry.spawn_local(
            "codex exec --full-auto task",
            cwd=str(tmp_path),
            watcher_config=config,
        )

    config["origin_source"]["chat_id"] = "mutated"
    persisted = json.loads(checkpoint.read_text())[0]
    assert persisted["notify_on_complete"] is True
    assert persisted["watcher_message_id"] == "om_origin"
    assert persisted["watcher_origin_source"]["chat_id"] == "oc_chat"
    assert persisted["watcher_profile"] == "coder"
    assert persisted["watcher_parent_session_id"] == "sess_parent"
    assert session.watcher_origin_source["chat_id"] == "oc_chat"
    assert session.output_log_path.startswith(str(tmp_path.parent))


def test_finished_notification_survives_restart_until_ack(tmp_path):
    checkpoint = tmp_path / "processes.json"
    output_log = tmp_path / "proc.log"
    output_log.write_text("final output\n")
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_final",
        command="codex exec task",
        pid=os.getpid(),
        started_at=1.0,
        output_buffer="final output\n",
        output_log_path=str(output_log),
        notify_on_complete=True,
    )
    registry._apply_watcher_config(session, _watcher_config())
    registry._running[session.id] = session
    session.exited = True
    session.exit_code = 0

    with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
        registry._move_to_finished(session)
        assert json.loads(checkpoint.read_text())[0]["exited"] is True

        restarted = ProcessRegistry()
        assert restarted.recover_from_checkpoint() == 1
        recovered = restarted.get("proc_final")
        assert recovered.exited is True
        assert recovered.output_buffer == "final output\n"
        assert restarted.pending_watchers[0]["origin_message_id"] == "om_origin"

        restarted.mark_notification_delivered("proc_final")
        assert json.loads(checkpoint.read_text()) == []
        assert not output_log.exists()


def test_dead_pid_recovers_one_lost_final_with_origin(tmp_path):
    checkpoint = tmp_path / "processes.json"
    entry = {
        "session_id": "proc_dead",
        "command": "codex exec task",
        "pid": 987654321,
        "pid_scope": "host",
        "host_start_time": 1,
        "started_at": 1.0,
        "notify_on_complete": True,
        "watcher_interval": 5,
        "watcher_message_id": "om_origin",
        "watcher_origin_source": _watcher_config()["origin_source"],
        "watcher_profile": "coder",
        "watcher_parent_session_id": "sess_parent",
    }
    checkpoint.write_text(json.dumps([entry]))
    registry = ProcessRegistry()

    with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint), \
         patch.object(registry, "_host_pid_is_ours", return_value=False), \
         patch.object(registry, "_is_host_pid_alive", return_value=False):
        assert registry.recover_from_checkpoint() == 1
        recovered = registry.get("proc_dead")
        assert recovered.exited is True
        assert recovered.completion_reason == "lost"
        assert recovered.termination_source == "restart_recovery"
        assert registry.pending_watchers[0]["origin_profile"] == "coder"
        assert json.loads(checkpoint.read_text())[0]["exited"] is True


def test_completion_event_carries_terminal_origin(tmp_path):
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_event",
        command="codex exec task",
        started_at=1.0,
        notify_on_complete=True,
        output_buffer="done",
    )
    registry._apply_watcher_config(session, _watcher_config())
    registry._running[session.id] = session
    session.exited = True
    session.exit_code = 0

    with patch.object(registry, "_write_checkpoint"):
        registry._move_to_finished(session)
    event = registry.completion_queue.get_nowait()
    assert event["origin_message_id"] == "om_origin"
    assert event["origin_source"]["chat_id"] == "oc_chat"
    assert event["origin_profile"] == "coder"
    assert event["parent_session_id"] == "sess_parent"


def test_fast_exit_cannot_be_reinserted_as_running(tmp_path):
    registry = ProcessRegistry()
    checkpoint = tmp_path / "processes.json"
    fake_proc = MagicMock(pid=4242, returncode=0)
    fake_proc.poll.return_value = 0

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint), \
         patch("tools.process_registry.get_hermes_home", return_value=tmp_path), \
         patch("tools.process_registry.subprocess.Popen", return_value=fake_proc), \
         patch("tools.process_registry.threading.Thread", ImmediateThread), \
         patch.object(registry, "_safe_host_start_time", return_value=99):
        session = registry.spawn_local(
            "true", cwd=str(tmp_path), watcher_config=_watcher_config()
        )

    assert session.id not in registry._running
    assert registry._finished[session.id] is session
    assert registry.completion_queue.get_nowait()["session_id"] == session.id


def test_child_owned_log_keeps_growing_after_registry_restart(tmp_path):
    checkpoint = tmp_path / "processes.json"
    registry = ProcessRegistry()
    command = (
        f'{sys.executable} -c "import time; print(\'first\', flush=True); '
        'time.sleep(0.35); print(\'second\', flush=True)"'
    )
    no_reader = MagicMock()

    with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint), \
         patch("tools.process_registry.get_hermes_home", return_value=tmp_path), \
         patch("tools.process_registry.threading.Thread", return_value=no_reader):
        session = registry.spawn_local(
            command, cwd=str(tmp_path), watcher_config=_watcher_config()
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            if "first" in open(session.output_log_path, encoding="utf-8").read():
                break
            time.sleep(0.02)

        restarted = ProcessRegistry()
        with patch.object(
            restarted, "_host_pid_is_ours",
            side_effect=lambda *_args: session.process.poll() is None,
        ):
            assert restarted.recover_from_checkpoint() == 1
            assert "first" in restarted.get(session.id).output_buffer

            deadline = time.time() + 3
            while time.time() < deadline:
                recovered = restarted.get(session.id)
                if "second" in recovered.output_buffer and recovered.exited:
                    break
                time.sleep(0.02)

            assert "second" in recovered.output_buffer
            assert recovered.exited is True
            assert recovered.exit_code == 0


def test_prune_preserves_undelivered_final_and_artifacts(tmp_path):
    registry = ProcessRegistry()
    output_log = tmp_path / "pending.log"
    exit_file = tmp_path / "pending.exit"
    output_log.write_text("done\n")
    exit_file.write_text("0\n")
    session = ProcessSession(
        id="proc_pending",
        command="codex exec task",
        started_at=1.0,
        exited=True,
        notify_on_complete=True,
        output_log_path=str(output_log),
        exit_code_path=str(exit_file),
    )
    registry._finished[session.id] = session

    registry._prune_if_needed()

    assert registry._finished[session.id] is session
    assert output_log.exists()
    assert exit_file.exists()
