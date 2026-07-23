"""Single-owner lifecycle helpers for WhatsApp pairing.

Pairing and the gateway must never use the same Baileys session concurrently.
Official setup surfaces disable WhatsApp in both persisted configuration stores,
restart a running gateway to release its bridge/session lock, and only re-enable
after the pair-only process exits successfully.
"""

from __future__ import annotations

import subprocess
import sys


def persist_whatsapp_enabled(enabled: bool) -> None:
    """Write one consistent enabled state to legacy env and YAML config."""
    from hermes_cli.config import save_env_value, write_platform_config_field

    value = "true" if enabled else "false"
    save_env_value("WHATSAPP_ENABLED", value)
    write_platform_config_field("whatsapp", "enabled", enabled)


def restart_gateway_if_running(*, timeout: float = 120.0) -> bool:
    """Restart the active-profile gateway, returning False when it is stopped."""
    from gateway.status import get_running_pid

    if get_running_pid() is None:
        return False

    try:
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out restarting the gateway for WhatsApp pairing.") from exc

    if result.returncode != 0:
        detail = (result.stdout or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(f"Gateway restart failed while preparing WhatsApp pairing{suffix}")
    return True


def prepare_whatsapp_pairing(*, restart_gateway: bool = True) -> bool:
    """Disable WhatsApp and quiesce any gateway-managed bridge before pairing."""
    persist_whatsapp_enabled(False)
    return restart_gateway_if_running() if restart_gateway else False


def activate_whatsapp_after_pairing(*, restart_gateway: bool = True) -> bool:
    """Enable WhatsApp only after verified pairing, then refresh the gateway."""
    persist_whatsapp_enabled(True)
    return restart_gateway_if_running() if restart_gateway else False
