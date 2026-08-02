#!/usr/bin/env python3
"""Fail only for OSV vulnerabilities introduced by head lockfiles.

The baseline OSV workflow is intentionally report-only. This gate compares two
OSV SARIF/JSON payloads and returns non-zero only when the head scan contains
vulnerability identities absent from the base scan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


def load_payload(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _result_locations(result: dict[str, Any]) -> Iterable[str]:
    for location in result.get("locations", []) or []:
        physical = location.get("physicalLocation", {}) or {}
        artifact = physical.get("artifactLocation", {}) or {}
        uri = artifact.get("uri")
        if uri:
            yield str(uri).replace("\\", "/")


def vulnerability_ids(payload: dict[str, Any]) -> set[str]:
    """Extract stable vulnerability identities from OSV JSON or SARIF payloads."""
    ids: set[str] = set()

    # Native OSV JSON shape: {"results": [{"packages": [{"vulnerabilities": ...}]}]}
    for result in payload.get("results", []) or []:
        if isinstance(result, dict) and "packages" in result:
            for package in result.get("packages", []) or []:
                for vuln in package.get("vulnerabilities", []) or []:
                    vuln_id = vuln.get("id") or vuln.get("aliases", [None])[0]
                    if vuln_id:
                        ids.add(str(vuln_id))

    # SARIF shape emitted by scanners in GitHub workflows.
    for run in payload.get("runs", []) or []:
        for result in run.get("results", []) or []:
            if not isinstance(result, dict):
                continue
            rule_id = result.get("ruleId")
            if not rule_id:
                continue
            locations = sorted(_result_locations(result))
            if locations:
                for location in locations:
                    ids.add(f"{rule_id}@{location}")
            else:
                ids.add(str(rule_id))
    return ids


def newly_introduced(base_payload: dict[str, Any], head_payload: dict[str, Any]) -> set[str]:
    return vulnerability_ids(head_payload) - vulnerability_ids(base_payload)


def render(new_ids: set[str]) -> str:
    if not new_ids:
        return "No newly introduced OSV vulnerabilities found.\n"
    lines = ["Newly introduced OSV vulnerabilities:"]
    lines.extend(f"- {item}" for item in sorted(new_ids))
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare base/head OSV findings and fail only on new IDs.")
    parser.add_argument("--base", type=Path, required=True, help="Base OSV JSON/SARIF payload.")
    parser.add_argument("--head", type=Path, required=True, help="Head OSV JSON/SARIF payload.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    new_ids = newly_introduced(load_payload(args.base), load_payload(args.head))
    print(render(new_ids), end="")
    return 1 if new_ids else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
