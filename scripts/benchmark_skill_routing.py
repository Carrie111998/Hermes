#!/usr/bin/env python3
"""Run the deterministic public-safe skill-routing relevance benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agent.skill_routing import rank_skills


def evaluate(fixture: dict) -> dict:
    """Return deterministic aggregate metrics and public-safe result rows."""
    rows = []
    for case in fixture["queries"]:
        ranking = rank_skills(fixture["skills"], case["query"], limit=8)
        names = [item["name"] for item in ranking["skills"]]
        relevant = case["relevant"]
        ranks = [names.index(name) + 1 for name in relevant if name in names]
        reciprocal_rank = 1.0 / min(ranks) if ranks else (1.0 if not relevant else 0.0)
        recall = len(ranks) / len(relevant) if relevant else 1.0
        dcg = sum(1.0 / math.log2(rank + 1) for rank in ranks)
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, len(relevant) + 1))
        ndcg = dcg / ideal if ideal else 1.0
        rows.append({
            "depth": len(names),
            "ndcg": ndcg,
            "query": case["query"],
            "ranked": names,
            "recall": recall,
            "reciprocal_rank": reciprocal_rank,
            "relevant": relevant,
        })

    count = len(rows)
    return {
        "average_depth": round(sum(row["depth"] for row in rows) / count, 6),
        "mean_ndcg": round(sum(row["ndcg"] for row in rows) / count, 6),
        "mrr": round(sum(row["reciprocal_rank"] for row in rows) / count, 6),
        "query_count": count,
        "recall": round(sum(row["recall"] for row in rows) / count, 6),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    result = evaluate(fixture)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    sys.stderr.write(f"sha256={hashlib.sha256(rendered.encode()).hexdigest()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
