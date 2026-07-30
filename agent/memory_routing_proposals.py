"""Proposal queue helpers for memory-routing policy evolution.

The queue is append/update-only JSON. It never edits the source policy file; a
human or later workflow can review proposals and promote them deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.memory_routing import MemoryRoute

PROPOSAL_FILENAME = "memory-routing-proposals.json"


def record_policy_proposal(
    output_dir: str | Path, route: MemoryRoute, query: str
) -> Path | None:
    """Record a policy proposal for ``route.new_topic_candidate``.

    Returns the proposal JSON path when a proposal is recorded, otherwise None.
    The policy itself is never mutated.
    """

    topic = (route.new_topic_candidate or "").strip()
    if not topic:
        return None

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    proposal_path = output_path / PROPOSAL_FILENAME

    proposals = _read_proposals(proposal_path)
    entry = proposals.setdefault(
        topic,
        {"count": 0, "examples": [], "suggested_slots": []},
    )
    entry["count"] = _safe_int(entry.get("count")) + 1
    entry["examples"] = _append_unique_strings(entry.get("examples"), [str(query or "")])
    entry["suggested_slots"] = _append_unique_strings(
        entry.get("suggested_slots"), route.suggested_slots
    )

    proposal_path.write_text(
        json.dumps(proposals, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return proposal_path


def _read_proposals(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _append_unique_strings(existing: object, additions: list[str]) -> list[str]:
    result: list[str] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str) and item not in result:
                result.append(item)
    for item in additions:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


__all__ = ["PROPOSAL_FILENAME", "record_policy_proposal"]
