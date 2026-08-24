"""Gateway control plane for session-scoped SSH execution.

This is intentionally a user-owned control plane.  Selecting an SSH target
requires an explicit ``/ssh use`` command; ordinary agent turns never switch
targets on their own.  The selected alias is persisted by durable gateway
session key while connection details continue to live in the profile-scoped
SSH target registry.
"""

from __future__ import annotations

import shlex
from typing import Any

from gateway.platforms.base import MessageEvent
from gateway.ssh_bindings import (
    LOCAL_BACKEND,
    clear_ssh_binding,
    get_ssh_binding,
    resolve_binding_target,
    set_ssh_binding,
)
from gateway.ssh_targets import (
    find_ssh_target,
    load_ssh_targets,
    render_ssh_targets,
    validate_ssh_target_for_runtime,
)


def _parse_ssh_args(raw_args: str) -> tuple[list[str], str | None]:
    try:
        return shlex.split(raw_args), None
    except ValueError as exc:
        return [], f"Invalid /ssh arguments: {exc}"


def _parse_use_args(parts: list[str]) -> tuple[str, str | None, str | None]:
    """Return ``(alias, cwd, error)`` for ``/ssh use`` arguments."""

    if not parts:
        return "", None, "Usage: /ssh use <alias> [--cwd <remote-path>]"
    alias = parts[0].strip()
    cwd = None
    index = 1
    while index < len(parts):
        token = parts[index]
        if token != "--cwd":
            return "", None, f"Unknown /ssh use option: `{token}`"
        if cwd is not None or index + 1 >= len(parts):
            return "", None, "Usage: /ssh use <alias> [--cwd <remote-path>]"
        cwd = parts[index + 1].strip() or None
        index += 2
    return alias, cwd, None


