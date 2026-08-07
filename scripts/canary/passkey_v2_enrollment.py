#!/usr/bin/env python3
"""Owner-controlled, phone-friendly passkey enrollment for team members.

Enrollment is intentionally separate from report authorization.  Root/the
authority creates one short-lived invitation for one exact Discord user.  The
browser can register one passkey, producing a create-only fsynced audit receipt
that can be imported into the existing passkey-v2 credential table.  Enrollment
never authorizes a report and report use never requires a new owner approval.
"""

from __future__ import annotations

import base64
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Mapping

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_webauthn as passkey_webauthn


INVITATION_SCHEMA = "muncho-passkey-v2-enrollment-invitation.v1"
RECEIPT_SCHEMA = "muncho-passkey-v2-enrollment-receipt.v1"
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 24 * 60 * 60
PRODUCTION_ENROLLMENT_ROOT = Path(
    "/var/lib/muncho-owner-gate/authority/enrollment"
)
_DISCORD_ID = re.compile(r"^[1-9][0-9]{16,21}$")
_INVITATION_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INVITATION_FIELDS = frozenset({
    "schema",
    "invitation_id",
    "owner_discord_user_id",
    "user_label",
    "user_handle_b64url",
    "token_sha256",
    "challenge_b64url",
    "rp_id",
    "origin",
    "issued_at_unix",
    "expires_at_unix",
    "invitation_sha256",
})
_RECEIPT_FIELDS = frozenset({
    "schema",
    "invitation_id",
    "invitation_sha256",
    "owner_discord_user_id",
    "credential_id_b64url",
    "public_key_cose_b64url",
    "sign_count",
    "credential_backed_up",
    "user_handle_b64url",
    "rp_id",
    "origin",
    "verified_at_unix",
    "receipt_sha256",
})


class PasskeyV2EnrollmentError(RuntimeError):
    """One stable, secret-free enrollment boundary failure."""


def _fail(code: str) -> None:
    raise PasskeyV2EnrollmentError(code)


def _effective_identity() -> tuple[int, int]:
    """Return the POSIX owner identity or fail closed on unsupported hosts."""
    get_euid = getattr(os, "geteuid", None)
    get_egid = getattr(os, "getegid", None)
    if not callable(get_euid) or not callable(get_egid):
        _fail("passkey_v2_enrollment_runtime_unsupported")
    return int(get_euid()), int(get_egid())


def _load_registration_verifier() -> tuple[Any, type[Exception]]:
    _require_selected_runtime()
    try:
        from webauthn import verify_registration_response
        from webauthn.helpers.exceptions import InvalidRegistrationResponse
    except (ImportError, ModuleNotFoundError):
        _fail("passkey_v2_enrollment_runtime_unavailable")
    return verify_registration_response, InvalidRegistrationResponse


