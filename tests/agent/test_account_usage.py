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


class _AnthropicUsageClient:
    """Strict usage-only transport: any non-GET request fails the test."""

    def __init__(self, calls, payload=None, *, get_error=None, json_error=None):
        self.calls = calls
        self.payload = payload
        self.get_error = get_error
        self.json_error = json_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers, **kwargs):
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "kwargs": kwargs,
            }
        )
        if self.get_error is not None:
            raise self.get_error
        if self.json_error is not None:
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: (_ for _ in ()).throw(self.json_error),
            )
        return _FakeResponse(self.payload)

    def post(self, *args, **kwargs):
        self.calls.append({"method": "POST", "args": args, "kwargs": kwargs})
        raise AssertionError("Anthropic account usage must never make an inference POST")


def _mock_anthropic_usage(monkeypatch, payload, *, token="cc-synthetic-oauth-token", **client_kwargs):
    calls = []
    monkeypatch.setattr(account_usage, "_resolve_anthropic_token_readonly", lambda: token)
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _AnthropicUsageClient(
            calls,
            payload,
            **client_kwargs,
        ),
    )
    return calls


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


def test_codex_usage_does_not_swap_to_pool_on_transient_resolver_error(monkeypatch, codex_usage_payload):
    """A transient refresh/network failure (non-AuthError) must NOT silently
    downgrade to a possibly-different pool account. It fails open (no snapshot)
    instead of reporting the wrong account's usage."""
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, codex_usage_payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("refresh endpoint 503")),
    )

    pool_entry = SimpleNamespace(
        runtime_api_key="pooled-token-WRONG-ACCOUNT",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(select=lambda: pool_entry)

    import agent.credential_pool as credential_pool

    # If the guard regressed, this pool would be consulted and return a snapshot
    # for the wrong account. It must NOT be.
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: pool)

    snapshot = account_usage.fetch_account_usage("openai-codex")

    assert snapshot is None
    assert calls == []  # HTTP usage endpoint never hit with a wrong-account token


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


def test_codex_usage_treats_wham_used_percent_as_used_not_remaining(monkeypatch):
    """ChatGPT UI says "left"; /wham/usage.used_percent is already used."""
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 85,
                "reset_at": 1779846359,
            },
            "secondary_window": {
                "used_percent": 14,
                "reset_at": 1780230796,
            },
        },
        "credits": {"has_credits": False},
    }
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeClient(calls, payload),
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_codex_runtime_credentials",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("explicit auth should be used")),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    assert [window.used_percent for window in snapshot.windows] == [85, 14]
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot, markdown=True))
    assert "85% used" in rendered
    assert "14% used" in rendered
    assert "15% used" not in rendered
    assert "86% used" not in rendered


def test_anthropic_usage_preserves_decimal_percentages_in_secret_safe_get(
    monkeypatch, caplog, capsys
):
    token = "cc-synthetic-oauth-token-never-real"
    calls = _mock_anthropic_usage(
        monkeypatch,
        {
            "five_hour": {
                "utilization": 0.05,
                "resets_at": "2026-07-26T20:00:00Z",
            },
            "seven_day": {"utilization": 0.5},
            "seven_day_opus": {"utilization": 80.0},
            "seven_day_sonnet": {"utilization": 100.0},
        },
        token=token,
    )

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.available
    assert snapshot.provider == "anthropic"
    assert snapshot.source == "oauth_usage_api"
    assert [window.label for window in snapshot.windows] == [
        "Current session",
        "Current week",
        "Opus week",
        "Sonnet week",
    ]
    assert [window.used_percent for window in snapshot.windows] == [
        0.05,
        0.5,
        80.0,
        100.0,
    ]
    assert snapshot.windows[0].reset_at is not None
    assert calls == [
        {
            "method": "GET",
            "url": "https://api.anthropic.com/api/oauth/usage",
            "headers": {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
            "kwargs": {},
        }
    ]
    assert token not in caplog.text
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert token not in repr(snapshot)


@pytest.mark.parametrize(
    "invalid_utilization",
    [
        True,
        False,
        -0.01,
        101,
        10**400,
        float("nan"),
        float("inf"),
        float("-inf"),
        "0.5",
    ],
)
def test_anthropic_usage_rejects_invalid_or_non_finite_percentages(
    monkeypatch, invalid_utilization
):
    calls = _mock_anthropic_usage(
        monkeypatch,
        {"seven_day": {"utilization": invalid_utilization}},
    )

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert snapshot is not None
    assert snapshot.windows == ()
    assert not snapshot.available
    assert [call["method"] for call in calls] == ["GET"]


def test_anthropic_usage_missing_token_makes_no_request(monkeypatch):
    monkeypatch.setattr(account_usage, "_resolve_anthropic_token_readonly", lambda: None)
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing credentials must not make a request")
        ),
    )

    assert account_usage.fetch_account_usage("anthropic") is None


