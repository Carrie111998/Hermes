from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml


SUPPORTED_MODES = ("baseline", "routed", "open-set")


def assert_isolated_hermes_home(path: Path) -> Path:
    """Return a resolved Hermes home path, rejecting the user's live ~/.hermes tree."""

    candidate = Path(path).expanduser().resolve(strict=False)
    live_home = (Path.home() / ".hermes").resolve(strict=False)
    if candidate == live_home or _is_relative_to(candidate, live_home):
        raise ValueError(
            f"Hermes home must be isolated for benchmarks; refused live path {candidate}"
        )
    return candidate


def score_answer(answer: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Score an answer with conservative substring matching over judged claims.

    This MVP deliberately avoids pretending to be an LLM judge. A claim is satisfied
    only when each `must_satisfy` substring is present and every
    `must_not_satisfy` substring is absent in the answer.
    """

    normalized_answer = _normalize(answer)
    violations: list[str] = []
    satisfied = 0
    forbidden_absent = 0
    total_required = 0
    total_forbidden = 0

    for claim in expected.get("judged_claims", []) or []:
        for required in claim.get("must_satisfy", []) or []:
            total_required += 1
            needle = _normalize(str(required))
            if needle and needle in normalized_answer:
                satisfied += 1
            else:
                violations.append(f"missing:{required}")

        for forbidden in claim.get("must_not_satisfy", []) or []:
            total_forbidden += 1
            needle = _normalize(str(forbidden))
            if needle and needle in normalized_answer:
                violations.append(f"forbidden:{forbidden}")
            else:
                forbidden_absent += 1

    return {
        "passed": not violations,
        "satisfied": satisfied,
        "required": total_required,
        "forbidden_absent": forbidden_absent,
        "forbidden": total_forbidden,
        "violations": violations,
    }


def compute_metrics(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Compute benchmark MVP metrics from already-scored result rows."""

    direct = [row for row in results if row.get("task_type") == "direct_recall"]
    indirect = [row for row in results if row.get("task_type") == "indirect_application"]
    unseen_non_control = [
        row
        for row in results
        if row.get("split") == "unseen_topic" and not bool(row.get("control", False))
    ]
    control = [row for row in results if bool(row.get("control", False))]

    return {
        "direct_recall_accuracy": _accuracy_metric(direct),
        "indirect_application_accuracy": _accuracy_metric(indirect),
        "unseen_topic_useful_routing_rate": _rate(
            unseen_non_control, lambda row: bool(row.get("routing_useful", False))
        ),
        "false_positive_personalization_rate": _rate(
            control, lambda row: bool(row.get("personalized", False))
        ),
        "safety_miss_rate": _rate(results, lambda row: bool(row.get("safety_miss", False))),
    }


def load_fixture(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict):
        cases = raw.get("cases", [])
    else:
        cases = raw
    if not isinstance(cases, list):
        raise ValueError(f"Fixture {path} must contain a list of cases")
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_routing_benchmark",
        description="Scorer-only MVP for isolated Hermes memory routing benchmark results.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    score_parser = subparsers.add_parser("score", help="Score JSONL runner outputs")
    score_parser.add_argument("--mode", choices=SUPPORTED_MODES, required=True)
    score_parser.add_argument("--results", type=Path, required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "score":
        rows = _load_jsonl(args.results)
        scored = [_ensure_scored(row) for row in rows]
        payload = {
            "mode": args.mode,
            "scorer_only": True,
            "p0_metrics_claimed": False,
            "runner_results_present": bool(rows),
            "result_count": len(scored),
            "metrics": compute_metrics(scored),
        }
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), end="")
        return 0
    parser.error(f"unsupported command: {args.command}")
    return 2


def _ensure_scored(row: dict[str, Any]) -> dict[str, Any]:
    if "score" in row:
        return row
    scored = dict(row)
    scored["score"] = score_answer(str(row.get("answer", "")), row.get("expected", {}) or {})
    return scored


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"JSONL row at {path}:{line_number} must be an object")
        rows.append(loaded)
    return rows


def _accuracy_metric(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "overall": _rate(rows, _passed),
        "by_memory_location": _grouped_rate(rows, "memory_location", _passed),
        "by_split": _grouped_rate(rows, "split", _passed),
    }


def _grouped_rate(
    rows: Sequence[dict[str, Any]], key: str, predicate: Any
) -> dict[str, float | None]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key, "unknown")), []).append(row)
    return {group: _rate(group_rows, predicate) for group, group_rows in sorted(groups.items())}


def _rate(rows: Sequence[dict[str, Any]], predicate: Any) -> float | None:
    if not rows:
        return None
    return sum(1 for row in rows if predicate(row)) / len(rows)


def _passed(row: dict[str, Any]) -> bool:
    score = row.get("score", {}) or {}
    if isinstance(score, dict):
        return bool(score.get("passed", False))
    return bool(score)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _is_relative_to(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
