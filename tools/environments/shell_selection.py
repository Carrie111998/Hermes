"""Single source of truth for the ``terminal.shell`` selection.

All consumers resolve the active shell dialect through this module so the
selection semantics cannot drift between entry points:

- ``agent.prompt_builder.build_environment_hints()`` — model-facing dialect
  hint (the Windows-local shell hint becomes selection-aware);
- ``tools.environments.local.LocalEnvironment`` — local foreground
  execution (``_run_bash`` / ``init_session``);
- ``tools.process_registry.ProcessRegistry.spawn_local`` — local background
  and PTY execution.

Value policy (fixed by this slice):

- ``bash`` — the default; allowed on every host and backend; preserves the
  existing behavior byte-for-byte.
- ``pwsh`` — opt-in; valid ONLY for the native Windows local backend
  (``sys.platform == "win32"``, not WSL, ``TERMINAL_ENV`` local). Synchronous
  foreground execution is implemented; background and PTY entry points raise
  :class:`ShellExecutionNotImplementedError` instead of silently running bash.
- Anything else — rejected with :class:`ShellSelectionError` and a clear
  message naming the allowed values.

Normalization policy (pinned by tests): leading/trailing whitespace is
trimmed and case is ignored; the value must then match one of the two
allowed words exactly.

The value is read from the ``TERMINAL_SHELL`` environment projection, which
is an internal implementation detail of the existing config bridge
(``hermes_cli.config.apply_terminal_config_to_env`` and the CLI/gateway
``TERMINAL_*`` maps).  No new user-facing environment variable is added.
"""

import ntpath
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable

from hermes_constants import is_wsl

SHELL_BASH = "bash"
SHELL_PWSH = "pwsh"
ALLOWED_SHELLS = frozenset({SHELL_BASH, SHELL_PWSH})
DEFAULT_SHELL = SHELL_BASH

_active_shell_name: str | None = None
_active_shell_lock = threading.Lock()

# Internal env projection of ``terminal.shell`` / ``terminal.backend``.
SHELL_ENV_VAR = "TERMINAL_SHELL"
BACKEND_ENV_VAR = "TERMINAL_ENV"


class ShellSelectionError(ValueError):
    """``terminal.shell`` is not a supported value, or a supported value is
    used outside its only allowed host/backend combination."""


class ShellExecutionNotImplementedError(RuntimeError):
    """A valid shell selection reached an execution path that this build has
    not implemented yet.  Raised instead of silently running a different
    shell."""


class PwshExecutableNotFoundError(FileNotFoundError):
    """PowerShell 7 could not be resolved and validated for native execution."""


def _windows_path_key(path: str) -> str:
    """Return a case-insensitive comparison key for a Windows path."""
    return ntpath.normcase(ntpath.normpath(path))


