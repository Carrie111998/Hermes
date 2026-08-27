"""Tests for bounded HTTP response reads in Discord image/download paths.

Companion to #60122, #60112 (REST body bounding) — extends the same
resource-limiting pattern to image/animation/attachment downloads
in the Discord adapter that were left unbounded.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import plugins.platforms.discord.adapter as discord_adapter
from plugins.platforms.discord.adapter import (
    _read_response_bytes_bounded,
    _DISCORD_IMAGE_DOWNLOAD_MAX_BYTES,
)


class _CaseInsensitiveHeaders(dict[str, str]):
    """Small aiohttp ``CIMultiDict``-like header mapping for the fake response."""

    def __init__(self, values: dict[str, str]):
        super().__init__((str(key).lower(), value) for key, value in values.items())

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(str(key).lower(), default)


class _FakeResponseContent:
    """Deterministic stream that returns one available chunk per read."""

    def __init__(self, chunks: tuple[bytes, ...]):
        self._chunks = chunks
        self._next_index = 0
        self.consumed_chunks = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self._next_index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._next_index]
        self._next_index += 1
        self.consumed_chunks += 1
        return chunk

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        while self._next_index < len(self._chunks):
            yield await self.read()


class _FakeResponse:
    status = 200

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        headers: dict[str, str] | None = None,
        close_error: Exception | None = None,
    ):
        self.headers = _CaseInsensitiveHeaders(headers or {})
        self.content = _FakeResponseContent(chunks)
        self.read_called = False
        self.close_called = 0
        self._close_error = close_error

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def read(self) -> bytes:
        """Model aiohttp's unbounded ``ClientResponse.read()`` behavior."""
        self.read_called = True
        chunks = []
        async for chunk in self.content.iter_chunked(64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self.close_called += 1
        if self._close_error is not None:
            raise self._close_error


class _ReleaseOnlyResponse:
    """Response variant exposing release() but no close()."""

    def __init__(self, chunks: tuple[bytes, ...]):
        self.headers: dict[str, str] = {}
        self.content = _FakeResponseContent(chunks)
        self.release_called = 0

    def release(self) -> None:
        self.release_called += 1


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


_IMAGE_URL = "https://cdn.example.test/image.png"


def _read_url_image(response: _FakeResponse) -> tuple[int, bytes, dict[str, str]]:
    session = _FakeSession(response)
    timeout = object()
    result = asyncio.run(
        discord_adapter._read_url_image_with_redirect_guard(
            session,
            _IMAGE_URL,
            timeout=timeout,
            request_kwargs={},
        )
    )
    assert len(session.calls) == 1
    requested_url, request_kwargs = session.calls[0]
    assert requested_url == _IMAGE_URL
    assert request_kwargs["timeout"] is timeout
    assert request_kwargs["allow_redirects"] is False
    return result


class TestReadResponseBytesBounded:
    def test_reads_all_chunks_within_limit(self):
        resp = _FakeResponse((b"xx", b"yyy"))

        result = asyncio.run(_read_response_bytes_bounded(resp, 6))

        assert result == b"xxyyy"
        assert resp.content.consumed_chunks == 2
        assert resp.close_called == 0

    def test_raises_on_aggregate_overflow(self):
        resp = _FakeResponse((b"xxx", b"y"))

        with pytest.raises(ValueError, match="exceeded 3 bytes"):
            asyncio.run(_read_response_bytes_bounded(resp, 3))

        assert resp.close_called == 1
        assert resp.content.consumed_chunks == 2

    def test_close_failure_does_not_mask_size_error(self):
        resp = _FakeResponse((b"xxxx",), close_error=RuntimeError("close failed"))

        with pytest.raises(ValueError, match="exceeded 3 bytes"):
            asyncio.run(_read_response_bytes_bounded(resp, 3))

        assert resp.close_called == 1

    def test_release_is_used_when_close_is_unavailable(self):
        resp = _ReleaseOnlyResponse((b"xxxx",))

        with pytest.raises(ValueError, match="exceeded 3 bytes"):
            asyncio.run(_read_response_bytes_bounded(resp, 3))

        assert resp.release_called == 1

    def test_exact_limit_passes(self):
        resp = _FakeResponse((b"x" * 2, b"y" * 3))

        result = asyncio.run(_read_response_bytes_bounded(resp, 5))

        assert result == b"xxyyy"
        assert resp.content.consumed_chunks == 2


def test_content_length_over_limit_is_rejected_before_body_consumption(monkeypatch):
    monkeypatch.setattr(discord_adapter, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(discord_adapter, "_DISCORD_IMAGE_DOWNLOAD_MAX_BYTES", 4)
    response = _FakeResponse(
        (b"body must not be consumed",),
        {"Content-Length": "5"},
    )

    with pytest.raises(ValueError, match="exceeded 4 bytes"):
        _read_url_image(response)

    assert response.content.consumed_chunks == 0
    assert response.read_called is False
    assert response.close_called == 1


def test_missing_content_length_rejects_aggregate_overflow(monkeypatch):
    monkeypatch.setattr(discord_adapter, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(discord_adapter, "_DISCORD_IMAGE_DOWNLOAD_MAX_BYTES", 4)
    response = _FakeResponse((b"AA", b"BBB"))

    with pytest.raises(ValueError, match="exceeded 4 bytes"):
        _read_url_image(response)

    assert response.content.consumed_chunks == 2
    assert response.close_called == 1


def test_underreported_content_length_rejects_later_chunk_overflow(monkeypatch):
    monkeypatch.setattr(discord_adapter, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(discord_adapter, "_DISCORD_IMAGE_DOWNLOAD_MAX_BYTES", 4)
    response = _FakeResponse(
        (b"AAA", b"BB"),
        {"Content-Length": "3"},
    )

    with pytest.raises(ValueError, match="exceeded 4 bytes"):
        _read_url_image(response)

    assert response.content.consumed_chunks == 2
    assert response.close_called == 1


def test_multi_chunk_response_at_exact_limit_is_returned_intact(monkeypatch):
    monkeypatch.setattr(discord_adapter, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(discord_adapter, "_DISCORD_IMAGE_DOWNLOAD_MAX_BYTES", 4)
    response = _FakeResponse(
        (b"AA", b"BB"),
        {"Content-Length": "4"},
    )

    status, body, headers = _read_url_image(response)

    assert status == 200
    assert body == b"AABB"
    assert headers["content-length"] == "4"
    assert response.content.consumed_chunks == 2
    assert response.read_called is False
    assert response.close_called == 0


class TestImageDownloadLimits:
    def test_outbound_image_limit_is_50_mib(self):
        assert _DISCORD_IMAGE_DOWNLOAD_MAX_BYTES == 50 * 1024 * 1024
