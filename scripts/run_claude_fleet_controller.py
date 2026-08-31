#!/usr/bin/env python3
"""Thin CLI for one P6 fleet-controller pass.

All policy lives in the tracked config; all decisions live in the planner.
This wrapper parses flags, runs one pass, prints one summary line, and
propagates the controller's exit code (0 ok, 2 config error, 3 lock held,
4 runtime failure).

``--allow-enforce`` is the SECOND enforcement gate. The deployed scheduled
task deliberately omits it, so even a config edited to mode=enforce cannot
act from the scheduled lane without a new, reviewed task definition.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make the agent-src root importable when invoked by path (the scheduled
# runner calls this file directly rather than via -m).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from claude_fleet_control.controller import Controller  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=None,
                        help="policy config path (default: tracked claude_fleet_control/config.json)")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="state/lock directory (default: ~/.hermes/fleet_control)")
    parser.add_argument("--allow-enforce", action="store_true",
                        help="invocation-side enforcement gate; NEVER set on the scheduled task "
                             "without fresh explicit approval")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    controller = Controller(
        config_path=args.config,
        state_dir=args.state_dir,
        allow_enforce=args.allow_enforce,
    )
    exit_code, result = controller.run_once()
    if result is not None:
        print(
            f"fleet-controller: status={result.status} executor_called={result.executor_called} "
            f"plan={result.plan_id} detail={result.detail}"
        )
    else:
        print(f"fleet-controller: no pass (exit {exit_code})")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
