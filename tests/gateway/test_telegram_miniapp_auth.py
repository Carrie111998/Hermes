import json
from unittest.mock import patch
from unittest.mock import Mock

import pytest

from gateway.platforms.telegram_miniapp_auth import (
    MiniAppAuthError,
    build_data_check_string,
    TELEGRAM_PRODUCTION_WEBAPP_PUBLIC_KEY_HEX,
    verify_telegram_init_data_signature,
    validate_telegram_init_data,
)


def _init_data(user_id: int = 42, auth_date: int | str = 1_700_000_000, signature: str = "test-signature") -> str:
    user = json.dumps({"id": user_id, "first_name": "Diego"}, separators=(",", ":"))
    return (
        f"auth_date={auth_date}"
        f"&query_id=AAEAAAE"
        f"&user={user}"
        f"&signature={signature}"
    )


def test_build_data_check_string_includes_bot_id_prefix():
    data = build_data_check_string(
        bot_id="123456",
        fields={
            "auth_date": "1700000000",
            "hash": "should-not-appear",
            "query_id": "AAEAAAE",
            "signature": "should-also-not-appear",
            "user": "{\"id\":42}",
        },
    )
    assert data == (
        "123456:WebAppData\n"
        "auth_date=1700000000\n"
        "query_id=AAEAAAE\n"
        "user={\"id\":42}"
    )
    assert "hash=" not in data
    assert "signature=" not in data


def test_validate_telegram_init_data_accepts_allowed_owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "99")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        ident = validate_telegram_init_data(_init_data(), now=1_700_000_120)
    assert ident.user_id == "42"
    assert ident.first_name == "Diego"
    assert ident.is_telegram is True


def test_validate_telegram_init_data_accepts_global_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "777")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        ident = validate_telegram_init_data(_init_data(user_id=777), now=1_700_000_120)
    assert ident.user_id == "777"


def test_validate_telegram_init_data_accepts_wildcard_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "*")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        ident = validate_telegram_init_data(_init_data(user_id=888), now=1_700_000_120)
    assert ident.user_id == "888"


def test_validate_telegram_init_data_allows_telegram_allow_all_before_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        ident = validate_telegram_init_data(_init_data(user_id=777), now=1_700_000_120)
    assert ident.user_id == "777"


def test_validate_telegram_init_data_allows_gateway_allow_all_before_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        ident = validate_telegram_init_data(_init_data(user_id=777), now=1_700_000_120)
    assert ident.user_id == "777"


def test_validate_telegram_init_data_rejects_expired_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(_init_data(auth_date=1_700_000_000), now=1_700_001_000)
    assert exc.value.code == "telegram_init_data_expired"


def test_validate_telegram_init_data_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=False,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(_init_data(), now=1_700_000_120)
    assert exc.value.code == "telegram_signature_invalid"


def test_validate_telegram_init_data_rejects_malformed_auth_date(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(_init_data(auth_date="not-a-number"), now=1_700_000_120)
    assert exc.value.code == "telegram_auth_date_invalid"


def test_validate_telegram_init_data_rejects_malformed_user_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(
                "auth_date=1700000000&query_id=AAEAAAE&user=not-json&signature=test-signature",
                now=1_700_000_120,
            )
    assert exc.value.code == "telegram_user_payload_invalid"


def test_validate_telegram_init_data_rejects_user_payload_missing_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(
                "auth_date=1700000000&query_id=AAEAAAE&user={\"first_name\":\"Diego\"}&signature=test-signature",
                now=1_700_000_120,
            )
    assert exc.value.code == "telegram_user_payload_invalid"


def test_validate_telegram_init_data_rejects_user_outside_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    with patch(
        "gateway.platforms.telegram_miniapp_auth.verify_telegram_init_data_signature",
        return_value=True,
    ):
        with pytest.raises(MiniAppAuthError) as exc:
            validate_telegram_init_data(_init_data(user_id=777), now=1_700_000_120)
    assert exc.value.code == "telegram_user_not_allowed"


def test_verify_telegram_init_data_signature_decodes_and_verifies():
    public_key = Mock()
    with patch(
        "gateway.platforms.telegram_miniapp_auth.Ed25519PublicKey.from_public_bytes",
        return_value=public_key,
    ) as from_public_bytes:
        result = verify_telegram_init_data_signature(
            _init_data(signature="AQID"),
            bot_id="123456",
        )

    assert result is True
    from_public_bytes.assert_called_once_with(bytes.fromhex(TELEGRAM_PRODUCTION_WEBAPP_PUBLIC_KEY_HEX))
    public_key.verify.assert_called_once_with(
        b"\x01\x02\x03",
        (
            "123456:WebAppData\n"
            "auth_date=1700000000\n"
            "query_id=AAEAAAE\n"
            "user={\"id\":42,\"first_name\":\"Diego\"}"
        ).encode("utf-8"),
    )