def _require_selected_runtime() -> None:
    expected = {
        "webauthn": "3.0.0",
        "cbor2": "6.1.3",
        "cryptography": "49.0.0",
    }
    try:
        actual = {
            package: importlib.metadata.version(package)
            for package in expected
        }
    except importlib.metadata.PackageNotFoundError:
        _fail("passkey_v2_enrollment_runtime_unavailable")
    if actual != expected:
        _fail("passkey_v2_enrollment_runtime_mismatch")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_b64(value: Any, *, label: str, maximum: int = 4096) -> bytes:
    if not isinstance(value, str):
        _fail(f"passkey_v2_enrollment_{label}_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        _fail(f"passkey_v2_enrollment_{label}_invalid")
    if (
        not raw
        or len(raw) > maximum
        or _b64(raw) != value
    ):
        _fail(f"passkey_v2_enrollment_{label}_invalid")
    return raw


def _validate_invitation(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _INVITATION_FIELDS:
        _fail("passkey_v2_enrollment_invitation_invalid")
    item = dict(value)
    if (
        item.get("schema") != INVITATION_SCHEMA
        or not isinstance(item.get("invitation_id"), str)
        or _INVITATION_ID.fullmatch(item["invitation_id"]) is None
        or not isinstance(item.get("owner_discord_user_id"), str)
        or _DISCORD_ID.fullmatch(item["owner_discord_user_id"]) is None
        or not isinstance(item.get("user_label"), str)
        or not 1 <= len(item["user_label"]) <= 120
        or item["user_label"] != item["user_label"].strip()
        or item.get("rp_id") != protocol.PRODUCTION_RP_ID
        or item.get("origin") != protocol.PRODUCTION_ORIGIN
        or not isinstance(item.get("issued_at_unix"), int)
        or isinstance(item.get("issued_at_unix"), bool)
        or not isinstance(item.get("expires_at_unix"), int)
        or isinstance(item.get("expires_at_unix"), bool)
        or not MIN_TTL_SECONDS
        <= item["expires_at_unix"] - item["issued_at_unix"]
        <= MAX_TTL_SECONDS
        or not isinstance(item.get("token_sha256"), str)
        or _SHA256.fullmatch(item["token_sha256"]) is None
    ):
        _fail("passkey_v2_enrollment_invitation_invalid")
    _decode_b64(item.get("user_handle_b64url"), label="user_handle", maximum=64)
    _decode_b64(item.get("challenge_b64url"), label="challenge", maximum=64)
    expected = protocol.sha256_json({
        key: entry
        for key, entry in item.items()
        if key != "invitation_sha256"
    })
    if item.get("invitation_sha256") != expected:
        _fail("passkey_v2_enrollment_invitation_invalid")
    return item


def _write_create_only(path: Path, value: Mapping[str, Any]) -> None:
    effective_uid, effective_gid = _effective_identity()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent = path.parent.lstat()
    except OSError:
        _fail("passkey_v2_enrollment_state_unavailable")
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != effective_uid
        or parent.st_gid != effective_gid
    ):
        _fail("passkey_v2_enrollment_state_invalid")
    os.chmod(path.parent, 0o700, follow_symlinks=False)
    raw = protocol.canonical_json_bytes(value) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
    except FileExistsError:
        _fail("passkey_v2_enrollment_state_exists")
    try:
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count < 1:
                _fail("passkey_v2_enrollment_state_unavailable")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path: Path) -> Mapping[str, Any]:
    effective_uid, effective_gid = _effective_identity()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        _fail("passkey_v2_enrollment_state_unavailable")
    try:
        item = os.fstat(descriptor)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != effective_uid
            or item.st_gid != effective_gid
            or stat.S_IMODE(item.st_mode) != 0o400
            or item.st_size < 2
            or item.st_size > 64 * 1024
        ):
            _fail("passkey_v2_enrollment_state_invalid")
        chunks: list[bytes] = []
        remaining = item.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                _fail("passkey_v2_enrollment_state_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("passkey_v2_enrollment_state_invalid")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not raw.endswith(b"\n"):
        _fail("passkey_v2_enrollment_state_invalid")
    try:
        value = protocol.decode_canonical_json(raw[:-1])
    except protocol.PasskeyV2ProtocolError:
        _fail("passkey_v2_enrollment_state_invalid")
    if not isinstance(value, Mapping):
        _fail("passkey_v2_enrollment_state_invalid")
    return dict(value)


def create_invitation(
    *,
    root: Path,
    owner_discord_user_id: str,
    user_label: str,
    now_unix: int | None = None,
    ttl_seconds: int = 3600,
) -> tuple[Mapping[str, Any], bytes]:
    now = int(time.time()) if now_unix is None else now_unix
    if (
        not isinstance(owner_discord_user_id, str)
        or _DISCORD_ID.fullmatch(owner_discord_user_id) is None
        or not isinstance(user_label, str)
        or not 1 <= len(user_label) <= 120
        or user_label != user_label.strip()
        or type(now) is not int
        or now < 1
        or type(ttl_seconds) is not int
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        _fail("passkey_v2_enrollment_invitation_invalid")
    token = secrets.token_bytes(32)
    invitation_id = _b64(secrets.token_bytes(24))
    unsigned = {
        "schema": INVITATION_SCHEMA,
        "invitation_id": invitation_id,
        "owner_discord_user_id": owner_discord_user_id,
        "user_label": user_label,
        "user_handle_b64url": _b64(secrets.token_bytes(32)),
        "token_sha256": hashlib.sha256(token).hexdigest(),
        "challenge_b64url": _b64(secrets.token_bytes(32)),
        "rp_id": protocol.PRODUCTION_RP_ID,
        "origin": protocol.PRODUCTION_ORIGIN,
        "issued_at_unix": now,
        "expires_at_unix": now + ttl_seconds,
    }
    invitation = _validate_invitation({
        **unsigned,
        "invitation_sha256": protocol.sha256_json(unsigned),
    })
    _write_create_only(root / "invitations" / f"{invitation_id}.json", invitation)
    return invitation, token


def _authorized_invitation(
    *,
    root: Path,
    invitation_id: str,
    token: bytes,
    now_unix: int,
) -> Mapping[str, Any]:
    if (
        not isinstance(invitation_id, str)
        or _INVITATION_ID.fullmatch(invitation_id) is None
        or not isinstance(token, bytes)
        or len(token) != 32
        or type(now_unix) is not int
    ):
        _fail("passkey_v2_enrollment_invitation_invalid")
    invitation = _validate_invitation(
        _read(root / "invitations" / f"{invitation_id}.json")
    )
    if (
        hashlib.sha256(token).hexdigest() != invitation["token_sha256"]
        or not invitation["issued_at_unix"] <= now_unix
        < invitation["expires_at_unix"]
    ):
        _fail("passkey_v2_enrollment_invitation_denied")
    return invitation


def registration_options(
    *,
    root: Path,
    invitation_id: str,
    token: bytes,
    now_unix: int,
) -> Mapping[str, Any]:
    _require_selected_runtime()
    invitation = _authorized_invitation(
        root=root,
        invitation_id=invitation_id,
        token=token,
        now_unix=now_unix,
    )
    try:
        from webauthn import generate_registration_options
        from webauthn.helpers import options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
        options = generate_registration_options(
            rp_id=protocol.PRODUCTION_RP_ID,
            rp_name="Muncho trusted team",
            user_name=invitation["user_label"],
            user_display_name=invitation["user_label"],
            user_id=_decode_b64(
                invitation["user_handle_b64url"], label="user_handle", maximum=64
            ),
            challenge=_decode_b64(
                invitation["challenge_b64url"], label="challenge", maximum=64
            ),
            timeout=300_000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        value = json.loads(options_to_json(options))
    except (ImportError, ModuleNotFoundError, ValueError, TypeError):
        _fail("passkey_v2_enrollment_runtime_unavailable")
    if not isinstance(value, Mapping):
        _fail("passkey_v2_enrollment_options_invalid")
    return {
        "schema": "muncho-passkey-v2-enrollment-options.v1",
        "invitation_id": invitation_id,
        "publicKey": dict(value),
    }


def _validate_receipt(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
        _fail("passkey_v2_enrollment_receipt_invalid")
    receipt = dict(value)
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or not isinstance(receipt.get("invitation_id"), str)
        or _INVITATION_ID.fullmatch(receipt["invitation_id"]) is None
        or not isinstance(receipt.get("owner_discord_user_id"), str)
        or _DISCORD_ID.fullmatch(receipt["owner_discord_user_id"]) is None
        or receipt.get("rp_id") != protocol.PRODUCTION_RP_ID
        or receipt.get("origin") != protocol.PRODUCTION_ORIGIN
        or type(receipt.get("sign_count")) is not int
        or receipt["sign_count"] < 0
        or not isinstance(receipt.get("credential_backed_up"), bool)
        or type(receipt.get("verified_at_unix")) is not int
        or receipt["verified_at_unix"] < 1
    ):
        _fail("passkey_v2_enrollment_receipt_invalid")
    for name in ("invitation_sha256", "receipt_sha256"):
        if (
            not isinstance(receipt.get(name), str)
            or _SHA256.fullmatch(receipt[name]) is None
        ):
            _fail("passkey_v2_enrollment_receipt_invalid")
    _decode_b64(receipt.get("credential_id_b64url"), label="credential_id")
    _decode_b64(receipt.get("public_key_cose_b64url"), label="public_key")
    _decode_b64(receipt.get("user_handle_b64url"), label="user_handle", maximum=64)
    expected = protocol.sha256_json({
        key: entry
        for key, entry in receipt.items()
        if key != "receipt_sha256"
    })
    if receipt["receipt_sha256"] != expected:
        _fail("passkey_v2_enrollment_receipt_invalid")
    return receipt


def credential_from_receipt(value: Any) -> Mapping[str, Any]:
    receipt = _validate_receipt(value)
    return passkey_webauthn.build_migrated_credential(
        owner_discord_user_id=receipt["owner_discord_user_id"],
        credential_id=_decode_b64(
            receipt["credential_id_b64url"], label="credential_id"
        ),
        public_key_cose=_decode_b64(
            receipt["public_key_cose_b64url"], label="public_key"
        ),
        rp_id=receipt["rp_id"],
        origin=receipt["origin"],
        imported_at_unix=receipt["verified_at_unix"],
        migration_receipt_sha256=receipt["receipt_sha256"],
        initial_sign_count=receipt["sign_count"],
        initial_credential_backed_up=receipt["credential_backed_up"],
        expected_user_handle=_decode_b64(
            receipt["user_handle_b64url"], label="user_handle", maximum=64
        ),
    )


def complete_enrollment(
    *,
    root: Path,
    invitation_id: str,
    token: bytes,
    credential: Mapping[str, Any],
    now_unix: int,
) -> Mapping[str, Any]:
    invitation = _authorized_invitation(
        root=root,
        invitation_id=invitation_id,
        token=token,
        now_unix=now_unix,
    )
    receipt_path = root / "receipts" / f"{invitation_id}.json"
    if receipt_path.exists():
        receipt = _validate_receipt(_read(receipt_path))
        if receipt["invitation_sha256"] != invitation["invitation_sha256"]:
            _fail("passkey_v2_enrollment_receipt_invalid")
        return credential_from_receipt(receipt)
    verifier, invalid_response = _load_registration_verifier()
    try:
        verified = verifier(
            credential=dict(credential),
            expected_challenge=_decode_b64(
                invitation["challenge_b64url"], label="challenge", maximum=64
            ),
            expected_rp_id=protocol.PRODUCTION_RP_ID,
            expected_origin=protocol.PRODUCTION_ORIGIN,
            require_user_presence=True,
            require_user_verification=True,
        )
    except invalid_response:
        _fail("passkey_v2_enrollment_cryptographic_verification_failed")
    unsigned = {
        "schema": RECEIPT_SCHEMA,
        "invitation_id": invitation_id,
        "invitation_sha256": invitation["invitation_sha256"],
        "owner_discord_user_id": invitation["owner_discord_user_id"],
        "credential_id_b64url": _b64(bytes(verified.credential_id)),
        "public_key_cose_b64url": _b64(bytes(verified.credential_public_key)),
        "sign_count": int(verified.sign_count),
        "credential_backed_up": bool(verified.credential_backed_up),
        "user_handle_b64url": invitation["user_handle_b64url"],
        "rp_id": protocol.PRODUCTION_RP_ID,
        "origin": protocol.PRODUCTION_ORIGIN,
        "verified_at_unix": now_unix,
    }
    receipt = _validate_receipt({
        **unsigned,
        "receipt_sha256": protocol.sha256_json(unsigned),
    })
    _write_create_only(receipt_path, receipt)
    return credential_from_receipt(receipt)


def main(
    argv: list[str] | None = None,
    *,
    root: Path = PRODUCTION_ENROLLMENT_ROOT,
) -> int:
    """Create one local-only, single-use iPhone enrollment invitation."""

    parser = argparse.ArgumentParser(prog="muncho-passkey-enrollment")
    parser.add_argument("create", choices=("create",))
    parser.add_argument("--owner-discord-user-id", required=True)
    parser.add_argument("--user-label", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    invitation, token = create_invitation(
        root=root,
        owner_discord_user_id=args.owner_discord_user_id,
        user_label=args.user_label,
        ttl_seconds=args.ttl_seconds,
    )
    # The fragment is consumed locally by the phone browser and removed from
    # history before any request. Never post this output to Discord.
    print(
        f"{protocol.PRODUCTION_ORIGIN}/enroll/"
        f"{invitation['invitation_id']}#{_b64(token)}"
    )
    return 0


__all__ = [
    "INVITATION_SCHEMA",
    "PasskeyV2EnrollmentError",
    "RECEIPT_SCHEMA",
    "complete_enrollment",
    "create_invitation",
    "credential_from_receipt",
    "registration_options",
]


if __name__ == "__main__":
    raise SystemExit(main())
