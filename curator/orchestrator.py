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

# Curator default Constitutional Principles and seed Learned Patterns, one
# entry per agent. VENDORED -- copied verbatim from the legacy bootstrap module
# ``profiles/curator/workspace/memory_bootstrap.py``, which lives in the
# ``~/.hermes`` PARENT repo, not in this one, and which has been marked
# "DEPRECATED 2026-04-26 ... do NOT invoke" since the day this package
# superseded it.
#
# The previous form hard-wired that developer-machine absolute path and
# ``exec_module``'d the script at IMPORT time, falling back to ``({}, {})``
# when ``.exists()`` was False. So on any machine or container that was not
# this laptop -- a fresh clone, CI, a deploy -- ``curator.orchestrator``
# imported clean with BOTH dicts empty, and every agent silently lost its
# Constitutional Principles (used by ``_render_agent``) and its seeded pattern
# candidates (``_seed_patterns``) from the rendered MEMORY.md. Silent, and
# semantically significant: the docstring called that import "a single source
# of truth" while it was really a cross-repo dependency on a deprecated script
# with no warning anywhere on the failure path.
#
# Vendoring removes the runtime dependency instead of relocating it: resolving
# the path via HERMES_HOME would still be a cross-repo read that legitimately
# returns nothing on a standalone checkout. It is also what the sibling
# constant already does -- ``audit_slicer.AGENT_SOURCES`` mirrors the very same
# legacy file by copy (see the comment above it). If that file is ever revived,
# these are kept in sync by hand; ``tests/curator/test_orchestrator.py`` pins
# that both stay populated and cover every agent in ``AGENTS``.
CONSTITUTIONAL: Dict[str, List[str]] = {
    "scout": [
        "Never submit scraped data without dedupe against last 30d.",
        "Never auto-rotate sources without recording which source was retired and why.",
        "Always emit SCOUT_DISCOVERY batch summary even if batch is empty.",
    ],
    "sentinel": [
        "Never auto-apply to a VIP without Jaum's WhatsApp confirmation.",
        "Never skip the LinkedIn rate-limit cool-down (30-60s between interactions).",
        "Always route VIP fast-path through Tracker so pipeline state stays canonical.",
    ],
    "matcher": [
        "Never score a role I don't have a full JD for.",
        "Never skip CV Handler grounding before calibrating.",
        "Always emit SCORE_BATCH_SUMMARY even if batch is empty.",
    ],
    "tailor": [
        "Never fabricate experience. Grounding over fantasy.",
        "Never reshuffle bullets in a way that creates false implications.",
        "Always preserve user voice — reshuffle, don't rewrite from scratch.",
    ],
    "applier": [
        "Never auto-submit without Jaum's explicit SUBMIT_CONFIRM (WhatsApp).",
        "Never skip the dry-run capture (screenshots required for audit).",
        "Always emit BLOCKED_QUESTION with full field spec if the form can't be completed.",
    ],
    "tracker": [
        "Only Tracker writes canonical stage state; other agents emit STATE_TRANSITION_INTENT.",
        "Never skip the 14-day follow-up scan even when the pipeline is quiet.",
        "Always reconcile inconsistent state (e.g. stage=applied but no SUBMIT_RESULT) before advancing.",
    ],
    "notifier": [
        "Respect quiet hours (23:00-07:00 ET) except for breakthrough events (interview_signal, offer_signal).",
        "Never compose an empty digest — say 'quiet overnight' explicitly.",
        "Apply ADR-0016 rate-limiting (token bucket per subscriber+event_type).",
    ],
    "cv-handler": [
        "Read-only from other agents' perspective — never update source files autonomously.",
        "Always return KB_RESPONSE even on lookup failure (with error payload); never silently drop.",
        "Never expose raw files; scope KB_RESPONSE to requested variants/accomplishments.",
    ],
    "devflow": [
        "Network / secret / deploy are deny-by-default — require explicit Jaum approval.",
        "Never merge to main without all tests passing.",
        "Always emit approval_requested for any sub-task that exceeds the gate policy.",
    ],
    "main": [
        "Only main sends WhatsApp. Other agents route via main's mailbox.",
        "Never auto-apply Critic's structural proposals — always confirm via WhatsApp first.",
        "Preserve Diego's overrides above any agent's autonomous decision.",
    ],
}

