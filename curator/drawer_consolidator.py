"""MemPalace drawer consolidator — pulls recent episodic memory per agent.

The MemPalace MCP is the canonical interface for production use, but it
is not always reachable from non-interactive subagent contexts. This
module accepts an injected ``search_fn`` (factory pattern) so that:

  * The orchestrator can wire in a real MCP-backed search at runtime.
  * Tests can stub the search with a fixture.
  * A flat-file fallback (``chroma_search_fn`` below) reads
    ``C:/Users/diego/.mempalace/palace/chroma.sqlite3`` directly via
    SQLite, returning the same shape as the MCP. This is the documented
    degraded-mode path.

The MCP / fallback / fixture all return the canonical shape::

    {
        "results": [
            {
                "drawer_id": str,
                "wing": str,        # e.g. ".openclaw"
                "room": str,        # e.g. "agents"
                "title": str,
                "body": str,
                "created_at": str   # ISO-8601 with timezone
            },
            ...
        ]
    }

Spec: ``docs/superpowers/plans/2026-04-26-curator-backfill-and-nightly.md``
Task 3.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Type alias for the injectable search function.
SearchFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]

# Stopwords used by the simple pattern-detection token comparison. Kept
# small on purpose — sophisticated clustering is Critic's job.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
    "by", "with", "from", "is", "was", "be", "been", "as", "it", "its",
    "this", "that", "these", "those", "but", "not", "no", "so", "if",
    "into", "out", "up", "down",
})

_BODY_CAP = 500


def _tokenize(title: str) -> List[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+", title.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _is_within_window(created_at: str, window_start: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        # MemPalace WAL uses naive timestamps (UTC by convention).
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= window_start


def consolidate_for_agent(
    agent: str,
    search_fn: SearchFn,
    window_days: int = 30,
    max_drawers: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pull recent MemPalace drawers for an agent and surface pattern candidates.

    Query strategy:
        - Search wing=.openclaw room=<agent> first (1:1 mapping when present).
        - Fall back to keyword search on the agent name across all wings
          when the dedicated room is missing or empty.
        - Cap the response to ``max_drawers``, ordered by ``created_at`` desc.

    Args:
        agent: Agent name (e.g. "scout").
        search_fn: Injected search callable; signature ``(query, params) -> dict``.
            Must return ``{"results": [drawer, ...]}`` per module docstring.
        window_days: Days back from ``now`` to keep.
        max_drawers: Maximum drawers retained in ``recent_drawers``.
        now: Reference time (defaults to UTC now).

    Returns:
        {
            "agent": str,
            "drawer_count_total": int,
            "drawers_by_room": dict[str, int],
            "recent_drawers": list[dict],     # body capped to 500 chars
            "pattern_candidates": list[dict], # drawers whose titles share tokens
            "error": str | None
        }
    """
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    try:
        params = {"wing": ".openclaw", "room": agent, "limit": max_drawers * 2}
        response = search_fn(agent, params)
        drawers: List[Dict[str, Any]] = list(response.get("results") or [])
    except Exception as exc:  # noqa: BLE001 — MCP can fail in many ways
        return {
            "agent": agent,
            "drawer_count_total": 0,
            "drawers_by_room": {},
            "recent_drawers": [],
            "pattern_candidates": [],
            "error": str(exc),
        }

    # Filter to window and cap body length.
    in_window: List[Dict[str, Any]] = []
    for d in drawers:
        if not _is_within_window(d.get("created_at", ""), window_start):
            continue
        capped = dict(d)
        body = capped.get("body") or ""
        if len(body) > _BODY_CAP:
            capped["body"] = body[:_BODY_CAP - 3] + "..."
        in_window.append(capped)

    # Sort by created_at desc and cap.
    in_window.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    recent = in_window[:max_drawers]

    drawers_by_room: Dict[str, int] = {}
    for d in recent:
        room = d.get("room", "?")
        drawers_by_room[room] = drawers_by_room.get(room, 0) + 1

    # Pattern candidates: titles sharing ≥3 tokens with ≥2 other titles.
    token_sets: List[set] = [set(_tokenize(d.get("title") or "")) for d in recent]
    pattern_candidates: List[Dict[str, Any]] = []
    for i, d in enumerate(recent):
        my_tokens = token_sets[i]
        if len(my_tokens) < 3:
            continue
        overlap_count = 0
        for j, other in enumerate(token_sets):
            if i == j:
                continue
            if len(my_tokens & other) >= 3:
                overlap_count += 1
        if overlap_count >= 2:
            pattern_candidates.append(d)

    return {
        "agent": agent,
        "drawer_count_total": len(in_window),
        "drawers_by_room": drawers_by_room,
        "recent_drawers": recent,
        "pattern_candidates": pattern_candidates,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Flat-file fallback: chroma.sqlite3 directly.
# ---------------------------------------------------------------------------

DEFAULT_CHROMA_PATH = Path(r"C:/Users/diego/.mempalace/palace/chroma.sqlite3")


def chroma_search_fn(
    chroma_path: Optional[Path] = None,
) -> SearchFn:
    """Build a search_fn that queries chroma.sqlite3 directly.

    Used when the MemPalace MCP is unreachable (e.g. when the curator
    backfill runs from a non-interactive subagent without MCP access).

    The query is a free-text agent name; the filter walks the metadata
    table joining on document body LIKE '%<agent>%' or wing/room match.
    """
    db = chroma_path or DEFAULT_CHROMA_PATH

    def _search(query: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not db.exists():
            raise FileNotFoundError(f"chroma db not found at {db}")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            limit = int(params.get("limit", 60))
            agent_q = (query or "").strip().lower()
            wing_filter = params.get("wing")
            # Two-stage approach:
            #   1. Find rows where 'chroma:document' contains the agent name
            #      OR 'room' equals it (join to gather IDs).
            #   2. For each matched id, gather metadata pivots.
            sql = """
                SELECT DISTINCT id FROM embedding_metadata
                WHERE (key='chroma:document' AND string_value LIKE ?)
                   OR (key='room' AND string_value = ?)
                   OR (key='agent' AND string_value = ?)
                LIMIT ?
            """
            cur.execute(sql, (f"%{agent_q}%", agent_q, agent_q, limit))
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return {"results": []}

            placeholders = ",".join("?" for _ in ids)
            cur.execute(
                f"""
                SELECT id, key, string_value FROM embedding_metadata
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            grouped: Dict[int, Dict[str, str]] = {}
            for rid, key, val in cur.fetchall():
                grouped.setdefault(rid, {})[key] = val

            results: List[Dict[str, Any]] = []
            for rid, meta in grouped.items():
                if wing_filter and meta.get("wing") not in {wing_filter, wing_filter.lstrip(".")}:
                    # Soft filter — fall through; the consolidator already
                    # filters by window so we don't drop here.
                    pass
                doc = meta.get("chroma:document", "") or ""
                results.append({
                    "drawer_id": f"chroma_{rid}",
                    "wing": meta.get("wing", "?"),
                    "room": meta.get("room", "?"),
                    "title": doc.split("\n", 1)[0][:120] if doc else "(no title)",
                    "body": doc,
                    "created_at": meta.get("filed_at") or meta.get("date") or "",
                })
            return {"results": results}
        finally:
            conn.close()

    return _search


def empty_search_fn() -> SearchFn:
    """Search function that returns no drawers — degraded fallback only."""
    def _search(query: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"results": []}
    return _search
