"""Minimal Nostr crypto helpers for Buzz observer frames.

Implements enough of NIP-01 signing, NIP-44 v2 encryption, and NIP-98 HTTP
auth to POST Kind 24200 observer frames to a Buzz relay without the Rust CLI.
Deps: coincurve + cryptography (+ optional bech32 for nsec/npub).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

try:
    from coincurve import PrivateKey as CCPrivateKey
    from coincurve.keys import PublicKey as CCPublicKey
except Exception:  # pragma: no cover
    CCPrivateKey = None  # type: ignore[misc, assignment]
    CCPublicKey = None  # type: ignore[misc, assignment]

KIND_OBSERVER = 24200
NIP44_VERSION = 2
NIP44_MIN_CONTENT_LEN = 132
NIP44_MAX_CONTENT_LEN = 87_472
OBSERVER_MAX_PLAINTEXT = 65_535
OBSERVER_AGENT_TAG = "agent"
OBSERVER_FRAME_TAG = "frame"


class NostrCryptoError(RuntimeError):
    """Raised when local crypto/signing fails."""


def _require_coincurve() -> None:
    if CCPrivateKey is None or CCPublicKey is None:
        raise NostrCryptoError(
            "coincurve is required for observer frames; pip install coincurve cryptography"
        )


def normalize_sk(private_key: str) -> bytes:
    """Accept 64-char hex or nsec bech32 and return 32-byte secret."""
    value = private_key.strip()
    if not value:
        raise NostrCryptoError("private key is empty")
    if value.startswith("nsec1"):
        try:
            import bech32
        except Exception as exc:  # pragma: no cover
            raise NostrCryptoError("bech32 package required to decode nsec") from exc
        hrp, data = bech32.bech32_decode(value)
        if hrp != "nsec" or data is None:
            raise NostrCryptoError("invalid nsec bech32")
        decoded = bech32.convertbits(data, 5, 8, False)
        if not decoded or len(decoded) != 32:
            raise NostrCryptoError("invalid nsec payload length")
        return bytes(decoded)
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return bytes.fromhex(value)
    raise NostrCryptoError("private key must be 64-char hex or nsec1...")


def normalize_pubkey(pubkey: str) -> str:
    value = pubkey.strip()
    if value.startswith("npub1"):
        try:
            import bech32
        except Exception as exc:  # pragma: no cover
            raise NostrCryptoError("bech32 package required to decode npub") from exc
        hrp, data = bech32.bech32_decode(value)
        if hrp != "npub" or data is None:
            raise NostrCryptoError("invalid npub bech32")
        decoded = bech32.convertbits(data, 5, 8, False)
        if not decoded or len(decoded) != 32:
            raise NostrCryptoError("invalid npub payload length")
        return bytes(decoded).hex()
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    raise NostrCryptoError("pubkey must be 64-char hex or npub1...")


def pubkey_from_sk(sk: bytes) -> str:
    _require_coincurve()
    assert CCPrivateKey is not None
    pub = CCPrivateKey(sk).public_key.format(compressed=True)
    return pub[1:].hex()


def _xonly_to_compressed(x_hex: str) -> bytes:
    _require_coincurve()
    assert CCPublicKey is not None
    x = bytes.fromhex(x_hex)
    if len(x) != 32:
        raise NostrCryptoError("recipient pubkey must be 32 bytes")
    # Prefer even-Y compressed key; fall back to odd-Y.
    try:
        return CCPublicKey(b"\x02" + x).format(compressed=True)
    except Exception:
        return CCPublicKey(b"\x03" + x).format(compressed=True)


def _calc_conversation_key(sk: bytes, recipient_pubkey_hex: str) -> bytes:
    """NIP-44 conversation key = HKDF-Extract(salt='nip44-v2', IKM=sharedX)."""
    _require_coincurve()
    assert CCPrivateKey is not None
    shared = CCPrivateKey(sk).ecdh(_xonly_to_compressed(recipient_pubkey_hex))
    # HMAC-SHA256 as HKDF-Extract
    h = hmac.HMAC(b"nip44-v2", hashes.SHA256())
    h.update(shared)
    return h.finalize()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869) with SHA-256."""
    okm = bytearray()
    t = b""
    counter = 1
    while len(okm) < length:
        h = hmac.HMAC(prk, hashes.SHA256())
        h.update(t + info + bytes([counter]))
        t = h.finalize()
        okm.extend(t)
        counter += 1
    return bytes(okm[:length])


def _message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    material = _hkdf_expand(conversation_key, nonce, 76)
    return material[:32], material[32:44], material[44:76]


