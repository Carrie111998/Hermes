"""Regression tests for Codex OAuth store isolation.

Hermes runtime credentials are scoped to the Hermes auth store they came from:
a profile-local store or the global fallback store. Codex CLI credentials are
only adopted by the explicit interactive import/login flow; runtime resolution
must never copy ``~/.codex/auth.json`` into a Hermes profile because OAuth
refresh tokens are single-use.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import AuthError, _refresh_codex_auth_tokens, resolve_codex_runtime_credentials

STALE = {"access_token": "stale-access", "refresh_token": "stale-refresh"}


def _store(tokens):
    return {
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": dict(tokens),
                "last_refresh": "2026-06-01T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        },
    }


def test_rejected_refresh_does_not_adopt_codex_cli_tokens(monkeypatch):
    """Runtime failures require explicit Hermes re-auth, never a CLI import."""
    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(
        auth,
        "_import_codex_cli_tokens",
        lambda: pytest.fail("runtime refresh must not import Codex CLI credentials"),
    )

    with pytest.raises(AuthError, match="refresh token rejected"):
        _refresh_codex_auth_tokens(STALE, 20.0)


def test_missing_profile_token_does_not_adopt_codex_cli_tokens(tmp_path, monkeypatch):
    """A malformed profile store stays malformed until explicit auth/import."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps(_store({"refresh_token": "stale-refresh"})))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": "cli-access", "refresh_token": "cli-refresh"},
    }))
    before = (hermes_home / "auth.json").read_text()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials()

    assert exc.value.code == "codex_auth_missing_access_token"
    assert (hermes_home / "auth.json").read_text() == before


def test_refresh_keeps_profile_local_source_store(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "darla"
    global_home = tmp_path / "global"
    profile_home.mkdir(parents=True)
    global_home.mkdir()
    (profile_home / "auth.json").write_text(json.dumps(_store(STALE)))
    (global_home / "auth.json").write_text(json.dumps(_store({
        "access_token": "global-access", "refresh_token": "global-refresh",
    })))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_home / "auth.json")
    monkeypatch.setattr(auth, "_load_global_auth_store", lambda: json.loads((global_home / "auth.json").read_text()))
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *_a, **_k: {
        "access_token": "profile-fresh", "refresh_token": "profile-rotated",
    })

    resolved = resolve_codex_runtime_credentials(force_refresh=True)

    assert resolved["api_key"] == "profile-fresh"
    assert json.loads((profile_home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"]["refresh_token"] == "profile-rotated"
    assert json.loads((global_home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"]["refresh_token"] == "global-refresh"


def test_global_fallback_refreshes_global_source_store(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "darla"
    global_home = tmp_path / "global"
    profile_home.mkdir(parents=True)
    global_home.mkdir()
    (profile_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    (global_home / "auth.json").write_text(json.dumps(_store(STALE)))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: global_home / "auth.json")
    monkeypatch.setattr(auth, "_load_global_auth_store", lambda: json.loads((global_home / "auth.json").read_text()))
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *_a, **_k: {
        "access_token": "global-fresh", "refresh_token": "global-rotated",
    })

    resolved = resolve_codex_runtime_credentials(force_refresh=True)

    assert resolved["api_key"] == "global-fresh"
    assert "openai-codex" not in json.loads((profile_home / "auth.json").read_text())["providers"]
    assert json.loads((global_home / "auth.json").read_text())["providers"]["openai-codex"]["tokens"]["refresh_token"] == "global-rotated"


def test_concurrent_global_fallback_reuses_rotated_source_token(tmp_path, monkeypatch):
    """A second reader must observe the first writer's rotation, not reuse it."""
    global_auth = tmp_path / "auth.json"
    global_auth.write_text(json.dumps(_store(STALE)))
    calls = []

    def _refresh(*_a, **_k):
        calls.append(1)
        return {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _refresh)

    first = _refresh_codex_auth_tokens(STALE, 20.0, source_path=global_auth)
    second = _refresh_codex_auth_tokens(STALE, 20.0, source_path=global_auth)

    assert first["refresh_token"] == "rotated-refresh"
    assert second["refresh_token"] == "rotated-refresh"
    assert len(calls) == 1
