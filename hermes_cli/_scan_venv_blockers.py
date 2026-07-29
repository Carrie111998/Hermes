"""``hermes_cli/_scan_venv_blockers.py`` — Standalone venv-process scan for JSON consumption.

Invoked by the Desktop Electron app::

    venv\\Scripts\\python.exe -m hermes_cli._scan_venv_blockers

Exits 0 for valid clear or blocked results.  Non-zero exit signals probe
failure (the detector itself crashed, psutil unavailable, etc.).  Exactly
one JSON document on stdout; diagnostics on stderr only.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, NoReturn


_SCAN_SCHEMA_VERSION = 2

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


def _probe_fail_json() -> str:
    """Return the standard probe-failure JSON document."""
    return json.dumps(
        {
            "ok": False,
            "schema_version": _SCAN_SCHEMA_VERSION,
            "blocked": False,
            "processes": [],
            "updater_managed_processes": [],
        }
    )


def _emit_probe_fail(diagnostic: str) -> NoReturn:
    """Print one JSON to stdout, diagnostic to stderr, exit non-zero."""
    print(_probe_fail_json())
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


def _gateway_managed_holder_pids(
    matches: list[tuple[int, str, str]],
    *,
    gateway_pid_finder: Callable[..., list[int]] | None = None,
    process_factory: Callable[[int], Any] | None = None,
) -> set[int]:
    """Return venv-holder PIDs belonging to a live Gateway process tree.

    Desktop must leave independent Gateways alive until the staged official
    updater owns their pause/resume lifecycle.  This classifier is deliberately
    narrow: it trusts the existing Gateway discovery as the seed, then only
    exempts holder PIDs that are live ancestors or descendants of that seed.
    Any discovery or process-tree failure leaves the affected holder blocked.
    """
    holder_pids = {pid for pid, _name, _cmdline in matches}
    if not holder_pids:
        return set()

    if gateway_pid_finder is None:
        try:
            from hermes_cli.gateway import find_gateway_pids
        except Exception:
            return set()
        gateway_pid_finder = find_gateway_pids

    if process_factory is None:
        try:
            import psutil
        except Exception:
            return set()
        process_factory = psutil.Process

    try:
        gateway_pids = gateway_pid_finder(all_profiles=True)
    except Exception:
        return set()

    managed: set[int] = set()
    for gateway_pid in gateway_pids:
        try:
            process = process_factory(int(gateway_pid))
            related_pids = {int(process.pid)}
            related_pids.update(int(parent.pid) for parent in process.parents())
            related_pids.update(
                int(child.pid) for child in process.children(recursive=True)
            )
        except Exception:
            # The PID could have exited, been reused, or become inaccessible
            # between discovery and inspection.  Keep every such holder blocked.
            continue
        managed.update(holder_pids.intersection(related_pids))
    return managed


def _process_payload(match: tuple[int, str, str]) -> dict[str, int | str]:
    """Serialize one holder after the classification phase used raw argv."""
    pid, name, cmdline = match
    return {
        "pid": pid,
        "name": name,
        "cmdline": _redact_sensitive_cmdline(cmdline),
    }


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

    updater_managed_pids = _gateway_managed_holder_pids(matches)
    processes = [_process_payload(match) for match in matches if match[0] not in updater_managed_pids]
    updater_managed_processes = [
        _process_payload(match) for match in matches if match[0] in updater_managed_pids
    ]
    data = {
        "ok": True,
        "schema_version": _SCAN_SCHEMA_VERSION,
        "blocked": bool(processes),
        "processes": processes,
        "updater_managed_processes": updater_managed_processes,
    }
    print(json.dumps(data))
    sys.exit(0)


if __name__ == "__main__":
    main()
