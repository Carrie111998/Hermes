"""Remote device control platform — the host side of Hermes Remote.

Wire contract: HRA-2026-001 (the Hermes Remote Android app's
``/api/remote/v1`` surface, pinned by ``core:testkit`` FakeHost and the
24 hermetic integration tests). The Android app is the contract owner;
this module implements the host side that the app was built against.

Design (host-integration doctrine: build the delta layer, not a
parallel protocol):

- Subclasses :class:`APIServerAdapter`: the remote surface MOUNTS the
  same session/run/job/approval handlers the API server serves, behind
  DEVICE-token auth + per-route scope enforcement instead of the global
  API key. The SSE run stream, approvals, jobs and sessions are the
  exact same code paths.
- Adds the pairing ceremony (``hra://`` QR code, single-use secret,
  TTL, 6-digit host-side confirmation code derived via HKDF — pinned by
  the app's ConfirmationCode golden vectors), the device registry
  (token hashes at rest, tokens issued inside an ECIES envelope to the
  device's own P-256 public key), the event ring, audit, idempotency
  keys, and the approval front-door.
- TLS is mandatory: a self-signed cert whose SPKI SHA-256 is the QR's
  ``fp``. The app pins exactly that (PinOrCaTrustManager) — trust-on-
  first-use by design; CA-verified hosts still work.

Run standalone with ``hermes remote start`` (the CLI owns lifecycle).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

logger = logging.getLogger("hermes.remote")

# ---------------------------------------------------------------------------
# Constants (pinned by the app's contract: PairingClient, FakeHost, DTOs)
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 600
PAIRING_RATE_LIMIT_PER_MIN = 5
EVENT_RING_SIZE = 200
TOKEN_PREFIX = "hrd_"

_PUBLIC_PATHS = (
    "/api/remote/v1/health",
    "/api/remote/v1/pair/register",
)

# Path -> required scope (SPEC §2.1.1 route table + device scopes).
_SCOPE_READ = "read"
_SCOPE_CHAT = "chat"
_SCOPE_CONTROL = "control"
_SCOPE_APPROVE = "approve"
_SCOPE_ADMIN = "admin"

_ALL_SCOPES = {_SCOPE_READ, _SCOPE_CHAT, _SCOPE_CONTROL, _SCOPE_APPROVE, _SCOPE_ADMIN}

# The default grant for every paired device: read + approve, plus
# control when the phone asks for it (the Operator role). Higher scopes
# are host decisions (SPEC §2.3 D-2).
_DEFAULT_GRANT = {_SCOPE_READ, _SCOPE_APPROVE}

_CAPABILITIES = {
    "jobs": True,
    "kanban": True,
    "remote_sessions": True,
    "devices": True,
    "audit": True,
}

_REMOTE_SCOPES = ["read", "approve", "chat", "control", "admin"]
_MIN_HOST_VERSION = "0.9.0"

# The remote platform's own release (HRA-2026-001 v1.0.0 contract), NOT
# the hermes-agent core's version. The Android app gates the connection
# on capabilities.version >= its MIN_HOST_VERSION (1.0.0); the core's
# 0.20.0 would read as an outdated host and block the WS. The core
# version is still exposed as "hermesVersion" for diagnosis.
_REMOTE_PLATFORM_VERSION = "1.0.0"


def _platform_version() -> str:
    try:
        from importlib import metadata

        return metadata.version("hermes-agent") or "0.0.0"
    except Exception:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Pairing primitives (byte-identical to the app's domain layer)
# ---------------------------------------------------------------------------


def derive_confirmation_code(secret_hex: str) -> str:
    """The 6-digit confirmation code (SPEC §2.3 D-2 step 3).

    ikm = the 32-byte pairing secret; salt = ``hermes-pair-code-v1``;
    info = ``pair-confirmation``; 4 output bytes -> big-endian int
    % 1_000_000 -> zero-padded 6 digits. Pinned by the app's
    ConfirmationCodeTest golden vectors (717261 / 802349).
    """
    ikm = bytes.fromhex(secret_hex)
    okm = HKDF(
        algorithm=hashes.SHA256(), length=4,
        salt=b"hermes-pair-code-v1", info=b"pair-confirmation",
    ).derive(ikm)
    return f"{int.from_bytes(okm, 'big') % 1_000_000:06d}"


def ecies_encrypt(peer_spki_b64: str, plaintext: bytes) -> bytes:
    """ECIES envelope exactly as EciesEnvelope.kt builds/parses it.

    Layout: 65 B SEC1 uncompressed ephemeral point || 12 B IV ||
    ciphertext||tag. Key derivation: ECDH(P-256), HKDF-SHA256 with
    salt = ``hermes-ecies-v1`` + ephemeral point + peer point, info =
    ``hermes-remote-push-v1``; AES-128-GCM (12 B nonce).
    """
    peer_der = base64.b64decode(peer_spki_b64)
    peer = serialization.load_der_public_key(peer_der)
    if not isinstance(peer, ec.EllipticCurvePublicKey):
        raise ValueError("pairing public key must be P-256")
    ephem = ec.generate_private_key(ec.SECP256R1())
    point = ephem.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    peer_point = peer.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    shared = ephem.exchange(ec.ECDH(), peer)
    salt = b"hermes-ecies-v1" + point + peer_point
    okm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=b"hermes-remote-push-v1",
    ).derive(shared)
    iv = secrets.token_bytes(12)
    ct = AESGCM(okm).encrypt(iv, plaintext, None)
    return point + iv + ct


def qr_url_for(
    host_name: str,
    urls: List[str],
    fp: str,
    secret_hex: str,
    ttl_seconds: int,
) -> str:
    """The ``hra://`` payload the app's PairingQr parses (SPEC §2.3 D-1)."""
    urls_part = "[" + ",".join(urls) + "]"
    return (
        f"hra://pair?v=1&host={host_name}&urls={urls_part}"
        f"&fp={fp}&secret={secret_hex}&ttl={ttl_seconds}"
    )


