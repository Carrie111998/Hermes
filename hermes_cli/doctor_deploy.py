"""``hermes doctor deploy`` — verify every running hermes-agent process is on current code (deploy discipline).

For every running long-lived process running hermes-agent code, report:

    pid | process kind | started_at | HEAD-at-start | current HEAD | STALE?

HEAD-at-start comes from ``HERMES_AGENT_HEAD`` stamped into the process
environment at spawn time by the three spawn sites (gateway/run.py,
hermes_cli/web_server.py, apps/desktop/electron/main.ts) plus the cmdline
classification in :mod:`hermes_cli.process_discovery`. A process whose
``HERMES_AGENT_HEAD`` differs from the current install HEAD is flagged STALE.

Exit code: 0 when no process is provably stale (including the case where
nothing verifiable is running); non-zero when at least one running process is
provably on old code, or when the current install HEAD cannot be resolved (a
verifier that cannot run is not a clean bill of health). A process with no
readable HEAD stamp is reported as "(unknown)" and does NOT by itself fail the
check — that is a gap, not a definitive stale proof; the deployment gate
(deploy_gate.py) fails closed when the verifier is missing rather than when an
individual process is unverifiable.
"""

from __future__ import annotations

import datetime
import sys
from typing import Optional

from hermes_cli.colors import Colors, color


def _discover() -> list:
    """Return discovered hermes-agent long-lived processes (fail-safe empty)."""
    try:
        from hermes_cli.process_discovery import discover_processes

        return discover_processes()
    except Exception:
        return []


def _current_head() -> Optional[str]:
    """Resolve the current install HEAD (None when it cannot be resolved)."""
    try:
        from hermes_cli.process_discovery import current_install_head

        return current_install_head()
    except Exception:
        return None


def _fmt_started(started: Optional[float]) -> str:
    if not started:
        return "unknown"
    try:
        return datetime.datetime.fromtimestamp(
            started, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{started:.0f}"


def run_doctor_deploy(args=None) -> int:
    """Implement ``hermes doctor deploy``: list processes, flag stale, exit non-zero if any."""
    processes = _discover()
    current_head = _current_head()

    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│        🚀 Hermes Doctor — deploy verification            │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    if current_head is None:
        print()
        print(color("⚠ Cannot resolve current install HEAD — stale check skipped.", Colors.YELLOW))
        print("  Run this from the hermes-agent install directory or set HERMES_AGENT_INSTALL.")
        return 1

    print()
    print(f"  Current install HEAD: {current_head}")
    print()

    if not processes:
        print("  No running hermes-agent long-lived processes found.")
        print(color("  ✓ No stale processes (nothing to verify)", Colors.GREEN))
        return 0

    header = f"{'PID':>7}  {'KIND':<10} {'STARTED':<20} {'HEAD-AT-START':<12} STALE?"
    print(f"  {header}")
    print(f"  {'-' * len(header)}")

    stale: list = []
    for p in processes:
        head_at_start = p.head_at_start
        if head_at_start is None:
            stale_mark = color("?", Colors.YELLOW)
        elif head_at_start != current_head:
            stale_mark = color("STALE", Colors.RED)
            stale.append(p)
        else:
            stale_mark = color("ok", Colors.GREEN)

        short_head = (head_at_start or "unknown")[:12]
        print(
            f"  {p.pid:>7}  {p.kind:<10} {_fmt_started(p.start_time):<22} "
            f"{short_head:<12} {stale_mark}"
        )

    print()
    if not stale:
        print(color(f"  ✓ No stale processes — all on {current_head[:12]}", Colors.GREEN))
        return 0

    print(
        color(
            f"  ✗ {len(stale)} process(es) not running current code "
            f"(HEAD {current_head[:12]}). Restart via "
            f"`hermes gateway restart --all`.",
            Colors.RED,
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(run_doctor_deploy())
