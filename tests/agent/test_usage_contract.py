import json
from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.usage_contract import build_usage_contract


def _write_fixture(home, providers):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": providers}),
        encoding="utf-8",
    )


def test_usage_contract_separates_two_accounts_with_stable_sanitized_ids(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [
        {
            "id": "persisted-alpha",
            "label": "personal@example.test",
            "auth_type": "oauth",
            "priority": 1,
            "source": "device_code",
            "access_token": "secret-alpha",
            "refresh_token": "refresh-alpha",
        },
        {
            "id": "persisted-beta",
            "label": "work@example.test",
            "auth_type": "oauth",
            "priority": 0,
            "source": "device_code",
            "access_token": "secret-beta",
            "refresh_token": "refresh-beta",
        },
    ]
    _write_fixture(hermes_home, {"openai-codex": entries})

    def fake_fetch(provider, *, api_key, base_url=None):
        used = 25 if api_key == "secret-alpha" else 70
        return AccountUsageSnapshot(
            provider=provider,
            source="fixture",
            fetched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            windows=(AccountUsageWindow(label="Weekly", used_percent=used),),
        )

    first = build_usage_contract(
        session_usage={"calls": 3, "input": 100, "output": 40, "total": 140},
        session_provider="openai-codex",
        session_model="dynamic-model",
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    _write_fixture(hermes_home, {"openai-codex": list(reversed(entries))})
    second = build_usage_contract(
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    first_accounts = first["providers"][0]["accounts"]
    second_accounts = second["providers"][0]["accounts"]
    assert first["contract"] == {"name": "usage.accounts", "version": 1}
    assert first["capabilities"]["provider_usage"]["per_account"] is True
    assert len(first_accounts) == 2
    assert {account["account_id"] for account in first_accounts} == {
        account["account_id"] for account in second_accounts
    }
    assert all(account["account_id"].startswith("acct_") for account in first_accounts)
    assert all(account["quota"]["status"] == "available" for account in first_accounts)
    assert first["local"] == {
        "status": "available",
        "provider": "openai-codex",
        "model": "dynamic-model",
        "calls": 3,
        "tokens": {"input": 100, "output": 40, "total": 140},
    }

    serialized = json.dumps(first)
    for forbidden in (
        "secret-alpha",
        "secret-beta",
        "refresh-alpha",
        "refresh-beta",
        "personal@example.test",
        "work@example.test",
        "persisted-alpha",
        "persisted-beta",
        "access_token",
        "refresh_token",
    ):
        assert forbidden not in serialized


def test_usage_contract_reports_health_and_truthful_usage_states(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {
            "openrouter": [
                {
                    "id": "ready",
                    "priority": 0,
                    "source": "manual",
                    "access_token": "fails-provider-request",
                    "last_status": "ok",
                },
                {
                    "id": "cooldown",
                    "priority": 1,
                    "source": "manual",
                    "access_token": "cooldown-token",
                    "last_status": "exhausted",
                    "last_error_reset_at": 1_800_000_000,
                },
                {
                    "id": "expired",
                    "priority": 2,
                    "source": "manual",
                    "access_token": "expired-token",
                    "expires_at": "2026-08-09T00:00:00Z",
                },
            ],
            "local-provider": [
                {
                    "id": "local-only",
                    "priority": 0,
                    "source": "manual",
                    "access_token": "local-secret",
                }
            ],
        },
    )

    def failing_fetch(provider, *, api_key, base_url=None):
        raise RuntimeError(f"must not escape: {api_key}")

    payload = build_usage_contract(
        fetcher=failing_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    providers = {provider["provider"]: provider for provider in payload["providers"]}
    openrouter = providers["openrouter"]

    assert [account["health"]["status"] for account in openrouter["accounts"]] == [
        "ready",
        "cooldown",
        "expired",
    ]
    assert [account["quota"]["status"] for account in openrouter["accounts"]] == [
        "error",
        "unavailable",
        "unavailable",
    ]
    assert providers["local-provider"]["usage_capability"] == "unsupported"
    assert providers["local-provider"]["accounts"][0]["quota"]["status"] == "unsupported"
    assert payload["local"] == {"status": "unavailable"}
    serialized = json.dumps(payload)
    assert "fails-provider-request" not in serialized
    assert "cooldown-token" not in serialized
    assert "expired-token" not in serialized
    assert "local-secret" not in serialized
