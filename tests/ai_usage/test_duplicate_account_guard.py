"""The tray's two Anthropic rows must track two DISTINCT subscriptions.

Regression cover for 2026-08-23: the isolated ``~/.claude-anthropic2`` login
landed on the already-signed-in browser account, so "Claude" and "Claude 2"
reported identical percentages for one subscription with no visible symptom.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ai_usage.budget import budget_provider
from ai_usage.collector import _flag_duplicate_accounts

NOW = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)


@dataclass
class FakeWin:
    label: str
    used_percent: Optional[float]
    reset_at: Optional[datetime]


@dataclass
class FakeSnap:
    available: bool
    windows: tuple
    fetched_at: datetime = NOW
    unavailable_reason: Optional[str] = None
    account_uuid: Optional[str] = None
    account_email: Optional[str] = None


def _row(key, label, uuid, email):
    snap = FakeSnap(
        available=True,
        windows=(FakeWin("Current session", 17.0, None),),
        account_uuid=uuid,
        account_email=email,
    )
    return budget_provider(key, label, snap)


def test_budget_provider_carries_account_identity():
    row = _row("anthropic", "Claude", "uuid-a", "a@example.com")
    assert row["account_uuid"] == "uuid-a"
    assert row["account_email"] == "a@example.com"


def test_budget_provider_omits_identity_when_absent():
    snap = FakeSnap(available=True, windows=(FakeWin("Current session", 17.0, None),))
    row = budget_provider("kimi", "Kimi K3", snap)
    assert "account_uuid" not in row
    assert "account_email" not in row


def test_distinct_accounts_are_left_alone():
    rows = [
        _row("anthropic", "Claude", "uuid-a", "a@example.com"),
        _row("anthropic2", "Claude 2", "uuid-b", "b@example.com"),
    ]
    _flag_duplicate_accounts(rows)
    assert [r["state"] for r in rows] == ["ok", "ok"]
    assert all("duplicate_of" not in r for r in rows)


def test_same_account_flags_the_later_row_only():
    rows = [
        _row("anthropic", "Claude", "uuid-a", "dup@example.com"),
        _row("anthropic2", "Claude 2", "uuid-a", "dup@example.com"),
    ]
    _flag_duplicate_accounts(rows)
    assert rows[0]["state"] == "ok"
    assert "duplicate_of" not in rows[0]
    assert rows[1]["state"] == "error"
    assert rows[1]["duplicate_of"] == "anthropic"
    assert rows[1]["detail"] == "same account as Claude (dup@example.com)"


def test_rows_without_identity_never_collide():
    # Every non-Anthropic provider reports no account_uuid; they must not all
    # collapse into one "duplicate" group on the shared empty key.
    rows = [
        {"key": "kimi", "label": "Kimi K3", "state": "ok"},
        {"key": "xai", "label": "Grok", "state": "ok"},
        {"key": "gemini", "label": "Gemini", "state": "ok", "account_uuid": ""},
    ]
    _flag_duplicate_accounts(rows)
    assert [r["state"] for r in rows] == ["ok", "ok", "ok"]


def test_non_ok_row_is_not_treated_as_the_incumbent():
    # A carried-forward/errored row must not shadow a healthy one: the healthy
    # row keeps its data and the duplicate is still caught.
    rows = [
        {"key": "anthropic", "label": "Claude", "state": "error", "account_uuid": "uuid-a"},
        _row("anthropic2", "Claude 2", "uuid-a", "dup@example.com"),
    ]
    _flag_duplicate_accounts(rows)
    assert rows[1]["state"] == "ok"


class _FakeResponse:
    def __init__(self, payload, *, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("403 permission_error")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append(url)
        return self._response


def test_identity_reads_uuid_and_email():
    from agent.account_usage import _fetch_anthropic_account_identity

    client = _FakeClient(
        _FakeResponse({"account": {"uuid": "c61d6560", "email": "who@example.com"}})
    )
    assert _fetch_anthropic_account_identity(client, {}) == ("c61d6560", "who@example.com")
    assert client.calls == ["https://api.anthropic.com/api/oauth/profile"]


def test_identity_failure_never_costs_the_usage_numbers():
    from agent.account_usage import _fetch_anthropic_account_identity

    client = _FakeClient(_FakeResponse({}, status_ok=False))
    assert _fetch_anthropic_account_identity(client, {}) == (None, None)


def test_primary_row_prefers_the_pinned_profile_over_the_live_login(monkeypatch):
    """~/.claude follows the desktop app's current account; the row must not.

    Regression for 2026-08-23 14:18, when the primary profile was switched to
    the second subscription's account and the "Claude" row silently became a
    duplicate of "Claude 2".
    """
    from agent import account_usage

    pinned = {"accessToken": "sk-ant-oat01-pinned", "expiresAt": 1 << 62}
    monkeypatch.setattr(account_usage, "_read_anthropic1_credentials", lambda: pinned)
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        lambda: (_ for _ in ()).throw(AssertionError("must not read the live login")),
    )
    seen = {}

    def fake_fetch(token, *, timeout=None, provider="anthropic"):
        seen["token"] = token
        seen["provider"] = provider
        return "snapshot"

    monkeypatch.setattr(account_usage, "_fetch_anthropic_usage_with_token", fake_fetch)
    assert account_usage._fetch_anthropic_account_usage() == "snapshot"
    assert seen == {"token": "sk-ant-oat01-pinned", "provider": "anthropic"}


def test_primary_row_falls_back_to_the_live_login_when_unpinned(monkeypatch):
    from agent import account_usage

    monkeypatch.setattr(account_usage, "_read_anthropic1_credentials", lambda: None)
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-live")
    monkeypatch.setattr(
        account_usage,
        "_fetch_anthropic_usage_with_token",
        lambda token, *, timeout=None, provider="anthropic": ("fallback", token),
    )
    assert account_usage._fetch_anthropic_account_usage() == ("fallback", "sk-ant-oat01-live")
