"""Gateway/session persistence recovery contracts for storage contention."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from tests.run_agent.test_tool_call_incremental_persistence import (
    _make_agent,
    _mock_response,
    _mock_tool_call,
)


@pytest.mark.parametrize(
    ("error_text", "cause"),
    [
        ("attempt to write a readonly database", "disk"),
        ("database disk image is malformed", "corrupt"),
    ],
)
def test_non_lock_persistence_failures_stay_terminal(error_text, cause):
    """Only locked/busy errors enter awaiting-persistence retry."""
    agent = _make_agent()
    tool_call = _mock_tool_call(call_id="non-lock-must-not-run")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="I'll inspect the repository now.",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    agent._flush_messages_to_session_db = MagicMock(
        side_effect=sqlite3.OperationalError(error_text)
    )
    agent._execute_tool_calls = MagicMock()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("inspect the repository")

    agent._execute_tool_calls.assert_not_called()
    assert agent._flush_messages_to_session_db.call_count == 1
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "session_persistence_failed"
    assert result["failure_reason"] == f"session_persistence_failed:{cause}"


def test_mixed_surface_lock_incident_recovers_in_subprocess(tmp_path):
    """Incident-shaped subprocess: mixed policy, active turn, queued messages, cron."""
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "hermes-home"
    script = textwrap.dedent(
        """
        from __future__ import annotations

        import json
        import os
        from pathlib import Path
        import sqlite3
        import threading
        import time
        from unittest.mock import MagicMock, patch

        import cron.scheduler as sched
        from agent.tool_dispatch_helpers import make_tool_result_message
        from hermes_cli import config as hermes_config
        from hermes_state import SessionDB
        from tests.run_agent.test_tool_call_incremental_persistence import (
            _make_agent,
            _mock_response,
            _mock_tool_call,
        )

        home = Path(os.environ["HERMES_INCIDENT_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(home)
        db_path = home / "state.db"

        def table_exists(name):
            conn = sqlite3.connect(str(db_path))
            try:
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?",
                    (name,),
                ).fetchone() is not None
            finally:
                conn.close()

        def hold_write_lock(path, hold_s, started_evt):
            conn = sqlite3.connect(str(path), timeout=1.0, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                started_evt.set()
                time.sleep(hold_s)
                conn.execute("COMMIT")
            finally:
                conn.close()

        hermes_config.load_config_readonly = (
            lambda: {"sessions": {"trigram_fts": True}}
        )
        initial = SessionDB(db_path=db_path)
        try:
            if not initial._trigram_available:
                print(json.dumps({"skip": "SQLite build lacks trigram tokenizer"}))
                raise SystemExit(0)
            initial.create_session("policy-seed", "gateway")
            initial.append_message(
                "policy-seed",
                role="user",
                content="大别山 mixed policy seed",
            )
        finally:
            initial.close()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM state_meta WHERE key = 'trigram_fts_policy'")
            conn.commit()
        finally:
            conn.close()

        hermes_config.load_config_readonly = (
            lambda: {"sessions": {"trigram_fts": False}}
        )
        os.environ["HERMES_DISABLE_FTS_TRIGRAM"] = "1"
        disabled = SessionDB(db_path=db_path)
        disabled.close()

        hermes_config.load_config_readonly = (
            lambda: {"sessions": {"trigram_fts": True}}
        )
        os.environ.pop("HERMES_DISABLE_FTS_TRIGRAM", None)
        owner_db = SessionDB(db_path=db_path)
        owner_db._TRANSCRIPT_WRITE_PATIENCE_S = 0.05
        owner_db._WRITE_RETRY_MIN_S = 0.01
        owner_db._WRITE_RETRY_MAX_S = 0.01
        owner_db._WRITE_LOCK_WARNING_AFTER_S = 0.0
        owner_db.create_session("gateway-active", "telegram")

        agent = _make_agent()
        agent._session_db = owner_db
        agent._session_db_created = True
        agent.session_id = "gateway-active"
        agent._last_flushed_db_idx = 0
        agent._flushed_db_message_ids = set()
        agent._flushed_db_message_session_id = None
        agent._persist_disabled = False
        agent._session_persistence_lock_wait_initial_s = 0.01
        agent._session_persistence_lock_wait_max_sleep_s = 0.02
        agent._session_persistence_lock_progress_interval_s = 0.0

        tool_call = _mock_tool_call(call_id="incident-tool-call")
        agent.client.chat.completions.create.side_effect = [
            _mock_response(
                content="I'll inspect the repository now.",
                finish_reason="tool_calls",
                tool_calls=[tool_call],
            ),
            _mock_response(content="active done", finish_reason="stop"),
        ]

        lock_started = threading.Event()
        lock_holder = {"thread": None}
        original_flush = agent._flush_messages_to_session_db

        def flush_with_incident_lock(messages, conversation_history=None):
            if (
                lock_holder["thread"] is None
                and messages
                and isinstance(messages[-1], dict)
                and messages[-1].get("role") == "assistant"
                and messages[-1].get("tool_calls")
            ):
                thread = threading.Thread(
                    target=hold_write_lock,
                    args=(owner_db.db_path, 0.25, lock_started),
                )
                lock_holder["thread"] = thread
                thread.start()
                assert lock_started.wait(5), "lock holder did not start"
            return original_flush(messages, conversation_history)

        agent._flush_messages_to_session_db = flush_with_incident_lock
        executed_tools = []

        def fake_execute(assistant_message, messages, effective_task_id, api_call_count=0):
            executed_tools.append(assistant_message.tool_calls[0].id)
            messages.append(
                make_tool_result_message(
                    "web_search",
                    "search result",
                    "incident-tool-call",
                )
            )

        active_result = {}
        with (
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_execute_tool_calls", side_effect=fake_execute),
        ):
            active_thread = threading.Thread(
                target=lambda: active_result.setdefault(
                    "result", agent.run_conversation("active inspect")
                )
            )
            active_thread.start()
            deadline = time.monotonic() + 5
            while (
                not getattr(agent, "_awaiting_session_persistence", False)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            queued_followups = ["queued one", "queued two"]
            owner_db._TRANSCRIPT_WRITE_PATIENCE_S = 1.0

            jobs = [
                    {
                        "id": f"incident-cron-{idx}",
                        "name": f"incident cron {idx}",
                        "prompt": "hello",
                        "model": "test-model",
                        "schedule": {"kind": "interval", "minutes": 5},
                    "enabled": True,
                    "next_run_at": "2020-01-01T00:00:00",
                    "deliver": "local",
                }
                for idx in range(3)
            ]

            class CronAgent:
                def __init__(self, **kwargs):
                    self.session_db = kwargs.get("session_db")
                    self.session_id = kwargs.get("session_id")
                    if self.session_db is not None and self.session_id:
                        self.session_db.create_session(self.session_id, "cron")

                def run_conversation(self, _prompt):
                    return {"final_response": "cron done"}

                def close(self):
                    return None

            with patch("hermes_state.SessionDB") as session_db_ctor, \
                 patch("cron.scheduler._hermes_home", home), \
                 patch("cron.scheduler._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch(
                     "hermes_cli.runtime_provider.resolve_runtime_provider",
                     return_value={
                         "api_key": "test-key",
                         "base_url": "https://example.invalid/v1",
                         "provider": "openrouter",
                         "api_mode": "chat_completions",
                     },
                 ), \
                 patch("run_agent.AIAgent", CronAgent), \
                 patch.object(sched, "get_due_jobs", return_value=jobs), \
                 patch.object(sched, "advance_next_runs"), \
                 patch.object(
                     sched,
                     "claim_job_for_fire",
                     side_effect=lambda job_id, return_job=False: next(
                         job for job in jobs if job["id"] == job_id
                     ),
                 ), \
                 patch.object(sched, "claim_dispatch", return_value=True), \
                 patch.object(
                     sched,
                     "create_execution",
                     side_effect=lambda job_id, source: {"id": f"exec-{job_id}"},
                 ), \
                 patch.object(sched, "mark_execution_running"), \
                 patch.object(sched, "finish_execution"), \
                 patch.object(sched, "save_job_output", return_value="/tmp/out"), \
                 patch.object(sched, "mark_job_run"), \
                 patch.object(sched, "_deliver_result", return_value=None):
                cron_count = sched.tick(
                    verbose=False,
                    owner_session_db=owner_db,
                )
                cron_constructor_calls = session_db_ctor.call_count

            active_thread.join(timeout=5)
            if active_thread.is_alive():
                raise RuntimeError("active turn did not complete")

            if lock_holder["thread"] is not None:
                lock_holder["thread"].join(timeout=5)

            agent.client.chat.completions.create.side_effect = [
                _mock_response(content="processed queued one", finish_reason="stop"),
                _mock_response(content="processed queued two", finish_reason="stop"),
            ]
            queued_results = [
                agent.run_conversation(text)["final_response"]
                for text in queued_followups
            ]

        durable = owner_db.get_messages_as_conversation("gateway-active")
        tool_call_rows = [
            row for row in durable
            if row.get("role") == "assistant" and row.get("tool_calls")
        ]
        cron_sessions = owner_db._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source = 'cron'"
        ).fetchone()[0]
        result = {
            "trigram_policy": owner_db.get_meta("trigram_fts_policy"),
            "trigram_table_exists": table_exists("messages_fts_trigram"),
            "active_result": active_result["result"],
            "queued_results": queued_results,
            "executed_tools": executed_tools,
            "tool_call_rows": len(tool_call_rows),
            "cron_count": cron_count,
            "cron_constructor_calls": cron_constructor_calls,
            "cron_sessions": cron_sessions,
            "awaiting_observed": bool(queued_followups),
        }
        owner_db.close()
        print(json.dumps(result, sort_keys=True))
        """
    )
    env = os.environ.copy()
    env["HERMES_INCIDENT_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    if payload.get("skip"):
        pytest.skip(payload["skip"])

    assert payload["trigram_policy"] == "disabled"
    assert payload["trigram_table_exists"] is True
    assert payload["active_result"]["failed"] is False
    assert payload["active_result"]["final_response"] == "active done"
    assert "failure_reason" not in payload["active_result"]
    assert payload["queued_results"] == ["processed queued one", "processed queued two"]
    assert payload["executed_tools"] == ["incident-tool-call"]
    assert payload["tool_call_rows"] == 1
    assert payload["cron_count"] == 3
    assert payload["cron_constructor_calls"] == 0
    assert payload["cron_sessions"] == 3
