"""MEMORY.md template renderer — six canonical sections.

The output shape is verbatim from the source spec at
``~/.claude/plans/my-hermes-agents-shiny-sky.md`` Part 4 (lines 270-306).
Sections, in order:

  1. Operating Stats        (Curator weekly)
  2. Calibration State      (Curator nightly / Critic weekly)
  3. Learned Patterns       (Curator append-only; Critic curates)
  4. Skills in Use          (Curator-maintained)
  5. Constitutional Principles (static; only Critic + Diego can change)
  6. Nudges Inbox           (Critic / Curator → agent)

Pure function: takes slicer + consolidator outputs + per-agent extras,
returns a string. No I/O.

Spec: ``docs/superpowers/plans/2026-04-26-curator-backfill-and-nightly.md``
Task 4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Bootstrap separator used when appending to Diego's main MEMORY.md.
_MAIN_APPEND_HEADER = "# Curator-Bootstrapped Sections"
# Confidence threshold below which a skill is flagged for review.
_FLAG_THRESHOLD = 0.5

# Markers that begin curator-generated content in a rendered MEMORY.md.
_CURATOR_BANNERS = (_MAIN_APPEND_HEADER, "# MEMORY — ")


def _stable_prefix(content: str) -> str:
    """Human-authored head of a MEMORY.md, for bounded carry-forward.

    Returns everything before the first curator-generated banner
    (``_MAIN_APPEND_HEADER`` or ``# MEMORY — <agent>``), with accumulated
    ``## Prior Notes (pre-…)`` wrapper headers (preserve_with_prior artifacts)
    and any trailing blank / ``---`` separator lines stripped. ``main`` keeps
    its ``# Jaum Memory`` top matter; non-main files are 100% curator-generated
    so this returns ``""`` → the caller collapses to a clean replace.

    Stripping the trailing separator makes re-rendering idempotent: feeding a
    rendered file back in yields the same stable prefix, so nothing accumulates.
    """
    if not content:
        return ""
    cut = len(content)
    for banner in _CURATOR_BANNERS:
        pos = content.find(banner)
        if pos != -1 and pos < cut:
            cut = pos
    kept = [
        line for line in content[:cut].splitlines()
        if not line.startswith("## Prior Notes (pre-")
    ]
    # Drop trailing blank / horizontal-rule ('---') separator lines.
    while kept and (not kept[-1].strip() or set(kept[-1].strip()) == {"-"}):
        kept.pop()
    text = "\n".join(kept).strip()
    return text + "\n" if text else ""


def _format_operating_stats(agent: str, audit: Dict[str, Any], drawer: Dict[str, Any]) -> str:
    runs_total = audit.get("runs_total", 0)
    runs_ok = audit.get("runs_ok", 0)
    runs_fail = audit.get("runs_fail", 0)
    avg_lat = audit.get("avg_duration_s")
    avg_lat_str = f"{avg_lat:.1f}s per unit" if isinstance(avg_lat, (int, float)) else "n/a"
    drawer_total = drawer.get("drawer_count_total", 0)
    et_counts = audit.get("event_type_counts", {})
    mailbox_count = et_counts.get("mailbox_message", 0)
    # Confidence bands from audit data — coarse approximation: ok=high, fail=low,
    # remainder=medium. Real Curator nightly will refine; this is the floor.
    if runs_total:
        high_pct = round(100 * runs_ok / runs_total)
        low_pct = round(100 * runs_fail / runs_total)
        med_pct = max(0, 100 - high_pct - low_pct)
    else:
        high_pct = med_pct = low_pct = 0
    work_units = (
        f"{runs_total} runs / {drawer_total} drawers consolidated / "
        f"{mailbox_count} mailbox messages handled"
    )
    return (
        "## Operating Stats (updated weekly by Curator)\n"
        f"- **Runs last 30d:** {runs_total} ({runs_ok} ok, {runs_fail} fail)\n"
        f"- **Work units:** {work_units}\n"
        f"- **Confidence bands:** high={high_pct}%, medium={med_pct}%, low={low_pct}%\n"
        f"- **Avg latency:** {avg_lat_str}\n"
        + (
            "- **Event-type breakdown:**\n"
            + "\n".join(f"  - `{k}`: {v}" for k, v in sorted(et_counts.items(), key=lambda kv: -kv[1]))
            if et_counts else "- **Event-type breakdown:** _(no events in window)_"
        )
        + "\n"
    )


def _format_calibration(agent: str, calibration: Optional[Dict[str, Any]]) -> str:
    header = "## Calibration State (updated nightly by Curator / weekly by Critic)\n"
    if not calibration:
        return (
            header
            + "- Last calibration: not yet established (awaiting first Critic pass).\n"
            + "- _(Critic populates this section weekly; Curator refreshes nightly when calibration_state.json exists.)_\n"
        )
    snapshot = calibration.get("snapshot")
    last = calibration.get("last_calibration")
    body = ""
    if snapshot:
        body += f"- Scoring weights snapshot: {snapshot}\n"
    if last:
        body += f"- Last calibration: {last}\n"
    return header + (body or "- _(unknown calibration shape; raw data preserved in profiles/{agent}/workspace/calibration_state.json)_\n")


def _format_learned_patterns(agent: str, drawer: Dict[str, Any], audit: Dict[str, Any], generated_at: datetime) -> str:
    header = "## Learned Patterns (Curator appends; Critic curates)\n"
    out_lines: List[str] = []
    date_str = generated_at.date().isoformat()

    # Audit-derived patterns (always present, audit-only fallback when MCP down).
    runs_fail = audit.get("runs_fail", 0)
    runs_total = audit.get("runs_total", 0)
    if runs_total:
        if runs_fail / max(runs_total, 1) > 0.1:
            out_lines.append(
                f"- **Pattern ({date_str}):** Failure rate {runs_fail}/{runs_total} "
                "exceeds 10% over the 30d window — investigate root cause cluster."
            )
        else:
            out_lines.append(
                f"- **Pattern ({date_str}):** Stable run cadence — "
                f"{runs_total} total runs ({runs_fail} failures) over the 30d window."
            )

    # MemPalace-derived pattern candidates from drawer titles.
    for cand in drawer.get("pattern_candidates", []) or []:
        title = (cand.get("title") or "").strip()
        body = (cand.get("body") or "").strip()
        room = (cand.get("room") or "").strip()
        wing = (cand.get("wing") or "").strip()
        created = (cand.get("created_at") or "").split("T", 1)[0]
        if not title:
            continue
        snippet = body[:320].replace("\n", " ").strip()
        provenance_bits = [b for b in (wing, room, created) if b]
        provenance = (
            f" _[{' / '.join(provenance_bits)}]_" if provenance_bits else ""
        )
        out_lines.append(
            f"- **Pattern ({date_str}):** {title}"
            + (f" — {snippet}" if snippet else "")
            + provenance
        )

    # If nothing surfaced, leave a placeholder so the section is non-empty.
    if not out_lines:
        out_lines.append(
            f"- **Pattern ({date_str}):** _Insufficient evidence in the 30d window; "
            "nightly delta will surface patterns as they accumulate._"
        )

    return header + "\n".join(out_lines) + "\n"


def _format_skills_in_use(skills: List[Dict[str, Any]]) -> str:
    header = "## Skills in Use (Curator-maintained)\n"
    if not skills:
        return header + "_(no skill metadata yet; Critic promotes skills as patterns confirm.)_\n"
    # Sort by confidence DESC so high-confidence skills surface first; flagged sink to bottom.
    ordered = sorted(skills, key=lambda s: -float(s.get("confidence", 0.0)))
    lines: List[str] = []
    for s in ordered:
        name = s.get("name", "?")
        succ = int(s.get("success", 0))
        fail = int(s.get("fail", 0))
        conf = float(s.get("confidence", 0.0))
        flag = " **flagged for review**" if conf < _FLAG_THRESHOLD else ""
        lines.append(f"- `{name}` (success: {succ}/{succ + fail}, confidence: {conf:.2f}){flag}")
    return header + "\n".join(lines) + "\n"


def _format_constitutional(principles: List[str]) -> str:
    header = "## Constitutional Principles (static; only Critic + Diego can change)\n"
    if not principles:
        return header + "_(none recorded; populate from agent SOUL.md.)_\n"
    return header + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(principles)) + "\n"


def _format_nudges(nudges: Optional[List[str]]) -> str:
    header = "## Nudges Inbox (things Critic or Curator want this agent to try next)\n"
    if not nudges:
        return header + "_(empty; Critic populates after weekly retros.)_\n"
    return header + "\n".join(f"- [ ] {n}" for n in nudges) + "\n"


def _format_drawer_room_summary(drawer: Dict[str, Any]) -> str:
    rooms = drawer.get("drawers_by_room", {}) or {}
    recent = drawer.get("recent_drawers", []) or []
    if not rooms and not recent:
        return ""
    parts = ["\n### MemPalace drawer distribution (last 30d)\n"]
    parts.append(f"- **Total drawers in window:** {drawer.get('drawer_count_total', 0)}\n")
    if rooms:
        items = sorted(rooms.items(), key=lambda kv: -kv[1])
        body = "\n".join(f"  - `{room}`: {count}" for room, count in items)
        parts.append(f"- **By room:**\n{body}\n")
    # Show first 5 recent drawer titles for human readability.
    if recent:
        parts.append("- **Most recent (top 5):**\n")
        for d in recent[:5]:
            title = (d.get("title") or "(no title)")[:120].replace("\n", " ")
            created = (d.get("created_at") or "").split("T", 1)[0]
            parts.append(f"  - {created} — {title}\n")
    return "".join(parts)


def _format_footer(generated_at: datetime, source: str, degraded: bool) -> str:
    flag = " (degraded — MemPalace unreachable, audit-only)" if degraded else ""
    return (
        "\n---\n"
        f"_Generated by Curator at {generated_at.isoformat(timespec='seconds')}. "
        f"Source: {source}.{flag}_\n"
    )


def render(
    agent: str,
    audit_stats: Dict[str, Any],
    drawer_data: Dict[str, Any],
    constitutional_principles: List[str],
    skills_observed: List[Dict[str, Any]],
    existing_content: Optional[str] = None,
    mode: str = "replace",
    generated_at: Optional[datetime] = None,
    source: str = "bootstrap",
    nudges: Optional[List[str]] = None,
    calibration: Optional[Dict[str, Any]] = None,
) -> str:
    """Render MEMORY.md content from slicer + consolidator outputs.

    Modes:
        replace: emit just the rendered six sections (with banner header).
        preserve_with_prior: prepend a "## Prior Notes" block carrying any
            existing content >30 lines, then the six rendered sections.
        append: emit ``existing_content`` verbatim, then a separator,
            then the rendered six sections. Used for ``main`` only.
    """
    generated_at = generated_at or datetime.now(timezone.utc)
    degraded = bool(drawer_data.get("error"))

    rendered_block = (
        f"# MEMORY — {agent}\n"
        f"\n"
        f"_Bootstrapped/refreshed by Curator on {generated_at.date().isoformat()} from "
        f"audit.jsonl + MemPalace drawers (last 30 days). After this file exists, "
        f"Curator's nightly cadence owns Operating Stats / Calibration State / "
        f"Learned Patterns / Skills in Use. **Constitutional Principles** is static "
        f"and Diego-only._\n"
        f"\n"
        + _format_operating_stats(agent, audit_stats, drawer_data) + "\n"
        + _format_calibration(agent, calibration) + "\n"
        + _format_learned_patterns(agent, drawer_data, audit_stats, generated_at) + "\n"
        + _format_skills_in_use(skills_observed) + "\n"
        + _format_constitutional(constitutional_principles) + "\n"
        + _format_nudges(nudges)
        + _format_drawer_room_summary(drawer_data)
        + _format_footer(generated_at, source, degraded)
    )

    if mode == "append":
        # Carry forward ONLY the stable human prefix, not the entire prior file,
        # so nightly runs don't stack stale rendered blocks (unbounded growth).
        prefix = _stable_prefix(existing_content or "")
        if not prefix:
            return rendered_block
        sep = (
            f"\n\n---\n\n{_MAIN_APPEND_HEADER} ({generated_at.date().isoformat()})\n\n"
        )
        return prefix + sep + rendered_block
    if mode == "preserve_with_prior":
        # Same bounding: preserve genuine human content if any, else collapse to
        # a clean replace (non-main files are 100% curator-generated).
        prefix = _stable_prefix(existing_content or "")
        if prefix:
            preserved = (
                f"## Prior Notes (pre-{generated_at.date().isoformat()})\n\n"
                + prefix
                + "\n\n---\n\n"
            )
            return preserved + rendered_block
        return rendered_block
    return rendered_block
