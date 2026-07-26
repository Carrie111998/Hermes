"""Offline evaluator for local Hermes DecisionTrace JSONL files."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Iterable


NUMERIC_FIELDS = (
    "duration_ms",
    "api_duration_ms",
    "api_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
)


def load_records(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("each JSONL line must be an object")
        records.append(value)
    return records


def _mean(records: Iterable[dict[str, Any]], field: str) -> float:
    values = [float(r.get(field) or 0) for r in records]
    return round(statistics.fmean(values), 6) if values else 0.0


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "schema_versions": sorted({str(r.get("schema_version", "unknown")) for r in records}),
        "records": len(records),
        "statuses": statuses,
        "averages": {field: _mean(records, field) for field in NUMERIC_FIELDS},
        "totals": {
            field: round(sum(float(r.get(field) or 0) for r in records), 6)
            for field in NUMERIC_FIELDS
        },
    }


def compare(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    left_summary = summarize(left)
    right_summary = summarize(right)
    deltas = {}
    for field in NUMERIC_FIELDS:
        lval = left_summary["averages"][field]
        rval = right_summary["averages"][field]
        deltas[field] = round(rval - lval, 6)
    return {"left": left_summary, "right": right_summary, "right_minus_left": deltas}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_file", type=Path)
    parser.add_argument("--compare", type=Path, help="compare against a second JSONL file")
    args = parser.parse_args()
    left = load_records(args.trace_file)
    result = compare(left, load_records(args.compare)) if args.compare else summarize(left)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
