"""Read-only-Audit der historischen Async-Delegationsdaten.

Der Audit liest ausschließlich die bestehende Hermes-Datenbank und schreibt
nur einen separaten JSON-Report. Die Runtime und state.db bleiben unverändert.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

from tools.delegation_upcaster import upcast_async_result, upcast_async_task


def audit_database(db_path: Path, limit: int = 1000) -> dict:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """SELECT delegation_id, state, task_json, result_json
               FROM async_delegations ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    report = {
        "database": str(db_path),
        "rows_scanned": len(rows),
        "task_valid": 0,
        "task_invalid": 0,
        "result_valid": 0,
        "result_invalid": 0,
        "status_counts": Counter(),
        "invalid_examples": [],
    }

    for delegation_id, state, task_json, result_json in rows:
        try:
            task_payload = json.loads(task_json or "{}")
            task_payload["delegation_id"] = delegation_id
            task = upcast_async_task(task_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            task = None
        if task is None:
            report["task_invalid"] += 1
            if len(report["invalid_examples"]) < 10:
                report["invalid_examples"].append({
                    "delegation_id": delegation_id,
                    "kind": "task",
                    "state": state,
                })
        else:
            report["task_valid"] += 1

        try:
            result_payload = json.loads(result_json or "{}")
            result = upcast_async_result(result_payload, task_id=delegation_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            result = None
        if result is None:
            report["result_invalid"] += 1
            if len(report["invalid_examples"]) < 10:
                report["invalid_examples"].append({
                    "delegation_id": delegation_id,
                    "kind": "result",
                    "state": state,
                })
        else:
            report["result_valid"] += 1
            report["status_counts"][result.status] += 1

    report["status_counts"] = dict(report["status_counts"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Async-Delegation-Audit")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = audit_database(args.db, max(1, args.limit))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
