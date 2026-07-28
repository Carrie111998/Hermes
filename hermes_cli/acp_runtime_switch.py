"""Shared logic for the /acp-client-runtime slash command.

Toggles the ACP client runtime on or off by writing three config keys:

    model.provider:    "acp-client"  (enabled) or restored prior provider (disabled)
    model.acp_command: path/name of the ACP-compliant agent binary
    model.acp_args:    list of extra arguments passed to the binary

When enabled, Hermes routes turns to an external ACP-compliant agent via
JSON-RPC 2.0 over stdio (see agent/transports/acp_client.py).  The agent
must implement the Agent Client Protocol (session/new, session/prompt, etc.).

Both CLI (cli.py) and gateway (gateway/run.py) call into this module so the
behaviour stays identical across surfaces.

The actual routing happens in agent/agent_init.py which maps
``provider == "acp-client"`` → ``api_mode = "acp_client"``, then
``agent/conversation_loop.py`` dispatches to ``_run_acp_client_turn()``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Config key names
_KEY_PROVIDER = "provider"
_KEY_DEFAULT = "default"
_KEY_ACP_COMMAND = "acp_command"
_KEY_ACP_ARGS = "acp_args"
_MODEL_SECTION = "model"

# The provider value that activates ACP client mode
_PROVIDER_ACP = "acp-client"

VALID_STATES = ("acp_client", "auto")


@dataclass
class ACPRuntimeStatus:
    """Result of an /acp-client-runtime invocation.  Callers render this
    however suits their surface (CLI uses Rich panels, gateway sends text)."""

    success: bool
    new_value: Optional[str] = None      # "acp_client" | "auto"
    old_value: Optional[str] = None
    message: str = ""
    requires_new_session: bool = False
    acp_command_ok: bool = True
    acp_command_version: Optional[str] = None


def parse_args(arg_string: str) -> tuple[Optional[str], list[str]]:
    """Parse the slash-command argument string.  Returns (value, errors).

    No args               → return current state (value=None)
    'acp_client' / 'on'  → enable ACP client runtime
    'auto' / 'off'        → disable (revert to Hermes default)
    anything else         → error
    """
    raw = (arg_string or "").strip().lower()
    if not raw:
        return None, []
    if raw in {"on", "acp_client", "acp-client", "enable", "acp"}:
        return "acp_client", []
    if raw in {"off", "auto", "disable", "hermes", "default"}:
        return "auto", []
    return None, [
        f"Unknown value {raw!r}. Use: acp_client (or 'on') to enable, "
        "auto (or 'off') to disable."
    ]


def get_current_state(config: dict) -> str:
    """Read the current ACP runtime state from a config dict.
    Returns 'acp_client' when active, 'auto' otherwise.

    A runtime is considered active when ``model.provider`` is either the
    canonical ``acp-client`` or a registered ACP runtime provider whose
    resolver produces ``api_mode = "acp_client"``.
    Additionally, when ``model.acp_command`` is set and a backup snapshot
    exists (set by ``enable_runtime``), the runtime is considered active
    even if the display provider name is not directly resolvable.
    """
    if not isinstance(config, dict):
        return "auto"
    model_cfg = config.get(_MODEL_SECTION) or {}
    if not isinstance(model_cfg, dict):
        return "auto"
    provider_val = str(model_cfg.get(_KEY_PROVIDER) or "").strip().lower()
    if provider_val == _PROVIDER_ACP or provider_val == _PROVIDER_ACP.replace("-", "_"):
        return "acp_client"
    # Check if the provider is a registered ACP runtime provider that
    # resolves to acp_client transport.
    if provider_val:
        requested_model = model_cfg.get(_KEY_DEFAULT)
        # Try ACP runtime provider registry first (primary)
        try:
            from hermes_cli.acp_runtime_provider_registry import (
                resolve_acp_runtime_provider,
                get_acp_runtime_provider,
            )
            if get_acp_runtime_provider(provider_val):
                descriptor = resolve_acp_runtime_provider(
                    provider_val, requested_model, model_cfg,
                )
                if descriptor and descriptor.get("api_mode") == "acp_client":
                    return "acp_client"
        except Exception:
            pass
    # Fallback: if acp_command is set and a backup snapshot exists,
    # the runtime was enabled by enable_runtime and is still active.
    # The display provider name may not be resolvable via the registry
    # (e.g. "claude-code-acp" is a display name, not the registry key).
    if model_cfg.get(_KEY_ACP_COMMAND) and "_acp_runtime_backup" in config:
        return "acp_client"
    return "auto"


def get_current_command(config: dict) -> str:
    """Return the configured acp_command, or empty string if not set."""
    if not isinstance(config, dict):
        return ""
    model_cfg = config.get(_MODEL_SECTION) or {}
    if not isinstance(model_cfg, dict):
        return ""
    return str(model_cfg.get(_KEY_ACP_COMMAND) or "").strip()


def get_current_args(config: dict) -> list[str]:
    """Return the configured acp_args list, or [] if not set."""
    if not isinstance(config, dict):
        return []
    model_cfg = config.get(_MODEL_SECTION) or {}
    if not isinstance(model_cfg, dict):
        return []
    raw = model_cfg.get(_KEY_ACP_ARGS)
    if isinstance(raw, list):
        return [str(a) for a in raw]
    return []


def enable_runtime(
    config: dict,
    acp_command: str,
    acp_args: list[str],
    *,
    display_provider: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Mutate config dict to enable ACP client runtime.  Returns old state.

    When *display_provider* is set (e.g. ``claude-code-acp`` from a
    plugin-registered resolver), it is written as ``model.provider`` so the
    banner and session metadata show the logical provider name.  The
    canonical provider remains ``acp_client`` — resolved at agent init time
    via the ``api_mode == "acp_client"`` check in ``agent_init.py``.

    When *model* is set, it is written as ``model.default`` so new sessions
    pick up the ACP agent's intended model (e.g. ``opus[1m]``).

    A snapshot of the pre-enable ``model.provider``, ``model.default``,
    ``model.acp_command``, ``model.acp_args`` is saved under the
    ``_acp_runtime_backup`` key so ``disable_runtime`` can restore them.
    """
    old = get_current_state(config)
    if not isinstance(config.get(_MODEL_SECTION), dict):
        config[_MODEL_SECTION] = {}
    model_cfg = config[_MODEL_SECTION]

    # Snapshot the pre-enable values so disable_runtime can restore them.
    backup = {
        "provider": model_cfg.get(_KEY_PROVIDER),
        "default": model_cfg.get(_KEY_DEFAULT),
        "acp_command": model_cfg.get(_KEY_ACP_COMMAND),
        "acp_args": model_cfg.get(_KEY_ACP_ARGS),
    }
    # Only store backup when transitioning from non-acp to acp.
    if old != "acp_client":
        if not isinstance(config.get("_acp_runtime_backup"), dict):
            config["_acp_runtime_backup"] = {}
        config["_acp_runtime_backup"] = backup

    model_cfg[_KEY_PROVIDER] = display_provider or _PROVIDER_ACP
    model_cfg[_KEY_ACP_COMMAND] = acp_command
    model_cfg[_KEY_ACP_ARGS] = list(acp_args)
    if model:
        model_cfg[_KEY_DEFAULT] = model
    return old