def _calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len < 1 or unpadded_len > 65535:
        raise NostrCryptoError(f"plaintext length out of range: {unpadded_len}")
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (int(math.floor(math.log2(unpadded_len - 1))) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def _pad(plaintext: bytes) -> bytes:
    padded_len = _calc_padded_len(len(plaintext))
    return len(plaintext).to_bytes(2, "big") + plaintext + (b"\x00" * (padded_len - len(plaintext)))


def nip44_encrypt(sk: bytes, recipient_pubkey_hex: str, plaintext: str) -> str:
    """Encrypt plaintext to NIP-44 v2 base64 payload."""
    data = plaintext.encode("utf-8")
    if len(data) > OBSERVER_MAX_PLAINTEXT:
        raise NostrCryptoError(
            f"observer plaintext exceeds {OBSERVER_MAX_PLAINTEXT} bytes (got {len(data)})"
        )
    conversation_key = _calc_conversation_key(sk, recipient_pubkey_hex)
    nonce = secrets.token_bytes(32)
    chacha_key, chacha_nonce, hmac_key = _message_keys(conversation_key, nonce)
    ciphertext = ChaCha20Poly1305(chacha_key).encrypt(chacha_nonce, _pad(data), None)
    mac_h = hmac.HMAC(hmac_key, hashes.SHA256())
    mac_h.update(nonce + ciphertext)
    mac = mac_h.finalize()
    payload = bytes([NIP44_VERSION]) + nonce + ciphertext + mac
    return base64.b64encode(payload).decode("ascii")


def content_looks_like_nip44(content: str) -> bool:
    return NIP44_MIN_CONTENT_LEN <= len(content) <= NIP44_MAX_CONTENT_LEN


def _serialize_event_id(
    pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str
) -> str:
    payload = [0, pubkey, created_at, kind, tags, content]
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _schnorr_sign(sk: bytes, msg_hash_hex: str) -> str:
    _require_coincurve()
    assert CCPrivateKey is not None
    msg = bytes.fromhex(msg_hash_hex)
    sig = CCPrivateKey(sk).sign_schnorr(msg, aux_randomness=os.urandom(32))
    return sig.hex()


def sign_event(
    sk: bytes, kind: int, content: str, tags: list[list[str]] | None = None
) -> dict[str, Any]:
    tags = tags or []
    pubkey = pubkey_from_sk(sk)
    created_at = int(time.time())
    event_id = _serialize_event_id(pubkey, created_at, kind, tags, content)
    sig = _schnorr_sign(sk, event_id)
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


def sign_nip98(sk: bytes, method: str, url: str, body: bytes | None = None) -> str:
    tags = [["u", url], ["method", method.upper()]]
    if body is not None:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(sk, 27235, "", tags)
    token = base64.b64encode(
        json.dumps(event, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"Nostr {token}"


def http_to_events_url(relay_url: str) -> str:
    base = relay_url.rstrip("/")
    if base.startswith("ws://"):
        base = "http://" + base[len("ws://") :]
    elif base.startswith("wss://"):
        base = "https://" + base[len("wss://") :]
    return f"{base}/events"


def post_event(
    relay_url: str, sk: bytes, event: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    url = http_to_events_url(relay_url)
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    auth = sign_nip98(sk, "POST", url, body)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = int(getattr(resp, "status", 200))
            return {"ok": 200 <= code < 300, "code": code, "body": raw}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "code": int(exc.code), "body": raw, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "code": -1, "body": "", "error": str(exc)}


def build_and_submit_observer_frame(
    *,
    private_key: str,
    recipient_pubkey: str,
    frame: str,
    payload: dict[str, Any],
    relay_url: str,
    agent_pubkey: str | None = None,
) -> dict[str, Any]:
    if frame not in {"telemetry", "control"}:
        return {"ok": False, "error": "frame must be 'telemetry' or 'control'"}
    try:
        sk = normalize_sk(private_key)
        recipient = normalize_pubkey(recipient_pubkey)
        agent = normalize_pubkey(agent_pubkey) if agent_pubkey else pubkey_from_sk(sk)
        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        encrypted = nip44_encrypt(sk, recipient, plaintext)
        if not content_looks_like_nip44(encrypted):
            return {
                "ok": False,
                "error": f"encrypted payload length invalid: {len(encrypted)}",
            }
        tags = [
            ["p", recipient],
            [OBSERVER_AGENT_TAG, agent],
            [OBSERVER_FRAME_TAG, frame],
        ]
        event = sign_event(sk, KIND_OBSERVER, encrypted, tags)
        result = post_event(relay_url, sk, event)
        result["event_id"] = event["id"]
        result["kind"] = KIND_OBSERVER
        result["frame"] = frame
        result["agent_pubkey"] = agent
        result["recipient_pubkey"] = recipient
        return result
    except NostrCryptoError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"observer frame failed: {exc}"}
