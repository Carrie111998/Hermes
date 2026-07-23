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
from contextlib import contextmanager
from typing import Optional

from hermes_cli._subprocess_compat import (
    windows_detach_flags_without_breakaway,
    windows_detach_popen_kwargs,
)


def persist_whatsapp_enabled(enabled: bool) -> None:
    """Write one consistent enabled state to legacy env and YAML config."""
    from hermes_cli.config import (
        load_config,
        load_env,
        read_raw_config,
        save_env_value,
        write_platform_config_field,
    )
    from hermes_cli import managed_scope

    value = "true" if enabled else "false"
    save_env_value("WHATSAPP_ENABLED", value)
    write_platform_config_field("whatsapp", "enabled", enabled)

    env_value = str(load_env().get("WHATSAPP_ENABLED") or "").strip().lower()
    raw_config = read_raw_config()
    platforms = raw_config.get("platforms")
    whatsapp = platforms.get("whatsapp") if isinstance(platforms, dict) else None
    yaml_value = whatsapp.get("enabled") if isinstance(whatsapp, dict) else None
    effective_config = load_config()
    effective_platforms = effective_config.get("platforms")
    effective_whatsapp = (
        effective_platforms.get("whatsapp")
        if isinstance(effective_platforms, dict)
        else None
    )
    effective_yaml_value = (
        effective_whatsapp.get("enabled")
        if isinstance(effective_whatsapp, dict)
        else None
    )
    effective_env_value = str(
        managed_scope.load_managed_env().get("WHATSAPP_ENABLED", env_value)
    ).strip().lower()
    if (
        env_value != value
        or yaml_value is not enabled
        or effective_env_value != value
        or effective_yaml_value is not enabled
    ):
        desired = "enabled" if enabled else "disabled"
        raise RuntimeError(
            f"WhatsApp could not be persisted as {desired} in both .env and "
            "config.yaml. The setting may be managed or the files may be "
            "read-only; pairing was stopped before touching the session."
        )


def _gateway_restart_command(profile: Optional[str]) -> list[str]:
    requested = (profile or "").strip()
    profile_args: list[str] = []
    if requested and requested.lower() != "current":
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


@contextmanager
def _gateway_profile_scope(profile: Optional[str]):
    """Scope gateway service discovery to an explicit owner profile."""
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        yield
        return

    from hermes_cli import profiles
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profiles.get_profile_dir(requested)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def resolve_whatsapp_gateway_profile(profile: Optional[str]) -> Optional[str]:
    """Return the gateway profile that owns ``profile``'s WhatsApp adapter."""
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return profile

    from gateway.status import get_running_pid, read_runtime_status
    from hermes_cli import profiles

    target = profiles.normalize_profile_name(requested)
    if target == "default":
        return "default"

    default_home = profiles.get_profile_dir("default")
    if get_running_pid(default_home / "gateway.pid") is None:
        return target

    runtime = read_runtime_status(default_home / "gateway_state.json") or {}
    served = {
        profiles.normalize_profile_name(str(name))
        for name in runtime.get("served_profiles", [])
        if str(name).strip()
    }
    if target in served:
        return "default"

    # Older running gateways may predate the served_profiles status field.
    # Fall back to the effective default-profile config only when the default
    # process is live, preserving the same ownership rule.
    with _gateway_profile_scope("default"):
        from gateway.config import load_gateway_config

        if load_gateway_config().multiplex_profiles:
            return "default"
    return target


def _gateway_pid_path(profile: Optional[str]):
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return None
    from hermes_cli import profiles

    return profiles.get_profile_dir(requested) / "gateway.pid"


def _active_system_gateway_pid() -> Optional[int]:
    """Return the active system-scope gateway PID for the current profile."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        from hermes_cli.gateway import (
            _probe_systemd_service_running,
            _systemd_main_pid,
            supports_systemd_services,
        )

        if not supports_systemd_services():
            return None
        _selected_system, system_running = _probe_systemd_service_running(system=True)
        return _systemd_main_pid(system=True) if system_running else None
    except Exception:
        return None


def _system_gateway_pid_if_owned(old_pid: Optional[int]) -> Optional[int]:
    """Return the active system-service PID when it owns ``old_pid``."""
    system_pid = _active_system_gateway_pid()
    if system_pid is not None and old_pid in {None, system_pid}:
        return system_pid
    return None


def _raise_if_system_gateway_requires_root(old_pid: Optional[int]) -> Optional[int]:
    system_pid = _system_gateway_pid_if_owned(old_pid)
    geteuid = getattr(os, "geteuid", None)
    if system_pid is not None and (geteuid is None or geteuid() != 0):
        raise RuntimeError(
            "The running gateway is a system service and cannot be quiesced "
            "without root. Stop it first with "
            "`sudo hermes gateway stop --system`, then run WhatsApp pairing "
            "again."
        )
    return system_pid


def _preflight_gateway_restart(profile: Optional[str]) -> None:
    """Fail before config mutation when the running gateway needs root."""
    from gateway.status import get_running_pid

    pid_path = _gateway_pid_path(profile)
    with _gateway_profile_scope(profile):
        _raise_if_system_gateway_requires_root(get_running_pid(pid_path))


def restart_gateway_if_running(
    *,
    profile: Optional[str] = None,
    timeout: float = 120.0,
    poll_interval: float = 0.1,
) -> bool:
    """Restart the selected-profile gateway without tying it to this process."""
    from gateway.status import get_running_pid

    pid_path = _gateway_pid_path(profile)
    with _gateway_profile_scope(profile):
        old_pid = get_running_pid(pid_path)
        system_pid = _raise_if_system_gateway_requires_root(old_pid)
        system_scope = system_pid is not None
        if system_scope:
            old_pid = system_pid
        if old_pid is None:
            return False

        action_env = {**os.environ, "HERMES_NONINTERACTIVE": "1"}
        action_env.pop("_HERMES_GATEWAY", None)
        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": action_env,
            "close_fds": True,
        }
        command = _gateway_restart_command(profile)
        if system_scope:
            command.append("--system")
        try:
            proc = subprocess.Popen(
                command,
                **popen_kwargs,
                **windows_detach_popen_kwargs(),
            )
        except OSError as exc:
            if sys.platform != "win32":
                raise RuntimeError(
                    f"Could not start the gateway restart for WhatsApp pairing: {exc}"
                ) from exc
            try:
                proc = subprocess.Popen(
                    command,
                    **popen_kwargs,
                    creationflags=windows_detach_flags_without_breakaway(),
                )
            except OSError as fallback_exc:
                raise RuntimeError(
                    "Could not start the gateway restart for WhatsApp pairing "
                    f"without Windows job breakaway: {fallback_exc}"
                ) from fallback_exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if system_scope:
                current_pid = _active_system_gateway_pid()
            else:
                current_pid = get_running_pid(pid_path)
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
    gateway_profile: Optional[str] = None,
) -> bool:
    """Disable WhatsApp and quiesce any gateway-managed bridge before pairing."""
    owner_profile = gateway_profile if gateway_profile is not None else profile
    if restart_gateway:
        _preflight_gateway_restart(owner_profile)
    persist_whatsapp_enabled(False)
    return (
        restart_gateway_if_running(profile=owner_profile)
        if restart_gateway
        else False
    )


def activate_whatsapp_after_pairing(
    *,
    restart_gateway: bool = True,
    profile: Optional[str] = None,
) -> bool:
    """Enable WhatsApp only after verified pairing, then refresh the gateway."""
    persist_whatsapp_enabled(True)
    return restart_gateway_if_running(profile=profile) if restart_gateway else False
