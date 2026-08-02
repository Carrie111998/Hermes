"""Tests for the resolve_nous_access_token startup-burst memo (PR #66016).

The memo collapses the startup burst of managed-tool check_fn calls into a
single expensive resolution: within the short TTL, repeat calls return the
cached token without re-entering _provider_state_transaction (two
cross-process file locks + state reads) or triggering a network refresh.
"""

import json
import time

import pytest

import hermes_cli.auth as auth
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "isolated-shared"))
    monkeypatch.delenv("HERMES_PORTAL_BASE_URL", raising=False)
    monkeypatch.delenv("NOUS_PORTAL_BASE_URL", raising=False)
    monkeypatch.setattr(auth, "_RESOLVE_TOKEN_CACHE", None)
    yield


def _write_valid_auth_file(tmp_path, token="memo-token"):
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "nous",
                "providers": {
                    "nous": {
                        "access_token": token,
                        "refresh_token": "r",
                        "client_id": "hermes-cli-vps",
                        "expires_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600)
                        ),
                    }
                },
            }
        )
    )


def _count_transactions(monkeypatch):
    calls = {"n": 0}
    real = auth._provider_state_transaction

    def _counting(provider):
        calls["n"] += 1
        return real(provider)

    monkeypatch.setattr(auth, "_provider_state_transaction", _counting)
    return calls


def test_repeat_calls_within_ttl_hit_memo(monkeypatch, tmp_path):
    _write_valid_auth_file(tmp_path)
    calls = _count_transactions(monkeypatch)

    first = auth.resolve_nous_access_token()
    second = auth.resolve_nous_access_token()
    third = auth.resolve_nous_access_token()

    assert first == second == third == "memo-token"
    assert calls["n"] == 1, (
        "repeat calls within the TTL must not re-enter the state transaction"
    )


def test_memo_expires_after_ttl(monkeypatch, tmp_path):
    _write_valid_auth_file(tmp_path)
    calls = _count_transactions(monkeypatch)

    auth.resolve_nous_access_token()
    cached_at, cache_key, tok = auth._RESOLVE_TOKEN_CACHE
    monkeypatch.setattr(
        auth,
        "_RESOLVE_TOKEN_CACHE",
        (cached_at - auth._RESOLVE_TOKEN_CACHE_TTL_S - 1.0, cache_key, tok),
    )
    auth.resolve_nous_access_token()

    assert calls["n"] == 2, "an expired memo must re-resolve"


def test_insecure_callers_bypass_memo(monkeypatch, tmp_path):
    _write_valid_auth_file(tmp_path)
    calls = _count_transactions(monkeypatch)

    auth.resolve_nous_access_token()
    auth.resolve_nous_access_token(insecure=True)

    assert calls["n"] == 2, "insecure callers must bypass the memo entirely"


def test_different_refresh_skew_does_not_reuse_memo(monkeypatch, tmp_path):
    _write_valid_auth_file(tmp_path)
    calls = _count_transactions(monkeypatch)

    auth.resolve_nous_access_token(refresh_skew_seconds=120)
    auth.resolve_nous_access_token(refresh_skew_seconds=600)

    assert calls["n"] == 2, "refresh policy is part of token resolution semantics"


def test_profile_switch_does_not_reuse_another_profiles_token(tmp_path):
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    _write_valid_auth_file(profile_a, token="profile-a-token")
    _write_valid_auth_file(profile_b, token="profile-b-token")

    override = set_hermes_home_override(profile_a)
    try:
        assert auth.resolve_nous_access_token() == "profile-a-token"
    finally:
        reset_hermes_home_override(override)

    override = set_hermes_home_override(profile_b)
    try:
        assert auth.resolve_nous_access_token() == "profile-b-token"
    finally:
        reset_hermes_home_override(override)


def test_logout_invalidates_cached_token(tmp_path):
    _write_valid_auth_file(tmp_path, token="logged-in-token")
    assert auth.resolve_nous_access_token() == "logged-in-token"

    assert auth.clear_provider_auth("nous") is True

    with pytest.raises(auth.AuthError, match="not logged into Nous Portal"):
        auth.resolve_nous_access_token()


def test_auth_store_account_switch_invalidates_cached_token(tmp_path):
    _write_valid_auth_file(tmp_path, token="account-a-token")
    assert auth.resolve_nous_access_token() == "account-a-token"

    replacement = auth._load_auth_store()
    replacement["providers"]["nous"]["access_token"] = "account-b-token"
    auth._save_auth_store(replacement)

    assert auth.resolve_nous_access_token() == "account-b-token"


def test_shared_store_update_invalidates_cached_token(tmp_path):
    _write_valid_auth_file(tmp_path, token="local-token")
    assert auth.resolve_nous_access_token() == "local-token"

    shared_state = auth._load_provider_state(auth._load_auth_store(), "nous")
    shared_state["access_token"] = "shared-token"
    shared_state["refresh_token"] = "shared-refresh-token"
    shared_state["expires_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 7200)
    )
    auth._write_shared_nous_state(shared_state)

    assert auth.resolve_nous_access_token() == "shared-token"
