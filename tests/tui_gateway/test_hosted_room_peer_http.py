"""Scoped peer Runs adapter tests."""

from __future__ import annotations

import json
import io
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient, PeerRunsHTTPError


class FakePeer(BaseHTTPRequestHandler):
    sessions = []
    runs = {}
    idempotency = []
    approvals = []

    def _json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/runs/"):
            run_id = self.path.rsplit("/", 1)[-1]
            return self._json(type(self).runs[run_id])
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/runs":
            dispatch = body["hosted_room_dispatch"]
            logical_session = (
                "roomlink_"
                + __import__("hashlib").sha256(
                    f"{dispatch['room_id']}\0{dispatch['target_profile']}".encode()
                ).hexdigest()[:32]
            )
            type(self).idempotency.append(self.headers.get("Idempotency-Key"))
            run_id = "run-1"
            type(self).runs[run_id] = {
                "run_id": run_id,
                "session_id": logical_session,
                "status": "running",
            }
            return self._json(
                {"run_id": run_id, "status": "started", "replayed": False},
                202,
            )
        if self.path == "/v1/runs/run-1/stop":
            type(self).runs["run-1"]["status"] = "cancelled"
            return self._json({"run_id": "run-1", "status": "stopping"})
        if self.path == "/v1/runs/run-1/approval":
            type(self).approvals.append(body)
            return self._json({"run_id": "run-1", "resolved": 1})
        return self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


