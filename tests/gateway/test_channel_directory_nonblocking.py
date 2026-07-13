"""build_channel_directory must not run the synchronous state.db session
query on the asyncio event-loop thread.

Regression guard for the P0 diagnosed 2026-07-13: ``_build_from_sessions``
(→ ``list_gateway_sessions``) full-scans a multi-GB state.db and, when called
directly on the gateway's main loop, froze ``:8642`` ``/health``, the Telegram
long-poll, and every ``:8642`` route for 15-25s under concurrent write load.
The fix offloads each session-discovery call via ``asyncio.to_thread`` so the
loop yields during the query.
"""

import asyncio
import threading
from unittest.mock import patch

from gateway.channel_directory import build_channel_directory
from gateway.platforms.base import Platform


def test_session_discovery_runs_off_event_loop_thread(tmp_path):
    cache_file = tmp_path / "channel_directory.json"

    build_threads = []

    def record_thread(plat_name):
        build_threads.append(threading.get_ident())
        return [{"id": "1", "name": "chat"}]

    async def run():
        loop_thread = threading.get_ident()
        with patch(
            "gateway.channel_directory._build_from_sessions",
            side_effect=record_thread,
        ), patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
            await build_channel_directory({Platform.TELEGRAM: object()})
        return loop_thread

    loop_thread = asyncio.run(run())

    assert build_threads, "_build_from_sessions was never called"
    for tid in build_threads:
        assert tid != loop_thread, (
            "_build_from_sessions ran on the event-loop thread; it must be "
            "offloaded via asyncio.to_thread so the blocking state.db query "
            "cannot freeze /health"
        )


def test_session_discovery_still_populates_directory(tmp_path):
    """Offloading must not change the built result: the connected platform's
    session-discovered targets still land in the directory."""
    cache_file = tmp_path / "channel_directory.json"

    with patch(
        "gateway.channel_directory._build_from_sessions",
        return_value=[{"id": "42", "name": "chat"}],
    ) as mock_sessions, patch(
        "gateway.channel_directory.DIRECTORY_PATH", cache_file
    ):
        directory = asyncio.run(
            build_channel_directory({Platform.TELEGRAM: object()})
        )

    assert directory["platforms"]["telegram"] == [{"id": "42", "name": "chat"}]
    mock_sessions.assert_any_call("telegram")
