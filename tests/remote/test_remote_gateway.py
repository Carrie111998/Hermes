"""E2E tests for the remote device gateway (HRA-2026-001 host side).

Run against a real in-process aiohttp server with real TLS, a temp
HERMES_HOME, and the FULL pairing ceremony — QR payload, single-use
secret, 6-digit confirmation code, ECIES token envelope, device token,
scoped auth. Mirrors the Android app's own gate tests where the wire
contract overlaps (single-use secrets, revocation, scopes).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gateway.config import PlatformConfig
from gateway.platforms.remote import (
    RemoteDeviceAdapter,
    RemoteState,
    derive_confirmation_code,
    qr_url_for,
)

# ---------------------------------------------------------------------------
# Helpers: a fake "phone" (P-256 keypair + ECIES decrypt, as the app does)
# ---------------------------------------------------------------------------


class FakePhone:
    """What the Android app does on the pairing side."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    def public_key_spki_b64(self) -> str:
        return base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )).decode()

    def decrypt_envelope(self, envelope_b64: str) -> str:
        """Mirror EciesEnvelope.kt: 65 B point || 12 B IV || ct||tag."""
        raw = base64.b64decode(envelope_b64)
        point = raw[:65]
        iv = raw[65:77]
        ct = raw[77:]
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        shared = self.key.exchange(ec.ECDH(), peer)
        salt = b"hermes-ecies-v1" + point + self.key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        okm = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=salt,
            info=b"hermes-remote-push-v1",
        ).derive(shared)
        return AESGCM(okm).decrypt(iv, ct, None).decode()


@pytest.fixture()
def state(tmp_path: Path):
    return RemoteState(tmp_path)


@pytest.fixture()
def home_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _parse_qr(text: str) -> dict:
    q = text.split("?", 1)[1]
    return dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)


# ---------------------------------------------------------------------------
# Server harness
# ---------------------------------------------------------------------------