@pytest.fixture
def peer_server():
    FakePeer.sessions = []
    FakePeer.runs = {}
    FakePeer.idempotency = []
    FakePeer.approvals = []
    server = HTTPServer(("127.0.0.1", 0), FakePeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _dispatch(**overrides):
    import hashlib

    prompt = "Review this room message."
    return {
        "protocol_version": 2,
        "room_id": "room-1",
        "home_install_id": "install-home",
        "authority_gateway_id": "gateway-home",
        "authority_epoch": 1,
        "member_id": "member-reviewer",
        "target_install_id": "install-peer",
        "target_profile": "reviewer",
        "task_id": "task-1",
        "execution_generation": 1,
        "source_event_seq": 1,
        "cancellation_scope_id": "cancel-1",
        "prompt": prompt,
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "capability_digest": "a" * 64,
        "trace_id": "trace-1",
        **overrides,
    }


def test_peer_runs_client_rejects_broad_compatibility_grants(peer_server):
    client = PeerRunsHTTPClient(base_url=peer_server, api_key="k" * 32)
    with pytest.raises(PeerRunsHTTPError, match="scoped room grant"):
        client.prepare(
            room_id="room-1",
            profile="default",
            source="bot_room",
            grant="compatibility-only",
            create=True,
        )
    with pytest.raises(PeerRunsHTTPError, match="scoped room grant"):
        client.dispatch(dispatch=_dispatch(), grant="")
    assert FakePeer.runs == {}


def test_peer_client_rejects_plaintext_non_loopback():
    with pytest.raises(ValueError, match="https outside"):
        PeerRunsHTTPClient(
            base_url="http://peer.example.test:8377",
            api_key="k" * 32,
        )


def test_scoped_peer_runs_client_uses_logical_session_and_durable_run(peer_server):
    now = [0.0]
    client = PeerRunsHTTPClient(
        base_url=peer_server,
        api_key="",
        clock=lambda: now[0],
    )
    accepted = client.dispatch(dispatch=_dispatch(), grant="signed.room.grant")
    assert accepted["status"] == "accepted"
    assert accepted["session_id"].startswith("roomlink_")
    assert FakePeer.idempotency == ["room:task-1:1"]

    assert (
        client.status(
            room_id="room-1",
            profile="reviewer",
            session_id=accepted["session_id"],
            grant="signed.room.grant",
        )["active"]
        is True
    )

    FakePeer.runs["run-1"].update({
        "status": "completed",
        "output": "Remote review complete.",
    })
    now[0] = 0.1
    history = client.history(
        room_id="room-1",
        profile="reviewer",
        session_id=accepted["session_id"],
        grant="signed.room.grant",
    )
    assert history == [
        {
            "role": "assistant",
            "task_id": "task-1",
            "execution_generation": 1,
            "status": "settled",
            "message_id": "peer-run:run-1",
            "content": "Remote review complete.",
        }
    ]


def test_scoped_peer_runs_client_stops_exact_run(peer_server):
    client = PeerRunsHTTPClient(base_url=peer_server, api_key="")
    dispatch = _dispatch()
    client.dispatch(dispatch=dispatch, grant="signed.room.grant")
    stopped = client.stop(dispatch=dispatch, grant="signed.room.grant")
    assert stopped["status"] == "stopping"
    assert FakePeer.runs["run-1"]["status"] == "cancelled"


def test_remote_run_receipt_survives_home_restart(peer_server, tmp_path):
    db = tmp_path / "state.db"
    first = PeerRunsHTTPClient(
        base_url=peer_server,
        api_key="",
        receipt_db_path=db,
    )
    dispatch = _dispatch(source_event_seq=17)
    accepted = first.dispatch(dispatch=dispatch, grant="signed.room.grant")

    restarted = PeerRunsHTTPClient(
        base_url=peer_server,
        api_key="",
        receipt_db_path=db,
    )
    restarted.bind_observation(task_id="task-1", execution_generation=1)
    status = restarted.status(
        room_id="room-1",
        profile="reviewer",
        session_id=accepted["session_id"],
        grant="signed.room.grant",
    )
    assert status["run_id"] == accepted["run_id"]
    stopped = restarted.stop(dispatch=dispatch, grant="signed.room.grant")
    assert stopped["status"] == "stopping"


def test_ambiguous_admission_replays_the_identical_idempotency_key(tmp_path):
    client = PeerRunsHTTPClient(
        base_url="https://peer.example.test",
        api_key="",
        receipt_db_path=tmp_path / "state.db",
    )
    requests = []

    def response_lost_then_replayed(path, **kwargs):
        requests.append((path, kwargs))
        if len(requests) == 1:
            raise PeerRunsHTTPError(
                "peer response was lost",
                retryable=True,
                ambiguous=True,
            )
        return {"run_id": "run-recovered", "status": "running", "replayed": True}

    client._request = response_lost_then_replayed
    recovered = client.dispatch(dispatch=_dispatch(), grant="signed.room.grant")

    assert recovered["run_id"] == "run-recovered"
    assert [request[1]["headers"]["Idempotency-Key"] for request in requests] == [
        "room:task-1:1",
        "room:task-1:1",
    ]
    assert requests[0][1]["body"] == requests[1][1]["body"]

    restarted = PeerRunsHTTPClient(
        base_url="https://peer.example.test",
        api_key="",
        receipt_db_path=tmp_path / "state.db",
    )
    restarted._request = lambda *_args, **_kwargs: pytest.fail(
        "durable receipt should avoid another admission"
    )
    assert restarted.recover_dispatch(
        dispatch=_dispatch(), grant="signed.room.grant"
    )["run_id"] == "run-recovered"


def test_peer_approval_sends_the_exact_request_id(peer_server, tmp_path):
    client = PeerRunsHTTPClient(
        base_url=peer_server,
        api_key="",
        receipt_db_path=tmp_path / "state.db",
    )
    client.dispatch(dispatch=_dispatch(), grant="signed.room.grant")

    result = client.approve_receipt(
        task_id="task-1",
        execution_generation=1,
        request_id="approval-exact-1",
        choice="once",
        grant="signed.room.grant",
    )

    assert result["resolved"] == 1
    assert FakePeer.approvals == [
        {"choice": "once", "request_id": "approval-exact-1"}
    ]


def test_exact_receipt_polling_is_constant_with_large_room_history():
    now = [0.0]
    client = PeerRunsHTTPClient(
        base_url="https://peer.example.test",
        api_key="",
        poll_min_seconds=0.1,
        poll_max_seconds=0.4,
        clock=lambda: now[0],
    )
    session_id = "roomlink-session"
    for index in range(200):
        client._runs[(f"task-{index}", 1)] = {
            "run_id": f"run-{index}",
            "session_id": session_id,
            "room_id": "room-1",
            "member_id": "member-reviewer",
            "task_id": f"task-{index}",
            "execution_generation": 1,
            "target_install_id": "install-peer",
            "target_profile": "reviewer",
        }
    client.bind_observation(task_id="task-199", execution_generation=1)
    requests = []

    def status_response(path, **_kwargs):
        requests.append(path)
        if len(requests) == 1:
            return {"run_id": "run-199", "status": "running"}
        return {
            "run_id": "run-199",
            "status": "completed",
            "output": "bounded result",
            "ignored_large_field": "x" * 1000,
        }

    client._request = status_response
    for _ in range(50):
        assert client.history(
            room_id="room-1",
            profile="reviewer",
            session_id=session_id,
            grant="signed.room.grant",
        ) == []
        assert client.status(
            room_id="room-1",
            profile="reviewer",
            session_id=session_id,
            grant="signed.room.grant",
        )["active"] is True

    assert requests == ["/v1/runs/run-199"]
    now[0] = 0.1
    terminal = client.history(
        room_id="room-1",
        profile="reviewer",
        session_id=session_id,
        grant="signed.room.grant",
    )
    assert terminal[0]["content"] == "bounded result"

    now[0] = 100
    for _ in range(50):
        client.status(
            room_id="room-1",
            profile="reviewer",
            session_id=session_id,
            grant="signed.room.grant",
        )

    assert requests == ["/v1/runs/run-199", "/v1/runs/run-199"]
    compact = client._status_cache["run-199"]["status"]
    assert compact == {
        "run_id": "run-199",
        "status": "completed",
        "output": "bounded result",
    }
    client._runs[("task-200", 1)] = {
        **client._runs[("task-199", 1)],
        "run_id": "run-200",
        "task_id": "task-200",
    }
    client.bind_observation(task_id="task-200", execution_generation=1)
    assert ("task-199", 1) not in client._runs
    assert len(client._status_cache) == 0


def test_failed_exact_receipt_poll_honors_backoff():
    now = [0.0]
    client = PeerRunsHTTPClient(
        base_url="https://peer.example.test",
        api_key="",
        poll_min_seconds=0.1,
        poll_max_seconds=0.4,
        clock=lambda: now[0],
    )
    client._runs[("task-1", 1)] = {
        "run_id": "run-1",
        "session_id": "roomlink-session",
        "room_id": "room-1",
        "member_id": "member-reviewer",
        "task_id": "task-1",
        "execution_generation": 1,
        "target_install_id": "install-peer",
        "target_profile": "reviewer",
    }
    client.bind_observation(task_id="task-1", execution_generation=1)
    requests = []

    def unavailable(path, **_kwargs):
        requests.append(path)
        raise PeerRunsHTTPError("peer unavailable", retryable=True)

    client._request = unavailable
    for _ in range(20):
        with pytest.raises(PeerRunsHTTPError):
            client.status(
                room_id="room-1",
                profile="reviewer",
                session_id="roomlink-session",
                grant="signed.room.grant",
            )
    assert requests == ["/v1/runs/run-1"]

    now[0] = 0.1
    with pytest.raises(PeerRunsHTTPError):
        client.status(
            room_id="room-1",
            profile="reviewer",
            session_id="roomlink-session",
            grant="signed.room.grant",
        )
    assert requests == ["/v1/runs/run-1", "/v1/runs/run-1"]


def test_invalid_room_grant_is_classified_without_echoing_secret(monkeypatch):
    secret = "sensitive.room.grant"
    body = io.BytesIO(json.dumps({"error": {"code": "invalid_room_grant"}}).encode())

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://peer.example.test/v1/runs/run-1",
            401,
            "Unauthorized",
            {},
            body,
        )

    monkeypatch.setattr("hermes_cli.urllib_security.open_credentialed_url", rejected)
    client = PeerRunsHTTPClient(
        base_url="https://peer.example.test",
        api_key="",
    )
    with pytest.raises(PeerRunsHTTPError) as caught:
        client._request("/v1/runs/run-1", room_grant=secret)
    assert caught.value.needs_reauthorization is True
    assert secret not in str(caught.value)
