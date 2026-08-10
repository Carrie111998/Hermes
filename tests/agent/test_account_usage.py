from types import SimpleNamespace

import pytest

from agent import account_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payload):
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payload)


@pytest.fixture
def codex_usage_payload():
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 21,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 4,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }


def test_codex_usage_prefers_explicit_live_agent_credentials(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy auth should not be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert snapshot.provider == "openai-codex"
    assert snapshot.plan == "Plus"
    assert [w.label for w in snapshot.windows] == ["Session", "Weekly"]
    assert snapshot.windows[0].used_percent == 21
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer live-agent-token"


def test_codex_usage_falls_back_to_native_credential_pool(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    # Pool fallback fires only on AuthError (the documented "no creds" mode of
    # the resolver), NOT on arbitrary exceptions — see the transient-error guard
    # test below.
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(
            account_usage.AuthError("no singleton auth", provider="openai-codex", code="codex_auth_missing")
        ),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[1].label == "Weekly"
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer pooled-token"
    # Pool creds have no account_id concept — the ChatGPT-Account-Id header must
    # be omitted rather than sent stale/wrong.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]




def test_codex_usage_account_id_read_failure_keeps_singleton_token(monkeypatch, codex_usage_payload):
    """When the resolver succeeds but the separate account_id read raises, the
    working singleton token must still be used (best-effort account_id), NOT
    abandoned in favor of a header-less pool credential."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: {
            "api_key": "singleton-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    monkeypatch.setattr(
        account_usage,
        "_read_codex_tokens",
        lambda *a, **k: (_ for _ in ()).throw(
            account_usage.AuthError("partial store", provider="openai-codex", code="codex_auth_invalid_shape")
        ),
    )

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda provider: (_ for _ in ()).throw(AssertionError("pool must not be consulted")),
    )

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer singleton-token"
    # account_id read failed → header omitted, but the singleton token is kept.
    assert "ChatGPT-Account-Id" not in calls[0]["headers"]




# ── Banked rate-limit reset credits (`/usage reset`) ─────────────────────────


class _FakeResetClient:
    """GET returns the usage payload; POST returns the consume payload."""

    def __init__(self, calls, usage_payload, consume_payload=None):
        self.calls = calls
        self.usage_payload = usage_payload
        self.consume_payload = consume_payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return _FakeResponse(self.usage_payload)

    def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeResponse(self.consume_payload)


def _usage_payload_with_resets(primary_used, secondary_used, banked):
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": primary_used, "reset_at": 1779846359},
            "secondary_window": {"used_percent": secondary_used, "reset_at": 1780230796},
        },
        "rate_limit_reset_credits": {"available_count": banked},
        "credits": {"has_credits": False},
    }
















def test_redeem_missing_credentials_reports_unavailable(monkeypatch):
    monkeypatch.setattr(
        account_usage,
        "_resolve_codex_usage_credentials",
        lambda base_url, api_key: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    result = account_usage.redeem_codex_reset_credit()

    assert result.status == "unavailable"
    assert "hermes auth" in result.message


@pytest.fixture
def kimi_usage_payload():
    return {
        "user": {"userId": "u-1", "membership": {"level": "LEVEL_ADVANCED"}},
        "usage": {
            "limit": "100",
            "used": "77",
            "remaining": "23",
            "resetTime": "2026-08-10T16:51:42.536576Z",
        },
        "limits": [
            {
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {
                    "limit": "100",
                    "used": "7",
                    "remaining": "93",
                    "resetTime": "2026-08-10T02:51:42.536576Z",
                },
            }
        ],
        "parallel": {"limit": 30},
        "authentication": {"method": "METHOD_API_KEY", "scope": "FEATURE_CODING"},
    }


def test_kimi_usage_maps_membership_windows_and_details(monkeypatch, kimi_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, kimi_usage_payload),
    )

    snapshot = account_usage.fetch_account_usage(
        "kimi-coding",
        base_url="https://api.kimi.com/coding",
        api_key="sk-kimi-test",
    )

    assert snapshot is not None
    assert snapshot.provider == "kimi-coding"
    assert snapshot.plan == "Advanced"
    assert snapshot.source == "usage_api"
    assert [w.label for w in snapshot.windows] == ["Weekly", "5 hour"]
    assert snapshot.windows[0].used_percent == 77.0
    assert snapshot.windows[0].reset_at is not None
    assert snapshot.windows[1].used_percent == 7.0
    assert any("30" in detail for detail in snapshot.details)
    assert calls[0]["url"] == "https://api.kimi.com/coding/v1/usages"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-kimi-test"
    assert calls[0]["headers"]["User-Agent"] == "claude-code/0.1.0"
    assert "sk-kimi-test" not in repr(snapshot)


def test_kimi_usage_requires_api_key():
    assert account_usage.fetch_account_usage("kimi-coding", api_key="") is None
    assert (
        account_usage.fetch_account_usage_for_credential("kimi-coding", api_key="") is None
    )


class _TimeoutRecordingClient:
    def __init__(self, record, payload):
        self.record = record
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.record["gets"] += 1
        return _FakeResponse(self.payload)


def test_provider_fetch_phase_timeout_budget_within_deadline(monkeypatch):
    """Assert the CONFIGURED httpx phase timeouts (connect/read/write/pool,
    whichever the scalar applies to per phase) on the contract path stay under
    the 6.5s panel deadline — per request, and in total for multi-request
    fetchers.

    This does NOT prove a wall-clock bound: httpx applies the scalar to each
    phase, a provider call can span several phases/requests, and the Codex
    native-refresh path may block far longer on its own lock. The wall-clock
    bound arrives with M1 (singleflight + process-wide semaphore), tracked as
    a release gate.
    """
    providers_payloads = {
        "openai-codex": {"rate_limit": {}},
        "anthropic": {},
        "openrouter": {"data": {}},
        "kimi-coding": {},
    }
    # Anthropic only queries its OAuth usage endpoint for OAuth-shaped tokens.
    providers_tokens = {"anthropic": "eyJfixture-oauth-token"}
    for provider, payload in providers_payloads.items():
        record = {"timeouts": [], "gets": 0}

        def factory(*args, **kwargs):
            record["timeouts"].append(kwargs.get("timeout", args[0] if args else None))
            return _TimeoutRecordingClient(record, payload)

        monkeypatch.setattr(account_usage.httpx, "Client", factory)
        monkeypatch.setattr(
            account_usage,
            "_resolve_codex_account_id",
            lambda *args, **kwargs: None,
            raising=False,
        )
        account_usage.fetch_account_usage_for_credential(
            provider,
            api_key=providers_tokens.get(provider, "fixture-token"),
            base_url="https://fixture.example/v1",
        )
        assert record["gets"] >= 1, provider
        assert all(0 < timeout <= 6.5 for timeout in record["timeouts"]), (provider, record)
        # Multi-request fetchers (openrouter) must fit the deadline in total
        # configured phase-timeout budget (not a wall-clock guarantee).
        assert sum(record["timeouts"]) <= 6.5, (provider, record)
