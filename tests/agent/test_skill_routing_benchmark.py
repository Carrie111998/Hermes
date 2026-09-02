"""Frozen public-safe relevance and determinism gate for skill routing."""

import json
import math
from pathlib import Path


FIXTURE = Path(__file__).parents[1] / "fixtures" / "skill_routing_benchmark.json"


def _metrics(payload, skills):
    from agent.skill_routing import rank_skills

    rows = []
    for case in payload["queries"]:
        result = rank_skills(skills, case["query"], limit=8)
        names = [item["name"] for item in result["skills"]]
        relevant = case["relevant"]
        ranks = [names.index(name) + 1 for name in relevant if name in names]
        reciprocal_rank = 1.0 / min(ranks) if ranks else (1.0 if not relevant else 0.0)
        recall = len(ranks) / len(relevant) if relevant else 1.0
        dcg = sum(1.0 / math.log2(rank + 1) for rank in ranks)
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, len(relevant) + 1))
        ndcg = dcg / ideal if ideal else 1.0
        rows.append((names, reciprocal_rank, ndcg, recall))
    count = len(rows)
    return {
        "rows": rows,
        "mrr": sum(row[1] for row in rows) / count,
        "mean_ndcg": sum(row[2] for row in rows) / count,
        "recall": sum(row[3] for row in rows) / count,
        "average_depth": sum(len(row[0]) for row in rows) / count,
    }


def test_frozen_benchmark_thresholds_and_reversed_order_determinism():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    forward = _metrics(payload, payload["skills"])
    reverse = _metrics(payload, list(reversed(payload["skills"])))

    assert forward == reverse
    assert forward["mrr"] >= 0.95
    assert forward["mean_ndcg"] >= 0.96
    assert forward["recall"] == 1.0
    assert forward["average_depth"] <= 6.0
    assert 1 <= len(forward["rows"][-2][0]) <= 3
    assert forward["rows"][-1][0] == []