class GatewaySshModeMixin:
    """Session-scoped SSH command handling and runtime preparation."""

    @staticmethod
    def _ssh_help() -> str:
        return (
            "Usage:\n"
            "/ssh list — list local and configured SSH backends\n"
            "/ssh status — inspect this session's active backend\n"
            "/ssh test <alias> — validate a target configuration without switching\n"
            "/ssh use <alias> [--cwd <remote-path>] — use SSH for this session\n"
            "/ssh local — return this session to local execution\n"
            "/ssh off — alias for /ssh local"
        )

    def _ssh_session_key(self, event: MessageEvent) -> str:
        return self._session_key_for_source(event.source)

    def _ssh_backend_signature(self, session_key: str) -> dict[str, Any]:
        """Return cache-busting fields for the session's effective backend."""

        binding = get_ssh_binding(session_key)
        if binding is None:
            return {"terminal.session_backend": LOCAL_BACKEND}
        resolved = resolve_binding_target(
            session_key,
            targets=load_ssh_targets(),
        )
        if resolved is None:
            return {
                "terminal.session_backend": "ssh",
                "terminal.session_ssh_alias": binding.alias,
                "terminal.session_ssh_error": "target_unavailable",
            }
        _, target = resolved
        validation_error = validate_ssh_target_for_runtime(target)
        return {
            "terminal.session_backend": "ssh",
            "terminal.session_ssh_alias": binding.alias,
            "terminal.session_ssh_host": target.host,
            "terminal.session_ssh_user": target.user,
            "terminal.session_ssh_port": target.port or 22,
            "terminal.session_cwd": binding.cwd or target.cwd,
            "terminal.session_ssh_error": validation_error,
        }

    def _prepare_ssh_runtime(
        self,
        *,
        session_key: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Apply the persisted binding to this turn's tool task.

        Invalid or removed targets resolve to an SSH override with empty
        connection fields.  Environment construction then fails closed instead
        of silently running the turn's tools on the local host.
        """

        from gateway.ssh_bindings import resolve_binding_task_overrides
        from tools.terminal_tool import (
            clear_task_env_overrides,
            register_task_env_overrides,
            resolve_task_overrides,
        )

        overrides = resolve_binding_task_overrides(session_key)
        runtime_task_id = task_id or session_key
        if overrides:
            # This runs before every ordinary turn.  Re-registering an
            # unchanged cwd override would reseed the session cwd and undo a
            # ``cd`` performed by the previous turn, so only mutate the task
            # registry when the effective binding actually changed.
            if dict(resolve_task_overrides(runtime_task_id)) != overrides:
                register_task_env_overrides(runtime_task_id, overrides)
        elif resolve_task_overrides(runtime_task_id).get("env_type") == "ssh":
            clear_task_env_overrides(runtime_task_id)
        return overrides

    async def _handle_ssh_command(self, event: MessageEvent) -> str:
        """Handle the explicit gateway ``/ssh`` control plane."""

        parts, parse_error = _parse_ssh_args(event.get_command_args().strip())
        if parse_error:
            return parse_error
        action = parts[0].lower() if parts else "help"
        args = parts[1:]

        if action in {"help", ""}:
            return self._ssh_help()

        session_key = self._ssh_session_key(event)
        binding = get_ssh_binding(session_key)
        current_alias = binding.alias if binding else LOCAL_BACKEND

        if action == "list":
            if args:
                return "Usage: /ssh list"
            return render_ssh_targets(
                load_ssh_targets(),
                current_alias=current_alias,
            )

        if action == "status":
            if args:
                return "Usage: /ssh status"
            if binding is None:
                return (
                    "SSH status:\n"
                    "- current backend: `local`\n"
                    "- session binding: none"
                )
            resolved = resolve_binding_target(
                session_key,
                targets=load_ssh_targets(),
            )
            if resolved is None:
                return (
                    "SSH status:\n"
                    f"- requested backend: `{binding.alias}`\n"
                    "- current backend: unavailable (target is missing from the registry)\n"
                    "- execution: blocked until the target is restored or `/ssh local` is used"
                )
            _, target = resolved
            validation_error = validate_ssh_target_for_runtime(target)
            if validation_error:
                return (
                    "SSH status:\n"
                    f"- requested backend: `{binding.alias}`\n"
                    f"- current backend: unavailable ({validation_error})\n"
                    "- execution: blocked until the target is fixed or `/ssh local` is used"
                )
            lines = [
                "SSH status:",
                f"- current backend: `{binding.alias}` (ssh)",
                f"- host: {target.host}",
                f"- user: {target.user}",
                f"- port: {target.port or 22}",
            ]
            cwd = binding.cwd or target.cwd
            if cwd:
                lines.append(f"- cwd: {cwd}")
            if target.identity_file:
                lines.append("- identity: [REDACTED_PATH]")
            return "\n".join(lines)

        if action == "test":
            if len(args) != 1:
                return "Usage: /ssh test <alias>"
            alias = args[0]
            if alias.lower() == LOCAL_BACKEND:
                return "SSH test: `local` backend configuration is valid."
            target = find_ssh_target(load_ssh_targets(), alias)
            if target is None:
                return (
                    f"Unknown SSH target: `{alias}`. No backend change was made. "
                    "Use `/ssh list` to see configured targets."
                )
            target_error = validate_ssh_target_for_runtime(target)
            if target_error:
                return target_error
            return (
                f"SSH test: `{alias}` configuration is valid. "
                "No connection was opened and no backend change was made."
            )

        if action in {"local", "off"}:
            if args:
                return f"Usage: /ssh {action}"
            clear_ssh_binding(session_key)
            # The cached agent owns the old task id/environment.  Eviction is
            # the existing session-safe resource teardown path; the next turn
            # also clears any remaining task override before tool dispatch.
            self._evict_cached_agent(session_key)
            return "SSH disabled for this session. Current backend: `local`."

        if action == "use":
            alias, cwd, use_error = _parse_use_args(args)
            if use_error:
                return use_error
            if alias.lower() == LOCAL_BACKEND:
                return "Use `/ssh local` to return to local execution."
            targets = load_ssh_targets()
            target = find_ssh_target(targets, alias)
            if target is None:
                return (
                    f"Unknown SSH target: `{alias}`. No backend change was made. "
                    "Use `/ssh list` to see configured targets."
                )
            target_error = validate_ssh_target_for_runtime(target)
            if target_error:
                return target_error

            selected = set_ssh_binding(
                session_key,
                alias=alias,
                cwd=cwd,
                source="user",
            )
            # Changing backend changes prompt environment facts and tool
            # routing. Rebuild the per-session agent on the next turn.
            self._evict_cached_agent(session_key)
            lines = [
                f"SSH enabled for this session: `{selected.alias}`",
                "- backend: ssh",
            ]
            effective_cwd = selected.cwd or target.cwd
            if effective_cwd:
                lines.append(f"- cwd: {effective_cwd}")
            if target.identity_file:
                lines.append("- identity: [REDACTED_PATH]")
            lines.append(
                "Future terminal, file, and execute_code calls in this session "
                "will use the SSH backend."
            )
            return "\n".join(lines)

        return self._ssh_help()
