"""Consume nightly-gate reports into DevFlow work requests.

The gate writes a structured report and knows nothing about DevFlow. This
module is the only bridge: it reads unconsumed reports, routes each by failure
class, and hands it to the existing adapter. Every failure mode is a no-op or a
skip — a bad report can never crash the nightly pipeline.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from devflow_delegation.adapters import nightly_gate as nightly_gate_adapter
from devflow_delegation.gate_report import route_target, validate_report

logger = logging.getLogger(__name__)

REPORT_GLOB = "report-*.json"
CONSUMED_SUFFIX = ".consumed"


def _mark_consumed(path: Path) -> None:
    try:
        path.rename(path.with_name(path.name + CONSUMED_SUFFIX))
    except OSError:
        logger.warning("could not mark %s consumed", path.name)


def consume_reports(report_dir: Path, emitter, *, limit: int = 20) -> Dict[str, int]:
    """Emit a work request per unconsumed report. Never raises."""
    counts = {"found": 0, "emitted": 0, "skipped": 0}
    directory = Path(report_dir)
    if not directory.is_dir():
        return counts

    for path in sorted(directory.glob(REPORT_GLOB))[:limit]:
        counts["found"] += 1
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts["skipped"] += 1
            _mark_consumed(path)
            continue

        report = validate_report(payload)
        if report is None:
            counts["skipped"] += 1
            _mark_consumed(path)
            continue

        report = dict(report)
        report["target"] = {
            "repo": route_target(str(report.get("failure_class") or "")),
            "subsystem": str(report.get("subsystem") or "nightly-gate"),
        }
        try:
            result = nightly_gate_adapter.delegate_gate_failure(emitter, report)
        except Exception:
            logger.exception("adapter failed for %s", path.name)
            counts["skipped"] += 1
            _mark_consumed(path)
            continue

        if getattr(result, "status", "") == "queued":
            counts["emitted"] += 1
        else:
            counts["skipped"] += 1
        _mark_consumed(path)
    return counts


def main(argv=None) -> int:
    del argv
    from events import paths

    from devflow_delegation.emitter import DelegationEmitter

    report_dir = Path(paths.get_default_hermes_root()) / "logs" / "nightly-gate"
    counts = consume_reports(report_dir, DelegationEmitter())
    print(f"found={counts['found']} emitted={counts['emitted']} skipped={counts['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
