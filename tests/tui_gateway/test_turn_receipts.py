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


def test_compute_host_frame_carries_receipt_fence(monkeypatch, tmp_path):
    session = _session(tmp_path)
    session["cwd"] = str(tmp_path)
    prepared = prepare_turn(tmp_path, "durable-session")
    token, _running = claim_turn(
        tmp_path, "durable-session", prepared["turn_id"]
    )
    server._start_inflight_turn(
        session,
        "ship it",
        turn_id=prepared["turn_id"],
        execution_token=token,
        receipt_session_key="durable-session",
    )
    monkeypatch.setattr(server, "_session_source", lambda _session: "desktop")

    frame = server._compute_host_turn_frame(
        "request", "ui-session", session, "ship it"
    )

    assert frame["turn_id"] == prepared["turn_id"]
    assert frame["execution_token"] == token
    assert frame["receipt_session_key"] == "durable-session"


def test_compute_host_callback_preserves_child_terminal_receipt(monkeypatch, tmp_path):
    session = _session(tmp_path)
    prepared = prepare_turn(tmp_path, "durable-session")
    token, _running = claim_turn(
        tmp_path, "durable-session", prepared["turn_id"]
    )
    server._start_inflight_turn(
        session,
        "ship it",
        turn_id=prepared["turn_id"],
        execution_token=token,
        receipt_session_key="durable-session",
    )
    assert finish_turn(
        tmp_path,
        "durable-session",
        prepared["turn_id"],
        token,
        "committed",
        {"status": "complete"},
    )
    duplicate_writes = []
    monkeypatch.setattr(
        server,
        "_finish_inflight_turn_receipt",
        lambda *_args, **_kwargs: duplicate_writes.append(True) or prepared["turn_id"],
    )
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_args: False)

    server._on_compute_host_turn_done(
        "request",
        "ui-session",
        session,
        {"type": "turn.end", "status": "complete"},
    )

    assert duplicate_writes == []
    assert get_turn_status(
        tmp_path, "durable-session", prepared["turn_id"]
    )["state"] == "committed"


def test_context_refusal_terminalizes_admitted_turn(monkeypatch, tmp_path):
    class BlockedContext:
        blocked = True
        warnings = ["context too large"]

    class Agent:
        session_id = "durable-session"
        model = "test-model"
        base_url = ""
        api_key = ""
        provider = ""
        _config_context_length = 1000
        interim_assistant_callback = None

        def clear_interrupt(self):
            pass

    session = _session(tmp_path)
    session.update({"agent": Agent(), "cwd": str(tmp_path), "transport": None})
    prepared = prepare_turn(tmp_path, "durable-session")
    token, _running = claim_turn(
        tmp_path, "durable-session", prepared["turn_id"]
    )
    server._start_inflight_turn(
        session,
        "Review @file:large.txt",
        turn_id=prepared["turn_id"],
        execution_token=token,
        receipt_session_key="durable-session",
    )
    session["running"] = True
    emitted = []
    monkeypatch.setattr(
        "agent.context_references.preprocess_context_references",
        lambda *_args, **_kwargs: BlockedContext(),
    )
    monkeypatch.setattr(server, "_emit", lambda event, _sid, payload=None: emitted.append((event, payload)))
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *_args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "record_turn_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *_args: None)
    monkeypatch.setattr(server, "_emit_settled_session_info", lambda *_args: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_args: False)
    monkeypatch.setattr(server, "render_message", lambda text, _cols: text)

    assert server._run_prompt_submit(
        "request", "ui-session", session, "Review @file:large.txt"
    )
    session["_run_thread"].join(timeout=5)

    terminal = get_turn_status(
        tmp_path, "durable-session", prepared["turn_id"]
    )
    assert terminal["state"] == "failed"
    assert terminal["receipt"]["status"] == "error"
    complete = [payload for event, payload in emitted if event == "message.complete"]
    assert complete[-1]["turn_id"] == prepared["turn_id"]
