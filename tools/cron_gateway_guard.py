"""
Gateway self-kill guard for cron jobs (flap-loop prevention).

Root cause (#30719): a ``once`` cron job whose script
ran ``launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`` restarted the very
gateway that hosts the in-process cron scheduler. ``kickstart -k`` killed the
gateway process BEFORE the scheduler could persist the job's completion, so the
never-completed once-job re-fired on every launchd relaunch -> a self-
perpetuating restart (flap) loop.

This module refuses, at cron *create/update* time, to accept a job whose prompt
or script would restart / stop / kill an ``ai.hermes.gateway*`` launchd (or
systemd) service. Such an action must never run from inside the gateway's own
scheduler; it has to come from an EXTERNAL process (a standalone launchd/systemd
oneshot that OUTLIVES the gateway) or be run by a human from a shell.

Design notes:
  * Read-only launchctl/systemctl verbs (print, list, blame, dumpstate) are
    deliberately NOT matched, so monitoring/observability cron jobs that merely
    inspect gateway state are never blocked.
  * The label match requires the literal ``ai.hermes.gateway`` token, so a job
    that restarts an *unrelated* service is not blocked.
  * ``hermes\\s+gateway\\s+restart`` requires whitespace between the words, so
    the dotted label ``ai.hermes.gateway`` in prose ("summarize the
    ai.hermes.gateway logs") does not false-positive.

Reference: https://github.com/NousResearch/hermes-agent/issues/30719

Run ``python tools/cron_gateway_guard.py`` for the built-in self-test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# A Hermes gateway launchd/systemd label: ``ai.hermes.gateway`` and optional
# profile variants such as ``ai.hermes.gateway.<profile>``.
_GATEWAY_LABEL = r"ai\.hermes\.gateway[\w.\-]*"

# launchctl verbs that restart / stop / reload / kill a running service. Excludes
# the read-only verbs (print, list, blame, dumpstate, examine, procinfo) on
# purpose so status-checking cron jobs keep working.
_LAUNCHCTL_LIFECYCLE = (
    r"(?:kickstart|bootout|bootstrap|stop|kill|unload|load|remove|enable|disable)"
)
_SYSTEMCTL_LIFECYCLE = r"(?:restart|stop|start|kill|reload|try-restart|force-reload)"

_GATEWAY_SELFKILL_PATTERNS = re.compile(
    r"(?i)"
    # 1. launchctl <lifecycle-verb> ... ai.hermes.gateway*   (the exact incident)
    rf"(launchctl\s+{_LAUNCHCTL_LIFECYCLE}\b[^\n]*{_GATEWAY_LABEL})"
    # 2. kickstart/bootout/bootstrap are launchctl-specific verbs; block them near
    #    a gateway label even when the `launchctl` token is aliased / on a prior
    #    line (e.g. `lc=launchctl; ... kickstart -k .../ai.hermes.gateway`).
    rf"|((?:kickstart|bootout|bootstrap)\b[^\n]*{_GATEWAY_LABEL})"
    # 3. `hermes gateway restart|stop|start|reload` — the CLI restart of the
    #    gateway (whitespace-separated so the dotted label never matches).
    r"|(hermes\s+gateway\s+(?:restart|stop|start|reload))"
    # 4. systemctl restart/stop ... hermes*gateway   (Linux server deployments)
    rf"|(systemctl\s+(?:--\S+\s+)*{_SYSTEMCTL_LIFECYCLE}\s+[^\n]*hermes[\w.\-]*gateway)"
    # 5. pkill / kill targeting a hermes gateway process
    r"|(p?kill\b[^\n]*hermes[^\n]*gateway)"
)


BLOCK_MESSAGE = (
    "Blocked: this cron job would restart/stop/kill a Hermes gateway "
    "(ai.hermes.gateway*) from inside the gateway's own scheduler (see #30719).\n"
    "\n"
    "WHY this is blocked: the cron scheduler runs IN-PROCESS inside the gateway. "
    "A job that kickstart/bootout/stops its own gateway kills the process before "
    "the scheduler can persist the job's completion, so the job re-runs on every "
    "launchd/systemd relaunch — a self-perpetuating restart (flap) loop. This "
    "failure mode is documented in issue #30719.\n"
    "\n"
    "WHAT TO DO INSTEAD:\n"
    "  - Run the restart from an EXTERNAL process, never a cron job: a standalone "
    "launchd/systemd oneshot (or an `at`-style one-shot) that lives OUTSIDE the "
    "gateway, so killing the gateway does not kill the thing issuing the restart.\n"
    "  - Or have a human run "
    "`launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway` from a shell.\n"
    "  - Never schedule a gateway restart/stop/kill as an in-gateway cron job."
)


def contains_gateway_selfkill(text: Optional[str]) -> bool:
    """Return True if *text* would restart/stop/kill a hermes gateway service."""
    if not text:
        return False
    # Shell treats backslash-newline as a physical-line continuation. Normalize
    # it before applying command-shape patterns so a wrapped lifecycle command
    # cannot evade the guard merely because formatting split it across lines.
    normalized = re.sub(r"\\\r?\n[ \t]*", " ", text)
    return bool(_GATEWAY_SELFKILL_PATTERNS.search(normalized))


def _read_script_text(script: Optional[str]) -> str:
    """Best-effort read of a cron job's script body from ~/.hermes/scripts/.

    Mirrors ``_validate_cron_script_path`` / ``_run_job_script`` containment:
    only paths that resolve INSIDE ``HERMES_HOME/scripts/`` are read. Never
    raises — an absent/unreadable/out-of-tree script yields ``""`` so the prompt
    scan still runs (the runtime once-job attempt-cap is the backstop for a
    script written after job creation).
    """
    if not script or not str(script).strip():
        return ""
    raw = str(script).strip()
    try:
        from hermes_constants import get_hermes_home

        scripts_dir = (get_hermes_home() / "scripts").resolve()
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (scripts_dir / candidate).resolve()
        # Containment: raises ValueError if the resolved path escapes scripts_dir.
        path.relative_to(scripts_dir)
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def check_cron_gateway_selfkill(prompt: Optional[str], script: Optional[str]) -> Optional[str]:
    """Return a blocking error string if the job would self-kill a gateway, else None.

    Scans BOTH the user prompt and the referenced script's body (when the script
    already exists under ~/.hermes/scripts/). Returns ``BLOCK_MESSAGE`` on a hit.
    """
    combined = f"{prompt or ''}\n{_read_script_text(script)}"
    if contains_gateway_selfkill(combined):
        return BLOCK_MESSAGE
    return None


# ---------------------------------------------------------------------------
# Self-test — run `python tools/cron_gateway_guard.py`. No pytest / network /
# gateway required; pure regex assertions. Exits non-zero on any failure.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # (text, should_block)
    CASES = [
        # --- MUST BLOCK: the exact 2026-07-05 incident script + variants -------
        ("launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway", True),
        ("launchctl kickstart -k gui/502/ai.hermes.gateway", True),
        ("#!/bin/bash\nlaunchctl kickstart -k gui/$(id -u)/ai.hermes.gateway\n", True),
        ("launchctl bootout gui/502/ai.hermes.gateway", True),
        ("launchctl bootout gui/502/ai.hermes.gateway.example-profile", True),
        ("launchctl stop ai.hermes.gateway", True),
        ("launchctl kill SIGTERM gui/502/ai.hermes.gateway", True),
        ("launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway.plist", True),
        ("launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.hermes.gateway.plist", True),
        ("hermes gateway restart", True),
        ("hermes gateway stop", True),
        ("systemctl --user restart ai.hermes.gateway", True),
        ("systemctl restart hermes-gateway", True),
        ("pkill -f 'hermes_cli.main gateway'", True),
        ("lc=launchctl\n$lc kickstart -k gui/$(id -u)/ai.hermes.gateway", True),
        # --- MUST NOT BLOCK: legitimate monitoring / unrelated / prose ---------
        ("launchctl print gui/502/ai.hermes.gateway", False),
        ("launchctl list | grep ai.hermes.gateway", False),
        ("launchctl blame gui/502/ai.hermes.gateway", False),
        ("launchctl kickstart -k gui/502/com.other.service", False),
        ("launchctl stop com.example.unrelated", False),
        ("Summarize the ai.hermes.gateway logs and report any restart events.", False),
        ("restart the unrelated dev server on :3333", False),
        ("python -m hermes_cli.main gateway run --replace", False),
        ("echo 'gateway healthy'; curl -s http://localhost/health", False),
        ("", False),
    ]

    failures = []
    for text, expected in CASES:
        got = contains_gateway_selfkill(text)
        mark = "ok " if got == expected else "FAIL"
        if got != expected:
            failures.append((text, expected, got))
        print(f"[{mark}] block={got!s:5} expect={expected!s:5}  {text[:70]!r}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for text, expected, got in failures:
            print(f"  expected block={expected} got={got}: {text!r}")
        sys.exit(1)
    print(f"All {len(CASES)} cases passed.")
    print("\n--- Example blocking message ---")
    print(check_cron_gateway_selfkill(None, None) or "(none)")
    print(BLOCK_MESSAGE)