def candidate_pwsh_paths(
    env: dict | None = None,
    *,
    which_fn: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Return PowerShell 7 executable candidates in deterministic order.

    Existing PATH semantics remain authoritative.  The standard PowerShell 7
    installation is only a fallback for native Windows installations whose
    installer did not update the process PATH.  Windows PowerShell 5.1 is not
    a candidate because selecting ``pwsh`` must never change dialect silently.
    """
    env = os.environ if env is None else env
    which_fn = shutil.which if which_fn is None else which_fn
    candidates = [which_fn("pwsh"), which_fn("pwsh.exe")]
    program_files = env.get("ProgramFiles") or r"C:\Program Files"
    candidates.append(ntpath.join(program_files, "PowerShell", "7", "pwsh.exe"))

    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = _windows_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(candidate)
    return tuple(resolved)


def _probe_pwsh_executable(candidate: str) -> bool:
    """Return whether *candidate* starts a non-interactive PowerShell Core 7+."""
    from hermes_cli._subprocess_compat import windows_hide_flags

    probe = (
        "if ($PSVersionTable.PSEdition -eq 'Core' -and "
        "$PSVersionTable.PSVersion.Major -ge 7) { exit 0 }; exit 1"
    )
    try:
        result = subprocess.run(
            [candidate, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=windows_hide_flags(),
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def resolve_pwsh_executable(
    env: dict | None = None,
    *,
    which_fn: Callable[[str], str | None] | None = None,
    exists_fn: Callable[[str], bool] | None = None,
    probe_fn: Callable[[str], bool] | None = None,
) -> str:
    """Resolve and validate the PowerShell 7 executable, or fail closed."""
    env = os.environ if env is None else env
    which_fn = shutil.which if which_fn is None else which_fn
    exists_fn = os.path.isfile if exists_fn is None else exists_fn
    probe_fn = _probe_pwsh_executable if probe_fn is None else probe_fn

    resolved_from_path = {name: which_fn(name) for name in ("pwsh", "pwsh.exe")}
    path_hits = {
        _windows_path_key(hit)
        for hit in resolved_from_path.values()
        if hit
    }
    candidates = candidate_pwsh_paths(
        env,
        which_fn=resolved_from_path.get,
    )
    checked: list[str] = []
    for candidate in candidates:
        checked.append(candidate)
        if _windows_path_key(candidate) not in path_hits and not exists_fn(candidate):
            continue
        if probe_fn(candidate):
            return candidate

    rendered = ", ".join(checked) if checked else "PATH and the standard install location"
    raise PwshExecutableNotFoundError(
        "terminal.shell is 'pwsh', but no runnable PowerShell 7 pwsh.exe was "
        f"found. Checked: {rendered}. Install PowerShell 7 or add pwsh.exe to PATH; "
        "Hermes will not fall back to another shell dialect."
    )


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _is_local_backend(env: dict, backend: str | None) -> bool:
    active = (
        _normalize(backend)
        if backend is not None
        else _normalize(env.get(BACKEND_ENV_VAR) or "local")
    )
    return active in ("", "local")


def resolve_shell_name(
    env: dict | None = None,
    *,
    platform_name: str | None = None,
    wsl: bool | None = None,
    backend: str | None = None,
) -> str:
    """Return the canonical ``terminal.shell`` name (``"bash"`` | ``"pwsh"``).

    Reads the ``TERMINAL_SHELL`` projection (default ``bash`` when unset or
    empty), applies the pinned normalization policy, then enforces the
    host/backend constraint: ``pwsh`` requires a native Windows host (not
    WSL) with the local backend.  Raises :class:`ShellSelectionError` with a
    clear message otherwise.

    The keyword arguments exist so the semantics are testable without
    mutating the process environment; callers that omit them get the real
    host, WSL state, and backend env values.
    """
    env = os.environ if env is None else env
    raw_value = env.get(SHELL_ENV_VAR) or DEFAULT_SHELL
    name = _normalize(raw_value)

    if name not in ALLOWED_SHELLS:
        raise ShellSelectionError(
            f"unsupported terminal.shell value {raw_value!r}: expected one of "
            f"{sorted(ALLOWED_SHELLS)} (whitespace-trimmed, case-insensitive)"
        )

    if name == SHELL_PWSH:
        host = platform_name if platform_name is not None else sys.platform
        if host != "win32":
            raise ShellSelectionError(
                f"terminal.shell 'pwsh' requires a native Windows host; "
                f"current platform is {host!r}"
            )
        if wsl is None:
            wsl = is_wsl()
        if wsl:
            raise ShellSelectionError(
                "terminal.shell 'pwsh' requires a native Windows host; "
                "WSL keeps the bash contract"
            )
        if not _is_local_backend(env, backend):
            active = _normalize(backend) if backend is not None else _normalize(
                env.get(BACKEND_ENV_VAR) or "local"
            )
            raise ShellSelectionError(
                f"terminal.shell 'pwsh' requires the local terminal backend; "
                f"current backend is {active!r}"
            )

    return name


def get_active_shell_name() -> str:
    """Return the shell identity frozen for this Hermes process.

    ``terminal.shell`` is projected before tool/prompt modules are imported.
    Caching the first validated value keeps tool schema, prompt guidance, and
    subsequently-created local environments on one identity even if an
    in-process config command mutates the projection. A Hermes restart is
    required to adopt a different configured shell.
    """
    global _active_shell_name
    if _active_shell_name is None:
        with _active_shell_lock:
            if _active_shell_name is None:
                _active_shell_name = resolve_shell_name()
    return _active_shell_name


def _clear_active_shell_name_cache() -> None:
    """Reset the process identity for tests only."""
    global _active_shell_name
    with _active_shell_lock:
        _active_shell_name = None


def reject_unimplemented_shell(
    env: dict | None = None,
    *,
    platform_name: str | None = None,
    wsl: bool | None = None,
    backend: str | None = None,
) -> None:
    """Fail closed when the selected shell has no implemented execution path.

    A valid ``pwsh`` selection (native Windows local backend) that reaches an
    unimplemented entry point (currently background or PTY execution) raises
    :class:`ShellExecutionNotImplementedError` — the caller must NOT fall back
    to bash. Invalid combinations surface first as
    :class:`ShellSelectionError` so configuration mistakes are diagnosed as
    configuration errors, not missing features.
    """
    if resolve_shell_name(
        env=env,
        platform_name=platform_name,
        wsl=wsl,
        backend=backend,
    ) != SHELL_BASH:
        raise ShellExecutionNotImplementedError(
            "terminal.shell 'pwsh' is configured, but PowerShell execution "
            "for background processes and PTY sessions is not implemented in "
            "this build. Refusing to run bash under a pwsh selection; use "
            "foreground execution or set terminal.shell back to 'bash'."
        )