def disable_runtime(config: dict) -> str:
    """Mutate config dict to disable ACP client runtime.  Returns old state.

    Restores the pre-enable ``model.provider``, ``model.default``,
    ``model.acp_command``, ``model.acp_args`` from the backup snapshot
    saved by ``enable_runtime`` (stored under ``_acp_runtime_backup``).
    If no backup exists (e.g. config was hand-edited or enabled before this
    fix), falls back to simply removing the ACP-specific keys.
    """
    old = get_current_state(config)
    if not isinstance(config.get(_MODEL_SECTION), dict):
        return old
    model_cfg = config[_MODEL_SECTION]

    # Try restoring from the backup snapshot.
    backup = config.pop("_acp_runtime_backup", None)
    if isinstance(backup, dict):
        for key in (_KEY_PROVIDER, _KEY_DEFAULT, _KEY_ACP_COMMAND, _KEY_ACP_ARGS):
            if key in backup:
                val = backup[key]
                if val is not None:
                    model_cfg[key] = val
                else:
                    model_cfg.pop(key, None)
            else:
                model_cfg.pop(key, None)
        return old

    # No backup: remove ACP-specific keys conservatively.
    current_provider = str(model_cfg.get(_KEY_PROVIDER) or "").strip().lower()
    if current_provider in (_PROVIDER_ACP, _PROVIDER_ACP.replace("-", "_")):
        model_cfg.pop(_KEY_PROVIDER, None)
    elif old == "acp_client":
        # Provider is a registered display name; remove it.
        model_cfg.pop(_KEY_PROVIDER, None)
    model_cfg.pop(_KEY_ACP_COMMAND, None)
    model_cfg.pop(_KEY_ACP_ARGS, None)
    return old


