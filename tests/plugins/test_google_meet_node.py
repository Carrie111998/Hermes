"""Tests for the google_meet node primitive.

Covers protocol helpers, the file-backed registry, the server's
token-and-dispatch machinery, a mocked client, a real localhost WebSocket
round trip, and the CLI plumbing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


# ---------------------------------------------------------------------------
# protocol.py
# ---------------------------------------------------------------------------

def test_protocol_encode_decode_roundtrip():
    from plugins.google_meet.node import protocol

    msg = protocol.make_request("ping", "tok", {"x": 1}, req_id="abc")
    raw = protocol.encode(msg)
    out = protocol.decode(raw)
    assert out == msg
    assert out["type"] == "ping"
    assert out["id"] == "abc"
    assert out["token"] == "tok"
    assert out["payload"] == {"x": 1}


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------

def test_registry_add_get_roundtrip_persists(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    p = tmp_path / "nodes.json"
    r = NodeRegistry(path=p)
    r.add("mac", "ws://mac.local:18789", "deadbeef")

    # Second instance sees it.
    r2 = NodeRegistry(path=p)
    entry = r2.get("mac")
    assert entry is not None
    assert entry["name"] == "mac"
    assert entry["url"] == "ws://mac.local:18789"
    assert entry["token"] == "deadbeef"
    assert "added_at" in entry


# ---------------------------------------------------------------------------
# server.py — token + dispatch
# ---------------------------------------------------------------------------

def test_server_ensure_token_generates_and_persists(tmp_path):
    from plugins.google_meet.node.server import NodeServer

    p = tmp_path / "tok.json"
    s1 = NodeServer(token_path=p)
    t1 = s1.ensure_token()
    assert isinstance(t1, str) and len(t1) == 32

    # Reuse on a fresh instance.
    s2 = NodeServer(token_path=p)
    t2 = s2.ensure_token()
    assert t1 == t2

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["token"] == t1
    assert "generated_at" in data


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_server_handle_request_rejects_bad_token(tmp_path):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol

    s = NodeServer(token_path=tmp_path / "t.json")
    s.ensure_token()
    bad = protocol.make_request("ping", "not-the-token", {})
    resp = asyncio.run(s._handle_request(bad))
    assert resp["type"] == "error"
    assert "token" in resp["error"].lower()


def test_server_handle_request_ping(tmp_path):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol

    s = NodeServer(token_path=tmp_path / "t.json", display_name="node-x")
    tok = s.ensure_token()
    req = protocol.make_request("ping", tok, {})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "pong"
    assert resp["id"] == req["id"]
    assert resp["payload"]["display_name"] == "node-x"


def test_server_handle_request_status_dispatches_to_pm(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    monkeypatch.setattr(pm, "status",
                        lambda: {"ok": True, "alive": True, "meetingId": "abc"})

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("status", tok, {})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert resp["id"] == req["id"]
    assert resp["payload"] == {"ok": True, "alive": True, "meetingId": "abc"}


def test_server_handle_request_start_bot_dispatches(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "pid": 42, "meeting_id": "abc-defg-hij"}

    monkeypatch.setattr(pm, "start", fake_start)

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("start_bot", tok, {
        "url": "https://meet.google.com/abc-defg-hij",
        "guest_name": "Bot",
        "duration": "30m",
    })
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert resp["payload"]["ok"] is True
    assert captured["url"] == "https://meet.google.com/abc-defg-hij"
    assert captured["guest_name"] == "Bot"
    assert captured["duration"] == "30m"


def test_server_handle_request_start_bot_missing_url(tmp_path):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("start_bot", tok, {"guest_name": "x"})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "error"
    assert "url" in resp["error"]


def test_server_handle_request_stop_dispatches(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    got = {}

    def fake_stop(*, reason="requested"):
        got["reason"] = reason
        return {"ok": True, "reason": reason}

    monkeypatch.setattr(pm, "stop", fake_stop)

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("stop", tok, {"reason": "user-cancel"})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert got["reason"] == "user-cancel"


def test_server_handle_request_transcript(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    got = {}

    def fake_transcript(last=None, *, include_finished=False, session_id=None):
        got["last"] = last
        got["include_finished"] = include_finished
        got["session_id"] = session_id
        return {"ok": True, "lines": ["a", "b"], "total": 2}

    monkeypatch.setattr(pm, "transcript", fake_transcript)

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request(
        "transcript",
        tok,
        {"last": 5, "include_finished": True, "session_id": "s1"},
    )
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert resp["payload"]["lines"] == ["a", "b"]
    assert got["last"] == 5
    assert got["include_finished"] is True
    assert got["session_id"] == "s1"


def test_server_handle_request_say_delegates_to_process_manager(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    got = {}

    def fake_enqueue_say(text):
        got["text"] = text
        return {"ok": True, "enqueued_id": "q1"}

    monkeypatch.setattr(pm, "enqueue_say", fake_enqueue_say)

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("say", tok, {"text": "hello"})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert resp["payload"]["ok"] is True
    assert resp["payload"]["enqueued_id"] == "q1"
    assert got["text"] == "hello"


def test_server_handle_request_say_without_active_preserves_rejection(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    monkeypatch.setattr(
        pm,
        "enqueue_say",
        lambda text: {"ok": False, "reason": "no active meeting"},
    )

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("say", tok, {"text": "hi"})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "response"
    assert resp["payload"] == {"ok": False, "reason": "no active meeting"}


def test_server_handle_request_wraps_pm_exceptions(tmp_path, monkeypatch):
    from plugins.google_meet.node.server import NodeServer
    from plugins.google_meet.node import protocol
    from plugins.google_meet import process_manager as pm

    def boom():
        raise ValueError("kaboom")

    monkeypatch.setattr(pm, "status", boom)

    s = NodeServer(token_path=tmp_path / "t.json")
    tok = s.ensure_token()
    req = protocol.make_request("status", tok, {})
    resp = asyncio.run(s._handle_request(req))
    assert resp["type"] == "error"
    assert "kaboom" in resp["error"]


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------

class _FakeWS:
    """Minimal context-manager stand-in for websockets.sync.client.connect."""

    def __init__(self, reply_builder):
        self._reply_builder = reply_builder
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def send(self, raw):
        self.sent.append(raw)

    def recv(self, timeout=None):
        return self._reply_builder(self.sent[-1])


def _install_fake_ws(monkeypatch, reply_builder):
    fake_ws_holder = {}

    def _connect(url, **kwargs):
        ws = _FakeWS(reply_builder)
        fake_ws_holder["ws"] = ws
        fake_ws_holder["url"] = url
        fake_ws_holder["kwargs"] = kwargs
        return ws

    # Patch the concrete import site inside client._rpc. The package is optional
    # at runtime, so tests install a fake module tree when it is not present.
    try:
        import websockets.sync.client as wsc  # type: ignore
    except ModuleNotFoundError:
        websockets_mod = types.ModuleType("websockets")
        sync_mod = types.ModuleType("websockets.sync")
        wsc = types.ModuleType("websockets.sync.client")
        wsc.connect = None
        sync_mod.client = wsc
        websockets_mod.sync = sync_mod
        monkeypatch.setitem(sys.modules, "websockets", websockets_mod)
        monkeypatch.setitem(sys.modules, "websockets.sync", sync_mod)
        monkeypatch.setitem(sys.modules, "websockets.sync.client", wsc)
    monkeypatch.setattr(wsc, "connect", _connect)
    return fake_ws_holder


def test_client_rpc_sends_correct_envelope_and_parses_response(monkeypatch):
    from plugins.google_meet.node.client import NodeClient
    from plugins.google_meet.node import protocol

    def reply(raw_out):
        req = protocol.decode(raw_out)
        return protocol.encode(protocol.make_response(req["id"], {"ok": True, "echo": req["type"]}))

    holder = _install_fake_ws(monkeypatch, reply)

    c = NodeClient("ws://remote:1", "tok123")
    out = c._rpc("ping", {"hello": 1})
    assert out == {"ok": True, "echo": "ping"}

    sent = json.loads(holder["ws"].sent[0])
    assert sent["type"] == "ping"
    assert sent["token"] == "tok123"
    assert sent["payload"] == {"hello": 1}
    assert sent["id"]  # non-empty
    assert holder["url"] == "ws://remote:1"


def test_client_server_ping_roundtrip_over_localhost(tmp_path):
    from plugins.google_meet.node import protocol
    from plugins.google_meet.node.client import NodeClient
    from plugins.google_meet.node.server import NodeServer
    from websockets.sync.server import serve

    node = NodeServer(
        token_path=tmp_path / "token.json",
        display_name="local-node",
    )
    token = node.ensure_token()

    def handler(connection):
        request = protocol.decode(connection.recv())
        response = asyncio.run(node._handle_request(request))
        connection.send(protocol.encode(response))

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            result = NodeClient(
                f"ws://127.0.0.1:{port}",
                token,
                timeout=2.0,
            ).ping()
        finally:
            server.shutdown()
            server_thread.join(timeout=2.0)

    assert result["display_name"] == "local-node"
    assert isinstance(result["ts"], float)


# ---------------------------------------------------------------------------
# cli.py
# ---------------------------------------------------------------------------

def _build_parser():
    from plugins.google_meet.node.cli import register_cli

    parser = argparse.ArgumentParser(prog="meet-node-test")
    register_cli(parser)
    return parser


def test_cli_approve_list_remove(capsys):
    from plugins.google_meet.node.registry import NodeRegistry

    p = _build_parser()

    args = p.parse_args(["approve", "mac", "ws://mac:1", "tok"])
    rc = args.func(args)
    assert rc == 0
    assert NodeRegistry().get("mac") is not None

    args = p.parse_args(["list"])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "mac" in out
    assert "ws://mac:1" in out

    args = p.parse_args(["remove", "mac"])
    rc = args.func(args)
    assert rc == 0
    assert NodeRegistry().get("mac") is None