def test_anthropic_usage_api_key_is_never_used_for_subscription_query(monkeypatch):
    token = "sk-ant-api03-synthetic-never-real"
    monkeypatch.setattr(account_usage, "_resolve_anthropic_token_readonly", lambda: token)
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("API keys must not be sent to the OAuth usage endpoint")
        ),
    )

    snapshot = account_usage.fetch_account_usage("anthropic")

    assert snapshot is not None
    assert not snapshot.available
    assert snapshot.unavailable_reason
    assert token not in snapshot.unavailable_reason
    assert token not in repr(snapshot)


def test_anthropic_usage_http_failure_fails_closed(monkeypatch):
    calls = _mock_anthropic_usage(
        monkeypatch,
        None,
        get_error=account_usage.httpx.TimeoutException("synthetic timeout"),
    )

    assert account_usage.fetch_account_usage("anthropic") is None
    assert [call["method"] for call in calls] == ["GET"]


@pytest.mark.parametrize(
    "payload",
    [
        [{"unexpected": "array"}],
        {"seven_day": ["unexpected", "array"]},
    ],
)
def test_anthropic_usage_malformed_payload_fails_closed(monkeypatch, payload):
    calls = _mock_anthropic_usage(monkeypatch, payload)

    assert account_usage.fetch_account_usage("anthropic") is None
    assert [call["method"] for call in calls] == ["GET"]


def test_anthropic_usage_json_failure_fails_closed(monkeypatch):
    calls = _mock_anthropic_usage(
        monkeypatch,
        None,
        json_error=ValueError("synthetic invalid JSON"),
    )

    assert account_usage.fetch_account_usage("anthropic") is None
    assert [call["method"] for call in calls] == ["GET"]


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


def test_usage_snapshot_shows_banked_resets_hint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(21, 4, 2)),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot))
    assert "You have 2 resets banked - use /usage reset to activate" in rendered


def test_usage_snapshot_hides_reset_hint_when_none_banked(monkeypatch, codex_usage_payload):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, codex_usage_payload),
    )

    snapshot = account_usage.fetch_account_usage(
        "openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert snapshot is not None
    rendered = "\n".join(account_usage.render_account_usage_lines(snapshot))
    assert "banked" not in rendered


def test_redeem_blocked_when_limits_not_exhausted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(60, 30, 2)),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "not_exhausted"
    assert not result.redeemed
    assert "--force" in result.message
    assert "60% used" in result.message
    assert result.available_count == 2
    # The consume endpoint must never be hit — the credit is protected.
    assert [c["method"] for c in calls] == ["GET"]


def test_redeem_force_bypasses_exhaustion_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(60, 30, 2),
            consume_payload={"code": "reset", "windows_reset": 2},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
        force=True,
    )

    assert result.redeemed
    assert result.windows_reset == 2
    assert result.available_count == 1  # 2 banked - 1 spent
    assert "1 banked reset remaining" in result.message
    post = [c for c in calls if c["method"] == "POST"][0]
    assert post["url"] == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
    assert post["json"]["redeem_request_id"]  # idempotency key present
    assert "credit_id" not in post["json"]


def test_redeem_allowed_without_force_when_window_exhausted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(100, 42, 1),
            consume_payload={"code": "reset", "windows_reset": 2},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.redeemed
    assert result.available_count == 0
    assert "0 banked resets remaining" in result.message


def test_redeem_refuses_when_no_credits_banked(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(calls, _usage_payload_with_resets(100, 100, 0)),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "no_credits_banked"
    assert [c["method"] for c in calls] == ["GET"]


def test_redeem_nothing_to_reset_reports_credit_not_spent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage.httpx,
        "Client",
        lambda timeout: _FakeResetClient(
            calls,
            _usage_payload_with_resets(100, 100, 3),
            consume_payload={"code": "nothing_to_reset"},
        ),
    )

    result = account_usage.redeem_codex_reset_credit(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="live-agent-token",
    )

    assert result.status == "nothing_to_reset"
    assert not result.redeemed
    assert "NOT spent" in result.message
    assert result.available_count == 3


