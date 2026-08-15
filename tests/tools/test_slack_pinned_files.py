"""Turn-scoped Slack pinned-file download tool tests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.session_context import (
    clear_session_vars,
    get_slack_pinned_file_ids,
    reset_session_vars,
    set_session_vars,
    set_slack_pinned_file_ids,
    reset_slack_pinned_file_ids,
)
from tools.slack_pinned_files import download_pinned_slack_file
from toolsets import resolve_toolset


@pytest.fixture(autouse=True)
def _clean_session_context():
    reset_session_vars()
    yield
    reset_session_vars()


@pytest.mark.asyncio
async def test_current_turn_pinned_file_downloads_through_live_adapter():
    adapter = SimpleNamespace(
        download_pinned_file=AsyncMock(
            return_value={
                "path": "/tmp/war-and-peace.txt",
                "name": "war-and-peace.txt",
                "mimetype": "text/plain",
            }
        )
    )
    runner = SimpleNamespace(adapters={Platform.SLACK: adapter})
    session_tokens = set_session_vars(
        platform="slack", chat_id="C1", scope_id="T1", session_key="session-1"
    )
    pin_token = set_slack_pinned_file_ids(["F0BQBQ4MVJR"])
    try:
        with patch("gateway.run._gateway_runner_ref", return_value=runner):
            result = json.loads(await download_pinned_slack_file("F0BQBQ4MVJR"))
    finally:
        reset_slack_pinned_file_ids(pin_token)
        clear_session_vars(session_tokens)

    assert result == {
        "success": True,
        "path": "/tmp/war-and-peace.txt",
        "name": "war-and-peace.txt",
        "mimetype": "text/plain",
    }
    adapter.download_pinned_file.assert_awaited_once_with(
        file_id="F0BQBQ4MVJR", channel_id="C1", team_id="T1"
    )


@pytest.mark.asyncio
async def test_unlisted_file_id_is_rejected_before_adapter_call():
    adapter = SimpleNamespace(download_pinned_file=AsyncMock())
    runner = SimpleNamespace(adapters={Platform.SLACK: adapter})
    session_tokens = set_session_vars(
        platform="slack", chat_id="C1", scope_id="T1", session_key="session-1"
    )
    pin_token = set_slack_pinned_file_ids(["F_ALLOWED"])
    try:
        with patch("gateway.run._gateway_runner_ref", return_value=runner):
            result = json.loads(await download_pinned_slack_file("F_OTHER"))
    finally:
        reset_slack_pinned_file_ids(pin_token)
        clear_session_vars(session_tokens)

    assert result["success"] is False
    assert "not attached to a pinned message" in result["error"]
    adapter.download_pinned_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_failure_is_returned_explicitly_without_path():
    adapter = SimpleNamespace(
        download_pinned_file=AsyncMock(side_effect=RuntimeError("file unavailable"))
    )
    runner = SimpleNamespace(adapters={Platform.SLACK: adapter})
    session_tokens = set_session_vars(
        platform="slack", chat_id="C1", scope_id="T1", session_key="session-1"
    )
    pin_token = set_slack_pinned_file_ids(["F_ALLOWED"])
    try:
        with patch("gateway.run._gateway_runner_ref", return_value=runner):
            result = json.loads(await download_pinned_slack_file("F_ALLOWED"))
    finally:
        reset_slack_pinned_file_ids(pin_token)
        clear_session_vars(session_tokens)

    assert result["success"] is False
    assert "file unavailable" in result["error"]
    assert "path" not in result


@pytest.mark.asyncio
async def test_prior_turn_file_id_is_rejected_after_context_reset():
    session_tokens = set_session_vars(
        platform="slack", chat_id="C1", scope_id="T1", session_key="session-1"
    )
    pin_token = set_slack_pinned_file_ids(["F_OLD"])
    reset_slack_pinned_file_ids(pin_token)
    try:
        result = json.loads(await download_pinned_slack_file("F_OLD"))
    finally:
        clear_session_vars(session_tokens)

    assert result["success"] is False
    assert "not attached to a pinned message" in result["error"]


@pytest.mark.asyncio
async def test_outliving_child_context_is_revoked_when_turn_ends():
    child_started = asyncio.Event()
    read_after_reset = asyncio.Event()

    async def inherited_child():
        child_started.set()
        await read_after_reset.wait()
        return get_slack_pinned_file_ids()

    pin_token = set_slack_pinned_file_ids(["F_CURRENT"])
    task = asyncio.create_task(inherited_child())
    await child_started.wait()
    reset_slack_pinned_file_ids(pin_token)
    read_after_reset.set()

    assert await task == frozenset()


@pytest.mark.asyncio
async def test_concurrent_turns_reject_each_others_file_ids():
    adapter = SimpleNamespace(download_pinned_file=AsyncMock())
    runner = SimpleNamespace(adapters={Platform.SLACK: adapter})
    both_ready = asyncio.Event()
    ready_count = 0

    async def run_turn(channel_id, own_file_id, other_file_id):
        nonlocal ready_count
        session_tokens = set_session_vars(
            platform="slack",
            chat_id=channel_id,
            scope_id="T1",
            session_key=f"session-{channel_id}",
        )
        pin_token = set_slack_pinned_file_ids([own_file_id])
        try:
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            await both_ready.wait()
            return json.loads(await download_pinned_slack_file(other_file_id))
        finally:
            reset_slack_pinned_file_ids(pin_token)
            clear_session_vars(session_tokens)

    with patch("gateway.run._gateway_runner_ref", return_value=runner):
        first, second = await asyncio.gather(
            run_turn("C1", "F_C1", "F_C2"),
            run_turn("C2", "F_C2", "F_C1"),
        )

    assert first["success"] is False
    assert second["success"] is False
    assert "not attached to a pinned message" in first["error"]
    assert "not attached to a pinned message" in second["error"]
    adapter.download_pinned_file.assert_not_awaited()


def test_download_tool_is_only_in_slack_platform_toolset():
    assert "slack_download_pinned_file" in resolve_toolset("hermes-slack")
    assert "slack_download_pinned_file" not in resolve_toolset("hermes-telegram")
    assert "slack_download_pinned_file" not in resolve_toolset("hermes-discord")


@pytest.mark.parametrize(
    "config",
    [{}, {"platform_toolsets": {"slack": ["web"]}}],
)
def test_download_toolset_is_enabled_in_real_slack_platform_resolution(config):
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import discover_builtin_tools

    discover_builtin_tools()
    enabled_toolsets = _get_platform_tools(config, "slack")

    assert "slack_pinned_files" in enabled_toolsets

    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(
        enabled_toolsets=sorted(enabled_toolsets),
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {definition["function"]["name"] for definition in definitions}
    assert "slack_download_pinned_file" in names
