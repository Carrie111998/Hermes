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


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.fixture/usage")
    return httpx.HTTPStatusError(
        f"fixture {status_code}",
        request=request,
        response=httpx.Response(status_code=status_code, request=request),
    )


def test_usage_contract_cache_identity_ignores_stale_persisted_fingerprint(tmp_path, monkeypatch):
    """A persisted secret_fingerprint that lags a rotation must not anchor the
    cache identity: the rotated credential must not inherit the old quota."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entry = {
        "id": "same-id",
        "source": "manual",
        "access_token": "token-before",
        "secret_fingerprint": "persisted-fingerprint-not-yet-rewritten",
    }
    _write_fixture(hermes_home, {"openrouter": [entry]})
    usage_contract._clear_usage_cache_for_tests()
    clock = [3000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])
    build_usage_contract(fetcher=lambda provider, **kwargs: _available_snapshot(provider))

    clock[0] += 61  # fresh window elapsed; only the stale snapshot remains
    entry["access_token"] = "token-after"  # rotated, fingerprint record stale
    _write_fixture(hermes_home, {"openrouter": [entry]})

    def timeout_fetch(provider, **kwargs):
        raise httpx.ReadTimeout("fixture timeout")

    payload = build_usage_contract(fetcher=timeout_fetch)
    quota = payload["providers"][0]["accounts"][0]["quota"]
    assert quota["status"] == "error"
    assert quota.get("stale") is not True


def test_canonical_endpoint_equivalence_classes():
    canonical = usage_contract._canonical_endpoint
    assert canonical("") == "<provider-default>"
    assert canonical(None) == "<provider-default>"
    # scheme/host case, trailing + duplicate slashes
    assert canonical("HTTPS://API.Example.COM/v1/") == "https://api.example.com/v1"
    assert canonical("https://api.example.com//v1//") == "https://api.example.com/v1"
    # default ports collapse, explicit ports survive
    assert canonical("https://api.example.com:443/v1") == "https://api.example.com/v1"
    assert canonical("http://api.example.com:80/v1") == "https://api.example.com/v1".replace("https", "http")
    assert canonical("https://api.example.com:8443/v1") == "https://api.example.com:8443/v1"
    # query / fragment / userinfo never participate in identity
    assert canonical("https://api.example.com/v1?sig=ephemeral") == "https://api.example.com/v1"
    assert canonical("https://api.example.com/v1#frag") == "https://api.example.com/v1"
    assert canonical("https://user:pw@api.example.com/v1") == "https://api.example.com/v1"
    # IPv6 authorities keep brackets
    assert canonical("http://[::1]:8080/v1") == "http://[::1]:8080/v1"


def test_usage_contract_cache_identity_scoped_per_profile(tmp_path, monkeypatch):
    entry = {"id": "e1", "source": "manual", "base_url": "https://api.example.com/v1"}
    monkeypatch.setattr(usage_contract, "get_hermes_home", lambda: tmp_path / "profile-a")
    identity_a = usage_contract._cache_identity("openrouter", entry, "token-x")
    monkeypatch.setattr(usage_contract, "get_hermes_home", lambda: tmp_path / "profile-b")
    identity_b = usage_contract._cache_identity("openrouter", entry, "token-x")
    assert identity_a and identity_b and identity_a != identity_b


def test_usage_contract_late_worker_does_not_pollute_cache(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entry = {"id": "slow", "source": "manual", "access_token": "token-slow"}
    _write_fixture(hermes_home, {"openrouter": [entry]})
    usage_contract._clear_usage_cache_for_tests()
    monkeypatch.setattr(usage_contract, "_FETCH_DEADLINE_SECONDS", 0.05)

    def blocked_fetch(provider, *, api_key, base_url=None):
        time.sleep(0.3)
        return _available_snapshot(provider)

    payload = build_usage_contract(fetcher=blocked_fetch)
    assert payload["providers"][0]["accounts"][0]["quota"]["status"] == "error"

    time.sleep(0.5)  # let the detached worker finish
    identity = usage_contract._cache_identity("openrouter", entry, "token-slow")
    assert usage_contract._cache_read(identity) == (None, None)


def test_usage_contract_caps_concurrency_with_more_jobs_than_workers(tmp_path, monkeypatch):
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [
        {"id": f"acct-{index}", "source": "manual", "access_token": f"token-{index}", "priority": index}
        for index in range(6)
    ]
    _write_fixture(hermes_home, {"openrouter": entries})
    usage_contract._clear_usage_cache_for_tests()

    lock = threading.Lock()
    running = [0]
    peak = [0]

    def tracking_fetch(provider, *, api_key, base_url=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.05)
            return _available_snapshot(provider)
        finally:
            with lock:
                running[0] -= 1

    payload = build_usage_contract(fetcher=tracking_fetch)
    quotas = [a["quota"]["status"] for a in payload["providers"][0]["accounts"]]
    assert quotas == ["available"] * 6
    assert 1 <= peak[0] <= 4


def test_usage_contract_stale_masking_only_for_retryable_failures(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {"openrouter": [{"id": "cached", "source": "manual", "access_token": "token-a"}]},
    )
    usage_contract._clear_usage_cache_for_tests()
    clock = [4000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])

    first = build_usage_contract(fetcher=lambda provider, **kwargs: _available_snapshot(provider))
    assert first["providers"][0]["accounts"][0]["quota"]["stale"] is False
    clock[0] += 61  # demote the cached snapshot to stale

    def quota_after(exc):
        def failing(provider, **kwargs):
            raise exc

        payload = build_usage_contract(fetcher=failing)
        return payload["providers"][0]["accounts"][0]["quota"]

    # Retryable: 5xx and transport errors may be masked by the stale snapshot.
    assert quota_after(_status_error(500))["stale"] is True
    assert quota_after(httpx.ConnectError("fixture connect"))["stale"] is True

    # Auth failures surface as unavailable with a safe reason — never stale,
    # never error-swallowed. Rate limiting surfaces as error. Each negative
    # classification is cached briefly, so advance past its TTL between cases.
    for status_code in (401, 403):
        quota = quota_after(_status_error(status_code))
        assert quota["status"] == "unavailable"
        assert "authentication failed" in quota["reason"]
        assert quota.get("stale") is not True
        clock[0] += 31  # expire the auth negative entry
    quota = quota_after(_status_error(429))
    assert quota["status"] == "error"
    assert quota.get("stale") is not True


def test_stale_classifier_excludes_protocol_and_programming_errors(tmp_path, monkeypatch):
    """Parent acceptance A1: LocalProtocolError/UnsupportedProtocol are local
    configuration or call-site bugs, not transient upstream failures — a stale
    snapshot must never mask them. Timeouts, network errors, remote protocol
    and proxy failures, and 5xx remain maskable."""
    # Unit-level classification.
    assert usage_contract._is_retryable_fetch_error(httpx.ReadTimeout("x")) is True
    assert usage_contract._is_retryable_fetch_error(httpx.ConnectError("x")) is True
    assert usage_contract._is_retryable_fetch_error(httpx.WriteError("x")) is True
    assert usage_contract._is_retryable_fetch_error(httpx.CloseError("x")) is True
    assert usage_contract._is_retryable_fetch_error(httpx.RemoteProtocolError("x")) is True
    assert usage_contract._is_retryable_fetch_error(httpx.ProxyError("x")) is True
    assert usage_contract._is_retryable_fetch_error(_status_error(500)) is True
    assert usage_contract._is_retryable_fetch_error(_status_error(503)) is True

    assert usage_contract._is_retryable_fetch_error(httpx.UnsupportedProtocol("x")) is False
    assert usage_contract._is_retryable_fetch_error(httpx.LocalProtocolError("x")) is False
    assert usage_contract._is_retryable_fetch_error(RuntimeError("x")) is False
    for status_code in (401, 403, 429, 400, 404):
        assert usage_contract._is_retryable_fetch_error(_status_error(status_code)) is False

    # End-to-end: a cached stale snapshot must not mask a LocalProtocolError.
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(
        hermes_home,
        {"openrouter": [{"id": "cached", "source": "manual", "access_token": "token-a"}]},
    )
    usage_contract._clear_usage_cache_for_tests()
    clock = [5000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])
    build_usage_contract(fetcher=lambda provider, **kwargs: _available_snapshot(provider))
    clock[0] += 61  # demote to stale

    def protocol_bug(provider, **kwargs):
        raise httpx.LocalProtocolError("fixture misconfiguration")

    payload = build_usage_contract(fetcher=protocol_bug)
    quota = payload["providers"][0]["accounts"][0]["quota"]
    assert quota["status"] == "error"
    assert quota.get("stale") is not True


def _status_error_with_retry_after(status_code: int, retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.fixture/usage")
    response = httpx.Response(status_code=status_code, request=request, headers={"Retry-After": retry_after})
    return httpx.HTTPStatusError(f"fixture {status_code}", request=request, response=response)


def test_display_names_stable_across_shuffle_priority_and_cooldown(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [
        {"id": "beta-id", "label": "b@example.test", "priority": 9, "source": "manual", "access_token": "tok-b"},
        {"id": "alpha-id", "label": "a@example.test", "priority": 0, "source": "manual", "access_token": "tok-a"},
    ]
    _write_fixture(hermes_home, {"openai-codex": entries})
    usage_contract._clear_usage_cache_for_tests()
    first = build_usage_contract(fetcher=lambda provider, **kw: _available_snapshot(provider))
    names_first = {a["account_id"]: a["display_name"] for a in first["providers"][0]["accounts"]}

    reversed_entries = [dict(entries[1], priority=0), dict(entries[0], priority=1, last_status="exhausted")]
    _write_fixture(hermes_home, {"openai-codex": reversed_entries})
    usage_contract._clear_usage_cache_for_tests()
    second = build_usage_contract(fetcher=lambda provider, **kw: _available_snapshot(provider))
    names_second = {a["account_id"]: a["display_name"] for a in second["providers"][0]["accounts"]}

    assert names_first == names_second
    assert sorted(names_first.values()) == ["Codex 1", "Codex 2"]
    serialized = json.dumps(second)
    for forbidden in ("a@example.test", "b@example.test", "alpha-id", "beta-id", "manual"):
        assert forbidden not in serialized


def test_display_names_legacy_entries_without_ids(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    legacy = [
        {"source": "env", "auth_type": "api_key", "label": "x@example.test", "access_token": "tok-x"},
        {"source": "manual", "auth_type": "oauth", "label": "y@example.test", "access_token": "tok-y"},
    ]
    _write_fixture(hermes_home, {"kimi-coding": legacy})
    usage_contract._clear_usage_cache_for_tests()
    first = build_usage_contract(fetcher=lambda provider, **kw: _available_snapshot(provider))
    names_first = {a["account_id"]: a["display_name"] for a in first["providers"][0]["accounts"]}
    _write_fixture(hermes_home, {"kimi-coding": list(reversed(legacy))})
    usage_contract._clear_usage_cache_for_tests()
    second = build_usage_contract(fetcher=lambda provider, **kw: _available_snapshot(provider))
    names_second = {a["account_id"]: a["display_name"] for a in second["providers"][0]["accounts"]}
    assert names_first == names_second
    assert sorted(names_first.values()) == ["Kimi 1", "Kimi 2"]
    assert "example.test" not in json.dumps(second)


def test_is_current_omitted_without_authoritative_signal(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()
    payload = build_usage_contract(fetcher=lambda provider, **kw: _available_snapshot(provider))
    assert "is_current" not in payload["providers"][0]["accounts"][0]


def test_negative_cache_short_circuits_repeat_failures(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()
    calls = []

    def failing(provider, **kw):
        calls.append(provider)
        raise _status_error(500)

    first = build_usage_contract(fetcher=failing)
    assert first["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    second = build_usage_contract(fetcher=failing)
    assert second["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    assert len(calls) == 1  # second build served from the negative cache


def test_429_retry_after_delta_and_date_capped(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()
    clock = [9000.0]
    monkeypatch.setattr(usage_contract, "_monotonic", lambda: clock[0])

    calls = []

    def limited(provider, **kw):
        calls.append(provider)
        raise _status_error_with_retry_after(429, "45")

    first = build_usage_contract(fetcher=limited)
    assert first["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    clock[0] += 30  # inside the Retry-After window
    build_usage_contract(fetcher=limited)
    assert len(calls) == 1
    clock[0] += 16  # past 45s
    build_usage_contract(fetcher=limited)
    assert len(calls) == 2

    # HTTP-date form and the 120s cap.
    delta = usage_contract._retry_after_seconds(_status_error_with_retry_after(429, "999999"))
    assert delta == 120.0
    from email.utils import formatdate
    import time as _time

    http_date = formatdate(_time.time() + 60, usegmt=True)
    delta_date = usage_contract._retry_after_seconds(_status_error_with_retry_after(429, http_date))
    assert 55.0 <= delta_date <= 61.0


def test_concurrent_builds_same_key_single_fetch(tmp_path, monkeypatch):
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def slow(provider, **kw):
        with lock:
            calls.append(provider)
        time.sleep(0.15)
        return _available_snapshot(provider)

    results = []

    def worker():
        barrier.wait(timeout=5)
        results.append(build_usage_contract(fetcher=slow))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 4
    assert len(calls) == 1
    assert all(r["providers"][0]["accounts"][0]["quota"]["status"] == "available" for r in results)


def test_expired_entry_replacement_and_orphan_safety(tmp_path, monkeypatch):
    """Owner misses its deadline; a later build registers a replacement; the
    orphan's late settle must not write cache nor remove the replacement."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()
    monkeypatch.setattr(usage_contract, "_FETCH_DEADLINE_SECONDS", 0.05)

    calls = []

    def slow(provider, **kw):
        calls.append(provider)
        time.sleep(0.25)
        return _available_snapshot(provider)

    first = build_usage_contract(fetcher=slow)  # owner times out at 0.05s
    assert first["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    # Owner removed its registration after finishing; replacement flight starts.
    second = build_usage_contract(fetcher=slow)
    assert second["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    time.sleep(0.6)  # let both orphan futures settle
    stats = usage_contract._usage_fetch_stats_for_tests()
    assert stats["in_flight_submitted"] == 0
    assert stats["registered_flights"] == 0
    # Orphan completions never wrote the positive cache.
    third = build_usage_contract(fetcher=slow)
    assert third["providers"][0]["accounts"][0]["quota"]["status"] == "error"
    assert len(calls) == 3


def test_process_wide_bound_across_keys_and_rounds(tmp_path, monkeypatch):
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [{"id": f"e{i}", "source": "manual", "access_token": f"tok-{i}"} for i in range(6)]
    _write_fixture(hermes_home, {"openrouter": entries})
    usage_contract._clear_usage_cache_for_tests()
    monkeypatch.setattr(usage_contract, "_FETCH_DEADLINE_SECONDS", 0.05)

    calls = []
    running = [0]
    running_peak = [0]
    lock = threading.Lock()

    def slow(provider, **kw):
        with lock:
            calls.append(provider)
            running[0] += 1
            running_peak[0] = max(running_peak[0], running[0])
        try:
            time.sleep(0.3)
            return _available_snapshot(provider)
        finally:
            with lock:
                running[0] -= 1

    peak = 0
    for _ in range(4):  # K rounds of deadline misses across multiple keys
        build_usage_contract(fetcher=slow)
        peak = max(peak, usage_contract._usage_fetch_stats_for_tests()["in_flight_submitted"])
    # Real work happened, submissions (running + queued) stayed under the
    # process-wide admission cap, and the shared executor capped actual
    # concurrency at its worker count.
    assert len(calls) > 0
    assert 0 < peak <= 8  # _MAX_IN_FLIGHT
    assert running_peak[0] <= 4  # _FETCH_MAX_WORKERS
    time.sleep(1.2)
    assert usage_contract._usage_fetch_stats_for_tests()["in_flight_submitted"] == 0


def test_native_refresh_flight_dedupes_without_cache_key(tmp_path, monkeypatch):
    """cache_key=None (Codex native refresh shape) still singleflights on a
    non-secret flight key and never writes cache."""
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openai-codex": [{"id": "solo", "source": "device_code", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    calls = []

    def resolver_fetch(provider, *, api_key, base_url=None):
        assert api_key is None  # native resolver path
        calls.append(provider)
        time.sleep(0.15)
        return _available_snapshot(provider)

    entry_job = {
        "account": {"account_id": "acct_test"},
        "provider": "openai-codex",
        "api_key": None,
        "base_url": None,
        "cache_key": None,
        "stale": None,
    }
    deadline = usage_contract._monotonic() + 1.0
    results = []

    def worker():
        results.append(usage_contract._begin_fetch(entry_job, resolver_fetch, deadline))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    modes = sorted(h["mode"] for h in results)
    assert modes == ["join", "join", "owner"]
    for h in results:
        usage_contract._finish_fetch(entry_job, h, deadline)
    assert len(calls) == 1
    assert usage_contract._usage_fetch_stats_for_tests()["cached_snapshots"] == 0


def test_begin_fetch_rechecks_cache_inside_critical_section(tmp_path, monkeypatch):
    """Both builds cache-miss at job creation, but B reaches _begin_fetch only
    after A's flight completed and wrote the cache — B must still not fetch."""
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    entry = {"id": "a", "source": "manual", "access_token": "tok"}
    key = usage_contract._cache_identity("openrouter", entry, "tok")
    job = {
        "account": {"account_id": "acct_a"},
        "provider": "openrouter",
        "api_key": "tok",
        "base_url": None,
        "cache_key": key,
        "stale": None,
    }
    calls = []

    def slow(provider, **kw):
        calls.append(provider)
        time.sleep(0.15)
        return _available_snapshot(provider)

    deadline = usage_contract._monotonic() + 2.0
    # A becomes owner and finishes, writing the positive cache.
    handle_a = usage_contract._begin_fetch(job, slow, deadline)
    assert handle_a["mode"] == "owner"
    outcome_a = usage_contract._finish_fetch(job, handle_a, deadline)
    assert outcome_a["status"] == "available"
    assert len(calls) == 1

    # B enters _begin_fetch only now: the in-lock cache re-check must serve it.
    handle_b = usage_contract._begin_fetch(job, slow, usage_contract._monotonic() + 2.0)
    assert handle_b["mode"] == "done"
    assert handle_b["outcome"]["status"] == "available"
    assert len(calls) == 1


def test_orphan_generation_cannot_remove_replacement(tmp_path, monkeypatch):
    """An old owner's compare-and-remove after a replacement registered must
    leave the replacement entry intact; late orphan settles write nothing."""
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    entry = {"id": "a", "source": "manual", "access_token": "tok"}
    key = usage_contract._cache_identity("openrouter", entry, "tok")
    job = {
        "account": {"account_id": "acct_a"},
        "provider": "openrouter",
        "api_key": "tok",
        "base_url": None,
        "cache_key": key,
        "stale": None,
    }

    a_may_finish = threading.Event()

    def slow_a(provider, **kw):
        a_may_finish.wait(timeout=5)
        return _available_snapshot(provider)

    def fast_b(provider, **kw):
        return _available_snapshot(provider)

    # A owns the flight with a deadline that expires before it may finish.
    handle_a = usage_contract._begin_fetch(job, slow_a, usage_contract._monotonic() + 0.05)
    assert handle_a["mode"] == "owner"
    flight = handle_a["flight"]
    time.sleep(0.1)  # A's original deadline is now past; future still blocked.

    # B cannot join the expired entry; it registers a replacement (gen+1).
    handle_b = usage_contract._begin_fetch(job, fast_b, usage_contract._monotonic() + 2.0)
    assert handle_b["mode"] == "owner"
    assert handle_b["generation"] != handle_a["generation"]

    # A finally finishes — past its own deadline, so it must render a timeout,
    # write no cache, and its compare-and-remove must NOT remove B's entry.
    a_may_finish.set()
    outcome_a = usage_contract._finish_fetch(job, handle_a)
    assert outcome_a["status"] == "error"
    with usage_contract._CACHE_LOCK:
        assert usage_contract._INFLIGHT.get(flight, {}).get("generation") == handle_b["generation"]
    # A's late success never reached the positive cache.
    fresh, _stale = usage_contract._cache_read(key)
    assert fresh is None

    outcome_b = usage_contract._finish_fetch(job, handle_b)
    assert outcome_b["status"] == "available"
    with usage_contract._CACHE_LOCK:
        assert flight not in usage_contract._INFLIGHT


def test_in_deadline_completion_survives_late_consumption(tmp_path, monkeypatch):
    """Job A consumes the whole deadline; job B settled before it. B must
    render its real result (and cache it) even though the request thread
    consumes B after the deadline has passed."""
    import threading

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    entries = [
        {"id": "slow-a", "priority": 0, "source": "manual", "access_token": "tok-a"},
        {"id": "fast-b", "priority": 1, "source": "manual", "access_token": "tok-b"},
    ]
    _write_fixture(hermes_home, {"openrouter": entries})
    usage_contract._clear_usage_cache_for_tests()
    monkeypatch.setattr(usage_contract, "_FETCH_DEADLINE_SECONDS", 0.3)

    a_may_finish = threading.Event()
    calls = []

    def fetch(provider, *, api_key, base_url=None):
        calls.append(api_key)
        if api_key == "tok-a":
            a_may_finish.wait(timeout=5)
        return _available_snapshot(provider)

    payload = build_usage_contract(fetcher=fetch)
    # A blocks past the 0.3s deadline inside the build (its finish waits the
    # full remaining budget); B settles almost immediately, in-deadline, but
    # is consumed after A — the settle timestamp, not consumption, decides.
    a_may_finish.set()
    quotas = {a["display_name"]: a["quota"] for a in payload["providers"][0]["accounts"]}
    # Entries iterate in fixture order: slow-a first.
    first_quota = payload["providers"][0]["accounts"][0]["quota"]
    second_quota = payload["providers"][0]["accounts"][1]["quota"]
    assert first_quota["status"] == "error" and "timed out" in first_quota["reason"]
    assert second_quota["status"] == "available"

    # B was cached by its owner; a second build serves it fresh, no refetch.
    second = build_usage_contract(fetcher=fetch)
    second_b = second["providers"][0]["accounts"][1]["quota"]
    assert second_b["status"] == "available"
    assert calls.count("tok-b") == 1


def test_begin_fetch_rechecks_negative_inside_critical_section(tmp_path, monkeypatch):
    """First (unlocked) negative read misses; by the time the thread holds the
    registration lock, a negative exists — it must serve it, never submit."""
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    entry = {"id": "a", "source": "manual", "access_token": "tok"}
    key = usage_contract._cache_identity("openrouter", entry, "tok")
    job = {
        "account": {"account_id": "acct_a"},
        "provider": "openrouter",
        "api_key": "tok",
        "base_url": None,
        "cache_key": key,
        "stale": None,
    }

    real_read = usage_contract._negative_read
    reads = []
    negative_payload = {"status": "unavailable", "reason": "Credential authentication failed (HTTP 401)", "windows": []}

    def staged(read_key):
        reads.append(read_key)
        if len(reads) == 1:
            return None  # unlocked pre-check misses
        return negative_payload  # in-lock re-check hits

    monkeypatch.setattr(usage_contract, "_negative_read", staged)
    calls = []

    def fetch(provider, **kw):
        calls.append(provider)
        return _available_snapshot(provider)

    handle = usage_contract._begin_fetch(job, fetch, usage_contract._monotonic() + 1.0)
    assert handle["mode"] == "done"
    assert handle["outcome"] == negative_payload
    assert calls == []
    assert len(reads) >= 2
    monkeypatch.setattr(usage_contract, "_negative_read", real_read)


def test_submit_failure_releases_admission_and_recovers(tmp_path, monkeypatch):
    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    entry = {"id": "a", "source": "manual", "access_token": "tok"}
    key = usage_contract._cache_identity("openrouter", entry, "tok")
    job = {
        "account": {"account_id": "acct_a"},
        "provider": "openrouter",
        "api_key": "tok",
        "base_url": None,
        "cache_key": key,
        "stale": None,
    }

    class BrokenExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("executor exploded")

    # A capacity-1 semaphore makes a leaked permit immediately observable:
    # any leak means the next acquire fails outright.
    import threading

    monkeypatch.setattr(usage_contract, "_ADMISSION", threading.Semaphore(1))
    monkeypatch.setattr(usage_contract, "_EXECUTOR", BrokenExecutor())
    handle = usage_contract._begin_fetch(job, lambda p, **kw: _available_snapshot(p), usage_contract._monotonic() + 1.0)
    # Safe error surface, no crash; permit released; nothing registered/counted.
    outcome = usage_contract._finish_fetch(job, handle)
    assert outcome["status"] == "error"
    stats = usage_contract._usage_fetch_stats_for_tests()
    assert stats["in_flight_submitted"] == 0
    assert stats["registered_flights"] == 0

    # Full capacity provably restored: with capacity 1, a second begin must
    # still acquire and complete as owner — impossible if the permit leaked.
    monkeypatch.setattr(usage_contract, "_EXECUTOR", None)
    handle2 = usage_contract._begin_fetch(job, lambda p, **kw: _available_snapshot(p), usage_contract._monotonic() + 1.0)
    assert handle2["mode"] == "owner"
    outcome2 = usage_contract._finish_fetch(job, handle2)
    assert outcome2["status"] == "available"


def test_completed_future_callback_keeps_counts_balanced(tmp_path, monkeypatch):
    """ImmediateExecutor returns an already-settled Future, so the done
    callback fires synchronously inside _begin_fetch. The submission counter
    and admission permit must end balanced (parent race review)."""
    from concurrent.futures import Future as _Future

    hermes_home = tmp_path / "fixture-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_fixture(hermes_home, {"openrouter": [{"id": "a", "source": "manual", "access_token": "tok"}]})
    usage_contract._clear_usage_cache_for_tests()

    entry = {"id": "a", "source": "manual", "access_token": "tok"}
    key = usage_contract._cache_identity("openrouter", entry, "tok")
    job = {
        "account": {"account_id": "acct_a"},
        "provider": "openrouter",
        "api_key": "tok",
        "base_url": None,
        "cache_key": key,
        "stale": None,
    }

    class ImmediateExecutor:
        def submit(self, fn, *args, **kwargs):
            future = _Future()
            future.set_result(fn(*args, **kwargs))
            return future

    monkeypatch.setattr(usage_contract, "_EXECUTOR", ImmediateExecutor())
    handle = usage_contract._begin_fetch(job, lambda p, **kw: _available_snapshot(p), usage_contract._monotonic() + 1.0)
    assert handle["mode"] == "owner"
    # The synchronous callback already ran: counter back to 0, permit released.
    assert usage_contract._usage_fetch_stats_for_tests()["in_flight_submitted"] == 0

    outcome = usage_contract._finish_fetch(job, handle)
    assert outcome["status"] == "available"

    stats = usage_contract._usage_fetch_stats_for_tests()
    assert stats["in_flight_submitted"] == 0
    assert stats["registered_flights"] == 0
    # Full admission capacity restored (no leak through the sync callback).
    assert usage_contract._ADMISSION._value == usage_contract._MAX_IN_FLIGHT
    # Result was cached by the owner: a second begin serves it, no refetch.
    handle2 = usage_contract._begin_fetch(job, lambda p, **kw: (_ for _ in ()).throw(AssertionError("refetch")), usage_contract._monotonic() + 1.0)
    assert handle2["mode"] == "done"
    assert handle2["outcome"]["status"] == "available"
