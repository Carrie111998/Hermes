#!/usr/bin/env python3
"""Non-agent cron reconciliation wrapper for github-pr-feedback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _cron_exit_code(stdout: str, process_returncode: int) -> int:
    """Keep partial repair retries visible without declaring the whole cron crashed."""

    if process_returncode == 0:
        return 0
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return process_returncode
    try:
        payload = json.loads(lines[-1])
    except (json.JSONDecodeError, TypeError):
        return process_returncode
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return process_returncode
    repair = payload.get("repair")
    if not isinstance(repair, dict) or repair.get("status") != "degraded":
        return process_returncode
    for lane in ("merge", "release_maintenance"):
        section = payload.get(lane)
        if isinstance(section, dict) and section.get("status") == "degraded":
            return process_returncode
    return 0


def main() -> int:
    configured = os.environ.get("HERMES_EXECUTABLE", "").strip()
    executable = Path(configured) if configured else None
    if (
        executable is None
        or not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        print(
            "HERMES_EXECUTABLE must name an executable absolute path",
            file=sys.stderr,
        )
        return 127
    completed = subprocess.run(
        [str(executable), "github-pr-feedback", "scan"],
        check=False,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return _cron_exit_code(completed.stdout, completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
