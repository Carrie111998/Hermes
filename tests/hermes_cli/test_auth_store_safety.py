"""Regression tests for auth-store path and transient-read safety."""

from pathlib import Path

import pytest


def test_auth_file_seat_belt_uses_platform_default_root(tmp_path, monkeypatch):
    """Pytest must reject the real Windows-native auth path, not only ~/.hermes."""
    from hermes_cli import auth
    import hermes_constants

    default_root = tmp_path / "AppData" / "Local" / "hermes"
    fake_user_home = tmp_path / "Users" / "Luke"
    default_root.mkdir(parents=True)
    fake_user_home.mkdir(parents=True)

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "auth seat belt")
    monkeypatch.setattr(auth, "get_hermes_home", lambda: default_root)
    monkeypatch.setattr(
        hermes_constants,
        "_get_platform_default_hermes_home",
        lambda: default_root,
    )
    monkeypatch.setattr(Path, "home", lambda: fake_user_home)

    with pytest.raises(RuntimeError, match="real user auth store"):
        auth._auth_file_path()


def test_auth_file_seat_belt_fails_closed_when_target_resolution_fails(
    tmp_path, monkeypatch
):
    """An unresolved alias must not bypass the active-store write guard."""
    from hermes_cli import auth
    import hermes_constants

    default_root = tmp_path / "AppData" / "Local" / "hermes"
    aliased_root = default_root.parent / "alias" / ".." / "hermes"
    target = aliased_root / "auth.json"
    real_resolve = Path.resolve

    def deny_target(self, *args, **kwargs):
        if self == target:
            raise PermissionError("cannot canonicalize active auth path")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "active auth resolve seat belt")
    monkeypatch.setattr(auth, "get_hermes_home", lambda: aliased_root)
    monkeypatch.setattr(
        hermes_constants,
        "_get_platform_default_hermes_home",
        lambda: default_root,
    )
    monkeypatch.setattr(Path, "resolve", deny_target)

    with pytest.raises(RuntimeError, match="real user auth store"):
        auth._auth_file_path()


def test_global_auth_fallback_does_not_read_platform_store_under_pytest(
    tmp_path, monkeypatch
):
    """Profile tests must not import credentials from the Windows-native root."""
    from hermes_cli import auth
    import hermes_constants

    default_root = tmp_path / "AppData" / "Local" / "hermes"
    default_root.mkdir(parents=True)
    global_auth = default_root / "auth.json"
    global_auth.write_text(
        '{"version": 1, "providers": {"openai-codex": {"access_token": "fake"}}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "global auth seat belt")
    monkeypatch.setenv("HOME", str(tmp_path / "Users" / "Luke"))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_auth)
    monkeypatch.setattr(
        hermes_constants,
        "_get_platform_default_hermes_home",
        lambda: default_root,
    )

    assert auth._load_global_auth_store() == {}


def test_global_auth_fallback_fails_closed_when_path_resolution_fails(
    tmp_path, monkeypatch
):
    """A canonicalization failure must not fall through to credential loading."""
    from hermes_cli import auth

    global_auth = tmp_path / "auth.json"
    global_auth.write_text('{"version": 1, "providers": {}}', encoding="utf-8")
    real_resolve = Path.resolve

    def deny_target(self, *args, **kwargs):
        if self == global_auth:
            raise PermissionError("cannot canonicalize auth path")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "global auth resolve seat belt")
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_auth)
    monkeypatch.setattr(auth, "_platform_default_auth_file_path", lambda: global_auth)
    monkeypatch.setattr(Path, "resolve", deny_target)
    monkeypatch.setattr(
        auth,
        "_load_auth_store",
        lambda *_args, **_kwargs: pytest.fail("real global auth store was read"),
    )

    assert auth._load_global_auth_store() == {}


def test_xai_write_through_refuses_platform_store_under_pytest(tmp_path, monkeypatch):
    """Profile refresh tests must not write xAI tokens into the native root."""
    from hermes_cli import auth

    global_auth = tmp_path / "AppData" / "Local" / "hermes" / "auth.json"
    global_auth.parent.mkdir(parents=True)
    persist_calls = []

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "xai global write seat belt")
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_auth)
    monkeypatch.setattr(auth, "_platform_default_auth_file_path", lambda: global_auth)
    monkeypatch.setattr(
        auth,
        "_persist_provider_state_to_store",
        lambda *args, **kwargs: persist_calls.append((args, kwargs)),
    )

    auth._write_through_xai_oauth_to_global_root({"tokens": {"access_token": "fake"}})

    assert persist_calls == []


def test_auth_store_permission_error_is_not_treated_as_corruption(tmp_path, monkeypatch):
    """A transient read denial must fail closed without creating an empty store."""
    from hermes_cli import auth

    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"version": 1, "providers": {}}', encoding="utf-8")
    real_read_text = Path.read_text

    def deny_target(self, *args, **kwargs):
        if self == auth_file:
            raise PermissionError("temporarily locked")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_target)

    with pytest.raises(PermissionError, match="temporarily locked"):
        auth._load_auth_store(auth_file)

    assert not auth_file.with_suffix(".json.corrupt").exists()


def test_auth_store_invalid_utf8_is_quarantined_on_legacy_locale(tmp_path, monkeypatch):
    """Auth JSON is always UTF-8, even when the platform default is cp1252."""
    from hermes_cli import auth

    auth_file = tmp_path / "auth.json"
    malformed = b'{"version": 1, "providers": {"example": "\x80"}}'
    auth_file.write_bytes(malformed)
    real_read_text = Path.read_text

    def emulate_legacy_default(self, *args, **kwargs):
        if kwargs.get("encoding") is None:
            kwargs["encoding"] = "cp1252"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", emulate_legacy_default)

    store = auth._load_auth_store(auth_file)

    assert store == {"version": auth.AUTH_STORE_VERSION, "providers": {}}
    assert auth_file.with_suffix(".json.corrupt").read_bytes() == malformed


def test_auth_store_malformed_json_is_preserved_and_reset(tmp_path):
    """Actual malformed JSON keeps the existing quarantine-and-reset behavior."""
    from hermes_cli import auth

    auth_file = tmp_path / "auth.json"
    malformed = "{ not-json"
    auth_file.write_text(malformed, encoding="utf-8")

    store = auth._load_auth_store(auth_file)

    assert store == {"version": auth.AUTH_STORE_VERSION, "providers": {}}
    assert auth_file.with_suffix(".json.corrupt").read_text(encoding="utf-8") == malformed
