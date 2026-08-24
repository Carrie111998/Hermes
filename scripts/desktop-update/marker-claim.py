#!/usr/bin/env python3
"""Ownership-safe marker claim used by the repo-owned Desktop handoffs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.update_lock import claim_desktop_handoff_marker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--desktop-pid", type=int, default=0)
    parser.add_argument("--lease-at", type=int, required=True)
    args = parser.parse_args()

    reason = claim_desktop_handoff_marker(
        args.marker,
        owner_pid=args.owner_pid,
        desktop_pid=args.desktop_pid if args.desktop_pid > 0 else None,
        lease_at=args.lease_at,
    )
    if reason is not None:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
