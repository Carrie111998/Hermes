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
from typing import Any, NoReturn

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
    return json.dumps({"ok": False, "blocked": False, "processes": []})


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


def _parse_desktop_child_pids(raw: str | None) -> set[int]:
    """Parse the Desktop's ``HERMES_DESKTOP_CHILD_PID`` env value.

    The Desktop hands the update process the PIDs of every backend it
    manages (comma-separated; a lone int parses for back-compat) so the
    reaper can skip them — the same value ``_kill_stale_dashboard_processes``
    reads from its own environment. Tolerant of junk, exactly like that
    reader. Returns an empty set when unset or unparsable.

    This is the canonical copy; ``hermes_cli.update_cmd`` imports it rather
    than re-defining it, so the preflight and the guard read one definition.
    """
    if not raw:
        return set()
    parsed: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.add(int(part))
        except (ValueError, TypeError):
            pass
    return parsed


def _is_desktop_managed_backend(proc: Any, argv: str) -> bool:
    """Return True when *proc* is a ``serve``/``dashboard`` backend the
    Desktop app spawned and supervises.

    The Desktop marks every backend it spawns (primary + pool profiles) with
    ``HERMES_DESKTOP=1`` in the child environment (apps/desktop/electron/
    main.ts), the same marker that routes the backend into its cron ticker.
    Killing a desktop-managed backend is futile — the app respawns it within
    seconds — so the pausable classification must refuse on it, exactly as
    ``_orphaned_desktop_backend_pids`` refuses on any live-parent backend.

    Three signals, first hit wins:

    - ``HERMES_DESKTOP=1`` in the holder's own environment — authoritative,
      works even when the parent hop is a ``cmd.exe`` launcher, and cannot
      be confused with a manually-started backend.
    - the holder's PID in OUR ``HERMES_DESKTOP_CHILD_PID`` — the Desktop
      passes the PIDs of every backend it manages to the process it spawns
      (the same list ``_kill_stale_dashboard_processes`` reads and skips); a
      holder on that list is desktop-managed by the Desktop's own admission.
    - parent fallback for unreadable holder env: a live parent whose
      executable is the packaged Electron app
      (``.../release/<plat>-unpacked/Hermes[.exe]``).

    psutil errors never raise — an undecidable holder simply does not count
    as desktop-managed and the pausable classification stands. A psutil
    stand-in without ``environ``/``parent``/``pid`` (unit-test fakes) thus
    classifies purely on argv, exactly like an unreadable-env real process.

    This is the shared predicate used by BOTH the Desktop preflight
    (``_scan_venv_blockers.main``) and the CLI updater's guard
    (``update_cmd._leftover_pausable_gateway_pids``), so one cmdline gets the
    same desktop-managed verdict in both views of the process table.
    """
    try:
        env = proc.environ() or {}
        marker = env.get("HERMES_DESKTOP")
        if isinstance(marker, bytes):
            marker = marker.decode("utf-8", "replace")
        if str(marker) == "1":
            return True
    except Exception:
        pass  # fall through to the parent check

    try:
        if int(proc.pid) in _parse_desktop_child_pids(
            os.environ.get("HERMES_DESKTOP_CHILD_PID")
        ):
            return True
    except (TypeError, ValueError, AttributeError):
        pass

    try:
        parent = proc.parent()
    except Exception:
        return False
    if parent is None or not parent.is_running():
        return False
    low = argv.lower()
    if not (
        "hermes_cli.main" in low
        and (
            " serve" in low
            or " dashboard" in low
            or low.endswith(" serve")
            or low.endswith(" dashboard")
        )
    ):
        return False
    try:
        parent_low = (parent.exe() or "").lower()
    except Exception:
        return False
    return parent_low.endswith(("hermes.exe", "hermes")) and "-unpacked" in parent_low


def _is_pausable_hermes_process(cmdline: str, proc: Any = None) -> bool:
    """Return True when *cmdline* is a backend the updater can stop itself.

    A running gateway shows up in the venv-holder scan as one or both halves
    of its launcher/worker chain (``venv\\Scripts\\python.exe -m
    hermes_cli.main gateway run`` and the uv-side interpreter re-running the
    same argv). A secondary profile's headless backend (``-m hermes_cli.main
    [--profile <p>] serve`` / ``dashboard``) is the same long-lived server
    under a different subcommand. Reporting either as blockers dead-ends the
    Desktop update: the preflight aborts with ``venv-blocked`` *before*
    spawning ``hermes-setup``, so the CLI updater's own machinery — which
    exists precisely to stop these processes — never gets the chance to run
    (``_pause_windows_gateways_for_update()`` pauses gateways, and
    ``_kill_stale_dashboard_processes()`` reaps stale serve/dashboard
    backends; both are always active under ``hermes update --yes``).

    Only backends the updater can stop are exempted. Anything else running
    from the venv (an operator's REPL, a stray script) has no stop machinery
    downstream and must keep blocking the handoff.

    A Desktop-MANAGED ``serve``/``dashboard`` holder (``HERMES_DESKTOP=1`` /
    on the Desktop's ``HERMES_DESKTOP_CHILD_PID`` list / live Electron-app
    parent) is NOT pausable: the app supervises and respawns it within
    seconds, so stopping it is futile and the holder must keep blocking —
    exactly as the CLI updater's guard refuses on the same class (see
    ``_is_desktop_managed_backend``). *proc*, when supplied, supplies that
    desktop-managed signal so the preflight and the guard classify one argv
    identically; when omitted (or unreadable), the cmdline alone decides.

    Delegates to ``gateway.status.looks_like_pausable_hermes_process`` — the
    canonical matcher shared with the pause discovery and the updater's
    guard fallback (``gateway run`` via the strict ``run``-only parser, plus
    ``serve``/``dashboard``; profile-selector aware, shlex tokenization) —
    so the exemption, the pause discovery, and the guard all classify one
    argv identically. A hand-rolled token scan here regressed
    ``--profile gateway gateway run``: the profile *value* shadowed the
    subcommand token. An import failure counts as not-pausable — the scan
    then reports the process as a blocker, which is exactly the
    pre-exemption behavior.
    """
    try:
        from gateway.status import (  # noqa: PLC0415
            looks_like_pausable_hermes_process,
        )
    except Exception:
        return False
    if not looks_like_pausable_hermes_process(cmdline):
        return False
    if proc is not None and _is_desktop_managed_backend(proc, cmdline):
        # The Desktop app is still open and would respawn this backend within
        # seconds; stopping it here is futile. Keep the hard refusal (the
        # same contract _orphaned_desktop_backend_pids documents) — the user
        # closes the app, which kills the backend.
        return False
    return True


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

    processes = []
    exempted = 0
    for pid, name, cmdline in matches:
        proc = None
        try:
            proc = psutil.Process(int(pid))
        except Exception:
            proc = None
        if not _is_pausable_hermes_process(cmdline, proc):
            processes.append(
                {
                    "pid": pid,
                    "name": name,
                    # Truncate for display AFTER the pausable exemption has
                    # seen the full cmdline (long managed-runtime interpreter
                    # paths would otherwise swallow the `gateway run` argv).
                    "cmdline": _redact_sensitive_cmdline(cmdline)[:120],
                }
            )
        else:
            exempted += 1
    data = {
        "ok": True,
        "blocked": bool(processes),
        "processes": processes,
        # Diagnostic only: pausable processes present but not counted as
        # blockers because the downstream updater stops them itself.
        "pausable_gateways": exempted,
    }
    print(json.dumps(data))
    sys.exit(0)


if __name__ == "__main__":
    main()