"""CLI for the read-only Mission Control routing replay."""

from __future__ import annotations

import argparse
import json

from events.paths import audit_log_path
from events.routing_replay import replay_audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--audit-path", default=str(audit_log_path()))
    args = parser.parse_args()
    report = replay_audit(args.audit_path, limit=args.limit)
    report.pop("rows", None)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