def check_acp_command_ok(command: str) -> tuple[bool, Optional[str]]:
    """Best-effort check that the ACP agent binary is reachable.

    Returns (ok, version_or_message).  We try ``command --version`` first;
    failing that, we check whether the binary is on PATH at all.
    The check is intentionally lenient: if the agent ignores ``--version``
    but is on PATH we still return True so users aren't blocked by agents
    that don't implement --version.
    """
    if not command:
        return False, "acp_command is empty — set it with /acp-client-runtime on <command>"
    # First: try --version for a clean version string
    try:
        proc = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ver = (proc.stdout.strip() or proc.stderr.strip() or "").splitlines()[0]
        return True, ver or "(no version output)"
    except FileNotFoundError:
        # Binary not on PATH
        return False, f"{command!r} not found on PATH"
    except Exception:
        pass
    # Second: check PATH only — agent may not support --version
    if shutil.which(command):
        return True, "(binary found, no --version)"
    return False, f"{command!r} not found on PATH"


def _try_registry_resolve(
    command: str,
) -> Optional[tuple[str, dict]]:
    """Look up *command* as a registered ACP runtime provider key.

    Returns ``(normalised_key, descriptor)`` when *command* matches a
    plugin-registered resolver, or ``None`` otherwise.  The descriptor is the
    dict returned by the resolver (contains ``command``, ``args``, ``model``,
    ``provider``, ``display_provider``, ``metadata``, …).

    Import-safe: if the runtime registry module is unavailable we return
    ``None`` and the caller falls through to the built-in PATH-based path.

    **Runtime registry only.**  This function backs ``/acp-client-runtime``,
    which manages the main agent's ACP runtime.  Only the ACP runtime
    provider registry is consulted here.
    """
    try:
        from hermes_cli.acp_runtime_provider_registry import (
            resolve_acp_runtime_provider,
            get_acp_runtime_provider,
        )
        if get_acp_runtime_provider(command):
            descriptor = resolve_acp_runtime_provider(command, None, {})
            if descriptor and descriptor.get("command"):
                return command, descriptor
    except ImportError:
        pass

    return None


