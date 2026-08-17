"""
Session Insights Engine for Hermes Agent.

Analyzes historical session data from the SQLite state database to produce
comprehensive usage insights — token consumption, cost estimates, tool usage
patterns, activity trends, model/platform breakdowns, and session metrics.

Inspired by Claude Code's /insights command, adapted for Hermes Agent's
multi-platform architecture with additional cost estimation and platform
breakdown capabilities.

Usage:
    from agent.insights import InsightsEngine
    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    print(engine.format_terminal(report))
"""

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_market_equivalent_cost,
    estimate_usage_cost,
    format_cost_label,
    format_duration_compact,
    has_known_pricing,
)


def _fmt_est_cost(est_cost: float) -> str:
    """Format an aggregate estimated cost via the shared cost-label helper.

    Routes through ``format_cost_label`` so sub-cent aggregates render at
    4dp instead of collapsing to "~$0.00" (#79220 bug class — the same
    dishonesty this module's cost buckets exist to fix, #77223).
    """
    return format_cost_label(Decimal(str(est_cost)))




def _estimate_cost(
    session_or_model: Dict[str, Any] | str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> tuple[float, str]:
    """Estimate the USD cost for a session row or a model/token tuple."""
    if isinstance(session_or_model, dict):
        session = session_or_model
        model = session.get("model") or ""
        usage = CanonicalUsage(
            input_tokens=session.get("input_tokens") or 0,
            output_tokens=session.get("output_tokens") or 0,
            cache_read_tokens=session.get("cache_read_tokens") or 0,
            cache_write_tokens=session.get("cache_write_tokens") or 0,
        )
        provider = session.get("billing_provider")
        base_url = session.get("billing_base_url")
    else:
        model = session_or_model or ""
        usage = CanonicalUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
    result = estimate_usage_cost(
        model,
        usage,
        provider=provider,
        base_url=base_url,
    )
    return float(result.amount_usd or 0.0), result.status


def _estimate_at_market_cost(session: Dict[str, Any]) -> Optional[float]:
    """Published list-price comparison for an included session's token load.

    The original route remains authoritative for billed cost. This comparison
    deliberately resolves a separate public-price route; an included OAuth
    route's zero-price entry proves inclusion, not market value. Unknown public
    pricing remains unavailable rather than becoming a fabricated ``0.0``.
    """
    usage = CanonicalUsage(
        input_tokens=session.get("input_tokens") or 0,
        output_tokens=session.get("output_tokens") or 0,
        cache_read_tokens=session.get("cache_read_tokens") or 0,
        cache_write_tokens=session.get("cache_write_tokens") or 0,
    )
    result = estimate_market_equivalent_cost(
        session.get("model") or "",
        usage,
        provider=session.get("billing_provider"),
        base_url=session.get("billing_base_url"),
    )
    return None if result.amount_usd is None else float(result.amount_usd)




def _bar_chart(values: List[int], max_width: int = 20) -> List[str]:
    """Create simple horizontal bar chart strings from values."""
    peak = max(values) if values else 1
    if peak == 0:
        return ["" for _ in values]
    return ["█" * max(1, int(v / peak * max_width)) if v > 0 else "" for v in values]


class InsightsEngine:
    """
    Analyzes session history and produces usage insights.

    Works directly with a SessionDB instance (or raw sqlite3 connection)
    to query session and message data.
    """

    def __init__(self, db):
        """
        Initialize with a SessionDB instance.

        Args:
            db: A SessionDB instance (from hermes_state.py)
        """
        self.db = db
        self._conn = db._conn
        # INDEXED BY is a hard dependency (SQLite errors on a missing index).
        # A read-only open of a state.db written by an older version skips
        # schema init and lacks the partial index — probe once and fall back
        # to the unpinned variants (identical rows, optimizer-chosen plan).
        try:
            self._has_assistant_calls_index = bool(
                self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                    (self._MESSAGES_ASSISTANT_CALLS_INDEX,),
                ).fetchone()
            )
        except sqlite3.Error:
            self._has_assistant_calls_index = False
        if not self._has_assistant_calls_index:
            _strip = f" INDEXED BY {self._MESSAGES_ASSISTANT_CALLS_INDEX}"
            # Loop over every pinned statement so adding a new one can't
            # forget its strip line (which would be a hard `no such index`
            # crash on read-only DBs — the exact bug this fallback prevents).
            for _attr in (
                "_GET_TOOL_CALLS_WITH_SOURCE",
                "_GET_TOOL_CALLS_ALL",
                "_GET_SKILL_CALLS_WITH_SOURCE",
                "_GET_SKILL_CALLS_ALL",
            ):
                setattr(self, _attr, getattr(self, _attr).replace(_strip, ""))

    def generate(self, days: int = 30, source: str = None) -> Dict[str, Any]:
        """
        Generate a complete insights report.

        Args:
            days: Number of days to look back (default: 30)
            source: Optional filter by source platform

        Returns:
            Dict with all computed insights
        """
        window_end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = (window_end - timedelta(days=days - 1)).timestamp()

        # Token/cost totals may still sit on the SessionDB's async
        # accounting queue; drain so the report reflects exact counters.
        # (self.db may be a raw sqlite3 connection in tests — guard.)
        flush = getattr(self.db, "flush_token_counts", None)
        if callable(flush):
            flush()

        # Gather raw data
        sessions = self._get_sessions(cutoff, source)
        tool_usage = self._get_tool_usage(cutoff, source)
        skill_usage = self._get_skill_usage(cutoff, source)
        message_stats = self._get_message_stats(cutoff, source)

        if not sessions:
            return {
                "days": days,
                "source_filter": source,
                "empty": True,
                "overview": {},
                "models": [],
                "platforms": [],
                "tools": [],
                "skills": {
                    "summary": {
                        "total_skill_loads": 0,
                        "total_skill_edits": 0,
                        "total_skill_actions": 0,
                        "distinct_skills_used": 0,
                    },
                    "top_skills": [],
                },
                "activity": {},
                "top_sessions": [],
                "daily_series": self._compute_daily_series(sessions, [], days, window_end),
            }

        # Compute insights
        usage = self._reconcile_usage(sessions, cutoff, source)
        models = self._compute_model_breakdown(sessions, usage)
        overview = self._compute_overview(sessions, message_stats, usage)
        platforms = self._compute_platform_breakdown(sessions, usage)
        tools = self._compute_tool_breakdown(tool_usage)
        skills = self._compute_skill_breakdown(skill_usage)
        activity = self._compute_activity_patterns(sessions)
        top_sessions = self._compute_top_sessions(sessions, usage)
        daily_series = self._compute_daily_series(sessions, usage, days, window_end)

        return {
            "days": days,
            "source_filter": source,
            "empty": False,
            "generated_at": time.time(),
            "overview": overview,
            "models": models,
            "platforms": platforms,
            "tools": tools,
            "skills": skills,
            "activity": activity,
            "top_sessions": top_sessions,
            "daily_series": daily_series,
        }

    def get_usage_breakdown(self, days: int = 30, source: str = None) -> Dict[str, Any]:
        """Return the analytics-usage payload without running a full generate().

        Uses the instr()-prefiltered _get_skill_usage query so only messages
        that reference skill_view or skill_manage are loaded from SQLite, while
        still preserving the per-tool breakdown used by the dashboard route.
        """
        cutoff = time.time() - (days * 86400)
        tool_usage = self._get_tool_usage(cutoff, source)
        skill_usage = self._get_skill_usage(cutoff, source)
        return {
            "tools": self._compute_tool_breakdown(tool_usage),
            "skills": self._compute_skill_breakdown(skill_usage),
        }

    # =========================================================================
    # Data gathering (SQL queries)
    # =========================================================================

    # Columns we actually need (skip system_prompt, model_config blobs)
    _SESSION_COLS = ("id, source, model, started_at, ended_at, "
                     "message_count, tool_call_count, input_tokens, output_tokens, "
                     "cache_read_tokens, cache_write_tokens, billing_provider, "
                     "billing_base_url, billing_mode, estimated_cost_usd, "
                     "actual_cost_usd, cost_status, cost_source, api_call_count")

    # Pre-computed query strings — f-string evaluated once at class definition,
    # not at runtime, so no user-controlled value can alter the query structure.
    _GET_SESSIONS_WITH_SOURCE = (
        f"SELECT {_SESSION_COLS} FROM sessions"
        " WHERE started_at >= ? AND source = ?"
        " ORDER BY started_at DESC"
    )
    _GET_SESSIONS_ALL = (
        f"SELECT {_SESSION_COLS} FROM sessions"
        " WHERE started_at >= ?"
        " ORDER BY started_at DESC"
    )

    # Assistant ``tool_calls`` scan for tool/skill usage.  ``INDEXED BY`` pins
    # the partial index ``idx_messages_assistant_calls_by_session`` so the plan
    # is deterministic on a freshly initialized state.db (before ANALYZE has
    # run) for BOTH the unfiltered and source-filtered branches — without the
    # hint the optimizer falls back to ``idx_messages_session_active`` for the
    # source-filtered probe and scans each session's non-tool-call rows.
    #
    # The pin is a HARD dependency: SQLite raises ``no such index`` when the
    # named index is absent. That happens in practice — the web dashboard's
    # usage analytics open the DB ``read_only=True`` (skipping
    # ``_init_schema``), so a state.db created by an older writer has no
    # partial index yet. ``__init__`` probes for the index once and falls
    # back to the unpinned (still-correct, just optimizer-chosen) variants.
    _MESSAGES_ASSISTANT_CALLS_INDEX = "idx_messages_assistant_calls_by_session"
    _GET_TOOL_CALLS_WITH_SOURCE = (
        "SELECT m.tool_calls"
        f" FROM messages m INDEXED BY {_MESSAGES_ASSISTANT_CALLS_INDEX}"
        " JOIN sessions s ON s.id = m.session_id"
        " WHERE s.started_at >= ? AND s.source = ?"
        " AND m.role = 'assistant' AND m.tool_calls IS NOT NULL"
    )
    _GET_TOOL_CALLS_ALL = (
        "SELECT m.tool_calls"
        f" FROM messages m INDEXED BY {_MESSAGES_ASSISTANT_CALLS_INDEX}"
        " JOIN sessions s ON s.id = m.session_id"
        " WHERE s.started_at >= ?"
        " AND m.role = 'assistant' AND m.tool_calls IS NOT NULL"
    )
    _GET_SKILL_CALLS_WITH_SOURCE = (
        "SELECT m.tool_calls, m.timestamp"
        f" FROM messages m INDEXED BY {_MESSAGES_ASSISTANT_CALLS_INDEX}"
        " JOIN sessions s ON s.id = m.session_id"
        " WHERE s.started_at >= ? AND s.source = ?"
        " AND m.role = 'assistant' AND m.tool_calls IS NOT NULL"
        " AND (instr(m.tool_calls, 'skill_view') > 0"
        " OR instr(m.tool_calls, 'skill_manage') > 0)"
    )
    _GET_SKILL_CALLS_ALL = (
        "SELECT m.tool_calls, m.timestamp"
        f" FROM messages m INDEXED BY {_MESSAGES_ASSISTANT_CALLS_INDEX}"
        " JOIN sessions s ON s.id = m.session_id"
        " WHERE s.started_at >= ?"
        " AND m.role = 'assistant' AND m.tool_calls IS NOT NULL"
        " AND (instr(m.tool_calls, 'skill_view') > 0"
        " OR instr(m.tool_calls, 'skill_manage') > 0)"
    )

    def _get_sessions(self, cutoff: float, source: str = None) -> List[Dict]:
        """Fetch sessions within the time window."""
        if source:
            cursor = self._conn.execute(self._GET_SESSIONS_WITH_SOURCE, (cutoff, source))
        else:
            cursor = self._conn.execute(self._GET_SESSIONS_ALL, (cutoff,))
        return [dict(row) for row in cursor.fetchall()]

    def _get_tool_usage(self, cutoff: float, source: str = None) -> List[Dict]:
        """Get tool call counts from messages.

        Uses two sources:
        1. tool_name column on 'tool' role messages (set by gateway)
        2. tool_calls JSON on 'assistant' role messages (covers CLI where
           tool_name is not populated on tool responses)
        """
        tool_counts = Counter()

        # Source 1: explicit tool_name on tool response messages
        if source:
            cursor = self._conn.execute(
                """SELECT m.tool_name, COUNT(*) as count
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?
                     AND m.role = 'tool' AND m.tool_name IS NOT NULL
                   GROUP BY m.tool_name
                   ORDER BY count DESC""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                """SELECT m.tool_name, COUNT(*) as count
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?
                     AND m.role = 'tool' AND m.tool_name IS NOT NULL
                   GROUP BY m.tool_name
                   ORDER BY count DESC""",
                (cutoff,),
            )
        for row in cursor.fetchall():
            tool_counts[row["tool_name"]] += row["count"]

        # Source 2: extract from tool_calls JSON on assistant messages
        # (covers CLI sessions where tool_name is NULL on tool responses)
        if source:
            cursor2 = self._conn.execute(
                self._GET_TOOL_CALLS_WITH_SOURCE, (cutoff, source)
            )
        else:
            cursor2 = self._conn.execute(self._GET_TOOL_CALLS_ALL, (cutoff,))

        tool_calls_counts = Counter()
        for row in cursor2.fetchall():
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
                if isinstance(calls, list):
                    for call in calls:
                        func = call.get("function", {}) if isinstance(call, dict) else {}
                        name = func.get("name")
                        if name:
                            tool_calls_counts[name] += 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # Merge: prefer tool_name source, supplement with tool_calls source
        # for tools not already counted
        if not tool_counts and tool_calls_counts:
            # No tool_name data at all — use tool_calls exclusively
            tool_counts = tool_calls_counts
        elif tool_counts and tool_calls_counts:
            # Both sources have data — use whichever has the higher count per tool
            # (they may overlap, so take the max to avoid double-counting)
            all_tools = set(tool_counts) | set(tool_calls_counts)
            merged = Counter()
            for tool in all_tools:
                merged[tool] = max(tool_counts.get(tool, 0), tool_calls_counts.get(tool, 0))
            tool_counts = merged

        # Convert to the expected format
        return [
            {"tool_name": name, "count": count}
            for name, count in tool_counts.most_common()
        ]

    def _get_skill_usage(self, cutoff: float, source: str = None) -> List[Dict]:
        """Extract per-skill usage from assistant tool calls."""
        skill_counts: Dict[str, Dict[str, Any]] = {}

        if source:
            cursor = self._conn.execute(
                self._GET_SKILL_CALLS_WITH_SOURCE, (cutoff, source)
            )
        else:
            cursor = self._conn.execute(self._GET_SKILL_CALLS_ALL, (cutoff,))

        for row in cursor.fetchall():
            try:
                calls = row["tool_calls"]
                if isinstance(calls, str):
                    calls = json.loads(calls)
                if not isinstance(calls, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue

            timestamp = row["timestamp"]
            for call in calls:
                if not isinstance(call, dict):
                    continue
                func = call.get("function", {})
                tool_name = func.get("name")
                if tool_name not in {"skill_view", "skill_manage"}:
                    continue

                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(args, dict):
                    continue

                skill_name = args.get("name")
                if not isinstance(skill_name, str) or not skill_name.strip():
                    continue

                entry = skill_counts.setdefault(
                    skill_name,
                    {
                        "skill": skill_name,
                        "view_count": 0,
                        "manage_count": 0,
                        "last_used_at": None,
                    },
                )
                if tool_name == "skill_view":
                    entry["view_count"] += 1
                else:
                    entry["manage_count"] += 1

                if timestamp is not None and (
                    entry["last_used_at"] is None or timestamp > entry["last_used_at"]
                ):
                    entry["last_used_at"] = timestamp

        return list(skill_counts.values())

    def _get_message_stats(self, cutoff: float, source: str = None) -> Dict:
        """Get aggregate message statistics."""
        if source:
            cursor = self._conn.execute(
                """SELECT
                     COUNT(*) as total_messages,
                     SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) as user_messages,
                     SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
                     SUM(CASE WHEN m.role = 'tool' THEN 1 ELSE 0 END) as tool_messages
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ? AND s.source = ?""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                """SELECT
                     COUNT(*) as total_messages,
                     SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) as user_messages,
                     SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
                     SUM(CASE WHEN m.role = 'tool' THEN 1 ELSE 0 END) as tool_messages
                   FROM messages m
                   JOIN sessions s ON s.id = m.session_id
                   WHERE s.started_at >= ?""",
                (cutoff,),
            )
        row = cursor.fetchone()
        return dict(row) if row else {
            "total_messages": 0, "user_messages": 0,
            "assistant_messages": 0, "tool_messages": 0,
        }

    # =========================================================================
    # Computation
    # =========================================================================

    def _compute_overview(
        self,
        sessions: List[Dict],
        message_stats: Dict,
        usage: List[Dict],
    ) -> Dict:
        """Compute high-level overview statistics."""
        total_input = sum(row["input_tokens"] for row in usage)
        total_output = sum(row["output_tokens"] for row in usage)
        total_cache_read = sum(row["cache_read_tokens"] for row in usage)
        total_cache_write = sum(row["cache_write_tokens"] for row in usage)
        total_tokens = total_input + total_output + total_cache_read + total_cache_write
        total_tool_calls = sum(s.get("tool_call_count") or 0 for s in sessions)
        total_messages = sum(s.get("message_count") or 0 for s in sessions)

        # Cost estimation (weighted by model)
        total_cost = 0.0
        actual_cost = 0.0
        models_with_pricing = set()
        models_without_pricing = set()
        unknown_cost_sessions = 0
        included_cost_sessions = 0
        cost_buckets = {
            "estimated": {"sessions": 0, "cost_usd": 0.0,
                          "input_tokens": 0, "output_tokens": 0,
                          "at_market_cost_usd": 0.0},
            "included": {"sessions": 0, "cost_usd": 0.0,
                         "input_tokens": 0, "output_tokens": 0,
                         "at_market_cost_usd": 0.0},
            "unknown": {"sessions": 0, "cost_usd": 0.0,
                        "input_tokens": 0, "output_tokens": 0},
        }
        usage_by_session = defaultdict(list)
        for row in usage:
            usage_by_session[row["session_id"]].append(row)
            model = row["model"]
            display = model.split("/")[-1] if "/" in model else (model or "unknown")
            if row["has_pricing"]:
                models_with_pricing.add(display)
            else:
                models_without_pricing.add(display)
            total_cost += row["estimated_cost_usd"]
            actual_cost += row["actual_cost_usd"]

        for session in sessions:
            rows = usage_by_session[session["id"]]
            statuses = {row["cost_bucket"] for row in rows}
            status = (
                "unknown" if "unknown" in statuses
                else "estimated" if "estimated" in statuses
                else "included" if "included" in statuses
                else "unknown"
            )
            bucket = cost_buckets[status]
            bucket["sessions"] += 1
            bucket["cost_usd"] += sum(row["estimated_cost_usd"] for row in rows)
            bucket["input_tokens"] += sum(row["input_tokens"] for row in rows)
            bucket["output_tokens"] += sum(row["output_tokens"] for row in rows)
            if status != "unknown":
                market_values = [
                    _estimate_at_market_cost(row)
                    if row["cost_bucket"] == "included"
                    else row["estimated_cost_usd"]
                    if row["cost_bucket"] == "estimated"
                    else None
                    for row in rows
                ]
                if (
                    bucket["at_market_cost_usd"] is None
                    or any(value is None for value in market_values)
                ):
                    bucket["at_market_cost_usd"] = None
                else:
                    bucket["at_market_cost_usd"] += sum(market_values)
            if status == "included":
                included_cost_sessions += 1
            elif status == "unknown":
                unknown_cost_sessions += 1

        # Session duration stats (guard against negative durations from clock drift)
        durations = []
        for s in sessions:
            start = s.get("started_at")
            end = s.get("ended_at")
            if start and end and end > start:
                durations.append(end - start)

        total_hours = sum(durations) / 3600 if durations else 0
        avg_duration = sum(durations) / len(durations) if durations else 0

        # Earliest and latest session
        started_timestamps = [s["started_at"] for s in sessions if s.get("started_at")]
        date_range_start = min(started_timestamps) if started_timestamps else None
        date_range_end = max(started_timestamps) if started_timestamps else None

        return {
            "total_sessions": len(sessions),
            "total_messages": total_messages,
            "total_tool_calls": total_tool_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_write_tokens": total_cache_write,
            "total_tokens": total_tokens,
            "estimated_cost": total_cost,
            "actual_cost": actual_cost,
            "actual_cost_available": any(
                session.get("actual_cost_usd") is not None for session in sessions
            ),
            "total_hours": total_hours,
            "avg_session_duration": avg_duration,
            "avg_messages_per_session": total_messages / len(sessions) if sessions else 0,
            "avg_tokens_per_session": total_tokens / len(sessions) if sessions else 0,
            "user_messages": message_stats.get("user_messages") or 0,
            "assistant_messages": message_stats.get("assistant_messages") or 0,
            "tool_messages": message_stats.get("tool_messages") or 0,
            "date_range_start": date_range_start,
            "date_range_end": date_range_end,
            "models_with_pricing": sorted(models_with_pricing),
            "models_without_pricing": sorted(models_without_pricing),
            "unknown_cost_sessions": unknown_cost_sessions,
            "included_cost_sessions": included_cost_sessions,
            "cost_buckets": cost_buckets,
        }

    _GET_MODEL_USAGE_WITH_SOURCE = (
        "SELECT u.session_id, u.model, u.billing_provider, u.billing_base_url,"
        " u.api_call_count, u.input_tokens, u.output_tokens,"
        " u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,"
        " u.estimated_cost_usd, u.actual_cost_usd, u.cost_status,"
        " u.cost_source, u.billing_mode, u.task"
        " FROM session_model_usage u"
        " JOIN sessions s ON s.id = u.session_id"
        " WHERE s.started_at >= ? AND s.source = ?"
    )
    _GET_MODEL_USAGE_ALL = (
        "SELECT u.session_id, u.model, u.billing_provider, u.billing_base_url,"
        " u.api_call_count, u.input_tokens, u.output_tokens,"
        " u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,"
        " u.estimated_cost_usd, u.actual_cost_usd, u.cost_status,"
        " u.cost_source, u.billing_mode, u.task"
        " FROM session_model_usage u"
        " JOIN sessions s ON s.id = u.session_id"
        " WHERE s.started_at >= ?"
    )

    def _get_model_usage(self, cutoff: float, source: str = None) -> List[Dict]:
        """Fetch per-model usage rows within the window (issue #51607).

        Returns an empty list when the table is missing (e.g. a DB opened by
        older code that never created it) so the caller can fall back to the
        per-session aggregate.
        """
        try:
            if source:
                cursor = self._conn.execute(
                    self._GET_MODEL_USAGE_WITH_SOURCE, (cutoff, source)
                )
            else:
                cursor = self._conn.execute(self._GET_MODEL_USAGE_ALL, (cutoff,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            return []

    def _reconcile_usage(
        self, sessions: List[Dict], cutoff: float, source: str = None
    ) -> List[Dict]:
        """Build one accounting population shared by every usage aggregate."""
        session_by_id = {session["id"]: session for session in sessions}
        usage_rows = self._get_model_usage(cutoff, source)
        totals = defaultdict(lambda: {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0,
            "api_call_count": 0, "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
        })
        reconciled = []

        def append_row(raw: Dict[str, Any], *, residual: bool = False) -> Dict[str, Any]:
            session = session_by_id[raw["session_id"]]
            auxiliary = bool(raw.get("task"))
            model = raw.get("model") or (
                "unknown" if auxiliary else session.get("model") or "unknown"
            )
            provider = raw.get("billing_provider") or (
                None if auxiliary else session.get("billing_provider") or None
            )
            base_url = raw.get("billing_base_url") or (
                None if auxiliary else session.get("billing_base_url") or None
            )
            stored = raw.get("estimated_cost_usd")
            status = raw.get("cost_status")
            if status == "unknown":
                # Unknown is an explicit absence-of-complete-evidence state.
                # A legacy/stale numeric column must neither leak as a partial
                # estimate nor trigger repricing from the same incomplete row.
                estimated = 0.0
            elif stored is not None and (
                raw.get("_estimated_cost_reconciled")
                or status or raw.get("cost_source") or float(stored) > 0
            ):
                estimated = float(stored)
                status = status or "estimated"
            else:
                estimated, status = _estimate_cost(
                    model,
                    raw.get("input_tokens") or 0,
                    raw.get("output_tokens") or 0,
                    cache_read_tokens=raw.get("cache_read_tokens") or 0,
                    cache_write_tokens=raw.get("cache_write_tokens") or 0,
                    provider=provider,
                    base_url=base_url,
                )
            session = session_by_id[raw["session_id"]]
            normalized = {
                "session_id": raw["session_id"],
                "source": session.get("source") or "unknown",
                "started_at": session.get("started_at"),
                "model": model,
                "billing_provider": provider,
                "billing_base_url": base_url,
                "billing_mode": raw.get("billing_mode") or (
                    None if auxiliary else session.get("billing_mode")
                ),
                "input_tokens": int(raw.get("input_tokens") or 0),
                "output_tokens": int(raw.get("output_tokens") or 0),
                "cache_read_tokens": int(raw.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(raw.get("cache_write_tokens") or 0),
                "reasoning_tokens": int(raw.get("reasoning_tokens") or 0),
                "api_call_count": int(raw.get("api_call_count") or 0),
                "estimated_cost_usd": estimated,
                "actual_cost_usd": float(raw.get("actual_cost_usd") or 0.0),
                # Per-model storage defaults actual cost to zero. Only row-local
                # evidence may authorize that zero as provider actual; the
                # nullable session aggregate proves availability only for the
                # report-wide total.
                "actual_cost_available": bool(
                    raw.get("actual_cost_available") is True
                    or float(raw.get("actual_cost_usd") or 0.0) > 0
                    or status == "actual"
                ),
                "cost_status": status,
                "cost_source": raw.get("cost_source"),
                # Provider-actual is a presentation/availability state, while
                # the comparison reducer still consumes the separately stored
                # local estimate. Keep those contracts orthogonal.
                "cost_bucket": (
                    "estimated" if status == "actual"
                    else status if status in {"estimated", "included"}
                    else "unknown"
                ),
                "has_pricing": has_known_pricing(model, provider, base_url),
                "residual": residual,
            }
            reconciled.append(normalized)
            return normalized

        for row in usage_rows:
            normalized = append_row(row)
            aggregate = totals[row["session_id"]]
            for key in (
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "api_call_count",
            ):
                aggregate[key] += row.get(key) or 0
            aggregate["estimated_cost_usd"] += normalized["estimated_cost_usd"]
            aggregate["actual_cost_usd"] += normalized["actual_cost_usd"]

        for session in sessions:
            aggregate = totals[session["id"]]
            session_estimated_cost = float(session.get("estimated_cost_usd") or 0.0)
            residual = {
                **session,
                "session_id": session["id"],
                "input_tokens": max(0, (session.get("input_tokens") or 0) - aggregate["input_tokens"]),
                "output_tokens": max(0, (session.get("output_tokens") or 0) - aggregate["output_tokens"]),
                "cache_read_tokens": max(0, (session.get("cache_read_tokens") or 0) - aggregate["cache_read_tokens"]),
                "cache_write_tokens": max(0, (session.get("cache_write_tokens") or 0) - aggregate["cache_write_tokens"]),
                "reasoning_tokens": 0,
                "api_call_count": max(0, (session.get("api_call_count") or 0) - aggregate["api_call_count"]),
                "estimated_cost_usd": max(
                    0.0, session_estimated_cost - aggregate["estimated_cost_usd"]
                ),
                # A positive session aggregate is authoritative reconciliation
                # evidence even when the more-specific model rows exceed it and
                # clamp the residual to zero. Do not re-price leftover tokens.
                "_estimated_cost_reconciled": session_estimated_cost > 0,
                "actual_cost_usd": max(0.0, float(session.get("actual_cost_usd") or 0.0) - aggregate["actual_cost_usd"]),
            }
            if not usage_rows or any(
                residual[key] for key in (
                    "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_write_tokens", "api_call_count",
                    "estimated_cost_usd", "actual_cost_usd",
                )
            ):
                append_row(residual, residual=True)
        return reconciled

    def _compute_model_breakdown(
        self, sessions: List[Dict], usage: List[Dict]
    ) -> List[Dict]:
        """Reduce the shared reconciled accounting population by model."""
        model_data = defaultdict(lambda: {
            "sessions": set(), "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "api_calls": 0,
            "tool_calls": 0, "cost": 0.0, "actual_cost": 0.0,
            "actual_cost_available": False, "statuses": set(),
            "has_pricing": False,
        })
        for row in usage:
            model = row["model"]
            display = model.split("/")[-1] if "/" in model else model
            data: Dict[str, Any] = model_data[display]
            data["sessions"].add(row["session_id"])
            for key in (
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens",
            ):
                data[key] += row[key]
            data["total_tokens"] += (
                row["input_tokens"] + row["output_tokens"]
                + row["cache_read_tokens"] + row["cache_write_tokens"]
            )
            data["api_calls"] += row["api_call_count"]
            data["cost"] += row["estimated_cost_usd"]
            data["actual_cost"] += row["actual_cost_usd"]
            data["actual_cost_available"] |= row["actual_cost_available"]
            data["statuses"].add(row["cost_status"])
            data["has_pricing"] |= row["has_pricing"]

        for session in sessions:
            model = session.get("model") or "unknown"
            display = model.split("/")[-1] if "/" in model else model
            model_data[display]["tool_calls"] += session.get("tool_call_count") or 0

        result = []
        for model, data in model_data.items():
            statuses = data.pop("statuses")
            entry = {"model": model, **data}
            entry["sessions"] = len(data["sessions"])
            entry["cost_status"] = (
                "unknown" if "unknown" in statuses
                else "actual" if "actual" in statuses
                else "estimated" if "estimated" in statuses
                else "included" if "included" in statuses
                else "unknown"
            )
            result.append(entry)
        result.sort(key=lambda row: (row["total_tokens"], row["sessions"]), reverse=True)
        return result

    def _compute_platform_breakdown(
        self, sessions: List[Dict], usage: List[Dict]
    ) -> List[Dict]:
        """Break down usage by platform/source."""
        platform_data = defaultdict(lambda: {
            "sessions": 0, "messages": 0, "input_tokens": 0,
            "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 0, "tool_calls": 0,
        })

        for s in sessions:
            source = s.get("source") or "unknown"
            d = platform_data[source]
            d["sessions"] += 1
            d["messages"] += s.get("message_count") or 0
            d["tool_calls"] += s.get("tool_call_count") or 0

        for row in usage:
            d = platform_data[row["source"]]
            d["input_tokens"] += row["input_tokens"]
            d["output_tokens"] += row["output_tokens"]
            d["cache_read_tokens"] += row["cache_read_tokens"]
            d["cache_write_tokens"] += row["cache_write_tokens"]
            d["total_tokens"] += (
                row["input_tokens"] + row["output_tokens"]
                + row["cache_read_tokens"] + row["cache_write_tokens"]
            )

        result = [
            {"platform": platform, **data}
            for platform, data in platform_data.items()
        ]
        result.sort(key=lambda x: x["sessions"], reverse=True)
        return result

    def _compute_tool_breakdown(self, tool_usage: List[Dict]) -> List[Dict]:
        """Process tool usage data into a ranked list with percentages."""
        total_calls = sum(t["count"] for t in tool_usage) if tool_usage else 0
        result = []
        for t in tool_usage:
            pct = (t["count"] / total_calls * 100) if total_calls else 0
            result.append({
                "tool": t["tool_name"],
                "count": t["count"],
                "percentage": pct,
            })
        return result

    def _compute_skill_breakdown(self, skill_usage: List[Dict]) -> Dict[str, Any]:
        """Process per-skill usage into summary + ranked list."""
        total_skill_loads = sum(s["view_count"] for s in skill_usage) if skill_usage else 0
        total_skill_edits = sum(s["manage_count"] for s in skill_usage) if skill_usage else 0
        total_skill_actions = total_skill_loads + total_skill_edits

        top_skills = []
        for skill in skill_usage:
            total_count = skill["view_count"] + skill["manage_count"]
            percentage = (total_count / total_skill_actions * 100) if total_skill_actions else 0
            top_skills.append({
                "skill": skill["skill"],
                "view_count": skill["view_count"],
                "manage_count": skill["manage_count"],
                "total_count": total_count,
                "percentage": percentage,
                "last_used_at": skill.get("last_used_at"),
            })

        top_skills.sort(
            key=lambda s: (
                s["total_count"],
                s["view_count"],
                s["manage_count"],
                s["last_used_at"] or 0,
                s["skill"],
            ),
            reverse=True,
        )

        return {
            "summary": {
                "total_skill_loads": total_skill_loads,
                "total_skill_edits": total_skill_edits,
                "total_skill_actions": total_skill_actions,
                "distinct_skills_used": len(skill_usage),
            },
            "top_skills": top_skills,
        }

    def _compute_activity_patterns(self, sessions: List[Dict]) -> Dict:
        """Analyze activity patterns by day of week and hour."""
        day_counts = Counter()  # 0=Monday ... 6=Sunday
        hour_counts = Counter()
        daily_counts = Counter()  # date string -> count

        for s in sessions:
            ts = s.get("started_at")
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts)
            day_counts[dt.weekday()] += 1
            hour_counts[dt.hour] += 1
            daily_counts[dt.strftime("%Y-%m-%d")] += 1

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_breakdown = [
            {"day": day_names[i], "count": day_counts.get(i, 0)}
            for i in range(7)
        ]

        hour_breakdown = [
            {"hour": i, "count": hour_counts.get(i, 0)}
            for i in range(24)
        ]

        # Busiest day and hour
        busiest_day = max(day_breakdown, key=lambda x: x["count"]) if day_breakdown else None
        busiest_hour = max(hour_breakdown, key=lambda x: x["count"]) if hour_breakdown else None

        # Active days (days with at least one session)
        active_days = len(daily_counts)

        # Streak calculation
        if daily_counts:
            all_dates = sorted(daily_counts.keys())
            current_streak = 1
            max_streak = 1
            for i in range(1, len(all_dates)):
                d1 = datetime.strptime(all_dates[i - 1], "%Y-%m-%d")
                d2 = datetime.strptime(all_dates[i], "%Y-%m-%d")
                if (d2 - d1).days == 1:
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 1
        else:
            max_streak = 0

        return {
            "by_day": day_breakdown,
            "by_hour": hour_breakdown,
            "busiest_day": busiest_day,
            "busiest_hour": busiest_hour,
            "active_days": active_days,
            "max_streak": max_streak,
        }

    def _compute_daily_series(
        self,
        sessions: List[Dict],
        usage: List[Dict],
        days: int,
        window_end: Optional[datetime] = None,
    ) -> List[Dict]:
        """Aggregate sessions into per-calendar-day token/cost buckets.

        Returns exactly ``days`` entries — one per calendar day ending today,
        oldest first — with zero-filled inactive days so the series forms a
        continuous axis for charting (heatmap columns, cumulative spend
        lines) instead of a sparse point list. Token/cost fields reconcile
        exactly with the overview totals: each session is bucketed into the
        calendar day of its ``started_at``.
        """
        # Seed the full window with zero buckets (oldest → today).
        today = window_end or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        series = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            series.append({
                "date": day.strftime("%Y-%m-%d"),
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "estimated_cost_usd": 0.0,
            })
        by_date = {row["date"]: row for row in series}

        for session in sessions:
            ts = session.get("started_at")
            if not ts:
                continue
            date_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            row = by_date.get(date_key)
            if row is None:
                # Outside the window (clock skew / boundary); skip.
                continue
            row["sessions"] += 1

        for item in usage:
            ts = item["started_at"]
            if not ts:
                continue
            row = by_date.get(datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
            if row is None:
                continue
            row["input_tokens"] += item["input_tokens"]
            row["output_tokens"] += item["output_tokens"]
            row["cache_read_tokens"] += item["cache_read_tokens"]
            row["cache_write_tokens"] += item["cache_write_tokens"]
            row["estimated_cost_usd"] += item["estimated_cost_usd"]

        return series

    def _compute_top_sessions(
        self, sessions: List[Dict], usage: List[Dict]
    ) -> List[Dict]:
        """Find notable sessions (longest, most messages, most tokens)."""
        top = []

        # Longest by duration
        sessions_with_duration = [
            s for s in sessions
            if s.get("started_at") and s.get("ended_at")
        ]
        if sessions_with_duration:
            longest = max(
                sessions_with_duration,
                key=lambda s: (s["ended_at"] - s["started_at"]),
            )
            dur = longest["ended_at"] - longest["started_at"]
            top.append({
                "label": "Longest session",
                "session_id": longest["id"][:16],
                "value": format_duration_compact(dur),
                "date": datetime.fromtimestamp(longest["started_at"]).strftime("%b %d"),
            })

        # Most messages
        most_msgs = max(sessions, key=lambda s: s.get("message_count") or 0)
        if (most_msgs.get("message_count") or 0) > 0:
            top.append({
                "label": "Most messages",
                "session_id": most_msgs["id"][:16],
                "value": f"{most_msgs['message_count']} msgs",
                "date": datetime.fromtimestamp(most_msgs["started_at"]).strftime("%b %d") if most_msgs.get("started_at") else "?",
            })

        # Most tokens
        tokens_by_session = Counter()
        for row in usage:
            tokens_by_session[row["session_id"]] += (
                row["input_tokens"] + row["output_tokens"]
                + row["cache_read_tokens"] + row["cache_write_tokens"]
            )
        most_tokens = max(sessions, key=lambda s: tokens_by_session[s["id"]])
        token_total = tokens_by_session[most_tokens["id"]]
        if token_total > 0:
            top.append({
                "label": "Most tokens",
                "session_id": most_tokens["id"][:16],
                "value": f"{token_total:,} tokens",
                "date": datetime.fromtimestamp(most_tokens["started_at"]).strftime("%b %d") if most_tokens.get("started_at") else "?",
            })

        # Most tool calls
        most_tools = max(sessions, key=lambda s: s.get("tool_call_count") or 0)
        if (most_tools.get("tool_call_count") or 0) > 0:
            top.append({
                "label": "Most tool calls",
                "session_id": most_tools["id"][:16],
                "value": f"{most_tools['tool_call_count']} calls",
                "date": datetime.fromtimestamp(most_tools["started_at"]).strftime("%b %d") if most_tools.get("started_at") else "?",
            })

        return top

    # =========================================================================
    # Formatting
    # =========================================================================

    def format_terminal(self, report: Dict) -> str:
        """Format the insights report for terminal display (CLI)."""
        if report.get("empty"):
            days = report.get("days", 30)
            src = f" (source: {report['source_filter']})" if report.get("source_filter") else ""
            return f"  No sessions found in the last {days} days{src}."

        lines = []
        o = report["overview"]
        days = report["days"]
        src_filter = report.get("source_filter")

        # Header
        lines.append("")
        lines.append("  ╔══════════════════════════════════════════════════════════╗")
        lines.append("  ║                    📊 Hermes Insights                    ║")
        period_label = f"Last {days} days"
        if src_filter:
            period_label += f" ({src_filter})"
        padding = 58 - len(period_label) - 2
        left_pad = padding // 2
        right_pad = padding - left_pad
        lines.append(f"  ║{' ' * left_pad} {period_label} {' ' * right_pad}║")
        lines.append("  ╚══════════════════════════════════════════════════════════╝")
        lines.append("")

        # Date range
        if o.get("date_range_start") and o.get("date_range_end"):
            start_str = datetime.fromtimestamp(o["date_range_start"]).strftime("%b %d, %Y")
            end_str = datetime.fromtimestamp(o["date_range_end"]).strftime("%b %d, %Y")
            lines.append(f"  Period: {start_str} — {end_str}")
            lines.append("")

        # Overview
        lines.append("  📋 Overview")
        lines.append("  " + "─" * 56)
        lines.append(f"  Sessions:          {o['total_sessions']:<12}  Messages:        {o['total_messages']:,}")
        lines.append(f"  Tool calls:        {o['total_tool_calls']:<12,}  User messages:   {o['user_messages']:,}")
        lines.append(f"  Input tokens:      {o['total_input_tokens']:<12,}  Output tokens:   {o['total_output_tokens']:,}")
        lines.append(f"  Total tokens:      {o['total_tokens']:,}")
        if o["total_hours"] > 0:
            lines.append(f"  Active time:       ~{format_duration_compact(o['total_hours'] * 3600):<11}  Avg session:     ~{format_duration_compact(o['avg_session_duration'])}")
        lines.append(f"  Avg msgs/session:  {o['avg_messages_per_session']:.1f}")
        lines.append("")

        # Cost breakdown — surface the three buckets so subscription-included
        # and unknown-cost sessions are visible instead of silently collapsing
        # to $0. See #77223.
        est_cost = o.get("estimated_cost", 0.0)
        included_sessions = o.get("included_cost_sessions", 0)
        unknown_sessions = o.get("unknown_cost_sessions", 0)
        if est_cost > 0 or included_sessions > 0 or unknown_sessions > 0:
            lines.append("  💰 Cost")
            lines.append("  " + "─" * 56)
            if est_cost > 0:
                lines.append(f"  Estimated:          {_fmt_est_cost(est_cost)}")
            if included_sessions > 0:
                lines.append(
                    f"  Included:           {included_sessions} session(s) "
                    f"(subscription — no provider invoice)"
                )
            if unknown_sessions > 0:
                lines.append(
                    f"  Unknown:            {unknown_sessions} session(s) "
                    f"(no pricing data)"
                )
            lines.append("")

        # Model breakdown
        if report["models"]:
            lines.append("  🤖 Models Used")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Model':<30} {'Sessions':>8} {'Tokens':>12}")
            for m in report["models"]:
                model_name = m["model"][:28]
                lines.append(f"  {model_name:<30} {m['sessions']:>8} {m['total_tokens']:>12,}")
            lines.append("")

        # Platform breakdown
        if len(report["platforms"]) > 1 or (report["platforms"] and report["platforms"][0]["platform"] != "cli"):
            lines.append("  📱 Platforms")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Platform':<14} {'Sessions':>8} {'Messages':>10} {'Tokens':>14}")
            for p in report["platforms"]:
                lines.append(f"  {p['platform']:<14} {p['sessions']:>8} {p['messages']:>10,} {p['total_tokens']:>14,}")
            lines.append("")

        # Tool usage
        if report["tools"]:
            lines.append("  🔧 Top Tools")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Tool':<28} {'Calls':>8} {'%':>8}")
            for t in report["tools"][:15]:  # Top 15
                lines.append(f"  {t['tool']:<28} {t['count']:>8,} {t['percentage']:>7.1f}%")
            if len(report["tools"]) > 15:
                lines.append(f"  ... and {len(report['tools']) - 15} more tools")
            lines.append("")

        # Skill usage
        skills = report.get("skills", {})
        top_skills = skills.get("top_skills", [])
        if top_skills:
            lines.append("  🧠 Top Skills")
            lines.append("  " + "─" * 56)
            lines.append(f"  {'Skill':<28} {'Loads':>7} {'Edits':>7} {'Last used':>11}")
            for skill in top_skills[:10]:
                last_used = "—"
                if skill.get("last_used_at"):
                    last_used = datetime.fromtimestamp(skill["last_used_at"]).strftime("%b %d")
                lines.append(
                    f"  {skill['skill'][:28]:<28} {skill['view_count']:>7,} {skill['manage_count']:>7,} {last_used:>11}"
                )
            summary = skills.get("summary", {})
            lines.append(
                f"  Distinct skills: {summary.get('distinct_skills_used', 0)}  "
                f"Loads: {summary.get('total_skill_loads', 0):,}  "
                f"Edits: {summary.get('total_skill_edits', 0):,}"
            )
            lines.append("")

        # Activity patterns
        act = report.get("activity", {})
        if act.get("by_day"):
            lines.append("  📅 Activity Patterns")
            lines.append("  " + "─" * 56)

            # Day of week chart
            day_values = [d["count"] for d in act["by_day"]]
            bars = _bar_chart(day_values, max_width=15)
            for i, d in enumerate(act["by_day"]):
                bar = bars[i]
                lines.append(f"  {d['day']}  {bar:<15} {d['count']}")

            lines.append("")

            # Peak hours (show top 5 busiest hours)
            busy_hours = sorted(act["by_hour"], key=lambda x: x["count"], reverse=True)
            busy_hours = [h for h in busy_hours if h["count"] > 0][:5]
            if busy_hours:
                hour_strs = []
                for h in busy_hours:
                    hr = h["hour"]
                    ampm = "AM" if hr < 12 else "PM"
                    display_hr = hr % 12 or 12
                    hour_strs.append(f"{display_hr}{ampm} ({h['count']})")
                lines.append(f"  Peak hours: {', '.join(hour_strs)}")

            if act.get("active_days"):
                lines.append(f"  Active days: {act['active_days']}")
            if act.get("max_streak") and act["max_streak"] > 1:
                lines.append(f"  Best streak: {act['max_streak']} consecutive days")
            lines.append("")

        # Notable sessions
        if report.get("top_sessions"):
            lines.append("  🏆 Notable Sessions")
            lines.append("  " + "─" * 56)
            for ts in report["top_sessions"]:
                lines.append(f"  {ts['label']:<20} {ts['value']:<18} ({ts['date']}, {ts['session_id']})")
            lines.append("")

        return "\n".join(lines)

    def format_gateway(self, report: Dict) -> str:
        """Format the insights report for gateway/messaging (shorter)."""
        if report.get("empty"):
            days = report.get("days", 30)
            return f"No sessions found in the last {days} days."

        lines = []
        o = report["overview"]
        days = report["days"]

        lines.append(f"📊 **Hermes Insights** — Last {days} days\n")

        # Overview
        lines.append(f"**Sessions:** {o['total_sessions']} | **Messages:** {o['total_messages']:,} | **Tool calls:** {o['total_tool_calls']:,}")
        lines.append(f"**Tokens:** {o['total_tokens']:,} (in: {o['total_input_tokens']:,} / out: {o['total_output_tokens']:,})")
        if o["total_hours"] > 0:
            lines.append(f"**Active time:** ~{format_duration_compact(o['total_hours'] * 3600)} | **Avg session:** ~{format_duration_compact(o['avg_session_duration'])}")
        lines.append("")

        # Cost breakdown — surface buckets so included/unknown are visible
        est_cost = o.get("estimated_cost", 0.0)
        included = o.get("included_cost_sessions", 0)
        unknown = o.get("unknown_cost_sessions", 0)
        cost_parts: list[str] = []
        if est_cost > 0:
            cost_parts.append(f"{_fmt_est_cost(est_cost)} estimated")
        if included > 0:
            cost_parts.append(f"{included} included (subscription)")
        if unknown > 0:
            cost_parts.append(f"{unknown} unknown")
        if cost_parts:
            lines.append(f"**Cost:** {' | '.join(cost_parts)}")
            lines.append("")

        # Models (top 5)
        if report["models"]:
            lines.append("**🤖 Models:**")
            for m in report["models"][:5]:
                lines.append(f"  {m['model'][:25]} — {m['sessions']} sessions, {m['total_tokens']:,} tokens")
            lines.append("")

        # Platforms (if multi-platform)
        if len(report["platforms"]) > 1:
            lines.append("**📱 Platforms:**")
            for p in report["platforms"]:
                lines.append(f"  {p['platform']} — {p['sessions']} sessions, {p['messages']:,} msgs")
            lines.append("")

        # Tools (top 8)
        if report["tools"]:
            lines.append("**🔧 Top Tools:**")
            for t in report["tools"][:8]:
                lines.append(f"  {t['tool']} — {t['count']:,} calls ({t['percentage']:.1f}%)")
            lines.append("")

        skills = report.get("skills", {})
        if skills.get("top_skills"):
            lines.append("**🧠 Top Skills:**")
            for skill in skills["top_skills"][:5]:
                suffix = ""
                if skill.get("last_used_at"):
                    suffix = f", last used {datetime.fromtimestamp(skill['last_used_at']).strftime('%b %d')}"
                lines.append(
                    f"  {skill['skill']} — {skill['view_count']:,} loads, {skill['manage_count']:,} edits{suffix}"
                )
            lines.append("")

        # Activity summary
        act = report.get("activity", {})
        if act.get("busiest_day") and act.get("busiest_hour"):
            hr = act["busiest_hour"]["hour"]
            ampm = "AM" if hr < 12 else "PM"
            display_hr = hr % 12 or 12
            lines.append(f"**📅 Busiest:** {act['busiest_day']['day']}s ({act['busiest_day']['count']} sessions), {display_hr}{ampm} ({act['busiest_hour']['count']} sessions)")
            if act.get("active_days"):
                lines.append(f"**Active days:** {act['active_days']}", )
            if act.get("max_streak", 0) > 1:
                lines.append(f"**Best streak:** {act['max_streak']} consecutive days")

        return "\n".join(lines)
