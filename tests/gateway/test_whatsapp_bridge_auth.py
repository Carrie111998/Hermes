"""Authentication contracts for the local WhatsApp bridge."""

import stat


def test_bridge_token_is_private_and_stable(tmp_path):
    from plugins.platforms.whatsapp.adapter import _load_or_create_bridge_token

    first = _load_or_create_bridge_token(tmp_path)
    second = _load_or_create_bridge_token(tmp_path)
    token_file = tmp_path / ".bridge-token"

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_standalone_bridge_auth_headers_load_private_token(tmp_path):
    from plugins.platforms.whatsapp.adapter import _bridge_auth_headers

    token = "a" * 32
    (tmp_path / ".bridge-token").write_text(token, encoding="utf-8")

    assert _bridge_auth_headers(tmp_path) == {"Authorization": f"Bearer {token}"}
    assert _bridge_auth_headers(tmp_path / "missing") == {}
