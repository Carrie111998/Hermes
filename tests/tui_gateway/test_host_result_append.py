import json
import threading

from tui_gateway import server


class FakeDB:
    def __init__(self):
        self.messages = []

    def append_message(self, **kwargs):
        self.messages.append(kwargs)
        return len(self.messages)

    def get_messages_as_conversation(self, _session_id, include_ancestors=True):
        return [item for item in self.messages]

    def get_messages(self, _session_id):
        return [
            {"id": index, "message_id": item.get("platform_message_id"), "content": item.get("content")}
            for index, item in enumerate(self.messages, 1)
        ]


def _receipt(session_id="canonical-1", request_id="req-1"):
    return {
        "receipt_version": 1,
        "status": "succeeded",
        "code": "SUCCEEDED",
        "request_id": request_id,
        "correlation": {"canonical_session_id": session_id, "mission_id": "mission-1"},
        "workspace_id": "fixture",
        "cwd": "test",
        "tool": "node_check",
        "stdout": "ok\n",
        "stderr": "",
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "cancelled": False,
        "artifacts": [],
        "started_at": "2026-07-17T00:00:00.000Z",
        "finished_at": "2026-07-17T00:00:00.010Z",
        "request_sha256": "abc",
    }


def test_host_result_append_is_provider_free_restricted_and_idempotent(monkeypatch):
    db = FakeDB()
    session = {
        "session_key": "canonical-1",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "host_result_pending": {},
    }
    monkeypatch.setattr(server, "_sessions", {"live-1": session})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    prepared = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.host_result.prepare", "params": {"session_id": "live-1", "request_id": "req-1", "correlation_id": "req-1:mission-1"}})
    prep = prepared["result"]
    receipt = _receipt()
    safe = {key: receipt[key] for key in server._HOST_RESULT_FIELDS}
    digest = server._host_result_hash(safe)
    params = {"session_id": "live-1", "result_type": server._HOST_RESULT_TYPE, "request_id": "req-1", "correlation_id": "req-1:mission-1", "capability": prep["capability"], "receipt_hash": digest, "receipt": receipt}
    first = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "session.host_result.append", "params": params})
    replay = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "session.host_result.append", "params": params})
    assert first["result"]["replayed"] is False
    assert replay["result"]["replayed"] is True
    assert len(db.messages) == 1
    assert "HOST_EXECUTION_RESULT_V1" in db.messages[0]["content"]
    assert "arbitrary_message" not in db.messages[0]["content"]

    modified = dict(params, receipt=dict(receipt, stdout="tampered\n"))
    rejected = server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "session.host_result.append", "params": modified})
    assert rejected["error"]["message"] == "host receipt hash mismatch"

    reconnected = dict(session, history=[], host_result_pending={})
    server._sessions = {"live-2": reconnected}
    replay_after_reconnect = server.handle_request({"jsonrpc": "2.0", "id": 5, "method": "session.host_result.append", "params": dict(params, session_id="live-2")})
    assert replay_after_reconnect["result"]["replayed"] is True
    assert len(db.messages) == 1
