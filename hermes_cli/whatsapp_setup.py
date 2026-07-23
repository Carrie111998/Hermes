"""Single-owner lifecycle helpers for WhatsApp pairing.

Pairing and the gateway must never use the same Baileys session concurrently.
Official setup surfaces disable WhatsApp in both persisted configuration stores,
restart a running gateway to release its bridge/session lock, and only re-enable
after the pair-only process exits successfully.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

from hermes_cli._subprocess_compat import windows_detach_popen_kwargs


def persist_whatsapp_enabled(enabled: bool) -> None:
    """Write one consistent enabled state to legacy env and YAML config."""
    from hermes_cli.config import save_env_value, write_platform_config_field

    value = "true" if enabled else "false"
    save_env_value("WHATSAPP_ENABLED", value)
    write_platform_config_field("whatsapp", "enabled", enabled)


def _gateway_restart_command(profile: Optional[str]) -> list[str]:
    requested = (profile or "").strip()
    profile_args: list[str] = []
    if requested and requested.lower() not in {"current", "default"}:
        from hermes_cli import profiles

        profile_args = ["-p", profiles.normalize_profile_name(requested)]
    return [
        sys.executable,
        "-m",
        "hermes_cli.main",
        *profile_args,
        "gateway",
        "restart",
    ]


def restart_gateway_if_running(
    *,
    profile: Optional[str] = None,
    timeout: float = 120.0,
    poll_interval: float = 0.1,
) -> bool:
    """Restart the selected-profile gateway without tying it to this process."""
    from gateway.status import get_running_pid

    old_pid = get_running_pid()
    if old_pid is None:
        return False

    action_env = {**os.environ, "HERMES_NONINTERACTIVE": "1"}
    action_env.pop("_HERMES_GATEWAY", None)
    proc = subprocess.Popen(
        _gateway_restart_command(profile),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=action_env,
        close_fds=True,
        **windows_detach_popen_kwargs(),
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_pid = get_running_pid()
        if current_pid is not None and current_pid != old_pid:
            return True
        returncode = proc.poll()
        if returncode not in {None, 0}:
            raise RuntimeError(
                "Gateway restart failed while preparing WhatsApp pairing "
                f"(exit code {returncode})."
            )
        time.sleep(poll_interval)

    raise RuntimeError(
        "Timed out waiting for the gateway restart handoff for WhatsApp pairing; "
        "the detached restart was left running."
    )


def prepare_whatsapp_pairing(
    *,
    restart_gateway: bool = True,
    profile: Optional[str] = None,
) -> bool:
    """Disable WhatsApp and quiesce any gateway-managed bridge before pairing."""
    persist_whatsapp_enabled(False)
    return restart_gateway_if_running(profile=profile) if restart_gateway else False


def activate_whatsapp_after_pairing(
    *,
    restart_gateway: bool = True,
    profile: Optional[str] = None,
) -> bool:
    """Enable WhatsApp only after verified pairing, then refresh the gateway."""
    persist_whatsapp_enabled(True)
    return restart_gateway_if_running(profile=profile) if restart_gateway else False
