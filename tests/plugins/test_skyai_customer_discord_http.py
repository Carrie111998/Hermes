from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest

from plugins.skyai_customer import dev_gateway


def rate_limit_error(
    *,
    body: bytes = b'{"retry_after": 0.25}',
    headers: dict[str, str] | None = None,
) -> HTTPError:
    return HTTPError(
        "https://discord.com/api/v10/channels/example",
        429,
        "Too Many Requests",
        headers or {},
        BytesIO(body),
    )


def test_discord_request_honors_json_retry_after(monkeypatch) -> None:
    responses = iter(
        (
            rate_limit_error(),
            BytesIO(b'{"status":"ok"}'),
        )
    )
    sleeps: list[float] = []

    def fake_urlopen(_request, timeout):
        assert timeout == 12
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(dev_gateway, "urlopen", fake_urlopen)
    monkeypatch.setattr(dev_gateway.time, "sleep", sleeps.append)

    assert dev_gateway._discord_json_value_request(
        "GET", "/channels/example", "secret-token"
    ) == {"status": "ok"}
    assert sleeps == [0.25]


def test_discord_request_uses_header_then_caps_retry_after(monkeypatch) -> None:
    responses = iter(
        (
            rate_limit_error(
                body=b"not-json",
                headers={"X-RateLimit-Reset-After": "90"},
            ),
            BytesIO(b"[]"),
        )
    )
    sleeps: list[float] = []

    def fake_urlopen(_request, timeout):
        assert timeout == 12
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(dev_gateway, "urlopen", fake_urlopen)
    monkeypatch.setattr(dev_gateway.time, "sleep", sleeps.append)

    assert dev_gateway._discord_json_value_request(
        "GET", "/channels/example", "secret-token"
    ) == []
    assert sleeps == [dev_gateway.DISCORD_RATE_LIMIT_MAX_SECONDS]


def test_discord_request_stops_after_bounded_rate_limit_attempts(
    monkeypatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_urlopen(_request, timeout):
        nonlocal calls
        assert timeout == 12
        calls += 1
        raise rate_limit_error(body=b"{}")

    monkeypatch.setattr(dev_gateway, "urlopen", fake_urlopen)
    monkeypatch.setattr(dev_gateway.time, "sleep", sleeps.append)

    with pytest.raises(HTTPError) as caught:
        dev_gateway._discord_json_value_request(
            "GET", "/channels/example", "secret-token"
        )

    assert caught.value.code == 429
    assert calls == dev_gateway.DISCORD_RATE_LIMIT_MAX_ATTEMPTS
    assert sleeps == [
        dev_gateway.DISCORD_RATE_LIMIT_DEFAULT_SECONDS
    ] * (dev_gateway.DISCORD_RATE_LIMIT_MAX_ATTEMPTS - 1)


def test_discord_request_does_not_retry_other_http_errors(monkeypatch) -> None:
    sleeps: list[float] = []

    def fake_urlopen(_request, timeout):
        assert timeout == 12
        raise HTTPError(
            "https://discord.com/api/v10/channels/example",
            403,
            "Forbidden",
            {},
            BytesIO(b"{}"),
        )

    monkeypatch.setattr(dev_gateway, "urlopen", fake_urlopen)
    monkeypatch.setattr(dev_gateway.time, "sleep", sleeps.append)

    with pytest.raises(HTTPError) as caught:
        dev_gateway._discord_json_value_request(
            "GET", "/channels/example", "secret-token"
        )

    assert caught.value.code == 403
    assert sleeps == []