def test_redeem_missing_credentials_reports_unavailable(monkeypatch):
    monkeypatch.setattr(
        account_usage,
        "_resolve_codex_usage_credentials",
        lambda base_url, api_key: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    result = account_usage.redeem_codex_reset_credit()

    assert result.status == "unavailable"
    assert "hermes auth" in result.message


# ── Readonly resolver hardening tests (no refresh/writes/pool persistence) ───


def test_readonly_resolver_env_var_no_writes(monkeypatch):
    """Verify env var path returns token without any write side effects.

    Spies on exact module-bound forms of the three writer functions.
    """
    import hermes_cli.auth as auth_mod
    from agent import credential_pool, anthropic_adapter

    monkeypatch.setenv("ANTHROPIC_TOKEN", "oauth-env-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    # Explicit zero-call spies on exact module-bound writer forms.
    write_pool_calls = []
    persist_calls = []
    write_creds_calls = []

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))

    def spy_persist(self, *args, **kwargs):
        persist_calls.append((self, args, kwargs))

    def spy_write_creds(*args, **kwargs):
        write_creds_calls.append((args, kwargs))

    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)
    monkeypatch.setattr(credential_pool.CredentialPool, "_persist", spy_persist)
    monkeypatch.setattr(anthropic_adapter, "_write_claude_code_credentials", spy_write_creds)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token == "oauth-env-token"
    assert write_pool_calls == [], "write_credential_pool must not be called"
    assert persist_calls == [], "CredentialPool._persist must not be called"
    assert write_creds_calls == [], "_write_claude_code_credentials must not be called"


def test_readonly_resolver_no_refresh_on_claude_code_creds(monkeypatch):
    """Verify Claude Code credentials path never calls refresh or writes.

    Spies on exact module-bound forms of all writer functions.
    """
    from agent import anthropic_adapter, credential_pool
    import hermes_cli.auth as auth_mod

    creds = {"accessToken": "token", "refreshToken": "refresh"}

    monkeypatch.setattr(
        anthropic_adapter,
        "read_claude_code_credentials",
        lambda: creds,
    )
    monkeypatch.setattr(
        anthropic_adapter,
        "is_claude_code_token_valid",
        lambda c: True,
    )
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Spies on refresh and exact module-bound writer forms.
    refresh_calls = []
    write_pool_calls = []
    persist_calls = []
    write_creds_calls = []

    def spy_refresh(c):
        refresh_calls.append(c)

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))

    def spy_persist(self, *args, **kwargs):
        persist_calls.append((self, args, kwargs))

    def spy_write_creds(*args, **kwargs):
        write_creds_calls.append((args, kwargs))

    monkeypatch.setattr(anthropic_adapter, "_refresh_oauth_token", spy_refresh)
    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)
    monkeypatch.setattr(credential_pool.CredentialPool, "_persist", spy_persist)
    monkeypatch.setattr(anthropic_adapter, "_write_claude_code_credentials", spy_write_creds)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token == "token"
    assert refresh_calls == [], "Refresh must not be called"
    assert write_pool_calls == [], "write_credential_pool must not be called"
    assert persist_calls == [], "CredentialPool._persist must not be called"
    assert write_creds_calls == [], "_write_claude_code_credentials must not be called"


def test_readonly_resolver_expired_token_mutation_sensitive(monkeypatch):
    """Verify expired token returns None without refresh (mutation-sensitive test).

    FAILS if someone reintroduces _refresh_oauth_token or any writer into resolver.
    Spies on exact module-bound forms of all writer functions.
    """
    from agent import anthropic_adapter, credential_pool
    import hermes_cli.auth as auth_mod

    creds = {"accessToken": "expired", "refreshToken": "refresh"}

    monkeypatch.setattr(
        anthropic_adapter,
        "read_claude_code_credentials",
        lambda: creds,
    )
    monkeypatch.setattr(
        anthropic_adapter,
        "is_claude_code_token_valid",
        lambda c: False,  # Expired
    )
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # MUTATION-SENSITIVE: spy on all write paths (exact module-bound forms).
    refresh_calls = []
    write_pool_calls = []
    persist_calls = []
    write_creds_calls = []

    def spy_refresh(c):
        refresh_calls.append(c)
        raise AssertionError("_refresh_oauth_token must not be called")

    def spy_write_pool(*args, **kwargs):
        write_pool_calls.append((args, kwargs))
        raise AssertionError("write_credential_pool must not be called")

    def spy_persist(self, *args, **kwargs):
        persist_calls.append((self, args, kwargs))
        raise AssertionError("CredentialPool._persist must not be called")

    def spy_write_creds(*args, **kwargs):
        write_creds_calls.append((args, kwargs))
        raise AssertionError("_write_claude_code_credentials must not be called")

    monkeypatch.setattr(anthropic_adapter, "_refresh_oauth_token", spy_refresh)
    monkeypatch.setattr(auth_mod, "write_credential_pool", spy_write_pool)
    monkeypatch.setattr(credential_pool.CredentialPool, "_persist", spy_persist)
    monkeypatch.setattr(anthropic_adapter, "_write_claude_code_credentials", spy_write_creds)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token is None, "Expired token must return None"
    assert refresh_calls == [], "Refresh must not be called (MUTATION-SENSITIVE)"
    assert write_pool_calls == [], "write_credential_pool must not be called (MUTATION-SENSITIVE)"
    assert persist_calls == [], "CredentialPool._persist must not be called (MUTATION-SENSITIVE)"
    assert write_creds_calls == [], "_write_claude_code_credentials must not be called (MUTATION-SENSITIVE)"


