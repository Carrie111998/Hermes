"""Read-only Christopher iLinked lookup adapter for Hermes PA business tools.

The adapter receives a JSON payload on stdin and shells out to the Christopher
CLI. It never submits iLinked forms; it only calls the CLI's read-only
``ilinked detail`` or ``ilinked search`` commands.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any, Mapping


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def _first(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _command_for(payload: Mapping[str, Any]) -> list[str]:
    cli_command = os.getenv("CHRISTOPHER_CLI_COMMAND")
    cli = (
        shlex.split(cli_command)
        if cli_command
        else [os.getenv("CHRISTOPHER_CLI", "christopher")]
    )
    job_no = _first(payload, "jobNo", "job_no", "job", "task_no")
    work_costing_no = _first(
        payload,
        "workCostingNo",
        "work_costing_no",
        "workCosting",
        "work_costing",
        "wcNo",
        "wc_no",
    )
    query = _first(payload, "query", "text", "address", "unit")
    limit = _first(payload, "limit") or "20"

    if job_no:
        return [*cli, "ilinked", "detail", "--job", job_no]
    if work_costing_no:
        return [*cli, "ilinked", "detail", "--work-costing", work_costing_no]
    if query:
        return [*cli, "ilinked", "search", "--query", query, "--limit", limit]
    raise ValueError("payload requires jobNo, workCostingNo, or query")


def main() -> int:
    try:
        payload = _read_payload()
        command = _command_for(payload)
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=float(os.getenv("CHRISTOPHER_ILINKED_LOOKUP_TIMEOUT", "90")),
            check=False,
        )
        if completed.returncode != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "CHRISTOPHER_ILINKED_LOOKUP_FAILED",
                            "message": completed.stderr.strip()
                            or completed.stdout.strip()
                            or f"christopher exited {completed.returncode}",
                        },
                    }
                )
            )
            return 1
        parsed = json.loads(completed.stdout)
        if isinstance(parsed, dict):
            parsed.setdefault("meta", {})
            if isinstance(parsed["meta"], dict):
                parsed["meta"].setdefault("adapter", "tools.tgg_ilinked_lookup")
            print(json.dumps(parsed))
            return 0
        print(json.dumps({"ok": True, "data": parsed, "meta": {"adapter": "tools.tgg_ilinked_lookup"}}))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CHRISTOPHER_ILINKED_LOOKUP_ADAPTER_ERROR",
                        "message": str(exc),
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
