"""Ephemeral-reply auto-delete scheduling for platform adapters.

Extracted verbatim from ``gateway/platforms/base.py`` (godfile decomposition
wave 1, shard s3, cluster c12: ``_get_ephemeral_system_ttl_default``, ``_schedule_ephemeral_delete`` (and its nested ``_run_delete``)).  The mixin is a base of
``BasePlatformAdapter``; ``delete_message``/``name`` stay on the adapter and resolve via MRO.
Config reads keep the original's lazy ``hermes_cli.config`` import.
"""

from __future__ import annotations

import asyncio
import logging

# Same logger object as ``gateway.platforms.base`` (logging keeps a
# name-keyed singleton registry), so log records keep the historical
# ``gateway.platforms.base`` name.
logger = logging.getLogger("gateway.platforms.base")


class EphemeralMixin:
    def _get_ephemeral_system_ttl_default(self) -> int:
        """Read ``display.ephemeral_system_ttl`` from config.

        Returns the TTL in seconds to use when an :class:`EphemeralReply`
        does not specify one explicitly.  ``0`` (the default) disables
        auto-deletion.  Non-fatal if config is unreadable.
        """
        try:
            from hermes_cli.config import load_config_readonly as _load_config
        except Exception:
            return 0
        try:
            cfg = _load_config()  # read-only: .get() only, never mutated
        except Exception:
            return 0
        display = cfg.get("display", {}) if isinstance(cfg, dict) else {}
        if not isinstance(display, dict):
            return 0
        raw = display.get("ephemeral_system_ttl", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _schedule_ephemeral_delete(
        self,
        chat_id: str,
        message_id: str,
        ttl_seconds: int,
    ) -> None:
        """Spawn a detached task that deletes ``message_id`` after ``ttl_seconds``.

        Best-effort — failures (gateway restart, permission denied, message
        too old for Telegram's 48h window) are swallowed at debug level.
        Does not block the caller.
        """

        async def _run_delete() -> None:
            try:
                await asyncio.sleep(max(1, int(ttl_seconds)))
                await self.delete_message(chat_id=chat_id, message_id=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "[%s] Ephemeral delete failed for %s/%s: %s",
                    self.name, chat_id, message_id, e,
                )

        coro = _run_delete()
        try:
            asyncio.create_task(coro)
        except RuntimeError:
            # No running loop (e.g. unit tests that never reach the async
            # path).  Close the coroutine cleanly so Python doesn't warn
            # about it never being awaited, then drop silently.
            coro.close()

