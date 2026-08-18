"""Exercise opt-in Codex attribution through real SDK request construction."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from hermes_cli import __version__
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


CODEX_URL = "https://chatgpt.com/backend-api/codex"
MODEL = "gpt-5.4"


def _jwt(account_id="acct-attribution-test"):
    payload = json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"e30.{encoded}.test-signature"


@pytest.fixture
def configure_attribution(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)

    def configure(enabled):
        (home / "config.yaml").write_text(
            yaml.safe_dump({
                "telemetry": {"usage_attribution": {"enabled": enabled}},
            }),
            encoding="utf-8",
        )

    try:
        yield configure
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def wire(configure_attribution, monkeypatch):
    """Replace only HTTP transports; use Hermes routing and the real SDK."""
    from agent import auxiliary_client
    from run_agent import AIAgent

    requests = []
    response = {
        "id": "resp_attribution_test",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": MODEL,
        "output": [{
            "type": "message",
            "id": "msg_test",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }],
    }

    def respond(request):
        requests.append(request)
        if json.loads(request.content).get("stream"):
            events = [
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": response["output"][0],
                },
                {"type": "response.completed", "response": response},
            ]
            content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=content + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json=response)

    def http_client(*_args, async_mode=False, **_kwargs):
        cls = httpx.AsyncClient if async_mode else httpx.Client
        return cls(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(
        auxiliary_client, "_openai_http_client_kwargs",
        lambda _url, *, async_mode=False: {
            "http_client": http_client(async_mode=async_mode),
        },
    )
    monkeypatch.setattr(AIAgent, "_build_keepalive_http_client", staticmethod(http_client))
    return requests


def _assert_identity(request, enabled, account_id="acct-attribution-test"):
    assert request.headers["originator"] == ("hermes-agent" if enabled else "codex_cli_rs")
    expected_ua = (
        f"HermesAgent/{__version__}"
        if enabled else "codex_cli_rs/0.0.0 (Hermes Agent)"
    )
    assert request.headers["user-agent"] == expected_ua
    assert request.headers["chatgpt-account-id"] == account_id
    assert "extra_headers" not in json.loads(request.content)


@pytest.mark.parametrize("enabled", [False, True])
def test_header_policy_preserves_account_id(configure_attribution, enabled):
    from agent.auxiliary_client import _codex_cloudflare_headers

    configure_attribution(enabled)
    headers = _codex_cloudflare_headers(_jwt())

    assert headers["originator"] == ("hermes-agent" if enabled else "codex_cli_rs")
    assert headers["ChatGPT-Account-ID"] == "acct-attribution-test"
    assert "ChatGPT-Account-ID" not in _codex_cloudflare_headers("not-a-jwt")


@pytest.mark.parametrize(
    ("base_url", "attributed"),
    [
        (CODEX_URL, True),
        (CODEX_URL + "/", True),
        (CODEX_URL + "/responses", True),
        ("https://CHATGPT.COM:443/backend-api/codex", True),
        ("http://chatgpt.com/backend-api/codex", False),
        ("https://chatgpt.com:8443/backend-api/codex", False),
        ("https://api.openai.com/v1", False),
        ("https://proxy.example/backend-api/codex", False),
        ("https://chatgpt.com.example/backend-api/codex", False),
        ("https://subdomain.chatgpt.com/backend-api/codex", False),
        ("https://chatgpt.com/backend-api/codex-other", False),
        ("https://chatgpt.com/backend-api/other", False),
        ("https://chatgpt.com:invalid/backend-api/codex", False),
    ],
)
def test_new_identity_is_limited_to_the_official_endpoint(
    configure_attribution, base_url, attributed,
):
    from agent.auxiliary_client import _codex_cloudflare_headers

    configure_attribution(True)
    headers = _codex_cloudflare_headers(_jwt(), base_url=base_url)

    assert headers["originator"] == ("hermes-agent" if attributed else "codex_cli_rs")


@pytest.mark.parametrize("enabled", [False, True])
def test_primary_client_and_credential_rebuild_send_expected_headers(
    configure_attribution, wire, enabled,
):
    from run_agent import AIAgent

    configure_attribution(enabled)
    agent = AIAgent(
        api_key=_jwt(),
        base_url=CODEX_URL,
        provider="openai-codex",
        model=MODEL,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    clients = [agent.client]
    try:
        agent.client.responses.create(model=MODEL, input="test")
        _assert_identity(wire[-1], enabled)

        agent._client_kwargs["api_key"] = _jwt("acct-rotated")
        agent._apply_client_headers_for_base_url(CODEX_URL)
        assert agent._replace_primary_openai_client(reason="attribution-test")
        clients.append(agent.client)
        agent.client.responses.create(model=MODEL, input="test")
        _assert_identity(wire[-1], enabled, "acct-rotated")

        direct_url = "https://api.openai.com/v1"
        agent._client_kwargs.update(api_key="test-direct-key", base_url=direct_url)
        agent._apply_client_headers_for_base_url(direct_url)
        assert agent._replace_primary_openai_client(reason="attribution-route-change")
        clients.append(agent.client)
        agent.client.responses.create(model=MODEL, input="test")
        assert "originator" not in wire[-1].headers
        assert "chatgpt-account-id" not in wire[-1].headers
        assert not wire[-1].headers["user-agent"].startswith("HermesAgent/")
    finally:
        for client in clients:
            client.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_auxiliary_raw_and_async_clients_send_expected_headers(
    configure_attribution, wire, monkeypatch, enabled,
):
    from agent import auxiliary_client

    configure_attribution(enabled)
    monkeypatch.setattr(auxiliary_client, "_select_pool_entry", lambda _p: (False, None))
    monkeypatch.setattr(auxiliary_client, "_read_codex_access_token", _jwt)

    wrapped, model = auxiliary_client._build_codex_client(MODEL)
    raw, raw_model = auxiliary_client.resolve_provider_client(
        "openai-codex", model=MODEL, raw_codex=True,
    )
    try:
        result = wrapped.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "test"}],
        )
        assert result.choices[0].message.content == "ok"
        _assert_identity(wire[-1], enabled)

        raw.responses.create(model=raw_model, input="test")
        _assert_identity(wire[-1], enabled)

        async def send_async():
            async_wrapped, _ = auxiliary_client._to_async_client(wrapped, model)
            result = await async_wrapped.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "test"}],
            )
            assert result.choices[0].message.content == "ok"
            _assert_identity(wire[-1], enabled)

            async_raw, _ = auxiliary_client._to_async_client(raw, raw_model)
            try:
                await async_raw.responses.create(model=raw_model, input="test")
                _assert_identity(wire[-1], enabled)
            finally:
                await async_raw.close()

        asyncio.run(send_async())
    finally:
        wrapped.close()
        raw.close()


def test_credential_pool_custom_endpoint_keeps_existing_identity(
    configure_attribution, wire, monkeypatch,
):
    from agent import auxiliary_client

    configure_attribution(True)
    entry = SimpleNamespace(
        runtime_api_key=_jwt(),
        runtime_base_url="https://proxy.example/backend-api/codex",
    )
    monkeypatch.setattr(auxiliary_client, "_select_pool_entry", lambda _p: (True, entry))

    client, model = auxiliary_client._build_codex_client(MODEL)
    try:
        client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "test"}],
        )
        assert wire[-1].url.host == "proxy.example"
        _assert_identity(wire[-1], False)
    finally:
        client.close()


def test_disabling_attribution_restores_compatibility_for_new_clients(
    configure_attribution, wire, monkeypatch,
):
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, "_read_codex_access_token", _jwt)
    configure_attribution(True)
    old, _ = auxiliary_client.resolve_provider_client(
        "openai-codex", model=MODEL, raw_codex=True,
    )
    configure_attribution(False)
    new, _ = auxiliary_client.resolve_provider_client(
        "openai-codex", model=MODEL, raw_codex=True,
    )
    try:
        old.responses.create(model=MODEL, input="test")
        _assert_identity(wire[-1], True)
        new.responses.create(model=MODEL, input="test")
        _assert_identity(wire[-1], False)
    finally:
        old.close()
        new.close()
