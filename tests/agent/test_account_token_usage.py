from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from agent.account_token_usage import (
    account_key_for_agent,
    build_codex_account_usage_report,
    codex_account_identity,
    format_codex_account_usage_report,
)


def _jwt(*, account_id: str | None = None, email: str | None = None, nonce: str = "a") -> str:
    claims: dict = {"nonce": nonce}
    if account_id is not None:
        claims["https://api.openai.com/auth"] = {
            "chatgpt_account_id": account_id,
        }
    if email is not None:
        claims["https://api.openai.com/profile"] = {"email": email}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.signature"


def test_codex_account_identity_groups_rotated_tokens_without_storing_pii():
    first = codex_account_identity(
        _jwt(account_id="acct-123", email="one@example.com", nonce="first")
    )
    rotated = codex_account_identity(
        _jwt(account_id="acct-123", email="one@example.com", nonce="rotated")
    )

    assert first is not None
    assert rotated is not None
    assert first.account_key == rotated.account_key
    assert first.email == "one@example.com"
    assert first.account_key.startswith("openai-codex:")
    assert "acct-123" not in first.account_key
    assert "one@example.com" not in first.account_key


def test_codex_account_identity_distinguishes_accounts_and_rejects_malformed_tokens():
    first = codex_account_identity(_jwt(account_id="acct-123"))
    second = codex_account_identity(_jwt(account_id="acct-456"))

    assert first is not None
    assert second is not None
    assert first.account_key != second.account_key
    assert codex_account_identity("not-a-jwt") is None
    assert codex_account_identity(_jwt(email="missing-account@example.com")) is None
    assert codex_account_identity(_jwt(account_id="   ")) is None


def test_account_key_for_agent_only_attributes_codex_oauth_accounts():
    token = _jwt(account_id="acct-stable", email="person@example.com")
    expected = codex_account_identity(token)

    assert account_key_for_agent(
        SimpleNamespace(provider="openai-codex", api_key=token)
    ) == expected.account_key
    assert account_key_for_agent(
        SimpleNamespace(provider="openrouter", api_key=token)
    ) is None
    assert account_key_for_agent(
        SimpleNamespace(provider="openai-codex", api_key=token),
        request_client=SimpleNamespace(is_moa_client=True),
    ) is None


def test_account_usage_report_deduplicates_pool_slots_and_never_leaks_tokens():
    first_token = _jwt(
        account_id="acct-123", email="one@example.com", nonce="first"
    )
    rotated_token = _jwt(
        account_id="acct-123", email="one@example.com", nonce="rotated"
    )
    identity = codex_account_identity(first_token)
    assert identity is not None
    entries = [
        SimpleNamespace(
            label="wrong-label@example.com",
            access_token=first_token,
            runtime_api_key=first_token,
            runtime_base_url="https://chatgpt.com/backend-api/codex",
        ),
        SimpleNamespace(
            label="openai-codex-oauth-2",
            access_token=rotated_token,
            runtime_api_key=rotated_token,
            runtime_base_url="https://chatgpt.com/backend-api/codex",
        ),
    ]
    local_rows = [{
        "account_key": identity.account_key,
        "billing_provider": "openai-codex",
        "api_call_count": 2,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 20,
        "cache_write_tokens": 0,
        "reasoning_tokens": 2,
        "total_tokens": 35,
        "first_seen": 1.0,
        "last_seen": 2.0,
    }]
    report = build_codex_account_usage_report(
        entries=entries,
        local_rows=local_rows,
    )

    assert len(report["accounts"]) == 1
    account = report["accounts"][0]
    assert account["email"] == "one@example.com"
    assert account["pool_labels"] == [
        "wrong-label@example.com",
        "openai-codex-oauth-2",
    ]
    assert account["local_usage"]["total_tokens"] == 35
    assert report["attribution_scope"]["supported"] == [
        "codex_responses",
        "codex_auxiliary_authoritative",
    ]
    assert "codex_app_server" in report["attribution_scope"]["omitted"]
    output = format_codex_account_usage_report(report)
    assert "app-server, MoA, and background-review calls are omitted" in output
    assert "Local total tokens: 35" in output
    rendered = repr(report)
    assert first_token not in rendered
    assert rotated_token not in rendered
    assert "acct-123" not in rendered


def test_codex_auxiliary_response_carries_anonymous_account_key(monkeypatch):
    from agent.auxiliary_client import _CodexCompletionsAdapter

    token = _jwt(account_id="acct-aux", email="aux@example.com")
    expected = codex_account_identity(token)
    assert expected is not None
    real_client = SimpleNamespace(
        api_key=token,
        base_url="https://chatgpt.com/backend-api/codex",
        responses=SimpleNamespace(create=lambda **_kwargs: iter(())),
    )

    monkeypatch.setattr(
        "agent.codex_runtime._consume_codex_event_stream",
        lambda _stream, *, model, on_event: SimpleNamespace(
            output=[],
            usage=SimpleNamespace(
                input_tokens=30,
                input_tokens_details=SimpleNamespace(cached_tokens=20),
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
                total_tokens=35,
            ),
        ),
    )

    response = _CodexCompletionsAdapter(
        real_client,
        "gpt-5.6-sol",
        account_provider="openai-codex",
    ).create(
        messages=[{"role": "user", "content": "summarize"}],
    )

    assert response._hermes_account_key == expected.account_key
    assert response._hermes_billing_provider == "openai-codex"
    assert response._hermes_billing_base_url == (
        "https://chatgpt.com/backend-api/codex"
    )
    from agent.usage_pricing import normalize_usage

    usage = normalize_usage(response.usage, provider="openai-codex")
    assert usage.input_tokens == 10
    assert usage.cache_read_tokens == 20
    assert usage.output_tokens == 5
    assert usage.reasoning_tokens == 2
    assert usage.total_tokens == 35
    assert token not in repr(response)


def test_generic_codex_adapter_does_not_guess_account_provider(monkeypatch):
    from agent.auxiliary_client import _CodexCompletionsAdapter

    token = _jwt(account_id="acct-custom", email="custom@example.com")
    real_client = SimpleNamespace(
        api_key=token,
        base_url="https://custom.example/v1",
        responses=SimpleNamespace(create=lambda **_kwargs: iter(())),
    )
    monkeypatch.setattr(
        "agent.codex_runtime._consume_codex_event_stream",
        lambda _stream, *, model, on_event: SimpleNamespace(
            output=[],
            usage=SimpleNamespace(input_tokens=4, output_tokens=1, total_tokens=5),
        ),
    )

    response = _CodexCompletionsAdapter(real_client, "custom-model").create(
        messages=[{"role": "user", "content": "summarize"}],
    )

    assert response._hermes_account_key is None
    assert response._hermes_billing_provider is None
