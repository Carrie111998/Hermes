"""Encryption boundary for per-tenant integration credentials."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: dict) -> str:
        if not self._fernet:
            raise RuntimeError("INTERFAZE_CREDENTIAL_KEY is required to store integration credentials")
        return self._fernet.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

    def decrypt(self, value: str | None) -> dict:
        if not value:
            return {}
        if not self._fernet:
            raise RuntimeError("Credential encryption key is not configured")
        try:
            return json.loads(self._fernet.decrypt(value.encode()).decode())
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Stored integration credentials cannot be decrypted") from exc

