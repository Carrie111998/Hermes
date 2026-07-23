"""Gateway-owned polling loop for worker-bridge created tasks."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("gateway.run")


def _settings(config: object) -> tuple[bool, float, int]:
    section = config.get("worker_bridge", {}) if isinstance(config, dict) else {}
    raw = section.get("auto_dispatch", {}) if isinstance(section, dict) else {}
    if isinstance(raw, bool):
        return raw, 5.0, 3
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get("enabled", True) is not False
    try:
        interval = max(0.1, float(raw.get("interval_seconds", 5.0)))
    except (TypeError, ValueError):
        interval = 5.0
    try:
        limit = max(1, int(raw.get("per_tick", 3)))
    except (TypeError, ValueError):
        limit = 3
    return enabled, interval, limit


class GatewayWorkerTaskDispatcherMixin:
    """Start worker-bridge tasks that are waiting in ``created`` state."""

    async def _worker_task_dispatcher_watcher(self) -> None:
        try:
            from hermes_cli.config import load_config
            from hermes_constants import get_hermes_home
            from hermes_worker_bridge.dispatch import dispatch_pending
            from hermes_worker_bridge.runner import _bridge_for
        except Exception as exc:
            logger.warning("worker task auto-dispatch unavailable: %s", exc)
            return

        while self._running:
            try:
                config = load_config() or {}
                enabled, interval, limit = _settings(config)
                if enabled:
                    section = config.get("worker_bridge", {})
                    store_path = section.get("store_path") if isinstance(section, dict) else None
                    if not store_path:
                        store_path = Path(get_hermes_home()) / "workers" / "bridge.db"
                    bridge = await asyncio.to_thread(_bridge_for, str(store_path))
                    started = await asyncio.to_thread(
                        dispatch_pending, bridge, limit=limit
                    )
                    if started:
                        logger.info(
                            "worker task auto-dispatched %d task(s): %s",
                            len(started),
                            ", ".join(started),
                        )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker task auto-dispatch tick failed")
                await asyncio.sleep(5.0)
