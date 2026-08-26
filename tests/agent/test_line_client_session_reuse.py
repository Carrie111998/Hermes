"""Tests for LINE HTTP connection reuse."""

import sys
import types

import pytest

from plugins.platforms.line.adapter import _LineClient


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.calls = []
        self.__class__.instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse()

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_aiohttp(monkeypatch):
    _FakeSession.instances.clear()
    module = types.SimpleNamespace(
        ClientTimeout=lambda **kwargs: kwargs,
        ClientSession=_FakeSession,
    )
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    return module


@pytest.mark.asyncio
async def test_line_client_reuses_session_and_closes_it(fake_aiohttp):
    client = _LineClient("token")

    await client.reply("reply-1", [])
    await client.push("chat-1", [])
    await client.loading("U-user", seconds=10)

    assert len(_FakeSession.instances) == 1
    session = _FakeSession.instances[0]
    assert len(session.calls) == 3
    assert session.closed is False

    await client.close()

    assert session.closed is True
    assert client._session is None
