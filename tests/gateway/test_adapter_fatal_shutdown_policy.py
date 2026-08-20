"""Regression tests for all-messaging-adapters-disconnected policy."""

from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _runner_with_failed_telegram(tmp_path):
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-token")
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.fatal_error_code = "telegram_network_error"
    adapter.fatal_error_message = "Telegram unavailable"
    adapter.fatal_error_retryable = False
    adapter.disconnect = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.delivery_router.adapters = runner.adapters
    runner.stop = AsyncMock()
    return runner, adapter


@pytest.mark.asyncio
async def test_all_messaging_down_keeps_gateway_alive_for_active_local_turn(tmp_path):
    runner, adapter = _runner_with_failed_telegram(tmp_path)
    runner._running_agents["agent:main:local:terminal"] = object()
    runner._session_sources = OrderedDict(
        {
            "agent:main:local:terminal": SessionSource(
                platform=Platform.LOCAL,
                chat_id="terminal",
            )
        }
    )

    await runner._handle_adapter_fatal_error_impl(adapter)

    runner.stop.assert_not_awaited()
    assert Platform.TELEGRAM in runner._failed_platforms


@pytest.mark.asyncio
async def test_exit_policy_false_keeps_headless_gateway_alive_and_retries(tmp_path):
    runner, adapter = _runner_with_failed_telegram(tmp_path)

    with patch(
        "gateway.run._load_gateway_config",
        return_value={
            "gateway": {"messaging_platform_exit_on_all_disconnected": False}
        },
    ):
        await runner._handle_adapter_fatal_error_impl(adapter)

    runner.stop.assert_not_awaited()
    assert Platform.TELEGRAM in runner._failed_platforms


@pytest.mark.asyncio
async def test_default_exit_policy_preserves_headless_restart_behavior(tmp_path):
    runner, adapter = _runner_with_failed_telegram(tmp_path)

    with patch("gateway.run._load_gateway_config", return_value={}):
        await runner._handle_adapter_fatal_error_impl(adapter)

    runner.stop.assert_awaited_once()


def test_all_disconnected_exit_policy_defaults_true():
    assert DEFAULT_CONFIG["gateway"][
        "messaging_platform_exit_on_all_disconnected"
    ] is True