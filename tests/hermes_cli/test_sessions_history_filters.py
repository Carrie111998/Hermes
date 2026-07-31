"""Regression tests for interactive CLI session-history filtering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import console_engine, sessions_cmd


class _SessionDB:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.closed = False

    def list_sessions_rich(self, **kwargs) -> list[dict]:
        self.list_calls.append(kwargs)
        return [
            {
                "id": "human",
                "source": "tui",
                "title": "Human chat",
                "preview": "continue this",
                "started_at": 1,
                "last_active": 1,
            }
        ]

    def session_count(self, *args, **kwargs) -> int:
        self.list_calls.append({"session_count": True, **kwargs})
        return 1

    def message_count(self) -> int:
        return 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_session_db(monkeypatch):
    import hermes_state

    db = _SessionDB()
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    return db


def test_sessions_list_excludes_cron_and_tool_by_default(fake_session_db, capsys):
    args = SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=10,
        workspace=None,
    )

    sessions_cmd.cmd_sessions(args)

    assert fake_session_db.list_calls[0]["exclude_sources"] == ["cron", "tool"]
    assert "human" in capsys.readouterr().out


def test_sessions_list_honors_explicit_cron_source(fake_session_db):
    args = SimpleNamespace(
        sessions_action="list",
        source="cron",
        limit=10,
        workspace=None,
    )

    sessions_cmd.cmd_sessions(args)

    assert fake_session_db.list_calls[0]["source"] == "cron"
    assert fake_session_db.list_calls[0]["exclude_sources"] is None


def test_sessions_browse_excludes_cron_and_tool_by_default(fake_session_db, monkeypatch, capsys):
    monkeypatch.setattr(sessions_cmd, "_session_browse_picker", lambda sessions: "human")
    relaunched: list[list[str]] = []

    def fake_relaunch(argv):
        relaunched.append(argv)

    monkeypatch.setattr("hermes_cli.relaunch.relaunch", fake_relaunch)
    args = SimpleNamespace(sessions_action="browse", source=None, limit=10)

    sessions_cmd.cmd_sessions(args)

    assert fake_session_db.list_calls[0]["exclude_sources"] == ["cron", "tool"]
    assert relaunched == [["--resume", "human"]]
    assert "Resuming session: human" in capsys.readouterr().out


def test_console_sessions_list_excludes_cron_and_tool(fake_session_db, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: fake_session_db)

    output = console_engine._sessions_list(None, ["--limit", "10"])

    assert fake_session_db.list_calls[0]["exclude_sources"] == ["cron", "tool"]
    assert "human" in output


def test_console_sessions_stats_listable_excludes_cron_and_tool(fake_session_db, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: fake_session_db)

    output = console_engine._sessions_stats(None, [])

    listable_call = next(call for call in fake_session_db.list_calls if call.get("exclude_sources"))
    assert listable_call["exclude_sources"] == ["cron", "tool"]
    assert "Listable sessions: 1" in output
