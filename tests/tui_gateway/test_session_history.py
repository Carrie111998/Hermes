"""Regression tests for human-facing TUI session history."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)


class _SessionDB:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def list_sessions_rich(self, **kwargs) -> list[dict]:
        self.calls.append(kwargs)
        return self.rows


def _with_db(monkeypatch, server, db: _SessionDB) -> None:
    @contextmanager
    def profile_db(_params: dict):
        yield db

    monkeypatch.setattr(server, "_profile_db", profile_db)


def test_session_list_excludes_cron_and_tool_runs(server, monkeypatch):
    db = _SessionDB(
        [
            {"id": "cron-newest", "source": "cron", "title": "cron", "preview": "", "started_at": 3},
            {"id": "tool-newer", "source": "tool", "title": "tool", "preview": "", "started_at": 2},
            {"id": "human", "source": "tui", "title": "Human chat", "preview": "continue this", "started_at": 1},
        ]
    )
    _with_db(monkeypatch, server, db)

    response = server._methods["session.list"]("request", {"limit": 10})

    assert response["result"]["sessions"] == [
        {
            "id": "human",
            "title": "Human chat",
            "preview": "continue this",
            "started_at": 1,
            "message_count": 0,
            "source": "tui",
        }
    ]


def test_most_recent_skips_cron_and_tool_runs(server, monkeypatch):
    db = _SessionDB(
        [
            {"id": "cron-newest", "source": "cron", "title": "cron", "started_at": 3},
            {"id": "tool-newer", "source": "tool", "title": "tool", "started_at": 2},
            {"id": "human", "source": "tui", "title": "Human chat", "started_at": 1},
        ]
    )
    _with_db(monkeypatch, server, db)

    response = server._methods["session.most_recent"]("request", {})

    assert response["result"] == {
        "session_id": "human",
        "title": "Human chat",
        "started_at": 1,
        "source": "tui",
    }
