import json
import time
from datetime import datetime, timezone

import httpx

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent import usage_contract
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


def _expired_jwt() -> str:
    import base64

    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg({'exp': 1_700_000_000})}.signature"


def test_usage_contract_rehydrates_env_sourced_credentials(tmp_path, monkeypatch):
    """Env-sourced pool entries persist without the secret value. The contract
    must re-read the env var at build time so the credential is usable and the
    fetcher receives the live key."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-fixture")
    _write_fixture(
        hermes_home,
        {
            "kimi-coding": [
                {
                    "id": "kimi-1",
                    "label": "KIMI_API_KEY",
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": "env:KIMI_API_KEY",
                    "base_url": "https://api.kimi.com/coding",
                    # access_token intentionally absent — sanitized on persist.
                }
            ]
        },
    )

    seen = {}

    def fake_fetch(provider, *, api_key, base_url=None):
        seen["api_key"] = api_key
        return AccountUsageSnapshot(
            provider=provider,
            source="fixture",
            fetched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            windows=(AccountUsageWindow(label="Weekly", used_percent=77.0),),
        )

    payload = build_usage_contract(
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    kimi = payload["providers"][0]
    assert kimi["provider"] == "kimi-coding"
    assert kimi["usage_capability"] == "supported"
    account = kimi["accounts"][0]
    assert account["health"]["status"] == "ready"
    assert account["quota"]["status"] == "available"
    assert seen["api_key"] == "sk-kimi-fixture"
    assert "sk-kimi-fixture" not in json.dumps(payload)


def test_usage_contract_marks_env_credential_unavailable_when_env_missing(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    _write_fixture(
        hermes_home,
        {
            "kimi-coding": [
                {
                    "id": "kimi-1",
                    "label": "KIMI_API_KEY",
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": "env:KIMI_API_KEY",
                    "base_url": "https://api.kimi.com/coding",
                }
            ]
        },
    )

    def fake_fetch(provider, *, api_key, base_url=None):
        raise AssertionError("fetcher must not run for an unavailable credential")

    payload = build_usage_contract(
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    account = payload["providers"][0]["accounts"][0]
    assert account["health"]["status"] == "unavailable"
    assert account["quota"]["status"] == "unavailable"


def test_usage_contract_delegates_expired_single_codex_token_to_refreshing_resolver(tmp_path, monkeypatch):
    """A single-account Codex credential whose JWT access_token has expired
    must be fetched through the refreshing resolver (api_key=None) rather than
    the stale token, so the quota fetch self-heals instead of 401-ing."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {
            "openai-codex": [
                {
                    "id": "codex-1",
                    "label": "device_code",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": "device_code",
                    "access_token": _expired_jwt(),
                    "refresh_token": "rt-1",
                }
            ]
        },
    )

    seen = {}

    def fake_fetch(provider, *, api_key, base_url=None):
        seen["api_key"] = api_key
        return AccountUsageSnapshot(
            provider=provider,
            source="fixture",
            fetched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            windows=(AccountUsageWindow(label="Session", used_percent=66.0),),
        )

    payload = build_usage_contract(
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    account = payload["providers"][0]["accounts"][0]
    assert account["health"]["status"] == "ready"
    assert account["quota"]["status"] == "available"
    assert seen["api_key"] is None


def test_usage_contract_marks_expired_codex_token_unavailable_in_multi_account_pool(tmp_path, monkeypatch):
    """With multiple Codex accounts, an expired token must NOT delegate to the
    resolver (it could return a different account's usage). The account shows
    an explicit expired state instead of another account's windows."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [
        {
            "id": f"codex-{idx}",
            "label": "device_code",
            "auth_type": "oauth",
            "priority": idx,
            "source": "device_code",
            "access_token": _expired_jwt(),
            "refresh_token": f"rt-{idx}",
        }
        for idx in range(2)
    ]
    _write_fixture(hermes_home, {"openai-codex": entries})

    def fake_fetch(provider, *, api_key, base_url=None):
        raise AssertionError("expired multi-account tokens must not be fetched")

    payload = build_usage_contract(
        fetcher=fake_fetch,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    accounts = payload["providers"][0]["accounts"]
    assert len(accounts) == 2
    for account in accounts:
        assert account["quota"]["status"] == "unavailable"
        assert "expired" in account["quota"]["reason"].lower()


def _available_snapshot(provider: str, used: float = 25.0) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        provider=provider,
        source="fixture",
        fetched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Weekly", used_percent=used),),
    )


def test_usage_contract_fetches_accounts_concurrently_and_orders_deterministically(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {
            "openrouter": [
                {"id": "second", "priority": 2, "source": "manual", "access_token": "token-b"},
                {"id": "first", "priority": 1, "source": "manual", "access_token": "token-a"},
            ],
            "kimi-coding": [
                {"id": "kimi", "priority": 0, "source": "manual", "access_token": "token-k"}
            ],
        },
    )
    usage_contract._clear_usage_cache_for_tests()

    def slow_fetch(provider, *, api_key, base_url=None):
        time.sleep(0.2)
        return _available_snapshot(provider)

    started = time.perf_counter()
    payload = build_usage_contract(fetcher=slow_fetch)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.38
    assert [item["provider"] for item in payload["providers"]] == ["kimi-coding", "openrouter"]
    openrouter = payload["providers"][1]
    assert [account["routing"]["priority"] for account in openrouter["accounts"]] == [1, 2]


def test_usage_contract_global_deadline_does_not_wait_for_running_worker(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {"openrouter": [{"id": "slow", "source": "manual", "access_token": "token-slow"}]},
    )
    usage_contract._clear_usage_cache_for_tests()
    monkeypatch.setattr(usage_contract, "_FETCH_DEADLINE_SECONDS", 0.05)

    def blocked_fetch(provider, *, api_key, base_url=None):
        time.sleep(0.3)
        return _available_snapshot(provider)

    started = time.perf_counter()
    payload = build_usage_contract(fetcher=blocked_fetch)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.18
    quota = payload["providers"][0]["accounts"][0]["quota"]
    assert quota["status"] == "error"
    assert "timed out" in quota["reason"].lower()


def test_usage_contract_uses_stale_cache_only_for_timeout(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {"openrouter": [{"id": "cached", "source": "manual", "access_token": "token-a"}]},
    )
    usage_contract._clear_usage_cache_for_tests()
    clock = [1000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])

    first = build_usage_contract(fetcher=lambda provider, **kwargs: _available_snapshot(provider, 42))
    assert first["providers"][0]["accounts"][0]["quota"]["stale"] is False

    clock[0] += 61

    def timeout_fetch(provider, **kwargs):
        raise httpx.ReadTimeout("fixture timeout")

    stale = build_usage_contract(fetcher=timeout_fetch)
    stale_quota = stale["providers"][0]["accounts"][0]["quota"]
    assert stale_quota["status"] == "available"
    assert stale_quota["stale"] is True
    assert stale_quota["fetched_at"] == "2026-08-10T00:00:00Z"

    def programming_error(provider, **kwargs):
        raise RuntimeError("fixture bug")

    failed = build_usage_contract(fetcher=programming_error)
    assert failed["providers"][0]["accounts"][0]["quota"]["status"] == "error"


def test_usage_contract_cache_isolated_across_credential_rotation(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entry = {"id": "same-id", "source": "manual", "access_token": "token-before"}
    _write_fixture(hermes_home, {"openrouter": [entry]})
    usage_contract._clear_usage_cache_for_tests()
    clock = [2000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])
    build_usage_contract(fetcher=lambda provider, **kwargs: _available_snapshot(provider))

    clock[0] += 61
    entry["access_token"] = "token-after"
    _write_fixture(hermes_home, {"openrouter": [entry]})

    def timeout_fetch(provider, **kwargs):
        raise httpx.ConnectTimeout("fixture timeout")

    payload = build_usage_contract(fetcher=timeout_fetch)
    quota = payload["providers"][0]["accounts"][0]["quota"]
    assert quota["status"] == "error"
    assert quota.get("stale") is not True