# Learned-patterns seed text per agent (from plan + session history).
PATTERNS_SEED: Dict[str, List[str]] = {
    "scout": [
        "Discovery sources: Indeed, Glassdoor, LinkedIn, Workday, hiring.cafe. Plan expects 8+ eventually.",
        "Workday JDs starting with 'we're on a mission to disrupt' → often boilerplate; downstream Matcher unreliable. Flag for boilerplate filter.",
        "URL dedupe by hash is stable. Role-title dedupe is noisy (same role posted 3× with slightly different titles is common).",
    ],
    "sentinel": [
        "LinkedIn saved-list scraping is the primary VIP input source.",
        "Chrome profile is fragile — re-auth required after any LinkedIn activity-spike lockout.",
        "VIP roles with <2 public engineers listed tend to benefit from a 'stealth mode' resume variant (Tailor skill candidate).",
    ],
    "matcher": [
        "7 scoring dimensions with initial weights: role_match=0.25, seniority=0.15, domain=0.15, tech=0.20, salary=0.10, location=0.10, culture=0.05.",
        "Bands: ≥8.75 auto-approve → Tailor; ≥7.0 → Jaum review; <7.0 archive with reason.",
        "Critic will revisit weights weekly based on rejection reason clusters; reasoning_effort is auto-tunable.",
    ],
    "tailor": [
        "7-phase workflow: parse JD → extract keywords → select summary variant → reshuffle bullets → cover letter anchor → ATS format check → grounding audit.",
        "Skill library anchored to {industry}_{seniority} namespace (e.g. tailor_swe_startup, tailor_pm_scaleup).",
        "Grounding audit is the last gate — if any bullet would require fabrication to match the JD, reject with reason.",
    ],
    "applier": [
        "ATS platforms in scope: Workday, Lever, Greenhouse, iCIMS, Taleo. Each gets a {apply}_{platform} skill.",
        "IPRoyal residential proxies are the current anti-bot baseline for captcha-heavy portals.",
        "Dry-run → Jaum review → SUBMIT_CONFIRM → actual submit. Never skip the human gate.",
    ],
    "tracker": [
        "15-stage pipeline: discovered → scoring → scored → approved → tailoring → review → ready → applying → final_submission → applied → response_received → interviewing → offer → rejected/withdrawn/archived.",
        "14-day follow-up alerts fire daily 10am ET for applied entries with no response_received.",
        "Weekly analytics (Monday 9am ET) computes apps sent, response rate, interview rate, offer rate, score-band → approval rate.",
    ],
    "notifier": [
        "Quiet hours 23:00-07:00 ET. Breakthrough events (interview_signal, offer_signal) bypass.",
        "Daily 7am AM digest consumes Tracker's overnight state.",
        "Scribe will subsume narrative rendering (Weekend 1+); Notifier becomes a thin router after.",
    ],
    "cv-handler": [
        "Knowledge base: master resume, 11 summary variants (by role_type), accomplishments index, certifications index, ATS-safe formats.",
        "Read-only for other agents. Diego updates source files directly in workspace/.",
        "Consumed by Matcher (scoring grounding) and Tailor (bundle fetch). No cron work.",
    ],
    "devflow": [
        "12 specialist sub-roles: router, orchestrator, product-manager, architect, planner, implementer, reviewer, qa, security, release, docs, research.",
        "Policy gates: network/secret/deploy are deny-by-default.",
        "DevFlow dashboard at ~/.hermes/infra/devflow/ exists as docker-compose (Next.js + FastAPI + Postgres/Redis) — dormant; Weekend 3 resurrection + hermes_bridge.py.",
    ],
    "main": [
        "jaum-skill-evolution cron has NEVER fired — Critic (Weekend 2) delegates or replaces.",
        "Inbox sweeper runs every 10min; categorizes incoming mailbox messages into approval-needed / FYI / blocked.",
        "Constitutional: only main sends WhatsApp; 8 WhatsApp escalation categories per plan §5.2.",
    ],
}


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
    # Try the real producer first (production path with real EventBus).
    try:
        # Local import so test envs without events/ on PYTHONPATH can fall through.
        import importlib.util as _importlib_util
        import sys as _sys

        # Fallback only -- never shadow an active checkout (C26 casualty
        # class). Two things this must not do, both of which the previous form
        # did: name the hard-wired deployed ``~/.hermes/agent-src`` rather than
        # this file's own root, and run even when ``events`` already imports
        # fine. Together they put the DEPLOYED checkout at sys.path[0] of any
        # process that emits a curator event, so every later first-time import
        # resolved from deployed code instead of the tree actually running --
        # a fix present here could be invisible, a bug fixed here could still
        # appear. Same defect and same fix as devflow_delegation/adopt_history.py
        # (e422d55ec0). Appending means it can never outrank the live checkout.
        if _importlib_util.find_spec("events") is None:
            _repo_root = str(Path(__file__).resolve().parents[1])
            if _repo_root not in _sys.path:
                _sys.path.append(_repo_root)
        from events.producers.curator import emit_curator_daily  # type: ignore
        emit_curator_daily(bus, payload)
        return
    except Exception as exc:
        logger.warning("producer import/emit failed (%s); falling back to dict event", exc)
    # Fallback dict event so tests / degraded environments still record.
    # The fake bus in tests stores whatever is passed.
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