# ---------------------------------------------------------------------------
# State (single source of truth on disk; mtime-reload so CLI confirm and
# revoke reach a running server; RLock because save() re-enters)
# ---------------------------------------------------------------------------


class RemoteState:
    """Persistent host state: host identity, pairings, devices, audit.

    The file is the single source of truth. EVERY mutator reloads first
    (``reload_if_changed``) before locking and saving, or it clobbers
    the file with stale in-memory state.
    """

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        if state_dir is None:
            base = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
            state_dir = base / "remote"
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.state_dir / "devices.json"
        self._audit_file = self.state_dir / "audit.jsonl"
        self._lock = threading.RLock()
        self._mtime: Optional[float] = None
        self._data: Dict[str, Any] = self._load()

    # -- file plumbing -----------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text())
                if isinstance(data, dict) and "pairings" in data:
                    self._mtime = self._file.stat().st_mtime
                    return data
            except Exception:
                logger.exception("[remote] state file unreadable; starting empty")
        return {
            "host": {"id": uuid.uuid4().hex, "name": self._default_host_name()},
            "pairings": {},
            "devices": {},
        }

    @staticmethod
    def _default_host_name() -> str:
        try:
            import getpass

            return getpass.getuser() or "hermes"
        except Exception:
            return "hermes"

    def reload_if_changed(self) -> None:
        """Pick up CLI-side writes (confirm, revoke) before mutating."""
        if not self._file.exists():
            return
        try:
            mtime = self._file.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            with self._lock:
                self._data = self._load()

    def save(self) -> None:
        with self._lock:
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            os.replace(tmp, self._file)
            self._mtime = self._file.stat().st_mtime

    # -- host identity -----------------------------------------------------

    def host_id(self) -> str:
        return self._data["host"]["id"]

    def host_name(self) -> str:
        return self._data["host"].get("name") or self._default_host_name()

    def set_host_name(self, name: str) -> None:
        self.reload_if_changed()
        with self._lock:
            self._data["host"]["name"] = str(name)[:64]
            self.save()

    def spki_fingerprint_hex(self) -> str:
        cert_path = self.state_dir / "tls-cert.pem"
        if not cert_path.exists():
            return ""
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            spki = cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return hashlib.sha256(spki).hexdigest()
        except Exception:
            return ""

    # -- pairings ----------------------------------------------------------

    def list_pairings(self) -> Dict[str, Any]:
        return self._data["pairings"]

    def create_pairing(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            reg_id = uuid.uuid4().hex
            secret = secrets.token_bytes(32).hex()
            pairing = {
                "secret_hex": secret,
                "ttl_seconds": ttl_seconds,
                "created_at_ms": int(time.time() * 1000),
                "status": "pending",
                "device_name": None,
                "requested_scopes": [],
                "public_key_spki": None,
                "device_id": None,
                "confirm_attempts": 0,
                "locked": False,
            }
            self._data["pairings"][reg_id] = pairing
            self.save()
            return {"registration_id": reg_id, **pairing}

    def find_pairing_by_code(self, code: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        for reg_id, p in self._data["pairings"].items():
            if p.get("status") != "pending":
                continue
            if derive_confirmation_code(p["secret_hex"]) == code:
                return reg_id, p
        return None

    def get_pairing(self, reg_id: str) -> Optional[Dict[str, Any]]:
        return self._data["pairings"].get(reg_id)

    def expire_pairings(self) -> None:
        now = int(time.time() * 1000)
        with self._lock:
            changed = False
            for p in self._data["pairings"].values():
                if p.get("status") == "pending" and p["created_at_ms"] + p["ttl_seconds"] * 1000 <= now:
                    p["status"] = "expired"
                    changed = True
            if changed:
                self.save()

    def confirm_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Host ceremony: match the 6-digit code shown on the phone.

        The phone must have POSTed pair/register FIRST (the secret is
        single-use and the pubkey is captured there); only then does
        the code match produce a confirmed device + ECIES token.
        """
        self.reload_if_changed()
        with self._lock:
            found = self.find_pairing_by_code(code)
            if found is None:
                return None
            reg_id, pairing = found
            if not pairing.get("consumed") or not pairing.get("public_key_spki"):
                return None
            device_id = uuid.uuid4().hex
            token = TOKEN_PREFIX + secrets.token_hex(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            scopes = _default_scopes(pairing.get("requested_scopes") or [])
            envelope = base64.b64encode(
                ecies_encrypt(pairing["public_key_spki"], token.encode())
            ).decode()
            device = {
                "id": device_id,
                "name": pairing.get("device_name") or "Android device",
                "public_key_spki": pairing.get("public_key_spki"),
                "token_hash": token_hash,
                "created_at_ms": int(time.time() * 1000),
                "revoked": False,
                "scopes": scopes,
                "requested_scopes": pairing.get("requested_scopes") or [],
                "upgrade_requested_at": None,
                "issued_envelope": envelope,
            }
            self._data["devices"][device_id] = device
            pairing["status"] = "confirmed"
            pairing["device_id"] = device_id
            self.save()
        self.audit("host", "pairing.confirmed", reg_id)
        return {
            "device_id": device_id,
            "name": device["name"],
            "scopes": scopes,
            "token": token,
            "registration_id": reg_id,
        }

    # -- devices -----------------------------------------------------------

    def list_devices(self) -> Dict[str, Any]:
        return self._data["devices"]

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self._data["devices"].get(device_id)

    def device_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        for d in self._data["devices"].values():
            if d.get("token_hash") == token_hash:
                return d
        return None

    def update_device(self, device_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        self.reload_if_changed()
        with self._lock:
            device = self._data["devices"].get(device_id)
            if device is None:
                return None
            for key, value in fields.items():
                if key in ("name", "requested_scopes", "scopes", "upgrade_requested_at"):
                    device[key] = value
            self.save()
            return device

    def revoke_device(self, device_id: str) -> bool:
        self.reload_if_changed()
        with self._lock:
            device = self._data["devices"].get(device_id)
            if device is None:
                return False
            device["revoked"] = True
            self.save()
        self.audit("host", "device.revoked", device_id)
        return True

    # -- audit -------------------------------------------------------------

    def audit(self, actor: str, action: str, target: str, result: str = "ok") -> None:
        row = {
            "ts": int(time.time() * 1000),
            "actor": actor,
            "action": action,
            "target": target,
            "result": result,
        }
        try:
            with self._audit_file.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            logger.exception("[remote] audit write failed")

    def read_audit(self) -> List[Dict[str, Any]]:
        if not self._audit_file.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in self._audit_file.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows


# ---------------------------------------------------------------------------
# The gateway adapter
# ---------------------------------------------------------------------------


class RemoteDeviceAdapter(APIServerAdapter):
    """The ``/api/remote/v1`` device surface (pairing + mounted handlers).

    Mounts the base host's session/run/job/approval handlers (the SAME
    code paths the API server serves) behind device-token auth and
    per-route scopes. Adds the pairing ceremony, device registry, event
    ring, audit, idempotency, rate limiting, the approval front-door,
    and the receive-only WebSocket event stream.
    """

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config)
        extra = config.extra or {}
        self._state = RemoteState(extra.get("state_dir"))
        self._remote_host = str(extra.get("host") or "0.0.0.0")
        self._remote_port = int(extra.get("port", 8643))
        self._urls: List[str] = [str(u) for u in (extra.get("urls") or [])]
        self._ttl_seconds = int(extra.get("ttl_seconds") or DEFAULT_TTL_SECONDS)

        self._ssl_context: Optional[ssl.SSLContext] = None
        self._spki_fingerprint_hex: str = ""

        self._event_lock = threading.Lock()
        self._event_id = 0
        self._events: deque = deque(maxlen=EVENT_RING_SIZE)
        self._approval_lock = threading.Lock()
        self._approval_index: Dict[str, Dict[str, Any]] = {}
        self._idem_lock = threading.Lock()
        self._idem: Dict[str, Dict[str, Any]] = {}
        self._pair_lock = threading.Lock()
        self._pair_attempts: Dict[str, List[float]] = {}

        self._app = None
        self._runner = None
        self._site = None

    @property
    def state(self) -> RemoteState:
        return self._state

    @property
    def tls_key_path(self) -> Path:
        return self._state.state_dir / "tls-key.pem"

    @property
    def tls_cert_path(self) -> Path:
        return self._state.state_dir / "tls-cert.pem"

    # -- TLS ---------------------------------------------------------------

    def _ensure_tls(self) -> ssl.SSLContext:
        if self._ssl_context is not None:
            return self._ssl_context
        if self.tls_key_path.exists() and self.tls_cert_path.exists():
            key = self.tls_key_path.read_bytes()
            cert = self.tls_cert_path.read_bytes()
        else:
            key, cert = self._generate_self_signed()
            self.tls_key_path.write_bytes(key)
            self.tls_cert_path.write_bytes(cert)
            os.chmod(self.tls_key_path, 0o600)
            os.chmod(self.tls_cert_path, 0o600)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(self.tls_cert_path), keyfile=str(self.tls_key_path))
        loaded = x509.load_pem_x509_certificate(cert)
        spki = loaded.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._spki_fingerprint_hex = hashlib.sha256(spki).hexdigest()
        self._ssl_context = ctx
        return ctx

    @staticmethod
    def _generate_self_signed() -> Tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        now = time.time()
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "hermes-remote")])
        # SANs: loopback + any LAN IP we can see, so standard TLS clients
        # (tests, browsers) verify; the Android app pins the SPKI and does
        # not rely on names (PinOrCaTrustManager).
        san_ips = ["127.0.0.1", "::1"]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                san_ips.append(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.fromtimestamp(now - 86400, tz=timezone.utc))
            .not_valid_after(datetime.fromtimestamp(now + 10 * 365 * 86400, tz=timezone.utc))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address(ip)) for ip in san_ips]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        return key_pem, cert_pem

    def spki_fingerprint_hex(self) -> str:
        return self._spki_fingerprint_hex or self._state.spki_fingerprint_hex()

    @staticmethod
    def _lan_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            return "127.0.0.1"

    # -- pairing (CLI-facing) ----------------------------------------------

    def pair(self, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Create a pending pairing; CLI prints the QR + confirmation code."""
        self.state.expire_pairings()
        created = self.state.create_pairing(
            ttl_seconds or self._ttl_seconds
        )
        return {
            "registration_id": created["registration_id"],
            "qr_payload": self.qr_payload(
                created["registration_id"], created
            ),
            "confirmation_code": derive_confirmation_code(created["secret_hex"]),
            "ttl_seconds": created["ttl_seconds"],
            "host_id": self.state.host_id(),
        }

    def qr_payload(self, reg_id: str, pairing: Dict[str, Any]) -> str:
        urls = self._urls or [f"https://{self._lan_ip()}:{self._remote_port}"]
        fp = self.spki_fingerprint_hex()
        return qr_url_for(
            host_name=self.state.host_name(),
            urls=urls,
            fp=fp,
            secret_hex=pairing["secret_hex"],
            ttl_seconds=pairing["ttl_seconds"],
        )

    def confirm_pairing(self, code: str) -> Optional[Dict[str, Any]]:
        """Host ceremony: match the 6-digit code shown on the phone."""
        result = self.state.confirm_by_code(code)
        if result is None:
            return None
        self._emit_event("device.paired", {"device_id": result["device_id"]})
        return result

    # -- auth -------------------------------------------------------------------

    def _check_auth(self, request) -> Optional[Any]:
        """Device-token auth + per-route scope enforcement (replaces the
        API-key check the base host applies to every handler)."""
        self.state.reload_if_changed()
        path = request.path
        if path.startswith(_PUBLIC_PATHS):
            return None
        if path.startswith("/api/remote/v1/pair/status/"):
            return None
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return self._unauthorized()
        token = header[7:].strip()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        device = self.state.device_by_token_hash(token_hash)
        if device is None or device.get("revoked"):
            return self._unauthorized()
        # Stash the REGISTRY id (never the token) so downstream code
        # (audit attribution, self-service checks, idempotency keys)
        # addresses the device by its stable id.
        request["remote_device_id"] = device["id"]
        required = _required_scope(path, request.method)
        if required and required not in (device.get("scopes") or []):
            return web_json({"code": "insufficient_scope", "message": "device lacks scope: " + required}, 403)
        return None

    @staticmethod
    def _unauthorized() -> Any:
        return web_json(
            {"code": "device_revoked", "message": "token invalid"}, 401
        )

    # -- pairing routes -----------------------------------------------------------

    async def _handle_pair_register(self, request) -> Any:
        self.state.reload_if_changed()
        try:
            body = await request.json()
        except Exception:
            return web_json({"code": "malformed_request"}, 400)
        secret = str(body.get("secret", ""))
        reg = self.state.list_pairings()
        pairing_id = None
        for rid, p in reg.items():
            if p.get("secret_hex") == secret:
                pairing_id = rid
                break
        if pairing_id is None:
            return web_json({"code": "pairing_secret_invalid"}, 409)
        p = reg[pairing_id]
        if p.get("consumed") or p.get("status") != "pending":
            return web_json({"code": "pairing_secret_used"}, 409)
        now = int(time.time() * 1000)
        if p["created_at_ms"] + p["ttl_seconds"] * 1000 <= now:
            p["status"] = "expired"
            self.state.save()
            return web_json({"code": "pairing_secret_expired"}, 410)
        p["consumed"] = True
        p["device_name"] = str(body.get("device_name", "") or "Android")
        p["requested_scopes"] = _coerce_scopes(body.get("requested_scopes"))
        p["public_key_spki"] = str(body.get("public_key", "") or "")
        self.state.save()
        self.state.audit("pending", "pairing.registered", pairing_id)
        return web_json({"registration_id": pairing_id}, 200)

    async def _handle_pair_status(self, request) -> Any:
        self.state.reload_if_changed()
        reg_id = request.match_info["registration_id"]
        p = self.state.get_pairing(reg_id)
        if p is None:
            return web_json({"code": "pairing_not_found"}, 404)
        status = p.get("status", "pending")
        body: Dict[str, Any] = {"status": status, "host_id": self.state.host_id()}
        if status == "confirmed":
            body["device_id"] = p.get("device_id")
            body["name"] = p.get("device_name")
            device = self.state.get_device(p.get("device_id") or "")
            if device:
                body["token_envelope"] = device.get("issued_envelope")
        return web_json(body, 200)

    # -- public + capability routes ----------------------------------------------

    async def _handle_health(self, request) -> Any:
        return web_json({
            "status": "ok",
            "platform": "hermes-agent",
            "version": _REMOTE_PLATFORM_VERSION,
            "hermesVersion": _platform_version(),
        }, 200)

    async def _handle_capabilities(self, request) -> Any:
        return web_json({
            "platform": "hermes-agent",
            "version": _REMOTE_PLATFORM_VERSION,
            "hermesVersion": _platform_version(),
            "capabilities": _CAPABILITIES,
            "minHostVersion": _MIN_HOST_VERSION,
            "remote": {
                "minHostVersion": _MIN_HOST_VERSION,
                "scopes": _REMOTE_SCOPES,
            },
        }, 200)

    # -- event ring + WS ----------------------------------------------------------

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        result = super()._set_run_status(run_id, status, **fields)
        event_type = {
            "running": "run.started",
            "completed": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.cancelled",
            "stopping": "run.stopping",
        }.get(status)
        if event_type:
            self._emit_event(event_type, {"run_id": run_id})
        return result

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self._event_lock:
            self._event_id += 1
            self._events.append({
                "id": str(self._event_id),
                "type": event_type,
                "ts": int(time.time() * 1000),
                "payload": payload,
            })

    async def _handle_events(self, request) -> Any:
        with self._event_lock:
            events = sorted(self._events, key=lambda e: int(e["id"]))
        return web_json(events, 200)

    async def _handle_ws(self, request) -> Any:
        """One receive-only WebSocket per device (SPEC §2.1.1 WS row).

        The app (WsEventSource) sends NO frames; it receives RemoteEventDto
        JSON frames and pings every 30 s (OkHttp pingInterval). aiohttp's
        heartbeat answers the pings; the poll loop replays the event ring
        (dedupe by id on the app side) and pushes new events as they
        arrive. Frames are event-only — the socket cannot mutate anything.
        """
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        last_id = 0
        try:
            while True:
                with self._event_lock:
                    events = list(self._events)
                for e in events:
                    eid = int(e["id"])
                    if eid > last_id:
                        last_id = eid
                        try:
                            await ws.send_str(json.dumps(e))
                        except (ConnectionResetError, RuntimeError):
                            return ws
                if ws.closed:
                    return ws
                await asyncio.sleep(1.0)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return ws

    async def _handle_audit(self, request) -> Any:
        rows = self.state.read_audit()
        return web_json(rows, 200)

    # -- approvals front-door ---------------------------------------------------------

    async def _handle_list_approvals(self, request) -> Any:
        cards: List[Dict[str, Any]] = []
        now = int(time.time() * 1000)
        with self._approval_lock:
            self._approval_index.clear()
            for run_id, session_key in list(getattr(self, "_run_approval_sessions", {}).items()):
                entries = _pending_entries(session_key)
                for entry in entries:
                    data = entry.data or {}
                    command_hash = str(data.get("command_hash") or "")
                    if not command_hash:
                        command_hash = _hash_command(str(data.get("command", "")))
                    approval_id = f"run:{run_id}:{command_hash}"
                    self._approval_index[approval_id] = {
                        "session_key": session_key,
                        "run_id": run_id,
                        "command_hash": command_hash,
                    }
                    cards.append(_approval_card(approval_id, run_id, session_key, data, now))
        return web_json(cards, 200)

    async def _handle_approval_decision(self, request) -> Any:
        approval_id = request.match_info["approval_id"]
        try:
            body = await request.json()
        except Exception:
            return web_json({"code": "malformed_request"}, 400)
        decision = str(body.get("decision", "")).strip().lower()
        choice = {
            "approve": "once",
            "deny": "deny",
            "allow_always": "always",
            "once": "once",
            "always": "always",
        }.get(decision)
        if choice is None:
            return web_json({"code": "invalid_decision", "message": "expected approve|deny|allow_always"}, 400)
        with self._approval_lock:
            index = self._approval_index.get(approval_id)
            if index is None:
                # Fall back to scanning live queues (index may be cold after restart)
                index = self._find_approval_in_queues(approval_id)
            if index is None:
                return web_json({"code": "approval_not_found"}, 404)
            session_key = index["session_key"]
            run_id = index["run_id"]
        try:
            from tools.approval import resolve_gateway_approval
            resolved = resolve_gateway_approval(session_key, choice, resolve_all=False)
        except Exception:
            logger.exception("[remote] approval resolution failed")
            return web_json({"code": "approval_resolution_failed"}, 500)
        if resolved <= 0:
            return web_json({"code": "approval_not_pending"}, 409)
        with self._approval_lock:
            self._approval_index.pop(approval_id, None)
        if run_id and run_id in getattr(self, "_run_statuses", {}):
            self._set_run_status(run_id, "running", last_event="approval.responded")
        self.state.audit(_device_id_of(request), "approval.decision", approval_id, result=choice)
        return web_json({"ok": True, "approval_id": approval_id, "choice": choice}, 200)

    def _find_approval_in_queues(self, approval_id: str) -> Optional[Dict[str, Any]]:
        for run_id, session_key in list(getattr(self, "_run_approval_sessions", {}).items()):
            for entry in _pending_entries(session_key):
                data = entry.data or {}
                command_hash = str(data.get("command_hash") or "")
                if not command_hash:
                    command_hash = _hash_command(str(data.get("command", "")))
                if f"run:{run_id}:{command_hash}" == approval_id:
                    return {"session_key": session_key, "run_id": run_id, "command_hash": command_hash}
        return None

    # -- runs list (new: the base host has no list-runs route) -------------------------

    async def _handle_list_runs(self, request) -> Any:
        limit = int(request.query.get("limit", "50") or 50)
        now = time.time()
        rows = []
        for run_id, status in list(getattr(self, "_run_statuses", {}).items()):
            created = getattr(self, "_run_streams_created", {}).get(run_id, now)
            rows.append({
                "id": run_id,
                "status": status,
                "created_at": created,
                "updated_at": created,
            })
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return web_json(rows[:limit], 200)

    # -- devices ---------------------------------------------------------------------

    async def _handle_get_device(self, request) -> Any:
        device_id = request.match_info["device_id"]
        device = self.state.get_device(device_id)
        if device is None:
            return web_json({"code": "device_not_found"}, 404)
        return web_json(_device_view(device), 200)

    async def _handle_patch_device(self, request) -> Any:
        device_id = request.match_info["device_id"]
        authed = _device_id_of(request)
        device = self.state.get_device(device_id)
        if device is None:
            return web_json({"code": "device_not_found"}, 404)
        if authed and authed != device_id:
            return web_json({"code": "insufficient_scope", "message": "a device may only update itself"}, 403)
        try:
            body = await request.json()
        except Exception:
            return web_json({"code": "malformed_request"}, 400)
        name = body.get("name")
        requested = _coerce_scopes(body.get("requested_scopes"))
        if name is not None:
            device = self.state.update_device(device_id, name=str(name)[:64])
        if requested:
            device = self.state.update_device(device_id, requested_scopes=requested,
                                               upgrade_requested_at=int(time.time() * 1000))
            self.state.audit(device_id, "device.scope_requested", ",".join(requested))
        if device is None:
            return web_json({"code": "device_not_found"}, 404)
        return web_json(_device_view(device), 200)

    async def _handle_test_push(self, request) -> Any:
        device_id = request.match_info["device_id"]
        if _device_id_of(request) != device_id:
            return web_json({"code": "insufficient_scope", "message": "a device may only test itself"}, 403)
        self.state.audit(device_id, "device.test_push", device_id)
        return web_json({"ok": True, "queued": False, "reason": "push bridge not configured"}, 200)

    # -- kanban (V-9 remount over the real board store) --------------------------------

    async def _handle_kanban(self, request) -> Any:
        now_ms = int(time.time() * 1000)
        empty = {"columns": [], "latest_event_id": 0, "now": now_ms}
        try:
            from dataclasses import asdict

            from hermes_cli import kanban_db

            db_path = kanban_db.kanban_db_path()
            if not db_path.exists():
                return web_json(empty, 200)
            conn = kanban_db._sqlite_connect(db_path)
            try:
                tasks = kanban_db.list_tasks(conn)
            finally:
                conn.close()
            by_status: Dict[str, List[Dict[str, Any]]] = {}
            for t in tasks:
                d = asdict(t) if not isinstance(t, dict) else dict(t)
                status = str(d.get("status") or "todo")
                by_status.setdefault(status, []).append(d)
            columns = []
            for status in kanban_db.VALID_STATUSES:
                if status in by_status:
                    columns.append({"name": status, "tasks": by_status[status]})
            return web_json({
                "columns": columns,
                "latest_event_id": now_ms,
                "now": now_ms,
            }, 200)
        except Exception:
            logger.exception("[remote] kanban board read failed")
            return web_json(empty, 200)

    async def _handle_kanban_transition(self, request) -> Any:
        task_id = request.match_info["task_id"]
        try:
            body = await request.json()
        except Exception:
            return web_json({"code": "malformed_request"}, 400)
        target = str(body.get("status", "")).strip().lower()
        try:
            from hermes_cli import kanban_db

            if target not in kanban_db.VALID_STATUSES:
                return web_json({"ok": False, "reason": f"invalid status: {target}"}, 200)
            db_path = kanban_db.kanban_db_path()
            if not db_path.exists():
                return web_json({"ok": False, "reason": "no kanban board on this host"}, 200)
            conn = kanban_db._sqlite_connect(db_path)
            try:
                task = kanban_db.get_task(conn, task_id)
                if task is None:
                    return web_json({"ok": False, "reason": f"task {task_id} not found"}, 200)
                if target == "running":
                    kanban_db.claim_task(conn, task_id)
                elif target == "done":
                    kanban_db.complete_task(conn, task_id)
                elif target == "archived":
                    kanban_db.archive_task(conn, task_id)
                else:
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                            (target, int(time.time()), task_id),
                        )
            finally:
                conn.close()
            return web_json({"ok": True}, 200)
        except Exception:
            logger.exception("[remote] kanban transition failed")
            return web_json({"ok": False, "reason": "transition failed on host"}, 200)

    async def _handle_kanban_decompose(self, request) -> Any:
        task_id = request.match_info["task_id"]
        return web_json({"ok": False, "reason": f"decompose is not available on this host (task {task_id})"}, 200)

    async def _handle_kanban_specify(self, request) -> Any:
        task_id = request.match_info["task_id"]
        return web_json({"ok": False, "reason": f"specify is not available on this host (task {task_id})"}, 200)

    # -- server lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        """Build + run the aiohttp app (TLS, device auth, remote routes)."""
        from aiohttp import web

        self._ensure_tls()
        # Seed the event ring so the first WS connect delivers a frame
        # immediately (the app marks the host "seen" on any valid event).
        self._emit_event("health.changed", {"status": "ok"})

        @web.middleware
        async def _idempotency_middleware(request, handler):
            if request.method == "GET":
                return await handler(request)
            key = request.headers.get("Idempotency-Key", "")
            if not key or request.path.startswith(_PUBLIC_PATHS) or "pair/" in request.path:
                return await handler(request)
            device_id = _device_id_of(request) or "anon"
            idem_key = f"{device_id}:{key}"
            raw_body = await request.read()
            body_hash = hashlib.sha256(raw_body).hexdigest()
            with self._idem_lock:
                stored = self._idem.get(idem_key)
            if stored is not None:
                if stored["body_hash"] != body_hash:
                    return web_json({"code": "idempotency_key_conflict"}, 409)
                return web.Response(
                    body=stored["body"], status=stored["status"],
                    content_type=stored["content_type"],
                )
            response = await handler(request)
            if isinstance(response, web.StreamResponse) and not isinstance(response, web.Response):
                return response
            body = response.body or b""
            with self._idem_lock:
                self._idem[idem_key] = {
                    "body": body, "status": response.status,
                    "content_type": response.content_type or "application/json",
                    "body_hash": body_hash, "at": time.time(),
                }
                if len(self._idem) > 5000:
                    oldest = min(self._idem, key=lambda k: self._idem[k]["at"])
                    self._idem.pop(oldest, None)
            return response

        @web.middleware
        async def _pairing_rate_middleware(request, handler):
            if not request.path.startswith("/api/remote/v1/pair/"):
                return await handler(request)
            ip = request.remote or "?"
            now = time.time()
            with self._pair_lock:
                window = [t for t in self._pair_attempts.setdefault(ip, []) if now - t < 60]
                if len(window) >= PAIRING_RATE_LIMIT_PER_MIN:
                    return web_json({"code": "rate_limited", "message": "pairing attempts limited — wait and retry"}, 429)
                window.append(now)
                self._pair_attempts[ip] = window
            return await handler(request)

        @web.middleware
        async def _device_auth_middleware(request, handler):
            # One gate for every route (inherited + remote-only): device
            # token + per-route scope. Base handlers re-check internally;
            # the double check is idempotent.
            rejected = self._check_auth(request)
            if rejected is not None:
                return rejected
            return await handler(request)

        app = web.Application(middlewares=[
            _device_auth_middleware,
            _idempotency_middleware,
            _pairing_rate_middleware,
        ])
        for method, path, handler in self._http_route_table():
            app.router.add_route(method, path, handler)
        app["api_server_adapter"] = self

        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self._remote_host, self._remote_port,
            ssl_context=self._ssl_context,
            reuse_address=False if sys.platform == "darwin" else None,
        )
        await self._site.start()
        logger.info(
            "[remote] device gateway on https://%s:%d (fp=%s…)",
            self._remote_host, self._remote_port,
            (self._spki_fingerprint_hex or "")[:12],
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    # -- route table -------------------------------------------------------------------

    def _http_route_table(self) -> List[Tuple[str, str, Any]]:
        base = "/api/remote/v1"
        return [
            ("GET", f"{base}/health", self._handle_health),
            ("GET", f"{base}/capabilities", self._handle_capabilities),
            ("GET", f"{base}/sessions", self._handle_list_sessions),
            ("POST", f"{base}/sessions", self._handle_create_session),
            ("GET", f"{base}/sessions/{{session_id}}/messages", self._handle_session_messages),
            ("POST", f"{base}/sessions/{{session_id}}/chat", self._handle_session_chat),
            ("GET", f"{base}/runs", self._handle_list_runs),
            ("GET", f"{base}/runs/{{run_id}}", self._handle_get_run),
            ("GET", f"{base}/runs/{{run_id}}/events", self._handle_run_events),
            ("POST", f"{base}/runs/{{run_id}}/approval", self._handle_run_approval),
            ("POST", f"{base}/runs/{{run_id}}/stop", self._handle_stop_run),
            ("GET", f"{base}/approvals", self._handle_list_approvals),
            ("POST", f"{base}/approvals/{{approval_id}}/decision", self._handle_approval_decision),
            ("GET", f"{base}/jobs", self._handle_list_jobs),
            ("POST", f"{base}/jobs", self._handle_create_job),
            ("GET", f"{base}/jobs/{{job_id}}", self._handle_get_job),
            ("PATCH", f"{base}/jobs/{{job_id}}", self._handle_update_job),
            ("DELETE", f"{base}/jobs/{{job_id}}", self._handle_delete_job),
            ("POST", f"{base}/jobs/{{job_id}}/pause", self._handle_pause_job),
            ("POST", f"{base}/jobs/{{job_id}}/resume", self._handle_resume_job),
            ("POST", f"{base}/jobs/{{job_id}}/run", self._handle_run_job),
            ("GET", f"{base}/kanban", self._handle_kanban),
            ("POST", f"{base}/kanban/{{task_id}}/transition", self._handle_kanban_transition),
            ("POST", f"{base}/kanban/{{task_id}}/decompose", self._handle_kanban_decompose),
            ("POST", f"{base}/kanban/{{task_id}}/specify", self._handle_kanban_specify),
            ("GET", f"{base}/events", self._handle_events),
            ("GET", f"{base}/ws", self._handle_ws),
            ("GET", f"{base}/audit", self._handle_audit),
            ("GET", f"{base}/devices/{{device_id}}", self._handle_get_device),
            ("PATCH", f"{base}/devices/{{device_id}}", self._handle_patch_device),
            ("POST", f"{base}/devices/{{device_id}}/test-push", self._handle_test_push),
            ("POST", f"{base}/pair/register", self._handle_pair_register),
            ("GET", f"{base}/pair/status/{{registration_id}}", self._handle_pair_status),
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percent_encode(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def _coerce_scopes(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        s = str(s).strip().lower()
        if s in _ALL_SCOPES and s not in out:
            out.append(s)
    return out


def _default_scopes(requested: List[str]) -> List[str]:
    scopes = set(_DEFAULT_GRANT)
    requested_set = set(requested)
    for scope in (_SCOPE_CONTROL, _SCOPE_CHAT, _SCOPE_ADMIN):
        if scope in requested_set:
            scopes.add(scope)
    return sorted(scopes)


def _required_scope(path: str, method: str) -> Optional[str]:
    if method == "GET":
        return _SCOPE_READ
    if path.endswith("/chat"):
        return _SCOPE_CHAT
    if "/approval" in path or "/decision" in path:
        return _SCOPE_APPROVE
    if path.endswith("/stop") or "/jobs/" in path or "/kanban/" in path or path.endswith("/test-push"):
        return _SCOPE_CONTROL
    if "/devices/" in path:
        return _SCOPE_CONTROL
    return _SCOPE_CONTROL


def _hash_command(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()[:32]


def _approval_card(approval_id: str, run_id: str, session_key: str, data: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    pattern_keys = data.get("pattern_keys") or ([data["pattern_key"]] if data.get("pattern_key") else [])
    expires = data.get("expires_at")
    if not isinstance(expires, (int, float)) or expires <= 0:
        expires = int(time.time()) + 300
    return {
        "approval_id": approval_id,
        "pattern_keys": [str(k) for k in pattern_keys],
        "destructive": bool(data.get("destructive", False)),
        "description": str(data.get("description") or "Approve this action on the host?"),
        "description_provenance": str(data.get("description_provenance") or "host_approval"),
        "surface": str(data.get("surface") or "gateway"),
        "session_key": session_key or None,
        "run_id": run_id,
        "created_at": now_ms,
        "expires_at": int(expires),
        "status": "pending",
    }


def _pending_entries(session_key: str) -> List[Any]:
    try:
        from tools import approval as _approval
        with _approval._lock:
            return list(_approval._gateway_queues.get(session_key, []))
    except Exception:
        return []


def _device_id_of(request) -> str:
    # The auth middleware stashes the registry id; never derive an id
    # from the bearer token (that leaks token material into audit rows).
    return request.get("remote_device_id", "")


def _device_view(device: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": device.get("id"),
        "name": device.get("name"),
        "scopes": device.get("scopes", []),
        "created_at": device.get("created_at_ms", 0) / 1000.0,
        "revoked": bool(device.get("revoked", False)),
        "requested_scopes": device.get("requested_scopes", []),
        "upgrade_requested_at": device.get("upgrade_requested_at"),
    }


def web_json(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=json.dumps(payload).encode(),
        status=status,
        content_type="application/json",
    )


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()
