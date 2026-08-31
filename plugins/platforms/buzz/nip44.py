"""NIP-44 v2 encryption for Buzz observer frames.

Ports the reference algorithm from nostr-rs (nostr 0.44.7, nips/nip44/v2.rs)
exactly: secp256k1 ECDH (x-coordinate), HKDF-SHA256 extract with salt
``nip44-v2``, HKDF expand to 76-byte message keys, ChaCha20 stream cipher,
HMAC-SHA256 over nonce+ciphertext, versioned payload ``0x02 || nonce(32) ||
padded || hmac(32)``, base64.

Only what Buzz observer frames need: encrypt. Decryption lives in the
Desktop app (owner side).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from typing import Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# secp256k1 curve parameters (P = 2**256 - 2**32 - 977)
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _point_add(p1: Tuple[int, int], p2: Tuple[int, int]) -> Tuple[int, int]:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and (p1[1] + p2[1]) % _P == 0:
        return None  # point at infinity
    if p1 == p2:
        lam = (3 * p1[0] * p1[0]) * pow(2 * p1[1], _P - 2, _P) % _P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], _P - 2, _P) % _P
    x = (lam * lam - p1[0] - p2[0]) % _P
    y = (lam * (p1[0] - x) - p1[1]) % _P
    return (x, y)


def _point_mul(k: int, point: Tuple[int, int] = _G) -> Tuple[int, int]:
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    t = b""
    i = 1
    while len(output) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        inbound = t
        output += inbound
        i += 1
    return output[:length]


def _calc_padding(length: int) -> int:
    if length <= 32:
        return 32
    next_power = 1 << ((length - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (((length - 1) // chunk) + 1)


def nip44_encrypt(private_key_hex: str, recipient_pubkey_hex: str, plaintext: str) -> str:
    """NIP-44 v2 encrypt; returns base64 ciphertext."""
    priv = int(private_key_hex, 16)
    recipient = (int(recipient_pubkey_hex, 16), None)
    # Lift x-only pubkey to even-y point (standard NIP-44 practice)
    y2 = (pow(recipient[0], 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y % 2:
        y = _P - y
    recipient_point = (int(recipient_pubkey_hex, 16), y)

    # ECDH: x-coordinate of priv * recipient_point
    shared_point = _point_mul(priv, recipient_point)
    shared_x = shared_point[0].to_bytes(32, "big")

    conversation_key = _hkdf_extract(b"nip44-v2", shared_x)

    nonce = os.urandom(32)
    keys = _hkdf_expand(conversation_key, nonce, 76)
    enc_key = keys[0:32]
    cha_nonce = keys[32:44]
    auth_key = keys[44:76]

    padded_len = _calc_padding(len(plaintext_bytes := plaintext.encode()))
    buffer = struct.pack(">H", len(plaintext_bytes)) + plaintext_bytes
    buffer += b"\x00" * (padded_len - len(plaintext_bytes))

    cipher = Cipher(
        algorithms.ChaCha20(enc_key, b"\x00\x00\x00\x00" + cha_nonce), mode=None
    ).encryptor()
    ciphertext = cipher.update(buffer)

    mac = hmac.new(auth_key, nonce + ciphertext, hashlib.sha256).digest()
    payload = bytes([2]) + nonce + ciphertext + mac
    return base64.b64encode(payload).decode()


def nip44_decrypt(private_key_hex: str, sender_pubkey_hex: str, payload_b64: str) -> str:
    """NIP-44 v2 decrypt (for tests / round-trip verification)."""
    payload = base64.b64decode(payload_b64)
    assert payload[0] == 2, "only NIP-44 v2 supported"
    nonce = payload[1:33]
    ciphertext = payload[33:-32]
    mac = payload[-32:]

    priv = int(private_key_hex, 16)
    sender_x = int(sender_pubkey_hex, 16)
    y2 = (pow(sender_x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y % 2:
        y = _P - y
    shared_point = _point_mul(priv, (sender_x, y))
    shared_x = shared_point[0].to_bytes(32, "big")
    conversation_key = _hkdf_extract(b"nip44-v2", shared_x)

    keys = _hkdf_expand(conversation_key, nonce, 76)
    cipher = Cipher(
        algorithms.ChaCha20(keys[0:32], b"\x00\x00\x00\x00" + keys[32:44]), mode=None
    ).decryptor()
    buffer = cipher.update(ciphertext)
    calc_mac = hmac.new(keys[44:76], nonce + ciphertext, hashlib.sha256).digest()
    assert hmac.compare_digest(calc_mac, mac), "HMAC mismatch"
    unpadded_len = struct.unpack(">H", buffer[:2])[0]
    plaintext = buffer[2 : 2 + unpadded_len]
    return plaintext.decode()
