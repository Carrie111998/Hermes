"""TASK 2 (2026-05-28): make Codex OAuth refresh proactive + non-silent.

Root cause of the 33-day token freeze: refresh is purely lazy (fires only when a
codex client is resolved AND the token is within the refresh skew of expiry), and
on failure the credential-pool path logged at DEBUG and silently exhausted the
entry — so a broken single-use refresh-token chain stayed dead, unnoticed, until a
manual re-login. Two hardening changes:

1. PROACTIVE: widen the refresh skew so renewal starts well before hard expiry,
   giving many more retry attempts (and surviving transient failures).
2. LOUD: a failed Codex refresh logs at WARNING and drops a sentinel marker file
   (get_hermes_home()/.codex_refresh_failed) that the health probe can read; a
   successful refresh / token save clears it.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import pytest

from hermes_cli.auth import (
    AuthError,
    CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    _codex_access_token_is_expiring,
    _save_codex_tokens,
    clear_codex_refresh_failure_marker,
    note_codex_refresh_failure,
)


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{encoded}.s"


# ── Part 1: proactive refresh skew ─────────────────────────────────────────

def test_refresh_skew_is_proactive_at_least_an_hour():
    """The skew must be wide enough that a token with 30 min left is 'expiring'.

    With the old 120s skew, a 30-min-out token was NOT considered expiring, so
    refresh only fired in the final 2 minutes — no runway for retries.
    """
    assert CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS >= 1800
    tok = _jwt_with_exp(int(time.time()) + 1800)  # 30 minutes out
    assert _codex_access_token_is_expiring(tok, CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) is True


# ── Part 2: loud failure + sentinel marker ─────────────────────────────────

def test_note_codex_refresh_failure_writes_marker_and_warns(tmp_path, monkeypatch, caplog):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    exc = AuthError("refresh token reused", provider="openai-codex",
                    code="refresh_token_reused", relogin_required=True)
    with caplog.at_level(logging.WARNING):
        note_codex_refresh_failure(exc, source="credential_pool")

    marker = hermes_home / ".codex_refresh_failed"
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["relogin_required"] is True
    assert data["source"] == "credential_pool"
    assert any(
        r.levelname == "WARNING" and "refresh" in r.message.lower()
        for r in caplog.records
    ), "a failed Codex refresh must be logged at WARNING, not DEBUG"


def test_clear_marker_is_idempotent(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # No marker yet — clearing must not raise.
    clear_codex_refresh_failure_marker()
    marker = hermes_home / ".codex_refresh_failed"
    marker.write_text("{}", encoding="utf-8")
    clear_codex_refresh_failure_marker()
    assert not marker.exists()


def test_save_codex_tokens_clears_marker(tmp_path, monkeypatch):
    """A successful token save (refresh or manual re-auth) clears the failure flag."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    marker = hermes_home / ".codex_refresh_failed"
    marker.write_text(json.dumps({"relogin_required": True}), encoding="utf-8")

    _save_codex_tokens({"access_token": "fresh-at", "refresh_token": "fresh-rt"})

    assert not marker.exists()


# ── Part 2b: the credential-pool path (the one that silently froze) ────────

def test_credential_pool_codex_refresh_failure_surfaces(tmp_path, monkeypatch, caplog):
    """pool._refresh_entry for codex must surface failures (WARNING + marker),
    not swallow them at DEBUG."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential
    import hermes_cli.auth as auth_mod

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def _boom(access_token, refresh_token, **kwargs):
        raise auth_mod.AuthError(
            "refresh token reused", provider="openai-codex",
            code="refresh_token_reused", relogin_required=True,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _boom)

    entry = PooledCredential(
        provider="openai-codex", id="x", label="l", auth_type=AUTH_TYPE_OAUTH,
        priority=0, source="device_code", access_token="expired-at", refresh_token="dead-rt",
    )
    pool = CredentialPool("openai-codex", [entry])

    with caplog.at_level(logging.WARNING):
        result = pool._refresh_entry(entry, force=True)

    assert result is None  # failed refresh → entry not usable
    assert (hermes_home / ".codex_refresh_failed").is_file()
    assert any(
        r.levelname == "WARNING" and "refresh" in r.message.lower()
        for r in caplog.records
    )


def test_credential_pool_codex_refresh_success_clears_marker(tmp_path, monkeypatch):
    from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential
    import hermes_cli.auth as auth_mod

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    marker = hermes_home / ".codex_refresh_failed"
    marker.write_text(json.dumps({"relogin_required": True}), encoding="utf-8")

    def _ok(access_token, refresh_token, **kwargs):
        return {
            "access_token": _jwt_with_exp(int(time.time()) + 9 * 86400),
            "refresh_token": "rotated-rt",
            "last_refresh": "2026-05-28T22:04:45Z",
        }

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _ok)

    entry = PooledCredential(
        provider="openai-codex", id="x", label="l", auth_type=AUTH_TYPE_OAUTH,
        priority=0, source="device_code", access_token="expired-at", refresh_token="rt",
    )
    pool = CredentialPool("openai-codex", [entry])

    result = pool._refresh_entry(entry, force=True)

    assert result is not None
    assert not marker.exists()
