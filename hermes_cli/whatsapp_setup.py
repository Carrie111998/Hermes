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
from typing import Mapping, Optional

from hermes_cli._subprocess_compat import (
    windows_detach_flags_without_breakaway,
    windows_detach_popen_kwargs,
)

BAILEYS_WHATSAPP_ENV_PREFIXES = ("WHATSAPP_",)
BAILEYS_WHATSAPP_ENV_EXCLUDED_PREFIXES = ("WHATSAPP_CLOUD_",)


def scrub_baileys_whatsapp_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop inherited Baileys settings while preserving Cloud API settings."""
    return {
        key: value
        for key, value in env.items()
        if not (
            any(key.startswith(prefix) for prefix in BAILEYS_WHATSAPP_ENV_PREFIXES)
            and not any(
                key.startswith(prefix)
                for prefix in BAILEYS_WHATSAPP_ENV_EXCLUDED_PREFIXES
            )
        )
    }


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
    try:
        # Write config first. A config failure must never leave the stronger
        # WHATSAPP_ENABLED env override enabled on its own.
        write_platform_config_field("whatsapp", "enabled", enabled)
        save_env_value("WHATSAPP_ENABLED", value)

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
            raise RuntimeError("persisted WhatsApp state did not read back")
    except Exception as exc:
        rollback_errors: list[str] = []
        if enabled:
            # Pairing preparation established disabled state before activation.
            # Restore that invariant if either write or read-back fails.
            try:
                write_platform_config_field("whatsapp", "enabled", False)
            except Exception as rollback_exc:
                rollback_errors.append(f"config rollback failed: {rollback_exc}")
            try:
                save_env_value("WHATSAPP_ENABLED", "false")
            except Exception as rollback_exc:
                rollback_errors.append(f"env rollback failed: {rollback_exc}")
        desired = "enabled" if enabled else "disabled"
        rollback = (
            f" Safety rollback also failed ({'; '.join(rollback_errors)})."
            if rollback_errors
            else ""
        )
        raise RuntimeError(
            f"WhatsApp could not be persisted as {desired} in both .env and "
            "config.yaml. The setting may be managed or the files may be "
            f"read-only; pairing was stopped before touching the session.{rollback}"
        ) from exc


def _gateway_restart_command(profile: Optional[str]) -> list[str]:
    requested = (profile or "").strip()
    from hermes_cli import profiles

    if not requested or requested.lower() == "current":
        active = profiles.get_active_profile_name()
        # A custom HERMES_HOME is the deployment's default/root profile. An
        # explicit selector prevents the detached child from following a
        # sticky active_profile file away from that inherited HERMES_HOME.
        requested = "default" if active in {"custom", "default"} else active
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
    from hermes_cli import profiles

    if not requested or requested.lower() == "current":
        active = profiles.get_active_profile_name()
        requested = "default" if active in {"custom", "default"} else active

    from gateway.status import get_running_pid, read_runtime_status

    target = profiles.normalize_profile_name(requested)
    if target == "default":
        return "default"

    default_home = profiles.get_profile_dir("default")
    target_home = profiles.get_profile_dir(target)
    target_pid = get_running_pid(target_home / "gateway.pid")
    default_pid = get_running_pid(default_home / "gateway.pid")
    if (
        target_pid is not None
        and default_pid is not None
        and target_pid != default_pid
    ):
        raise RuntimeError(
            f"WhatsApp pairing cannot choose one session owner while both default "
            f"and '{target}' gateways are running. Stop one gateway, then retry."
        )
    if default_pid is None:
        return target

    runtime = read_runtime_status(default_home / "gateway_state.json") or {}
    served = {
        profiles.normalize_profile_name(str(name))
        for name in runtime.get("served_profiles", [])
        if str(name).strip()
    }

    # Runtime status is updated in place and can retain an old served_profiles
    # list. Revalidate the live owner's current multiplex setting before using
    # that list; stale status must never redirect pairing to the wrong gateway.
    with _gateway_profile_scope("default"):
        from gateway.config import load_gateway_config

        multiplexing = bool(load_gateway_config().multiplex_profiles)
    if not multiplexing:
        return target
    # A populated status list is live coverage evidence. Missing coverage means
    # the named gateway remains the safer owner. Empty/missing lists may come
    # from older multiplex gateways, so fall back to current config authority.
    return "default" if not served or target in served else target


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
    if system_pid is None:
        return None
    if old_pid not in {None, system_pid}:
        raise RuntimeError(
            "WhatsApp pairing cannot choose one session owner while both a "
            "profile gateway and the system gateway are running. Stop one "
            "gateway first (for the system service: "
            "`sudo hermes gateway stop --system`), then retry."
        )
    return system_pid


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

        action_env = scrub_baileys_whatsapp_env(
            {**os.environ, "HERMES_NONINTERACTIVE": "1"}
        )
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