def test_readonly_resolver_pool_direct_read_no_load_pool(monkeypatch):
    """Verify pool is read directly, not via load_pool() which can write."""
    import hermes_cli.auth as auth_mod
    from agent import credential_pool, anthropic_adapter

    # Clear all env vars to reach pool read path.
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Mock Claude Code credentials to return None.
    monkeypatch.setattr(
        anthropic_adapter,
        "read_claude_code_credentials",
        lambda: None,
    )

    # Mock direct pool read to simulate pool entries.
    pool_entries = [
        {
            "provider": "anthropic",
            "id": "id1",
            "label": "test",
            "auth_type": "oauth",
            "priority": 0,
            "source": "manual",
            "access_token": "pool-oauth-token",
        }
    ]

    monkeypatch.setattr(
        account_usage,
        "_read_credential_pool_direct",
        lambda provider: pool_entries if provider == "anthropic" else [],
    )

    # Spy on load_pool to ensure it's NOT called.
    load_pool_calls = []

    def spy_load_pool(provider):
        load_pool_calls.append(provider)
        raise AssertionError("load_pool must not be called")

    monkeypatch.setattr(credential_pool, "load_pool", spy_load_pool)

    token = account_usage._resolve_anthropic_token_readonly()

    assert token == "pool-oauth-token"
    assert load_pool_calls == [], "load_pool must not be called (direct read only)"


def test_readonly_pool_direct_read_no_backup_on_corrupt(monkeypatch, tmp_path):
    """Verify _read_credential_pool_direct fails-closed on corrupt JSON (no backup write)."""
    import json
    from hermes_constants import get_hermes_home as real_get_hermes_home

    # Mock get_hermes_home to use tmp_path.
    corrupt_auth = tmp_path / "auth.json"
    corrupt_auth.write_bytes(b"{ corrupt json }")

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: tmp_path,
    )

    result = account_usage._read_credential_pool_direct("anthropic")

    assert result == [], "Corrupt JSON must return empty list (fail-closed)"
    # Verify no backup file was created.
    backup_files = list(tmp_path.glob("*.backup*")) + list(tmp_path.glob("*~"))
    assert backup_files == [], "No backup files must be created on corrupt parse"


def test_anthropic_usage_fetch_uses_readonly_resolver(monkeypatch):
    """Verify _fetch_anthropic_account_usage uses readonly resolver, not full resolver."""
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Track which resolver is called.
    readonly_calls = []
    full_calls = []

    def spy_readonly():
        readonly_calls.append(True)
        return "readonly-token"

    def spy_full():
        full_calls.append(True)
        raise AssertionError("Full resolver must not be called")

    monkeypatch.setattr(
        account_usage,
        "_resolve_anthropic_token_readonly",
        spy_readonly,
    )
    monkeypatch.setattr(
        account_usage,
        "resolve_anthropic_token",
        spy_full,
    )
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda t: True)

    # Mock HTTP response.
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def get(self, url, headers):
            from types import SimpleNamespace

            return SimpleNamespace(
                json=lambda: {"seven_day": {"utilization": 0.05}},
                raise_for_status=lambda: None,
            )

    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: FakeClient())

    snapshot = account_usage._fetch_anthropic_account_usage()

    assert readonly_calls == [True], "Readonly resolver must be called"
    assert full_calls == [], "Full resolver must not be called"
    assert snapshot is not None
