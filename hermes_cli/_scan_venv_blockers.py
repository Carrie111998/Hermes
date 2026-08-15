"""``hermes_cli/_scan_venv_blockers.py`` — Standalone venv-process scan for JSON consumption.

Invoked by the Desktop Electron app::

    venv\\Scripts\\python.exe -m hermes_cli._scan_venv_blockers

Exits 0 for valid clear or blocked results.  Non-zero exit signals probe
failure (the detector itself crashed, psutil unavailable, etc.).  Exactly
one JSON document on stdout; diagnostics on stderr only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from hermes_constants import get_hermes_home

# Long CLI flags whose argument value must be redacted from the cmdline.
_SENSITIVE_LONG_FLAGS: list[str] = [
    "--token",
    "--api-key",
    "--password",
    "--secret",
    "--authorization",
    "--access-key",
    "--private-key",
    "--session-key",
]


def _probe_fail_json(diagnostic: str = "probe failed") -> str:
    """Return the standard probe-failure JSON document.

    ``ok: false`` plus ``probe_failed: true`` means the detector itself could
    not run — this is *not* a clear scan. Callers must treat
    ``ok is not True`` / non-zero exit as probe failure, never as
    ``blocked: false`` "clear" (#83149).
    """
    return json.dumps(
        {
            "ok": False,
            "probe_failed": True,
            "blocked": False,
            "processes": [],
            "error": diagnostic,
        }
    )


def _emit_probe_fail(diagnostic: str) -> NoReturn:
    """Print one JSON to stdout, diagnostic to stderr, exit non-zero."""
    print(_probe_fail_json(diagnostic))
    print(diagnostic, file=sys.stderr)
    sys.exit(1)


def _find_flag(text: str, flag: str) -> int:
    """Return the index of *flag* when it starts the string or follows a space.

    Returns -1 when not found.  This avoids matching ``--token`` inside an
    embedded token or path like ``/some--token-thing``.
    """
    low = text.lower()
    fl = flag.lower()
    pos = 0
    while True:
        idx = low.find(fl, pos)
        if idx == -1:
            return -1
        if idx == 0 or text[idx - 1] == " ":
            return idx
        pos = idx + 1


def _redact_sensitive_cmdline(cmdline: str) -> str:
    """Apply generic secret redaction then long-flag redaction.

    If the generic redactor itself fails, return ``"<redacted>"`` — the PID
    and process name still provide actionable diagnostics.
    """
    # Generic pass: the project's shared secret redactor.
    try:
        from agent.redact import redact_sensitive_text  # noqa: PLC0415

        cmdline = redact_sensitive_text(cmdline, force=True)
    except Exception:
        return "<redacted>"

    # Conservative long-flag pass: preserve the flag name, replace the value
    # and everything after it with ``<redacted>``.  Short flags (-t, -k, -p)
    # are intentionally not redacted — they are ambiguous and may be useful
    # diagnostics (toolset, port, profile).
    earliest = len(cmdline)
    for flag in _SENSITIVE_LONG_FLAGS:
        # --flag=value  →  preserve "--flag="
        idx = _find_flag(cmdline, flag + "=")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1
        # --flag value  →  preserve "--flag "
        idx = _find_flag(cmdline, flag + " ")
        if idx != -1 and idx + len(flag) + 1 < earliest:
            earliest = idx + len(flag) + 1

    if earliest < len(cmdline):
        return cmdline[:earliest] + "<redacted>"
    return cmdline


def _is_pausable_gateway(cmdline: str) -> bool:
    """Return True when *cmdline* is a gateway process the updater can pause.

    A running gateway shows up in the venv-holder scan as one or both halves
    of its launcher/worker chain (``venv\\Scripts\\python.exe -m
    hermes_cli.main gateway run`` and the uv-side interpreter re-running the
    same argv). Reporting those as blockers dead-ends the Desktop update:
    the preflight aborts with ``venv-blocked`` *before* spawning
    ``hermes-setup``, so the CLI updater's own
    ``_pause_windows_gateways_for_update()`` — which exists precisely to
    stop these processes (and is always active: ``hermes-setup`` invokes
    ``hermes update --yes --gateway``) — never gets the chance to run.

    Only gateway invocations are exempted. Anything else running from the
    venv (an operator's REPL, a stray script, a ``serve`` backend that
    survived the desktop's own teardown) has no pause machinery downstream
    and must keep blocking the handoff.

    Delegates to ``gateway.status.looks_like_gateway_command_line`` — the
    canonical ``gateway run`` matcher (profile-selector aware, shlex
    tokenization, ``run``-only) — so this exemption, the pause discovery,
    and the updater's guard fallback all share one parser. A hand-rolled
    token scan here regressed ``--profile gateway gateway run``: the profile
    *value* shadowed the subcommand token. An import failure counts as
    not-pausable — the scan then reports the process as a blocker, which is
    exactly the pre-exemption behavior.
    """
    try:
        from gateway.status import looks_like_gateway_command_line  # noqa: PLC0415
    except Exception:
        return False
    return looks_like_gateway_command_line(cmdline)


def _detect_managed_node_processes() -> list[tuple[int, str, str]]:
    """Find processes executing from ``HERMES_HOME/node`` on Windows.

    The portable Node runtime is part of the managed Hermes installation.
    Long-lived tools such as n8n must not map its ``node.exe`` while an update
    may replace that tree.  Report these holders separately from venv Python
    processes so the Desktop updater can fail closed before mutating files.
    """
    if sys.platform != "win32":
        return []
    try:
        import psutil  # noqa: PLC0415
    except Exception:
        return []

    try:
        node_prefix = str((Path(get_hermes_home()) / "node").resolve()).lower().rstrip("\\/") + os.sep
    except (OSError, ValueError):
        node_prefix = str(Path(get_hermes_home()) / "node").lower().rstrip("\\/") + os.sep

    skip = {os.getpid()}
    try:
        skip.update(int(proc.pid) for proc in psutil.Process().parents())
    except Exception:
        pass

    matches: list[tuple[int, str, str]] = []
    try:
        processes = psutil.process_iter(["pid", "exe", "name", "cmdline"])
    except Exception:
        return []
    for proc in processes:
        try:
            info = proc.info
            pid = int(info.get("pid"))
            exe = info.get("exe")
            if pid in skip or not exe:
                continue
            try:
                exe_norm = str(Path(exe).resolve()).lower()
            except (OSError, ValueError):
                exe_norm = str(exe).lower()
            if not exe_norm.startswith(node_prefix):
                continue
            name = str(info.get("name") or Path(exe).name)
            cmdline = " ".join(info.get("cmdline") or [])
            matches.append((pid, name, cmdline))
        except Exception:
            continue
    return matches


def main() -> None:
    """Entry point.  Prints one JSON doc to stdout.  Exits 0 for valid scan."""
    try:
        import psutil  # noqa: PLC0415, F401
    except Exception as exc:
        _emit_probe_fail(f"psutil is not available: {exc}")

    try:
        from hermes_cli.main import _detect_venv_python_processes  # noqa: PLC0415

        matches = _detect_venv_python_processes()
    except Exception as exc:
        _emit_probe_fail(f"scan aborted: {exc}")

    venv_processes = [
        {
            "pid": pid,
            "name": name,
            # Truncate for display AFTER the gateway exemption has seen the
            # full cmdline (long managed-runtime interpreter paths would
            # otherwise swallow the `gateway run` argv).
            "cmdline": _redact_sensitive_cmdline(cmdline)[:120],
        }
        for pid, name, cmdline in matches
        if not _is_pausable_gateway(cmdline)
    ]
    managed_node_matches = _detect_managed_node_processes()
    managed_node_processes = [
        {
            "pid": pid,
            "name": name,
            "cmdline": _redact_sensitive_cmdline(cmdline)[:120],
        }
        for pid, name, cmdline in managed_node_matches
    ]
    processes = venv_processes + managed_node_processes
    exempted = sum(1 for _pid, _name, cmdline in matches if _is_pausable_gateway(cmdline))
    data = {
        "ok": True,
        "blocked": bool(processes),
        "processes": processes,
        # Diagnostic only: gateway processes present but not counted as
        # blockers because the downstream updater pauses them itself.
        "pausable_gateways": exempted,
        "managed_node_processes": len(managed_node_processes),
    }
    print(json.dumps(data))
    sys.exit(0)


if __name__ == "__main__":
    main()