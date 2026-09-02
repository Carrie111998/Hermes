from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager

from hermes_cli import auth


def _jwt_with_exp(exp: int) -> str:
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode())
        .decode()
        .rstrip("=")
    )
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


def test_xai_oauth_refresh_skew_is_one_hour() -> None:
    assert auth.XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS == 3600


def test_xai_oauth_token_expiring_uses_one_hour_skew() -> None:
    token = _jwt_with_exp(int(time.time()) + 30 * 60)

    assert auth._xai_access_token_is_expiring(
        token,
        auth.XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    )


def test_xai_proactive_refresh_skew_short_lived_token() -> None:
    token = _jwt_with_exp(int(time.time()) + 15 * 60)
    skew = auth._xai_proactive_refresh_skew_seconds(token)

    assert skew == 120
    assert not auth._xai_access_token_is_expiring(token, skew)


def test_xai_token_read_waits_for_full_refresh_lock_budget(monkeypatch) -> None:
    """A reader must outwait a valid in-progress single-use-token refresh."""
    seen = {}

    @contextmanager
    def _capture_lock(timeout_seconds=auth.AUTH_LOCK_TIMEOUT_SECONDS, **_kwargs):
        seen["timeout_seconds"] = timeout_seconds
        yield

    monkeypatch.setenv("HERMES_XAI_REFRESH_TIMEOUT_SECONDS", "40")
    monkeypatch.setattr(auth, "_auth_store_lock", _capture_lock)
    monkeypatch.setattr(
        auth,
        "_load_auth_store",
        lambda *_args, **_kwargs: {
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": "access",
                        "refresh_token": "refresh",
                    }
                }
            }
        },
    )

    data = auth._read_xai_oauth_tokens()

    assert data["tokens"]["access_token"] == "access"
    assert seen["timeout_seconds"] >= 45


