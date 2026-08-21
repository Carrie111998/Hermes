"""Regression coverage for silent Streamable HTTP GET loss (#91670).

The MCP SDK retries its optional GET/SSE notification channel internally. If
those retries are exhausted, the GET task returns normally while the POST
writer and Hermes session remain alive. POST-based pings can still succeed, so
Hermes otherwise receives no failure signal and never rebuilds the transport.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.mcp_tool import _HTTPNotificationStreamTracker


class _Response:
    def __init__(self, method: str = "GET", content_type: str = "text/event-stream"):
        self.request = SimpleNamespace(method=method)
        self.headers = {"content-type": content_type}
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


@pytest.mark.asyncio
async def test_unreplaced_notification_stream_close_requests_reconnect():
    reconnect = asyncio.Event()
    tracker = _HTTPNotificationStreamTracker(
        grace_seconds=0.01,
        on_stalled=reconnect.set,
    )
    response = _Response()

    await tracker.response_hook(response)
    await response.aclose()

    await asyncio.wait_for(reconnect.wait(), timeout=0.5)
    assert response.close_count == 1
    tracker.close()


@pytest.mark.asyncio
async def test_replacement_notification_stream_cancels_full_reconnect():
    reconnect = asyncio.Event()
    tracker = _HTTPNotificationStreamTracker(
        grace_seconds=0.05,
        on_stalled=reconnect.set,
    )
    first = _Response()
    replacement = _Response()

    await tracker.response_hook(first)
    await first.aclose()
    await asyncio.sleep(0)
    await tracker.response_hook(replacement)
    await asyncio.sleep(0.1)

    assert not reconnect.is_set()
    tracker.close()


@pytest.mark.asyncio
async def test_non_get_or_non_sse_responses_are_not_tracked():
    reconnect = asyncio.Event()
    tracker = _HTTPNotificationStreamTracker(
        grace_seconds=0.01,
        on_stalled=reconnect.set,
    )
    post = _Response(method="POST")
    json_get = _Response(content_type="application/json")

    await tracker.response_hook(post)
    await tracker.response_hook(json_get)
    await post.aclose()
    await json_get.aclose()
    await asyncio.sleep(0.05)

    assert not reconnect.is_set()
    assert post.close_count == 1
    assert json_get.close_count == 1
    tracker.close()
