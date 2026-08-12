"""Regression coverage for Discord semantic auto-thread renames.

Relay and native Discord adapters intentionally expose different keyword
contracts.  A shared call once leaked relay-only keywords into the native lane;
the resulting TypeError was swallowed at DEBUG and titles silently stopped
changing.  These tests pin both lane-specific calls and failure visibility.
"""

from __future__ import annotations

import logging
import types

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


_RENAME = GatewayRunner._rename_discord_auto_thread_for_session_title


def _runner(adapter):
    runner = types.SimpleNamespace(adapters={"discord": adapter})
    runner._adapter_for_source = lambda source: adapter
    runner._is_discord_auto_thread_lane = types.MethodType(
        GatewayRunner._is_discord_auto_thread_lane, runner
    )
    runner._is_relay_discord_channel_lane = types.MethodType(
        GatewayRunner._is_relay_discord_channel_lane, runner
    )
    runner._sanitize_discord_thread_title = types.MethodType(
        GatewayRunner._sanitize_discord_thread_title, runner
    )
    runner._relay_auto_thread_info = types.MethodType(
        GatewayRunner._relay_auto_thread_info, runner
    )
    return runner


def _native_source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        auto_thread_created=True,
        auto_thread_initial_name="raw opening message",
    )


@pytest.mark.asyncio
async def test_native_lane_passes_only_the_native_guard_keyword():
    calls = []

    class StrictNativeAdapter:
        async def rename_thread(
            self, thread_id, name, *, only_if_current_name=None
        ):
            calls.append((thread_id, name, only_if_current_name))
            return True

    await _RENAME(
        _runner(StrictNativeAdapter()),
        _native_source(),
        "session-1",
        "Semantic title",
    )

    assert calls == [
        ("thread-1", "Semantic title", "raw opening message")
    ]


@pytest.mark.asyncio
async def test_relay_lane_passes_connector_guard_and_parent_channel():
    calls = []

    class RelayAdapter:
        async def rename_thread(
            self,
            thread_id,
            name,
            *,
            prefer_connector_created=False,
            only_if_current_name=None,
            parent_chat_id=None,
        ):
            calls.append(
                (
                    thread_id,
                    name,
                    prefer_connector_created,
                    only_if_current_name,
                    parent_chat_id,
                )
            )
            return True

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-1",
        chat_type="channel",
        delivered_via_upstream_relay=True,
        prospective_thread_id="thread-2",
    )
    await _RENAME(
        _runner(RelayAdapter()),
        source,
        "session-2",
        "Relay semantic title",
        relay_info=("thread-2", ""),
    )

    assert calls == [
        ("thread-2", "Relay semantic title", True, None, "parent-1")
    ]


@pytest.mark.asyncio
async def test_unexpected_rename_failure_is_logged_at_warning(caplog):
    class BrokenAdapter:
        async def rename_thread(self, thread_id, name, **kwargs):
            raise TypeError("rename_thread() got an unexpected keyword argument")

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await _RENAME(
            _runner(BrokenAdapter()),
            _native_source(),
            "session-1",
            "Semantic title",
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("discord auto-thread rename failed" in message for message in messages)
    assert any("TypeError" in message for message in messages)