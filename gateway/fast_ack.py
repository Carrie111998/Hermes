"""Two-phase gateway acknowledgement for slow Grace turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Callable, Mapping


logger = logging.getLogger(__name__)

DEFAULT_FAST_ACK_TEXT = (
    "收到，我正在理解並處理這則訊息。若需要執行，我會在確認目標與範圍後"
    "交給 ClawOps，並另行回報 task ID 與進度。"
)


@dataclass(frozen=True)
class FastAckConfig:
    enabled: bool = False
    delay_seconds: float = 1.5
    text: str = DEFAULT_FAST_ACK_TEXT


def _setting(config: Mapping[str, Any], platform_key: str, key: str, default: Any) -> Any:
    display = config.get("display") if isinstance(config, Mapping) else None
    display = display if isinstance(display, Mapping) else {}
    platforms = display.get("platforms")
    platforms = platforms if isinstance(platforms, Mapping) else {}
    platform = platforms.get(platform_key)
    platform = platform if isinstance(platform, Mapping) else {}
    if key in platform:
        return platform[key]
    return display.get(key, default)


def resolve_fast_ack_config(config: Mapping[str, Any], platform_key: str) -> FastAckConfig:
    raw_delay = _setting(config, platform_key, "fast_ack_delay_seconds", 1.5)
    try:
        delay = float(raw_delay)
    except (TypeError, ValueError):
        delay = 1.5
    delay = max(0.1, min(30.0, delay))
    text = str(_setting(config, platform_key, "fast_ack_text", DEFAULT_FAST_ACK_TEXT) or "").strip()
    return FastAckConfig(
        enabled=bool(_setting(config, platform_key, "fast_ack", False)),
        delay_seconds=delay,
        text=text or DEFAULT_FAST_ACK_TEXT,
    )


async def deliver_fast_ack(
    *,
    adapter: Any,
    chat_id: str,
    metadata: Any,
    config: FastAckConfig,
    run_is_current: Callable[[], bool],
    interim_is_visible: Callable[[], bool],
    run_is_finished: Callable[[], bool],
    has_message: bool = True,
) -> bool:
    """Send one neutral acknowledgement only while a turn is still silent."""
    if not config.enabled or adapter is None or not has_message:
        return False
    await asyncio.sleep(config.delay_seconds)
    if run_is_finished() or interim_is_visible() or not run_is_current():
        return False
    try:
        result = await adapter.send(chat_id=chat_id, content=config.text, metadata=metadata)
        delivered = bool(getattr(result, "success", False))
        if delivered:
            logger.info(
                "Fast acknowledgement delivered (chat=%s delay=%.1fs)",
                chat_id,
                config.delay_seconds,
            )
        return delivered
    except asyncio.CancelledError:
        raise
    except Exception:
        # A fast acknowledgement is UX only. Delivery failure must never abort
        # the real Grace turn or suppress its final response.
        logger.debug("Fast acknowledgement delivery failed", exc_info=True)
        return False
