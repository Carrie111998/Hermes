"""Curator orchestrator — drives backfill / nightly across all agents.

Dependencies are injected (slicer, consolidator, renderer, bus, search_fn)
so tests can stub them. The orchestrator handles:
  * Per-agent dispatch (10 agents in canonical order).
  * Mode selection: append for ``main``, preserve_with_prior for agents
    whose existing MEMORY.md is non-trivial, replace otherwise.
  * Pre-write backup snapshots (``MEMORY.md.bak-<timestamp>``).
  * Per-agent failure isolation (one agent's exception does not stop the
    others; failed agents are reported in the result).
  * Optional ``curator_daily`` event emission.

Spec: ``docs/superpowers/plans/2026-04-26-curator-backfill-and-nightly.md``
Tasks 5 + 6 + 7.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .audit_slicer import slice_agent_events
from .drawer_consolidator import consolidate_for_agent
from .memory_renderer import render as default_render

logger = logging.getLogger(__name__)

# Canonical agent order (mirrors plan §Task 5.3).
AGENTS: List[str] = [
    "scout",
    "sentinel",
    "matcher",
    "tailor",
    "applier",
    "tracker",
    "notifier",
    "cv-handler",
    "devflow",
    "main",
]

# main is APPENDED to (Diego-authored), never replaced.
APPEND_ONLY_AGENTS = {"main"}

# Curator default Constitutional Principles (one entry per agent) —
# imported from the legacy bootstrap module so we maintain a single
# source of truth. Same for the seed Learned Patterns.
try:
    from profiles.curator.workspace.memory_bootstrap import CONSTITUTIONAL, PATTERNS_SEED
except Exception:  # pragma: no cover — legacy module path may not import
    CONSTITUTIONAL: Dict[str, List[str]] = {}
    PATTERNS_SEED: Dict[str, List[str]] = {}


RenderFn = Callable[..., str]
SearchFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]


@dataclass
class BackfillResult:
    """Aggregate outcome of a backfill or nightly run."""
    mode: str  # "backfill" | "nightly"
    agents_updated: List[str] = field(default_factory=list)
    agents_failed: List[Tuple[str, str]] = field(default_factory=list)
    patterns_seeded: int = 0
    skills_observed: int = 0
    drawers_scanned: int = 0
    bytes_written: int = 0
    duration_s: float = 0.0
    degraded: bool = False
    diffs: Dict[str, Tuple[int, int]] = field(default_factory=dict)


def _select_mode(agent: str, existing: str) -> str:
    if agent in APPEND_ONLY_AGENTS:
        return "append"
    line_count = existing.count("\n") if existing else 0
    if line_count > 30:
        return "preserve_with_prior"
    return "replace"


def _seed_patterns(agent: str) -> List[Dict[str, Any]]:
    """Convert legacy PATTERNS_SEED strings into pattern_candidates shape."""
    seeds = PATTERNS_SEED.get(agent, []) or []
    today = datetime.now(timezone.utc).date().isoformat()
    return [
        {
            "title": f"Seeded — {seed[:80]}",
            "body": seed,
            "created_at": f"{today}T00:00:00+00:00",
            "wing": ".openclaw",
            "room": agent,
        }
        for seed in seeds
    ]


def _emit_event(bus, mode: str, result: BackfillResult, generated_at: datetime) -> None:
    """Best-effort event emission. We don't import the real Event class
    here — the orchestrator exposes a simple shape, and the actual
    producer (``events/producers/curator.py``) wraps it into the bus's
    expected envelope.
    """
    if bus is None:
        return
    payload = {
        "mode": mode,
        "agents_updated": result.agents_updated,
        "patterns_seeded": result.patterns_seeded,
        "skills_observed": result.skills_observed,
        "drawers_scanned": result.drawers_scanned,
        "degraded": result.degraded,
        "duration_s": result.duration_s,
        "bytes_written": result.bytes_written,
        "generated_at": generated_at.isoformat(),
    }
    try:
        from .producers.curator import emit_curator_daily
        emit_curator_daily(bus, payload)
        return
    except Exception as exc:
        logger.warning("producer import failed (%s); falling back to dict event", exc)
    # Fallback dict event so tests / degraded environments still record.
    try:
        bus.emit({
            "event_type": "curator_daily",
            "source": "curator",
            "priority": "normal",
            "payload": payload,
            "timestamp": generated_at.isoformat(),
        })
    except Exception:
        logger.exception("could not emit curator_daily")


def _process_agent(
    agent: str,
    *,
    audit_path: Path,
    search_fn: SearchFn,
    hermes_root: Path,
    window_days: int,
    dry_run: bool,
    render_fn: RenderFn,
    generated_at: datetime,
    source: str,
) -> Dict[str, Any]:
    """Render and (if not dry-run) write MEMORY.md for one agent."""
    audit = slice_agent_events(audit_path, agent, window_days=window_days, now=generated_at)
    drawer = consolidate_for_agent(agent, search_fn, window_days=window_days, now=generated_at)

    # Inject seed pattern candidates so a fresh agent gets meaningful Learned Patterns
    # at bootstrap. Real drawer-derived candidates take priority but appear alongside.
    seeds = _seed_patterns(agent)
    if seeds:
        drawer = dict(drawer)
        drawer["pattern_candidates"] = (drawer.get("pattern_candidates") or []) + seeds

    target = hermes_root / "profiles" / agent / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    pre_size = len(existing.encode("utf-8"))

    mode = _select_mode(agent, existing)
    principles = CONSTITUTIONAL.get(agent, [])

    rendered = render_fn(
        agent,
        audit_stats=audit,
        drawer_data=drawer,
        constitutional_principles=principles,
        skills_observed=[],
        existing_content=existing,
        mode=mode,
        generated_at=generated_at,
        source=source,
    )

    post_size = len(rendered.encode("utf-8"))
    if not dry_run:
        if existing:
            try:
                target.with_suffix(
                    target.suffix + f".bak-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
                ).write_text(existing, encoding="utf-8")
            except OSError as exc:
                logger.warning("backup failed for %s: %s", agent, exc)
        target.write_text(rendered, encoding="utf-8")

    return {
        "mode": mode,
        "pre_size": pre_size,
        "post_size": post_size,
        "patterns_count": len(drawer.get("pattern_candidates", []) or []),
        "drawers_scanned": int(drawer.get("drawer_count_total", 0) or 0),
        "degraded": bool(drawer.get("error")),
        "audit_runs": int(audit.get("runs_total", 0) or 0),
    }


def run_backfill(
    *,
    window_days: int,
    dry_run: bool,
    emit_event: bool,
    audit_path: Path,
    search_fn: SearchFn,
    bus,
    hermes_root: Path,
    render_fn: Optional[RenderFn] = None,
    agents: Optional[List[str]] = None,
    source: str = "bootstrap",
    mode_label: str = "backfill",
) -> BackfillResult:
    """Orchestrate a backfill (or nightly delta) across the given agents."""
    started = time.monotonic()
    generated_at = datetime.now(timezone.utc)
    render_fn = render_fn or default_render
    target_agents = agents or AGENTS

    result = BackfillResult(mode=mode_label)

    for agent in target_agents:
        try:
            stats = _process_agent(
                agent,
                audit_path=audit_path,
                search_fn=search_fn,
                hermes_root=hermes_root,
                window_days=window_days,
                dry_run=dry_run,
                render_fn=render_fn,
                generated_at=generated_at,
                source=source,
            )
            result.agents_updated.append(agent)
            result.patterns_seeded += stats["patterns_count"]
            result.drawers_scanned += stats["drawers_scanned"]
            if not dry_run:
                # Approximate bytes_written = full rendered file size on disk.
                result.bytes_written += stats["post_size"]
            result.diffs[agent] = (stats["pre_size"], stats["post_size"])
            if stats["degraded"]:
                result.degraded = True
        except Exception as exc:
            logger.exception("agent %s failed during backfill", agent)
            result.agents_failed.append((agent, str(exc)))

    result.duration_s = time.monotonic() - started

    if emit_event:
        _emit_event(bus, mode_label, result, generated_at)

    return result


def run_nightly(
    *,
    audit_path: Path,
    search_fn: SearchFn,
    bus,
    hermes_root: Path,
    render_fn: Optional[RenderFn] = None,
) -> BackfillResult:
    """Nightly delta-pass: 24h window, append-mode for Learned Patterns."""
    return run_backfill(
        window_days=1,
        dry_run=False,
        emit_event=True,
        audit_path=audit_path,
        search_fn=search_fn,
        bus=bus,
        hermes_root=hermes_root,
        render_fn=render_fn,
        source="nightly",
        mode_label="nightly",
    )
