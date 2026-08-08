"""Live desktop sessions refresh model history from durable cross-process writes."""

from __future__ import annotations

import threading
import types

from tui_gateway import server


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False


class _HistoryDB:
    def __init__(self, messages):
        self.messages = list(messages)
        self.reads = []

    def get_messages_as_conversation(self, session_id, **kwargs):
        self.reads.append((session_id, kwargs))
        return list(self.messages)


def _msg(role: str, content: str, row_id: int | None = None) -> dict:
    message = {"role": role, "content": content}
    if row_id is not None:
        message["_row_id"] = row_id
    return message


def _session(history: list[dict]) -> dict:
    ready = threading.Event()
    ready.set()
    return {
        "agent": types.SimpleNamespace(),
        "agent_ready": ready,
        "session_key": "shared-session",
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "inflight_turn": None,
        "cols": 80,
        "cwd": "",
        "source": "desktop",
    }


def test_prompt_submit_refreshes_external_durable_history_before_dispatch(monkeypatch):
    initial = [_msg("user", "initial", 1), _msg("assistant", "reply", 2)]
    external = [_msg("user", "from gateway", 3), _msg("assistant", "gateway reply", 4)]
    db = _HistoryDB(initial + external)
    session = _session(initial)
    captured = []

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda _rid, _sid, live, _text: captured.append(list(live["history"])),
    )
    server._sessions["desktop-live"] = session

    response = server.handle_request(
        {
            "id": "submit-1",
            "method": "prompt.submit",
            "params": {"session_id": "desktop-live", "text": "continue"},
        }
    )

    assert response["result"]["status"] == "streaming"
    assert captured == [initial + external]
    assert db.reads == [
        (
            "shared-session",
            {"repair_alternation": True, "include_row_ids": True},
        )
    ]


def test_refresh_preserves_live_only_tail_when_durable_history_lags(monkeypatch):
    persisted = [_msg("user", "initial", 1), _msg("assistant", "reply", 2)]
    live_tail = [_msg("user", "local turn"), _msg("assistant", "local reply")]
    session = _session(persisted + live_tail)
    monkeypatch.setattr(server, "_get_db", lambda: _HistoryDB(persisted))

    changed = server._refresh_live_model_history(session)

    assert changed is False
    assert session["history"] == persisted + live_tail
    assert session["history_version"] == 0


def test_refresh_merges_external_and_live_only_append_tails(monkeypatch):
    initial = [_msg("user", "initial", 1), _msg("assistant", "reply", 2)]
    external = [_msg("user", "from gateway", 3), _msg("assistant", "gateway reply", 4)]
    live_marker = {
        "role": "user",
        "content": "[Model switched to test/model]",
        "display_kind": "model_switch",
    }
    session = _session(initial + [live_marker])
    monkeypatch.setattr(server, "_get_db", lambda: _HistoryDB(initial + external))

    changed = server._refresh_live_model_history(session)

    assert changed is True
    assert session["history"] == initial + external + [live_marker]
    assert session["history_version"] == 1


def test_refresh_failure_leaves_live_history_untouched(monkeypatch):
    live = [_msg("user", "keep me"), _msg("assistant", "still here")]
    session = _session(live)

    class _BrokenDB:
        def get_messages_as_conversation(self, *_args, **_kwargs):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(server, "_get_db", lambda: _BrokenDB())

    changed = server._refresh_live_model_history(session)

    assert changed is False
    assert session["history"] == live
    assert session["history_version"] == 0
