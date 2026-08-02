"""Tests for the google_meet node primitive.

Covers protocol helpers, the file-backed registry, the server's
token-and-dispatch machinery, a mocked client, and the CLI plumbing.
We never open a real socket — websockets.serve / websockets.sync.client
are fully mocked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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


def test_registry_file_is_private(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    p = tmp_path / "nodes.json"
    NodeRegistry(path=p).add("mac", "ws://mac.local:18789", "secret")
    if os.name == "posix":
        assert p.stat().st_mode & 0o077 == 0


def test_registry_get_returns_none_when_missing(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    assert r.get("ghost") is None


def test_registry_remove(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    r.add("a", "ws://a", "t")
    assert r.remove("a") is True
    assert r.get("a") is None
    assert r.remove("a") is False  # idempotent


def test_registry_list_all_sorted(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    r.add("zeta", "ws://z", "t1")
    r.add("alpha", "ws://a", "t2")
    names = [n["name"] for n in r.list_all()]
    assert names == ["alpha", "zeta"]


def test_registry_resolve_auto_picks_single(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    r.add("mac", "ws://mac", "t")
    picked = r.resolve(None)
    assert picked is not None
    assert picked["name"] == "mac"


def test_registry_resolve_ambiguous_returns_none(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    r.add("a", "ws://a", "t")
    r.add("b", "ws://b", "t")
    assert r.resolve(None) is None


def test_registry_resolve_empty_returns_none(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    assert r.resolve(None) is None


def test_registry_resolve_by_name(tmp_path):
    from plugins.google_meet.node.registry import NodeRegistry

    r = NodeRegistry(path=tmp_path / "n.json")
    r.add("a", "ws://a", "t")
    r.add("b", "ws://b", "t")
    picked = r.resolve("b")
    assert picked is not None
    assert picked["name"] == "b"
    assert r.resolve("ghost") is None


def test_registry_defaults_to_hermes_home(tmp_path, monkeypatch):
    from plugins.google_meet.node.registry import NodeRegistry

    # _isolate_home already set HERMES_HOME to tmp_path/.hermes; the
    # registry default path must live inside that tree.
    r = NodeRegistry()
    r.add("x", "ws://x", "t")
    expected = Path(tmp_path) / ".hermes" / "workspace" / "meetings" / "nodes.json"
    assert expected.is_file()


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

    # Patch the concrete import site inside client._rpc
    import websockets.sync.client as wsc  # type: ignore
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


