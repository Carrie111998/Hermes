"""Batched classification helpers for Tihna items."""

from __future__ import annotations

import json
from typing import Any

from hermes_cli.lanes.contracts import LaneTask
from hermes_cli.lanes.impls.tihna_templates import CLASSIFY_PROMPT


def _json_payload(text: str) -> Any:
    value = str(text).strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1])
        if value.lstrip().startswith("json"):
            value = value.lstrip()[4:].lstrip()
    return json.loads(value)


def parse_scores(text: str) -> list[dict[str, Any]]:
    try:
        raw = _json_payload(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        external_id = str(item.get("external_id") or "").strip()
        try:
            score = max(0, min(100, int(item.get("score"))))
        except (TypeError, ValueError):
            continue
        if not external_id:
            continue
        results.append(
            {
                "external_id": external_id,
                "score": score,
                "reason": str(item.get("reason") or "").strip()[:500],
            }
        )
    return results


def build_classify_prompt(tasks: list[LaneTask]) -> str:
    items = [
        {
            "external_id": task.external_id,
            "title": task.payload.get("title"),
            "summary": task.payload.get("summary"),
            "category": task.payload.get("category"),
            "link": task.payload.get("link"),
        }
        for task in tasks
    ]
    return CLASSIFY_PROMPT.replace(
        "<ITEMS>",
        json.dumps(items, ensure_ascii=False, sort_keys=True),
    )


__all__ = ["build_classify_prompt", "parse_scores"]
