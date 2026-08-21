import threading
import types

from tui_gateway import server
from tui_gateway.turn_receipts import (
    claim_turn,
    finish_turn,
    get_turn_status,
    prepare_turn,
)


def test_receipt_lifecycle_is_fenced_and_session_scoped(tmp_path):
    prepared = prepare_turn(tmp_path, "session-a")
    turn_id = prepared["turn_id"]

    assert prepared["state"] == "did_not_run"
    assert get_turn_status(tmp_path, "session-b", turn_id) == {
        "known": False,
        "state": "unknown",
    }

    token, running = claim_turn(tmp_path, "session-a", turn_id)
    assert token
    assert running["state"] == "running"

    duplicate_token, duplicate = claim_turn(tmp_path, "session-a", turn_id)
    assert duplicate_token is None
    assert duplicate["state"] == "running"

    assert finish_turn(
        tmp_path,
        "session-a",
        turn_id,
        "wrong-fence",
        "committed",
    ) is False
    assert finish_turn(
        tmp_path,
        "session-a",
        turn_id,
        token,
        "committed",
        {"status": "complete"},
    ) is True
    assert finish_turn(
        tmp_path,
        "session-a",
        turn_id,
        token,
        "failed",
    ) is False

    terminal = get_turn_status(tmp_path, "session-a", turn_id)
    assert terminal["state"] == "committed"
    assert terminal["receipt"]["status"] == "complete"
    assert terminal["receipt"]["turn_id"] == turn_id


def _session(tmp_path):
    ready = threading.Event()
    ready.set()
    return {
        "agent": types.SimpleNamespace(),
        "agent_ready": ready,
        "session_key": "durable-session",
        "profile_home": str(tmp_path),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
    }


def test_prompt_submit_admits_prepared_turn_once(monkeypatch, tmp_path):
    class ImmediateThread:
        def __init__(self, target=None, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    session = _session(tmp_path)
    server._sessions["ui-session"] = session
    dispatched = []
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda _rid, _sid, live, text, **_kwargs: dispatched.append(
            (text, dict(live["inflight_turn"]))
        ),
    )
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)

    try:
        prepared = server.handle_request(
            {
                "id": "prepare",
                "method": "turn.prepare",
                "params": {"session_id": "ui-session"},
            }
        )["result"]
        turn_id = prepared["turn_id"]
        assert prepared["state"] == "did_not_run"

        accepted = server.handle_request(
            {
                "id": "submit",
                "method": "prompt.submit",
                "params": {
                    "session_id": "ui-session",
                    "turn_id": turn_id,
                    "text": "ship it",
                },
            }
        )
        assert accepted["result"] == {"status": "streaming", "turn_id": turn_id}
        assert len(dispatched) == 1
        assert dispatched[0][1]["turn_id"] == turn_id
        assert dispatched[0][1]["execution_token"]

        duplicate = server.handle_request(
            {
                "id": "duplicate",
                "method": "prompt.submit",
                "params": {
                    "session_id": "ui-session",
                    "turn_id": turn_id,
                    "text": "ship it",
                },
            }
        )
        assert duplicate["error"]["code"] == 4009
        assert duplicate["error"]["data"]["turn"]["state"] == "running"
        assert len(dispatched) == 1

        assert server._finish_inflight_turn_receipt(
            session, "committed", status="complete"
        ) == turn_id
        status = server.handle_request(
            {
                "id": "status",
                "method": "turn.status",
                "params": {"session_id": "ui-session", "turn_id": turn_id},
            }
        )["result"]
        assert status["state"] == "committed"
        assert status["receipt"]["status"] == "complete"
    finally:
        server._sessions.pop("ui-session", None)


def test_unknown_turn_is_distinct_from_prepared_turn(monkeypatch, tmp_path):
    session = _session(tmp_path)
    server._sessions["ui-session"] = session
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    try:
        unknown = server.handle_request(
            {
                "id": "unknown",
                "method": "turn.status",
                "params": {"session_id": "ui-session", "turn_id": "not-issued"},
            }
        )["result"]
        prepared = server.handle_request(
            {
                "id": "prepare",
                "method": "turn.prepare",
                "params": {"session_id": "ui-session"},
            }
        )["result"]
        assert unknown == {"known": False, "state": "unknown"}
        assert prepared["known"] is True
        assert prepared["state"] == "did_not_run"
    finally:
        server._sessions.pop("ui-session", None)
