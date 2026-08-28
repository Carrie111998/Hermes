"""Runner handling for deterministic webhook constructor failures."""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.webhook import WebhookConfigurationError
from gateway.platforms.webhook_ledger import WebhookLedgerConfigurationError
from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE
from gateway.run import GatewayRunner


def _webhook_config(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        platforms={
            Platform.WEBHOOK: PlatformConfig(
                enabled=True,
                extra={"host": "127.0.0.1", "port": 0, "routes": {}},
            )
        },
        sessions_dir=tmp_path / "sessions",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        WebhookConfigurationError("rate_limit must be a positive integer"),
        WebhookLedgerConfigurationError(
            "configured ledger limit conflicts with persisted authority"
        ),
    ],
    ids=["typed-config", "persisted-ledger-config"],
)
async def test_primary_startup_parks_deterministic_webhook_constructor_failure(
    monkeypatch,
    tmp_path,
    failure,
):
    """Immutable webhook constructor errors exit once instead of retrying."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(_webhook_config(tmp_path))
    status = MagicMock()
    monkeypatch.setattr(runner, "_update_platform_runtime_status", status)

    def fail_construction(_platform, _platform_config):
        raise failure

    monkeypatch.setattr(runner, "_create_adapter", fail_construction)

    assert await runner.start() is True

    assert runner.should_exit_cleanly is True
    assert runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE
    assert Platform.WEBHOOK not in runner._failed_platforms
    status.assert_called_once_with(
        "webhook",
        platform_state="fatal",
        error_code="webhook_configuration_invalid",
        error_message=str(failure),
    )


@pytest.mark.asyncio
async def test_primary_startup_does_not_mask_untyped_constructor_failure(
    monkeypatch,
    tmp_path,
):
    """An unrelated constructor bug retains the startup fail-loud behavior."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner(_webhook_config(tmp_path))
    status = MagicMock()
    monkeypatch.setattr(runner, "_update_platform_runtime_status", status)

    def fail_construction(_platform, _platform_config):
        raise RuntimeError("unexpected webhook constructor bug")

    monkeypatch.setattr(runner, "_create_adapter", fail_construction)

    with pytest.raises(RuntimeError, match="unexpected webhook constructor bug"):
        await runner.start()

    assert Platform.WEBHOOK not in runner._failed_platforms
    status.assert_not_called()


def _reconnect_runner(tmp_path, failure):
    runner = object.__new__(GatewayRunner)
    config = _webhook_config(tmp_path)
    runner.config = config
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._draining = False
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._failed_platforms = {
        Platform.WEBHOOK: {
            "config": config.platforms[Platform.WEBHOOK],
            "attempts": 1,
            "next_retry": time.monotonic() - 1,
            "queued_at": time.monotonic(),
        }
    }
    runner._create_adapter = MagicMock(side_effect=failure)
    runner._update_platform_runtime_status = MagicMock()
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        WebhookConfigurationError("port must be an integer"),
        WebhookLedgerConfigurationError(
            "configured ledger limit conflicts with persisted authority"
        ),
    ],
    ids=["typed-config", "persisted-ledger-config"],
)
async def test_reconnect_drops_deterministic_webhook_constructor_failure(
    monkeypatch,
    tmp_path,
    failure,
):
    """Reconnect removes immutable config failures from its retry queue."""

    runner = _reconnect_runner(tmp_path, failure)

    def record_status(*args, **kwargs):
        if kwargs.get("platform_state") == "fatal":
            runner._running = False

    runner._update_platform_runtime_status.side_effect = record_status

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", no_sleep)

    await runner._platform_reconnect_watcher()

    assert Platform.WEBHOOK not in runner._failed_platforms
    assert Platform.WEBHOOK not in runner.adapters
    runner._create_adapter.assert_called_once_with(
        Platform.WEBHOOK,
        runner.config.platforms[Platform.WEBHOOK],
    )
    runner._update_platform_runtime_status.assert_called_once_with(
        "webhook",
        platform_state="fatal",
        error_code="webhook_configuration_invalid",
        error_message=str(failure),
    )


@pytest.mark.asyncio
async def test_reconnect_keeps_untyped_constructor_failure_retryable(
    monkeypatch,
    tmp_path,
):
    """Transient or unexpected exceptions retain the reconnect backoff path."""

    failure = RuntimeError("temporary constructor dependency failure")
    runner = _reconnect_runner(tmp_path, failure)

    def record_status(*args, **kwargs):
        if kwargs.get("platform_state") == "retrying":
            runner._running = False

    runner._update_platform_runtime_status.side_effect = record_status

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("gateway.run.asyncio.sleep", no_sleep)

    await runner._platform_reconnect_watcher()

    queued = runner._failed_platforms[Platform.WEBHOOK]
    assert queued["attempts"] == 2
    assert queued["next_retry"] > time.monotonic()
    runner._update_platform_runtime_status.assert_called_once_with(
        "webhook",
        platform_state="retrying",
        error_code=None,
        error_message=str(failure),
    )
