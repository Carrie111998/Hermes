import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import spectator_relay


def _relay_env(monkeypatch, tmp_path: Path, profile: str = "default") -> str:
    token = "r" * 43
    monkeypatch.setenv("HERMES_SPECTATOR_DESCRIPTOR_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_SPECTATOR_RELAY_TOKEN", token)
    monkeypatch.setenv("HERMES_DESKTOP_PROFILE", profile)
    return token


def test_descriptor_is_private_profile_scoped_and_owner_checked(monkeypatch, tmp_path):
    token = _relay_env(monkeypatch, tmp_path, "hermes2")
    descriptor = spectator_relay.write_desktop_descriptor(43123)

    assert descriptor is not None
    assert descriptor.profile == "hermes2"
    assert descriptor.token == token
    assert descriptor.path.stat().st_mode & 0o777 == 0o600
    assert spectator_relay.load_desktop_descriptor("hermes2") == descriptor
    assert spectator_relay.load_desktop_descriptor("default") is None
    assert spectator_relay.has_live_desktop_descriptors() is True

    # A descriptor made group/world-readable fails closed even if its content is valid.
    descriptor.path.chmod(0o644)
    assert spectator_relay.load_desktop_descriptor("hermes2") is None
    descriptor.path.chmod(0o600)

    # Teardown cannot remove a descriptor replaced by a newer backend owner.
    payload = json.loads(descriptor.path.read_text(encoding="utf-8"))
    payload["token"] = "n" * 43
    descriptor.path.write_text(json.dumps(payload), encoding="utf-8")
    descriptor.path.chmod(0o600)
    spectator_relay.remove_desktop_descriptor(descriptor)
    assert descriptor.path.exists()


def test_stale_descriptor_is_not_live(monkeypatch, tmp_path):
    _relay_env(monkeypatch, tmp_path)
    descriptor = spectator_relay.write_desktop_descriptor(43123)
    assert descriptor is not None
    monkeypatch.setattr(spectator_relay, "_pid_is_live", lambda _pid: False)
    assert spectator_relay.load_desktop_descriptor("default") is None
    assert spectator_relay.has_live_desktop_descriptors() is False


def test_pid_liveness_uses_shared_cross_platform_probe(monkeypatch):
    import gateway.status as gateway_status

    calls = []
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: calls.append(pid) or True)

    assert spectator_relay._pid_is_live(43123) is True
    assert calls == [43123]

    monkeypatch.setattr(
        gateway_status,
        "_pid_exists",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("probe unavailable")),
    )
    assert spectator_relay._pid_is_live(43123) is False


def test_relay_rejects_writer_rpc_before_upstream_lookup(monkeypatch, tmp_path):
    _relay_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        spectator_relay,
        "load_desktop_descriptor",
        lambda _profile: (_ for _ in ()).throw(AssertionError("upstream lookup reached")),
    )

    class Browser:
        def __init__(self):
            self.sent = []
            self.inbound = iter(
                [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "prompt.submit", "params": {}})]
            )

        async def accept(self):
            pass

        async def send_text(self, frame):
            self.sent.append(json.loads(frame))

        async def receive_text(self):
            try:
                return next(self.inbound)
            except StopIteration:
                raise RuntimeError("disconnect")

        async def close(self, **_kwargs):
            pass

    browser = Browser()
    asyncio.run(spectator_relay.relay_spectator_ws(browser))
    rejection = next(frame for frame in browser.sent if frame.get("id") == 9)
    assert rejection["error"] == {
        "code": -32601,
        "message": "method not allowed on read-only transport",
    }


def test_relay_routes_subscription_to_owning_profile(monkeypatch, tmp_path):
    token = _relay_env(monkeypatch, tmp_path)
    descriptor = spectator_relay.DesktopSpectatorDescriptor(
        tmp_path / "descriptor.json", os.getpid() + 1000, 43123, "hermes3", token
    )
    selected = []
    monkeypatch.setattr(
        spectator_relay,
        "load_desktop_descriptor",
        lambda profile: selected.append(profile) or (descriptor if profile == "hermes3" else None),
    )

    class Upstream:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def recv(self):
            return json.dumps(
                {"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready", "payload": {}}}
            )

        async def send(self, frame):
            self.sent.append(json.loads(frame))

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    upstream = Upstream()
    connect_calls = []

    async def connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return upstream

    import websockets

    monkeypatch.setattr(websockets, "connect", connect)

    class Browser:
        def __init__(self):
            self.sent = []
            self.inbound = iter(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "session.subscribe",
                            "params": {"session_id": "stored-1", "profile": "hermes3"},
                        }
                    )
                ]
            )

        async def accept(self):
            pass

        async def send_text(self, frame):
            self.sent.append(json.loads(frame))

        async def receive_text(self):
            try:
                return next(self.inbound)
            except StopIteration:
                raise RuntimeError("disconnect")

        async def close(self, **_kwargs):
            pass

    browser = Browser()
    asyncio.run(spectator_relay.relay_spectator_ws(browser))

    assert selected == ["hermes3"]
    assert len(connect_calls) == 1
    assert connect_calls[0][0].startswith("ws://127.0.0.1:43123/api/ws?spectator_relay=")
    assert upstream.sent == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.subscribe",
            "params": {"session_id": "stored-1", "profile": "hermes3"},
        }
    ]
    assert upstream.closed is True


def test_desktop_relay_credential_maps_to_spectator_fence(monkeypatch):
    import hermes_cli.web_server as web_server

    token = "z" * 43
    monkeypatch.setenv("HERMES_SPECTATOR_RELAY_TOKEN", token)
    ws = SimpleNamespace(query_params={"spectator_relay": token})
    assert web_server._ws_auth_reason(ws) == (None, "spectator")

    ws.query_params["spectator_relay"] = "wrong"
    assert web_server._ws_auth_reason(ws) == (
        "spectator_relay_mismatch",
        "spectator",
    )
