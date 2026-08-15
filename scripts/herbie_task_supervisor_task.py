#!/usr/bin/env python3
"""Deterministic task-ingestion entrypoint for supervised owner work."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from task_supervisor.ledger import create_task_entry, default_paths, format_time, promote_next_queued_task, utc_now  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register or promote Herbie supervised tasks")
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="register a consequential owner task")
    start.add_argument("--base-dir", type=Path, default=None)
    start.add_argument("--task-id", required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--owner", default="Steve")
    start.add_argument("--spec-path", type=Path, required=True)
    start.add_argument("--spec-version", default="unspecified")
    start.add_argument("--parallel-authorized", action="store_true")

    promote = sub.add_parser("promote-next", help="owner-controlled queue promotion")
    promote.add_argument("--base-dir", type=Path, default=None)

    args = parser.parse_args(argv)
    paths = default_paths(args.base_dir)
    if args.cmd == "start":
        spec = args.spec_path.resolve()
        if not spec.exists():
            parser.error(f"spec path does not exist: {spec}")
        task = create_task_entry(
            paths,
            task_id=args.task_id,
            title=args.title,
            owner=args.owner,
            spec_filename=spec.name,
            spec_path=str(spec),
            spec_version=args.spec_version,
            spec_sha256=_sha256(spec),
            parallel_authorized=args.parallel_authorized,
        )
        print(f"task_id={task['task_id']}")
        print(f"status={task['status']}")
        print(f"created_at={task['created_at']}")
        return 0
    task = promote_next_queued_task(paths, now=utc_now(), owner_controlled=False)
    if task is None:
        print("no queued task promoted")
        return 0
    print(f"promoted_task_id={task['task_id']}")
    print(f"status={task['status']}")
    print(f"promoted_at={format_time(utc_now())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
