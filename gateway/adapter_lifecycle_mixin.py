"""Adapter lifecycle helpers for :class:`gateway.run.GatewayRunner`.

This module is a mechanical extraction of the adapter connect, disconnect,
teardown, and timeout helpers from ``gateway.run``.  The methods intentionally
retain their original logger namespace, cancellation semantics, timeout
messages, and public call signatures; ``GatewayRunner`` composes this mixin as
its leftmost base so existing call sites continue to resolve through the MRO.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Optional

from agent.async_utils import consume_detached_task_result
from gateway.config import Platform

logger = logging.getLogger("gateway.run")

# Default per-adapter disconnect timeout (seconds).  The environment variable
# ``HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT`` may override this value.
_ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT = 5.0

# Default per-platform connect timeout (seconds).  The environment variable
# ``HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT`` may override this value.
_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT = 30.0

# Telegram cold polling proves one real getUpdates round trip before connect
# returns, so it retains a larger readiness budget than other platforms.
_TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT = 180.0


class GatewayAdapterLifecycleMixin:
    """Adapter connect/disconnect/teardown helpers lifted from the runner."""

    async def _await_adapter_cleanup_with_timeout(
        self, awaitable: Awaitable[Any], timeout: float
    ) -> bool:
        """Wait for adapter cleanup without letting cancellation swallowing hang us.

        ``asyncio.wait_for`` cancels an overdue child but then waits for it to
        exit.  An adapter close path that catches ``CancelledError`` can
        therefore block recovery forever.  Keep ownership of the old task
        through its done callback, but release the runner at the deadline.
        """
        if timeout <= 0:
            await awaitable
            return True

        task = asyncio.ensure_future(awaitable)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            await task
            return True

        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        return False

    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        """Call ``adapter.disconnect()`` defensively, swallowing any error."""
        timeout = self._adapter_disconnect_timeout_secs()
        try:
            completed = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if not completed:
                logger.warning(
                    "Timed out after %.1fs while disconnecting %s adapter; continuing shutdown",
                    timeout,
                    platform.value if platform is not None else "adapter",
                )
        except Exception as e:
            logger.debug(
                "Defensive %s disconnect after failed connect raised: %s",
                platform.value if platform is not None else "adapter",
                e,
            )

    async def _bounded_adapter_teardown(
        self, adapter, platform, *, profile: Optional[str] = None
    ) -> None:
        """Tear down one adapter on shutdown with bounded awaits."""
        timeout = self._adapter_disconnect_timeout_secs()
        suffix = f" (profile: {profile})" if profile else ""
        started_at = time.monotonic()
        try:
            cancelled = await self._await_adapter_cleanup_with_timeout(
                adapter.cancel_background_tasks(), timeout
            )
            if not cancelled:
                logger.warning(
                    "✗ %s background-task cancel timed out after %.1fs - forcing continue%s",
                    platform.value,
                    timeout,
                    suffix,
                )
        except Exception as e:
            logger.debug(
                "✗ %s background-task cancel error%s: %s",
                platform.value,
                suffix,
                e,
            )
        try:
            disconnected = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if disconnected:
                logger.info(
                    "✓ %s disconnected (%.2fs)%s",
                    platform.value,
                    time.monotonic() - started_at,
                    suffix,
                )
            else:
                logger.warning(
                    "✗ %s disconnect timed out after %.1fs - forcing continue%s",
                    platform.value,
                    timeout,
                    suffix,
                )
        except Exception as e:
            logger.error(
                "✗ %s disconnect error after %.2fs%s: %s",
                platform.value,
                time.monotonic() - started_at,
                suffix,
                e,
            )

    def _adapter_disconnect_timeout_secs(self) -> float:
        """Return the per-adapter disconnect timeout used during shutdown."""
        raw = os.getenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    def _platform_connect_timeout_secs(self, platform=None) -> float:
        """Return the per-platform connect timeout used during startup/retry."""
        raw = os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        if platform == Platform.TELEGRAM:
            return _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def _connect_adapter_with_timeout(
        self, adapter, platform, *, is_reconnect: bool = False
    ) -> bool:
        """Connect an adapter without allowing one platform to block others."""
        timeout = self._platform_connect_timeout_secs(platform)
        if timeout <= 0:
            return await adapter.connect(is_reconnect=is_reconnect)

        # Use the detach-on-timeout pattern instead of plain
        # ``asyncio.wait_for``: an adapter connect that catches
        # ``CancelledError`` must not block the next retry forever.
        task = asyncio.ensure_future(adapter.connect(is_reconnect=is_reconnect))
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            result = await task
            return bool(result)

        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        raise TimeoutError(f"{platform.value} connect timed out after {timeout:g}s")
