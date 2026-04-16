from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TELEGRAM_PRODUCTION_WEBAPP_PUBLIC_KEY_HEX = (
    "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
)
MAX_INIT_DATA_AGE_SECONDS = 300


class MiniAppAuthError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MiniAppIdentity:
    user_id: str
    first_name: str | None
    username: str | None
    raw_init_data: str
    is_telegram: bool = True


def build_data_check_string(*, bot_id: str, fields: dict[str, str]) -> str:
    rows = [
        f"{key}={value}"
        for key, value in sorted(fields.items())
        if key not in {"hash", "signature"}
    ]
    return f"{bot_id}:WebAppData\n" + "\n".join(rows)


def verify_telegram_init_data_signature(init_data: str, *, bot_id: str) -> bool:
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    signature = fields.get("signature", "").strip()
    if not signature:
        return False

    padded = signature + "=" * (-len(signature) % 4)
    try:
        signature_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(TELEGRAM_PRODUCTION_WEBAPP_PUBLIC_KEY_HEX)
        )
        public_key.verify(
            signature_bytes,
            build_data_check_string(bot_id=bot_id, fields=fields).encode("utf-8"),
        )
    except (ValueError, InvalidSignature):
        return False
    return True


def _allowed_users() -> set[str]:
    values = {
        part.strip()
        for part in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
        if part.strip()
    }
    values.update(
        part.strip()
        for part in os.getenv("GATEWAY_ALLOWED_USERS", "").split(",")
        if part.strip()
    )
    owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    if owner:
        values.add(owner)
    return values


def _allow_all_enabled() -> bool:
    return (
        os.getenv("TELEGRAM_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")
        or os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")
    )


def _parse_auth_date(fields: dict[str, str]) -> int:
    raw_auth_date = fields.get("auth_date", "").strip()
    try:
        return int(raw_auth_date)
    except (TypeError, ValueError) as exc:
        raise MiniAppAuthError("telegram_auth_date_invalid", "Telegram auth_date is invalid") from exc


def _parse_user_payload(fields: dict[str, str]) -> dict[str, object]:
    raw_user = fields.get("user", "").strip()
    if not raw_user:
        raise MiniAppAuthError("telegram_user_payload_invalid", "Telegram user payload is invalid")
    try:
        parsed_user = json.loads(raw_user)
        if not isinstance(parsed_user, dict):
            raise TypeError("Telegram user payload must be an object")
        return parsed_user
    except (TypeError, ValueError, KeyError) as exc:
        raise MiniAppAuthError("telegram_user_payload_invalid", "Telegram user payload is invalid") from exc


def _parse_user_id(user: dict[str, object]) -> str:
    try:
        return str(user["id"])
    except KeyError as exc:
        raise MiniAppAuthError("telegram_user_payload_invalid", "Telegram user payload is invalid") from exc


def validate_telegram_init_data(
    init_data: str,
    *,
    now: int | None = None,
    max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS,
) -> MiniAppIdentity:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or ":" not in token:
        raise MiniAppAuthError("telegram_bot_token_missing", "TELEGRAM_BOT_TOKEN is required")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    if not verify_telegram_init_data_signature(init_data, bot_id=token.split(":", 1)[0]):
        raise MiniAppAuthError("telegram_signature_invalid", "Telegram signature verification failed")

    auth_date = _parse_auth_date(fields)
    current_time = int(now) if now is not None else int(time.time())
    if current_time - auth_date > max_age_seconds:
        raise MiniAppAuthError("telegram_init_data_expired", "Telegram initData expired")

    user = _parse_user_payload(fields)
    user_id = _parse_user_id(user)
    if _allow_all_enabled():
        return MiniAppIdentity(
            user_id=user_id,
            first_name=user.get("first_name"),
            username=user.get("username"),
            raw_init_data=init_data,
        )
    allowed = _allowed_users()
    if allowed and "*" not in allowed and user_id not in allowed:
        raise MiniAppAuthError("telegram_user_not_allowed", "Telegram user not allowed")

    return MiniAppIdentity(
        user_id=user_id,
        first_name=user.get("first_name"),
        username=user.get("username"),
        raw_init_data=init_data,
    )
