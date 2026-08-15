#!/usr/bin/env python3
"""Script-only cron entrypoint for the Herbie active task supervisor."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from task_supervisor.watchdog import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
