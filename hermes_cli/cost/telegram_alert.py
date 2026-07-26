"""Send cost-gate alerts through Hermes' configured Telegram home channel."""

from __future__ import annotations

import json


def send_bridge_alert(message: str) -> None:
    """Send one already-formatted subscription-bridge alert."""
    _send_telegram_message(str(message))


def send_cost_alert(
    which_cap: str,
    daily_total: float,
    ledger_tail: str,
) -> None:
    message = (
        "🚨 Hermes cost gate PAUSED programme\n\n"
        f"Breach: {which_cap}\n"
        f"Daily spend: AUD {daily_total:.2f}\n"
        "Last 5 calls:\n"
        f"{ledger_tail}"
    )
    _send_telegram_message(message)


def _send_telegram_message(message: str) -> None:
    # Reuse the same environment bridge and delivery tool as ``hermes send``.
    # Telegram's bare platform target resolves to the configured home channel.
    from hermes_cli.send_cmd import _load_hermes_env
    from tools.send_message_tool import send_message_tool

    _load_hermes_env()
    result = send_message_tool(
        {
            "action": "send",
            "target": "telegram",
            "message": message,
        }
    )
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram cost alert returned invalid output: {result!r}") from exc
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(f"Telegram cost alert failed: {payload!r}")


__all__ = ["send_bridge_alert", "send_cost_alert"]