def apply(
    config: dict,
    new_value: Optional[str],
    *,
    acp_command: Optional[str] = None,
    acp_args: Optional[list[str]] = None,
    persist_callback=None,
) -> ACPRuntimeStatus:
    """Top-level entry point used by both CLI and gateway handlers.

    Args:
        config:          in-memory config dict (mutated when new_value is set)
        new_value:       "acp_client" to enable, "auto" to disable, None for
                         read-only state report
        acp_command:     ACP agent binary or registered provider key (required
                         when enabling; ignored on disable).  Falls back to
                         currently configured value if the runtime is already
                         enabled.
        acp_args:        Extra args for the binary (optional).
        persist_callback: Optional callable taking the mutated config dict and
                         persisting it to disk.  Skipped when None.

    Returns: ACPRuntimeStatus describing the outcome.

    When *acp_command* matches a plugin-registered ACP runtime provider key
    (e.g. ``claude-agent-acp`` → resolver key ``claude-code-acp``), the
    resolver produces the real ``command``/``args``/``model``/display
    ``provider``.  The PATH check is skipped for registry-resolved providers
    (the resolver is trusted, and the underlying binary may be ``npx`` which
    fetches the agent on first run).
    """
    current = get_current_state(config)
    current_cmd = get_current_command(config)

    # Read-only: just report current state
    if new_value is None:
        cmd = current_cmd or "(not configured)"
        cur_args = get_current_args(config)
        ok, ver = check_acp_command_ok(current_cmd) if current_cmd else (False, "not configured")
        cmd_line = f"{cmd} {' '.join(cur_args)}".rstrip() if cur_args else cmd
        msg = (
            f"acp_client runtime: {current}\n"
            f"acp_command: {cmd_line}\n"
            f"binary: {'OK — ' + ver if ok else 'not available — ' + (ver or 'check acp_command')}"
        )
        return ACPRuntimeStatus(
            success=True,
            new_value=current,
            old_value=current,
            message=msg,
            acp_command_ok=ok,
            acp_command_version=ver if ok else None,
        )

    # No change
    if new_value == current:
        if new_value == "acp_client":
            # Still report the command for reassurance
            cmd = current_cmd or "(not configured)"
            return ACPRuntimeStatus(
                success=True,
                new_value=current,
                old_value=current,
                message=f"acp_client runtime already enabled (command: {cmd})",
            )
        return ACPRuntimeStatus(
            success=True,
            new_value=current,
            old_value=current,
            message="acp_client runtime already disabled (Hermes default)",
        )

    # Enabling
    if new_value == "acp_client":
        effective_cmd = (acp_command or "").strip() or current_cmd
        if not effective_cmd:
            return ACPRuntimeStatus(
                success=False,
                new_value=None,
                old_value=current,
                message=(
                    "Cannot enable acp_client runtime: acp_command is required.\n"
                    "Usage: /acp-client-runtime on <command> [<arg> ...]\n"
                    "Example: /acp-client-runtime on claude-agent-acp"
                ),
                acp_command_ok=False,
            )

        # --- Registry-resolved provider path (e.g. claude-agent-acp) ---------
        # When the command string matches a registered ACP runtime provider key,
        # the plugin resolver produces the real binary, args, model, and display
        # provider.  We skip the PATH check (resolver is trusted; binary may be
        # ``npx`` which is always present) and write all resolved fields.
        registry_hit = _try_registry_resolve(effective_cmd)
        display_provider: Optional[str] = None
        resolved_model: Optional[str] = None

        if registry_hit is not None:
            _key, descriptor = registry_hit
            effective_cmd = descriptor.get("command") or effective_cmd
            effective_args = list(descriptor.get("args") or [])
            resolved_model = descriptor.get("model")
            # display_provider is the human-readable name (e.g.
            # ``claude-code-acp``); falls back to canonical ``acp_client``.
            display_provider = descriptor.get("display_provider") or descriptor.get("provider")
            # Trust the resolver — skip subprocess PATH check.
            ok, ver_or_msg = True, "(resolved via plugin provider)"
        else:
            effective_args = acp_args if acp_args is not None else get_current_args(config)
            ok, ver_or_msg = check_acp_command_ok(effective_cmd)
            if not ok:
                return ACPRuntimeStatus(
                    success=False,
                    new_value=None,
                    old_value=current,
                    message=(
                        f"Cannot enable acp_client runtime: {ver_or_msg}\n"
                        f"Ensure {effective_cmd!r} is installed and on PATH."
                    ),
                    acp_command_ok=False,
                )

        enable_runtime(
            config, effective_cmd, effective_args,
            display_provider=display_provider,
            model=resolved_model,
        )
        if persist_callback is not None:
            try:
                persist_callback(config)
            except Exception as exc:
                logger.exception("failed to persist acp_client runtime change")
                return ACPRuntimeStatus(
                    success=False,
                    new_value=new_value,
                    old_value=current,
                    message=f"updated config in memory but persist failed: {exc}",
                )

        cmd_display = effective_cmd
        if effective_args:
            cmd_display += " " + " ".join(effective_args)
        msg_lines = [
            f"acp_client runtime: {current} → {new_value}",
            f"command: {cmd_display}",
        ]
        if display_provider:
            msg_lines.append(f"provider: {display_provider}")
        if resolved_model:
            msg_lines.append(f"model: {resolved_model}")
        if ver_or_msg:
            msg_lines.append(f"binary: {ver_or_msg}")
        msg_lines.append(
            "Hermes turns now route to the ACP agent via JSON-RPC stdio "
            "(session/new + session/prompt)."
        )
        msg_lines.append(
            "Effective on next session — current cached agent keeps the "
            "prior runtime to preserve prompt cache."
        )
        return ACPRuntimeStatus(
            success=True,
            new_value=new_value,
            old_value=current,
            message="\n".join(msg_lines),
            requires_new_session=True,
            acp_command_ok=True,
            acp_command_version=ver_or_msg,
        )

    # Disabling
    disable_runtime(config)
    if persist_callback is not None:
        try:
            persist_callback(config)
        except Exception as exc:
            logger.exception("failed to persist acp_client runtime disable")
            return ACPRuntimeStatus(
                success=False,
                new_value=new_value,
                old_value=current,
                message=f"updated config in memory but persist failed: {exc}",
            )
    return ACPRuntimeStatus(
        success=True,
        new_value="auto",
        old_value=current,
        message=(
            "acp_client runtime: acp_client → auto\n"
            "Hermes default runtime restored.\n"
            "Effective on next session."
        ),
        requires_new_session=True,
    )
