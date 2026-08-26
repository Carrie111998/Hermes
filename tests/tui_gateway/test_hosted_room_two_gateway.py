"""Headless two-gateway text-room integration over the peer Runs adapter."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
from tui_gateway.hosted_room_peer_transport import PeerMemberRoute
from tui_gateway.hosted_room_service import HostedRoomService


class _LocalRPC:
    def resolve_exact(self, **kwargs):
        return None

    def create(self, **kwargs):
        return {"session_id": "local-session"}

    def resume(self, **kwargs):
        return {"session_id": kwargs["session_id"]}

    def submit(self, **kwargs):
        kwargs["on_terminal"]({"status": "settled", "text": "local reply"})
        return {"accepted": True}

    def history(self, **kwargs):
        return []

    def info(self, **kwargs):
        return {"active": False, "task_id": None}

    def interrupt(self, **kwargs):
        return {"interrupted": True}


class _RemoteGateway(BaseHTTPRequestHandler):
    sessions = []
    runs = {}
    auth = []

    def _json(self, value, status=200):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record_auth(self):
        type(self).auth.append(self.headers.get("Authorization"))

    def do_GET(self):
        self._record_auth()
        if self.path.startswith("/api/sessions"):
            return self._json({"data": list(type(self).sessions)})
        if self.path.startswith("/v1/runs/"):
            return self._json(type(self).runs[self.path.rsplit("/", 1)[-1]])
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        self._record_auth()
        size = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/api/sessions":
            session = {
                "id": "peer-group-session",
                "title": body["title"],
                "source": body["source"],
                "hidden": True,
            }
            type(self).sessions.append(session)
            return self._json({"session": session}, 201)
        if self.path == "/v1/runs":
            run = {
                "run_id": "peer-run-1",
                "session_id": body["session_id"],
                "status": "completed",
                "output": "Peer gateway completed the review.",
            }
            type(self).runs[run["run_id"]] = run
            return self._json(
                {"run_id": run["run_id"], "status": "started", "replayed": False},
                202,
            )
        return self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


@pytest.fixture
def remote_gateway():
    _RemoteGateway.sessions = []
    _RemoteGateway.runs = {}
    _RemoteGateway.auth = []
    server = HTTPServer(("127.0.0.1", 0), _RemoteGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_two_gateway_room_finishes_without_any_desktop_process(
    tmp_path: Path, remote_gateway: str
):
    db = tmp_path / "home-state.db"
    peer = PeerRunsHTTPClient(
        base_url=remote_gateway,
        api_key="peer-secret-key-1234567890",
        trusted_fleet_compatibility=True,
    )
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="compatibility-only",
    )
    service = HostedRoomService(
        _server(),
        db_path=db,
        peer_routes={("room-1", "member-peer"): route},
        peer_clients={"install-peer": peer},
    )
    service.rpc = _LocalRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default",)
    service.create_room(
        room_id="room-1",
        name="Distributed review",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "local",
            },
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@reviewer check this", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    reply = next(
        event
        for event in service._events("room-1")
        if event["kind"] == "message.member"
    )
    assert reply["payload"]["text"] == "Peer gateway completed the review."
    assert _RemoteGateway.sessions[0]["title"] == "Group: room-1"
    assert _RemoteGateway.sessions[0]["source"] == "bot_room"
    assert _RemoteGateway.auth
    assert all(
        header == "Bearer peer-secret-key-1234567890"
        for header in _RemoteGateway.auth
    )

