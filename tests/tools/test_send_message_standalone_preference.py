"""Regression coverage for cron standalone delivery with a live gateway present."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform
from hermes_cli.plugins import discover_plugins
from gateway.platform_registry import platform_registry
from cron.scheduler import _deliver_result
from tools.send_message_tool import _send_to_platform


def test_force_standalone_bypasses_loop_bound_live_plugin_adapter():
    """A cron fallback loop must not await a gateway adapter bound to another loop."""
    discover_plugins()
    entry = platform_registry.get("mattermost")
    assert entry is not None

    standalone = AsyncMock(
        return_value={
            "success": True,
            "platform": "mattermost",
            "message_id": "standalone-post",
        }
    )
    live_adapter = MagicMock()
    live_adapter.send = AsyncMock(
        side_effect=RuntimeError("Timeout context manager should be used inside a task")
    )
    runner = SimpleNamespace(adapters={Platform.MATTERMOST: live_adapter})
    pconfig = SimpleNamespace(enabled=True, token="test-token", extra={"url": "https://mm.invalid"})

    original = entry.standalone_sender_fn
    entry.standalone_sender_fn = standalone
    try:
        with patch("gateway.run._gateway_runner_ref", return_value=runner):
            result = asyncio.run(
                _send_to_platform(
                    Platform.MATTERMOST,
                    pconfig,
                    "channel-id",
                    "cron result",
                    thread_id="root-post",
                    force_standalone=True,
                )
            )
    finally:
        entry.standalone_sender_fn = original

    assert result["success"] is True
    live_adapter.send.assert_not_awaited()
    standalone.assert_awaited_once_with(
        pconfig,
        "channel-id",
        "cron result",
        thread_id="root-post",
        media_files=[],
        force_document=False,
    )


def test_cron_standalone_delivery_requests_standalone_sender():
    """The scheduler must declare its fresh-loop delivery mode to the sender."""
    pconfig = SimpleNamespace(enabled=True, token="test-token", extra={})
    config = SimpleNamespace(platforms={Platform.MATTERMOST: pconfig})
    send = AsyncMock(return_value={"success": True, "message_id": "post-id"})
    job = {
        "id": "price-monitor",
        "name": "provider-price-monitor",
        "deliver": "origin",
        "origin": {
            "platform": "mattermost",
            "chat_id": "channel-id",
            "thread_id": "root-post",
        },
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("tools.send_message_tool._send_to_platform", new=send),
    ):
        error = _deliver_result(job, "audit complete")

    assert error is None
    assert send.await_args is not None
    assert send.await_args.kwargs["force_standalone"] is True