class Server:
    def __init__(self, tmp_path: Path) -> None:
        self.adapter = RemoteDeviceAdapter(PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "urls": [],
                "state_dir": str(tmp_path),
            },
        ))
        self._tmp = tmp_path

    async def __aenter__(self):
        await self.adapter.start()
        port = self.adapter._site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        ctx = ssl.create_default_context()
        ctx.check_hostname = False  # the app pins the SPKI, not the name
        ctx.load_verify_locations(self._tmp / "tls-cert.pem")
        self.ssl = ctx
        self.base = f"https://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        await self.adapter.stop()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def server(tmp_path: Path):
    s = Server(tmp_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(s.__aenter__())
    yield s
    loop.run_until_complete(s.__aexit__())
    loop.close()


class Phone:
    def __init__(self, server) -> None:
        self.server = server
        self.phone = FakePhone()
        self.headers = {}

    def _request(self, method, path, body=None, headers=None, raw_body=None):
        import aiohttp

        async def go():
            kw = {}
            if body is not None:
                kw["data"] = json.dumps(body)
            hdrs = dict(headers or {})
            if body is not None:
                hdrs["Content-Type"] = "application/json"
            if raw_body is not None:
                kw["data"] = raw_body
            async with aiohttp.ClientSession() as s:
                async with s.request(
                    method, self.server.base + path, headers=hdrs,
                    ssl=self.server.ssl, **kw,
                ) as r:
                    text = await r.text()
                    return r.status, text
        return _run(go())

    def pair(self, secret_hex, device_name="Pixel 6"):
        return self._request("POST", "/api/remote/v1/pair/register", {
            "secret": secret_hex,
            "device_name": device_name,
            "requested_scopes": ["read", "chat", "control", "approve"],
            "public_key": self.phone.public_key_spki_b64(),
        })

    def poll(self, reg_id):
        return self._request("GET", f"/api/remote/v1/pair/status/{reg_id}")

    def get(self, path):
        return self._request("GET", path, headers=self.headers)

    def post(self, path, body=None, headers=None):
        merged = {**self.headers, **(headers or {})}
        return self._request("POST", path, body, headers=merged)

    def patch(self, path, body=None, headers=None):
        merged = {**self.headers, **(headers or {})}
        return self._request("PATCH", path, body, headers=merged)

    def delete(self, path, headers=None):
        merged = {**self.headers, **(headers or {})}
        return self._request("DELETE", path, None, headers=merged)

    def register_token(self, token):
        self.headers["Authorization"] = f"Bearer {token}"

    def decrypt_envelope(self, envelope_b64: str) -> str:
        return self.phone.decrypt_envelope(envelope_b64)


# ---------------------------------------------------------------------------
# Unit: QR payload shape (the app's PairingQr rules)
# ---------------------------------------------------------------------------


def test_qr_payload_parses_under_app_rules(state):
    pairing = state.create_pairing(ttl_seconds=600)
    url = qr_url_for("testhost", ["https://192.168.1.5:8643"], "ab" * 32,
                     pairing["secret_hex"], 600)
    p = _parse_qr(url)
    assert url.startswith("hra://pair?")
    assert p["v"] == "1"
    assert p["host"] == "testhost"
    assert p["urls"].startswith("[https://")
    assert p["urls"].endswith("]")
    assert len(p["fp"]) == 64 and all(c in "0123456789abcdef" for c in p["fp"])
    assert len(p["secret"]) == 64
    assert p["ttl"] == "600"


def test_confirmation_code_golden_vectors():
    # Pinned in ConfirmationCodeTest.kt (independent Python derivation).
    assert derive_confirmation_code(
        "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d5e6f708192a3b4c5d6e7f809"
    ) == "717261"
    assert derive_confirmation_code(
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    ) == "802349"


def test_ecies_envelope_decrypts_with_phone_key():
    phone = FakePhone()
    token = "hrd_" + uuid.uuid4().hex
    from gateway.platforms.remote import ecies_encrypt
    env = base64.b64encode(
        ecies_encrypt(phone.public_key_spki_b64(), token.encode())
    ).decode()
    assert phone.decrypt_envelope(env) == token


# ---------------------------------------------------------------------------
# E2E: the full ceremony
# ---------------------------------------------------------------------------


def test_full_pairing_ceremony(server, tmp_path):
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(ttl_seconds=600)
    phone = Phone(server)

    # wrong code does not confirm
    assert server.adapter.confirm_pairing("000000") is None

    # register consumes the secret once
    status, body = phone.pair(pairing["secret_hex"])
    assert status == 200, body
    reg_id = json.loads(body)["registration_id"]

    # replay is rejected
    status, body = phone.pair(pairing["secret_hex"])
    assert status == 409 and "used" in body

    # poll: pending until the host confirms
    status, body = phone.poll(reg_id)
    assert json.loads(body)["status"] == "pending"

    # host confirms with the 6-digit code
    code = derive_confirmation_code(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(code)
    assert result is not None

    # poll: confirmed, envelope decrypts to a working token
    status, body = phone.poll(reg_id)
    payload = json.loads(body)
    assert payload["status"] == "confirmed"
    assert payload["host_id"] == state.host_id()
    token = phone.decrypt_envelope(payload["token_envelope"])
    assert token.startswith("hrd_")

    phone.register_token(token)
    status, body = phone.get("/api/remote/v1/capabilities")
    assert status == 200
    caps = json.loads(body)
    assert caps["platform"] == "hermes-agent"


def test_health_is_public(server):
    phone = Phone(server)
    status, body = phone.get("/api/remote/v1/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_unauthenticated_requests_rejected(server):
    phone = Phone(server)
    status, body = phone.get("/api/remote/v1/sessions")
    assert status == 401
    assert "device_revoked" in body


def test_revocation_kills_every_path(server, tmp_path):
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    phone.register_token(result["token"])
    assert phone.get("/api/remote/v1/sessions")[0] == 200

    state.revoke_device(result["device_id"])
    status, body = phone.get("/api/remote/v1/sessions")
    assert status == 401


def test_scope_enforcement(server, tmp_path):
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"], device_name="reader")
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    state.update_device(result["device_id"], scopes=["read"])
    phone.register_token(result["token"])

    assert phone.get("/api/remote/v1/sessions")[0] == 200
    status, body = phone.post("/api/remote/v1/runs/some-run/stop", {})
    assert status == 403
    assert "insufficient_scope" in body


def test_idempotency_key_replay(server, tmp_path):
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    phone.register_token(result["token"])

    key = uuid.uuid4().hex
    hdrs = {"Idempotency-Key": key}
    status1, body1 = phone.post(
        "/api/remote/v1/jobs",
        {"name": "j", "prompt": "p", "schedule": "30m"},
        headers=hdrs,
    )
    # replay with the SAME body returns the stored response
    status2, body2 = phone.post(
        "/api/remote/v1/jobs",
        {"name": "j", "prompt": "p", "schedule": "30m"},
        headers=hdrs,
    )
    assert status2 == status1
    assert body2 == body1
    # same key, DIFFERENT body -> conflict
    status3, body3 = phone.post(
        "/api/remote/v1/jobs",
        {"name": "j2", "prompt": "p2", "schedule": "30m"},
        headers=hdrs,
    )
    assert status3 == 409
    assert "idempotency_key_conflict" in body3


def test_pairing_rate_limit(server, tmp_path):
    state = RemoteState(tmp_path)
    phone = Phone(server)
    from gateway.platforms.remote import PAIRING_RATE_LIMIT_PER_MIN
    for _ in range(PAIRING_RATE_LIMIT_PER_MIN):
        p = state.create_pairing(600)
        phone.pair(p["secret_hex"])
    p = state.create_pairing(600)
    status, body = phone.pair(p["secret_hex"])
    assert status == 429


def _paired_phone(server, tmp_path, scopes=("read", "approve", "control")):
    """Full ceremony; returns (phone, device_id) with a working token."""
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    phone.register_token(result["token"])
    return phone, result["device_id"]


def test_ws_stream_delivers_event_frames(server, tmp_path):
    """The app's WsEventSource contract: one receive-only WS, frames are
    RemoteEventDto JSON, replay + live push both arrive (SPEC §2.1.1)."""
    phone, _ = _paired_phone(server, tmp_path)

    async def go():
        import aiohttp

        frames = []
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(
                server.base + "/api/remote/v1/ws",
                headers=phone.headers, ssl=server.ssl,
            ) as ws:
                # seed frame: health.changed (replay of the ring)
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                assert msg.type == aiohttp.WSMsgType.TEXT
                first = json.loads(msg.data)
                assert {"id", "type", "ts", "payload"} <= set(first)
                # live push: an approval becomes pending
                server.adapter._emit_event(
                    "approval.pending", {"approval_id": "run:x:hash1"})
                # drain ring replay (health.changed, device.paired, ...)
                # until the pushed frame arrives
                second = None
                for _ in range(8):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    ev = json.loads(msg.data)
                    if ev["type"] == "approval.pending":
                        second = ev
                        break
                assert second is not None
                assert second["payload"]["approval_id"] == "run:x:hash1"
                assert int(second["id"]) > int(first["id"])
                frames.append(first["type"])
        return frames

    frames = _run(go())
    assert frames == ["health.changed"]


def test_ws_rejects_unauthenticated(server):
    async def go():
        import aiohttp

        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(
                    server.base + "/api/remote/v1/ws",
                    ssl=server.ssl,
                ) as ws:
                    await ws.receive()
            return None
        except aiohttp.WSServerHandshakeError as e:
            return e.status

    assert _run(go()) == 401


def test_device_self_service_patch(server, tmp_path):
    """T12 self-service: a device may rename itself and request scopes;
    another device's token cannot touch it (B1 regression)."""
    phone, device_id = _paired_phone(server, tmp_path)

    status, body = phone.patch(
        f"/api/remote/v1/devices/{device_id}", {"name": "Pixel Renamed"})
    assert status == 200, body
    dev = json.loads(body)
    assert dev["name"] == "Pixel Renamed"

    status, body = phone.patch(
        f"/api/remote/v1/devices/{device_id}",
        {"requested_scopes": ["read", "control"]})
    assert status == 200, body
    dev = json.loads(body)
    assert "control" in dev["requested_scopes"]
    assert dev["upgrade_requested_at"] > 0

    # a DIFFERENT device's token is rejected for this device
    other, _ = _paired_phone(server, tmp_path)
    status, body = other.patch(
        f"/api/remote/v1/devices/{device_id}", {"name": "Hijack"})
    assert status == 403
    assert "may only update itself" in body


def test_audit_never_contains_token_material(server, tmp_path, monkeypatch):
    """B1 regression: audit rows attribute the registry id; the bearer
    token (or its middle) never appears in audit.jsonl."""
    phone, device_id = _paired_phone(server, tmp_path)
    token = phone.headers["Authorization"][len("Bearer "):]

    # drive the audited paths: approval decision + scope request
    from tools.approval import resolve_gateway_approval  # noqa: F401 (name patched below)
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval", lambda *a, **k: 1)
    server.adapter._approval_index["run:r1:deadbeef"] = {
        "session_key": "sess-test", "run_id": "r1", "command_hash": "deadbeef"}
    status, body = phone.post(
        "/api/remote/v1/approvals/run:r1:deadbeef/decision",
        {"decision": "approve"})
    assert status == 200, body

    status, body = phone.patch(
        f"/api/remote/v1/devices/{device_id}",
        {"requested_scopes": ["read", "approve"]})
    assert status == 200, body

    rows = RemoteState(tmp_path).read_audit()
    assert any(r["action"] == "approval.decision" for r in rows)
    assert any(r["action"] == "device.scope_requested" for r in rows)
    blob = json.dumps(rows)
    assert token not in blob
    assert token.split("_", 1)[1] not in blob
    for r in rows:
        if r["action"] in ("approval.decision", "device.scope_requested"):
            assert r["actor"] == device_id


# ---------------------------------------------------------------------------
# v1.1: session lifecycle (pin/archive/rename/delete) + projects + profiles
# ---------------------------------------------------------------------------


def test_session_patch_pin_archive_and_delete(server, tmp_path, home_env):
    """PATCH /sessions/{id} {pinned|archived|title} and DELETE mirror the
    desktop's session sidebar actions against the SAME state.db — the
    desktop and the phone stay in sync because they share the host DB."""
    from hermes_state import SessionDB

    db = SessionDB(home_env / "state.db")
    phone, _ = _paired_phone(server, tmp_path)

    # create via the real API route (as the app does)
    status, body = phone.post(
        "/api/remote/v1/sessions",
        {"id": "phone-test-1", "title": "Phone Test", "cwd": str(home_env)})
    assert status == 201, body
    sid = "phone-test-1"

    # pin
    status, body = phone.patch(f"/api/remote/v1/sessions/{sid}", {"pinned": True})
    assert status == 200, body
    row = db.get_session(sid)
    assert row is not None and row["pinned"] == 1

    # archive
    status, body = phone.patch(f"/api/remote/v1/sessions/{sid}", {"archived": True})
    assert status == 200, body
    row = db.get_session(sid)
    assert row is not None and row["archived"] == 1

    # rename
    status, body = phone.patch(f"/api/remote/v1/sessions/{sid}", {"title": "Renamed by phone"})
    assert status == 200, body
    row = db.get_session(sid)
    assert row is not None and row["title"] == "Renamed by phone"

    # list excludes archived (matches desktop sidebar behaviour)
    status, body = phone.get("/api/remote/v1/sessions")
    assert status == 200, body
    sessions = json.loads(body)
    items = sessions if isinstance(sessions, list) else sessions.get("sessions", [])
    assert all(s["id"] != sid for s in items)

    # delete
    status, body = phone.delete(f"/api/remote/v1/sessions/{sid}")
    assert status == 200, body
    assert db.get_session(sid) is None


def test_session_patch_requires_chat_scope(server, tmp_path):
    """A read-only device cannot mutate sessions (chat scope required);
    session lifecycle is the chat surface, not control."""
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    state.update_device(result["device_id"], scopes=["read"])
    phone.register_token(result["token"])

    status, body = phone.patch("/api/remote/v1/sessions/nope", {"pinned": True})
    assert status == 403
    assert "insufficient_scope" in body


def test_projects_tree_route(server, tmp_path, home_env):
    """GET /projects returns the desktop sidebar tree: projects from
    projects.db, sessions grouped into repos/lanes, hydrated rows."""
    from hermes_state import SessionDB
    from hermes_cli import projects_db as pdb

    db = SessionDB(home_env / "state.db")
    cwd = str(home_env / "work" / "repo-a")
    # host-created sessions carry cwd (phone-created ones are unowned, as
    # on the desktop); the tree lane groups by cwd -> project folder match
    db.create_session("tree-session-1", source="tui", cwd=cwd)
    db.set_session_title("tree-session-1", "Tree Session")
    # tree only includes sessions with >= 1 message (desktop parity)
    db.append_message("tree-session-1", role="user", content="hello")
    sid = "tree-session-1"

    phone, _ = _paired_phone(server, tmp_path)
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="repo-a", folders=[cwd])
        pdb.set_active(conn, pid)

    status, body = phone.get("/api/remote/v1/projects")
    assert status == 200, body
    tree = json.loads(body)
    assert tree["active_project_id"] == pid
    projects = tree.get("projects", [])
    assert any(p.get("id") == pid for p in projects)
    for project in projects:
        if project.get("id") == pid:
            assert project["sessionCount"] >= 1
            nested = [
                s["id"]
                for repo in project.get("repos", [])
                for grp in repo.get("groups", [])
                for s in grp.get("sessions", [])
            ]
            assert sid in nested


def test_profiles_list_and_switch(server, tmp_path, monkeypatch):
    """GET /profiles mirrors `hermes profile list`; switch writes the
    active-profile marker (the desktop reads the same file)."""
    from hermes_cli import profiles as prof

    phone, _ = _paired_phone(server, tmp_path, scopes=("read", "approve", "control", "chat"))

    status, body = phone.get("/api/remote/v1/profiles")
    assert status == 200, body
    payload = json.loads(body)
    assert "profiles" in payload and "active" in payload
    names = [p["name"] for p in payload["profiles"]]
    assert "default" in names

    # switch to default (no-op safe)
    status, body = phone.post("/api/remote/v1/profiles/default/switch", {})
    assert status == 200, body
    assert json.loads(body)["active"] == "default"

    # unknown profile -> 404
    status, body = phone.post("/api/remote/v1/profiles/does-not-exist/switch", {})
    assert status == 404


def test_profiles_create_rename_delete(server, tmp_path):
    """Profile CRUD parity with `hermes profile create|rename|delete`."""
    from hermes_cli import profiles as prof

    phone, _ = _paired_phone(server, tmp_path, scopes=("read", "approve", "control", "chat"))

    # create
    status, body = phone.post("/api/remote/v1/profiles", {"name": "phone-created"})
    assert status == 201, body
    assert prof.get_profile_dir("phone-created").is_dir()

    # rename
    status, body = phone.patch("/api/remote/v1/profiles/phone-created", {"new_name": "phone-renamed"})
    assert status == 200, body
    assert prof.get_profile_dir("phone-renamed").is_dir()

    # delete
    status, body = phone.delete("/api/remote/v1/profiles/phone-renamed")
    assert status == 200, body
    assert not prof.get_profile_dir("phone-renamed").exists()

    # deleting the active profile is refused
    status, body = phone.delete("/api/remote/v1/profiles/default")
    assert status == 409


def test_profiles_require_control_scope(server, tmp_path):
    """Profile mutation is host admin: chat-only devices get 403."""
    state = RemoteState(tmp_path)
    pairing = state.create_pairing(600)
    phone = Phone(server)
    phone.pair(pairing["secret_hex"])
    result = server.adapter.confirm_pairing(
        derive_confirmation_code(pairing["secret_hex"]))
    state.update_device(result["device_id"], scopes=["read", "chat"])
    phone.register_token(result["token"])

    status, body = phone.post("/api/remote/v1/profiles", {"name": "sneaky"})
    assert status == 403
    assert "insufficient_scope" in body
