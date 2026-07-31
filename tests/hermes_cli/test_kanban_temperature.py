"""Kanban temperature support tests.

Covers:
  * DB schema: model_temperature REAL column present on fresh + legacy DBs
  * Task dataclass / from_row round-trip
  * create_task() persists model_override + model_temperature
  * worker spawn command line: --temperature <value> passed to the CLI
  * kanban create CLI: --model / --model-temperature flags
  * kanban_create tool schema exposes model / model_temperature
"""

import sqlite3
from typing import cast, Optional

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fresh kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    return home


def _task_cols(conn):
    return {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}


class TestKanbanDbSchema:

    def test_fresh_db_has_model_temperature_column(self, kanban_home):
        with kb.connect() as conn:
            cols = _task_cols(conn)
        assert "model_temperature" in cols
        assert "model_override" in cols

    def test_legacy_db_gets_model_temperature_via_migration(self, tmp_path, monkeypatch):
        # Build a legacy DB missing model_temperature, then open through init_db.
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        db_path = home / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_by TEXT,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                workspace_kind TEXT NOT NULL DEFAULT 'scratch',
                workspace_path TEXT,
                branch_name TEXT,
                claim_lock TEXT,
                claim_expires INTEGER,
                tenant TEXT,
                result TEXT,
                idempotency_key TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                worker_pid INTEGER,
                last_failure_error TEXT,
                max_runtime_seconds INTEGER,
                last_heartbeat_at INTEGER,
                current_run_id INTEGER,
                workflow_template_id TEXT,
                current_step_key TEXT,
                skills TEXT,
                model_override TEXT,
                max_retries INTEGER,
                session_id TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        with kb.connect() as conn:
            cols = _task_cols(conn)
            assert "model_temperature" in cols

    def test_model_temperature_column_type_is_real(self, kanban_home):
        with kb.connect() as conn:
            row = conn.execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()
        temp_col = next(r for r in row if r["name"] == "model_temperature")
        assert temp_col["type"].upper() == "REAL"


class TestKanbanCreateTask:

    def test_create_task_round_trips_temperature(self, kanban_home):
        with kb.connect() as conn:
            tid = kb.create_task(
                conn,
                title="temp task",
                assignee="coder",
                model_override="openrouter/anthropic/claude-opus-4.6",
                model_temperature=0.2,
            )
            task = kb.get_task(conn, tid)
        assert task is not None
        assert task.model_override == "openrouter/anthropic/claude-opus-4.6"
        assert task.model_temperature == 0.2

    def test_create_task_defaults_to_none(self, kanban_home):
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="plain task", assignee="coder")
            task = kb.get_task(conn, tid)
        assert task is not None
        assert task.model_override is None
        assert task.model_temperature is None

    def test_create_task_stores_integer_string_temperature(self, kanban_home):
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="t", assignee="coder",
                model_temperature=cast(Optional[float], "0.5"),
            )
            task = kb.get_task(conn, tid)
        assert task is not None
        assert task.model_temperature == 0.5


class TestKanbanSpawnCommand:

    def test_spawn_includes_temperature_flag(self, kanban_home, tmp_path, monkeypatch):
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_temp_spawn",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            model_override="openrouter/anthropic/claude-opus-4.6",
            model_temperature=0.3,
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        cmd = captured["cmd"]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "openrouter/anthropic/claude-opus-4.6"
        assert "--temperature" in cmd
        assert cmd[cmd.index("--temperature") + 1] == "0.3"
        # The flag lands before the `chat` subcommand (top-level flag).
        assert cmd.index("--temperature") < cmd.index("chat")

    def test_spawn_omits_temperature_when_unset(self, kanban_home, tmp_path, monkeypatch):
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 4243

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_no_temp_spawn",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )
        kb._default_spawn(task, str(tmp_path / "ws"))
        assert "--temperature" not in captured["cmd"]


class TestKanbanToolSchema:

    def test_kanban_create_schema_exposes_temperature(self):
        props = kanban_tools.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
        assert "model" in props
        assert "model_temperature" in props
        assert props["model_temperature"]["type"] == "number"

    def test_handle_create_accepts_temperature(self, kanban_home):
        result = kanban_tools._handle_create({
            "title": "tool temp task",
            "assignee": "coder",
            "model": "openrouter/anthropic/claude-opus-4.6",
            "model_temperature": 0.25,
        })
        assert '"task_id"' in result, result
        import json
        payload = json.loads(result)
        with kb.connect() as conn:
            task = kb.get_task(conn, payload["task_id"])
        assert task is not None
        assert task.model_temperature == 0.25
        assert task.model_override == "openrouter/anthropic/claude-opus-4.6"

    def test_handle_create_rejects_out_of_range_temperature(self, kanban_home):
        result = kanban_tools._handle_create({
            "title": "bad temp",
            "assignee": "coder",
            "model_temperature": 5.0,
        })
        assert "error" in result
        assert "0.0 and 2.0" in result


class TestKanbanCli:

    def test_create_parser_has_model_flags(self):
        import argparse
        from hermes_cli.kanban import build_parser

        wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
        top_sub = wrap.add_subparsers(dest="_top")
        parser = build_parser(top_sub)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        create = sub.choices["create"]
        seen = {}
        for action in create._actions:
            for opt in action.option_strings:
                seen[opt] = action
        assert "--model" in seen
        assert "--model-temperature" in seen

    def test_create_rejects_out_of_range_temperature(self):
        import argparse
        from hermes_cli.kanban import build_parser

        wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
        top_sub = wrap.add_subparsers(dest="_top")
        parser = build_parser(top_sub)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        create = sub.choices["create"]

        for bad in ("2.5", "-0.1", "hot"):
            with pytest.raises(SystemExit):
                create.parse_args(["t", "--model-temperature", bad])

    def test_create_accepts_in_range_temperature(self):
        import argparse
        from hermes_cli.kanban import build_parser

        wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
        top_sub = wrap.add_subparsers(dest="_top")
        parser = build_parser(top_sub)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        create = sub.choices["create"]

        args = create.parse_args(["t", "--model-temperature", "0.7"])
        assert args.model_temperature == 0.7
        args = create.parse_args(["t", "--model-temperature", "2.0"])
        assert args.model_temperature == 2.0
