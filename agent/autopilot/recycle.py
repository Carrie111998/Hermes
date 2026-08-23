"""Autopilot session-recycle — spin a fresh, trimmed conversation instead of
growing one forever.

A long unattended autopilot run keeps appending to the SAME conversation
(conversation_loop Seam B: ``messages.append(directive); continue``). On a
constrained-context harness that conversation eventually rides the compression
threshold permanently, and every turn pays to summarize-then-resend a huge
history. Session-recycle is the opt-in alternative: once context utilization
crosses a threshold, replace the conversational history with a compact FRESH
list seeded from the durable goal/contract + the run ledger + the ADR decision
log + a verbatim transcript tail (or a CMX briefing when the active context
engine is CMX). The durable goal/ledger/ADR side-stores are never touched — only
the in-memory message history is trimmed and reseeded.

Two hard invariants, both load-bearing:

  * PROMPT-CACHE (same rule as #51312): the seed goes on the MESSAGE LIST as a
    ``role:user`` "resume" turn. It is NEVER folded into ``effective_system`` /
    the cached system prefix. Seam B operates on ``messages`` (the conversation
    only); the system prompt is prepended separately downstream, so a recycled
    ``messages`` list inherently cannot mutate the cached system prefix. We also
    assert this in tests.

  * FAIL-OPEN: any error, any missing dependency, or utilization below the
    threshold returns ``None`` and the caller behaves EXACTLY as today (grow the
    one conversation). Recycle can never wedge or crash a run.

CMX-optional: when ``context.engine != cmx`` (the upstream default built-in
``compressor``), the seed degrades to the cheap ledger+adr+tail composition with
no CMX dependency at all.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agent.autopilot.driver import is_autopilot_active, resolve_goal
from agent.autopilot import adr as _adr
from agent.autopilot import ledger as _ledger
from agent.autopilot.resume import summarize_session_tail

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# Defaults mirror the config schema (hermes_cli/config_defaults.py autopilot
# block). Config bridges to these via the ``_autopilot_recycle_*`` agent attrs /
# env vars so the driver stays import-light.
_DEFAULT_THRESHOLD_PCT = 75
_DEFAULT_TAIL_TURNS = 6
_DEFAULT_SEED = "auto"


def _cfg(agent: Any, attr: str, env: str, default: Any) -> Any:
    """Resolve a recycle knob: agent attr first, then env, then default."""
    val = getattr(agent, attr, None)
    if val is None or val == "":
        val = os.environ.get(env, "")
    if val is None or val == "":
        return default
    return val


def _cfg_int(agent: Any, attr: str, env: str, default: int) -> int:
    try:
        return int(_cfg(agent, attr, env, default))
    except (TypeError, ValueError):
        return default


def recycle_enabled(agent: Any) -> bool:
    """Whether session-recycle is opted in for this agent (default OFF)."""
    val = getattr(agent, "_autopilot_recycle_enabled", None)
    if val is not None:
        return bool(val)
    return os.environ.get("AUTOPILOT_SESSION_RECYCLE", "").strip().lower() in _TRUTHY


def _context_engine_name(agent: Any) -> str:
    """Best-effort name of the active context engine ('compressor'|'cmx'|...)."""
    engine = getattr(agent, "context_compressor", None)
    if engine is None:
        return "compressor"
    try:
        name = getattr(engine, "name", "") or ""
        return str(name).strip().lower() or "compressor"
    except Exception:  # noqa: BLE001
        return "compressor"


def _utilization_pct(agent: Any) -> Optional[float]:
    """Best-effort context utilization as a 0-100 percentage.

    Reuses whatever the loop already computes for compression: the context
    engine's ``last_prompt_tokens`` (updated from each response's usage) against
    its resolved ``context_length``. Returns None when it can't be computed, so
    the caller treats "unknown utilization" as "don't recycle" (fail-open).
    """
    engine = getattr(agent, "context_compressor", None)
    if engine is None:
        return None
    try:
        ctx_len = int(getattr(engine, "context_length", 0) or 0)
        used = int(getattr(engine, "last_prompt_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    if ctx_len <= 0 or used <= 0:
        return None
    return 100.0 * used / ctx_len


def _read_tail_of_file(path, max_chars: int) -> str:
    """Read the last ``max_chars`` of a durable side-store file, or ''."""
    try:
        if path is None or not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = "…(earlier entries trimmed)…\n" + text[-max_chars:]
    return text


def _ledger_digest(agent: Any, goal: str, max_chars: int = 2400) -> str:
    """Compact digest of the durable GOAL-LEDGER (milestones/progress)."""
    try:
        path = _ledger.ledger_path(agent)
    except Exception:  # noqa: BLE001
        return ""
    return _read_tail_of_file(path, max_chars)


def _adr_digest(agent: Any, goal: str, max_chars: int = 2400) -> str:
    """Compact digest of the durable ADR decision log (recent decisions)."""
    try:
        path = _adr.adr_path(agent)
    except Exception:  # noqa: BLE001
        return ""
    return _read_tail_of_file(path, max_chars)


def _cmx_briefing(agent: Any, goal: str) -> str:
    """Full CMX briefing when the active engine is CMX and exposes one.

    The context-engine plugin surface is optional; probe a few conventional
    method names and fall back to '' (which drives the cheap-seed degradation).
    Never raises.
    """
    engine = getattr(agent, "context_compressor", None)
    if engine is None:
        return ""
    for meth in ("resume_briefing", "get_briefing", "briefing", "build_briefing"):
        fn = getattr(engine, meth, None)
        if callable(fn):
            try:
                out = fn(goal=goal) if _accepts_goal(fn) else fn()
                if out:
                    return str(out).strip()
            except Exception:  # noqa: BLE001
                continue
    return ""


def _accepts_goal(fn: Any) -> bool:
    try:
        import inspect

        return "goal" in inspect.signature(fn).parameters
    except Exception:  # noqa: BLE001
        return False


def _resolve_seed_mode(agent: Any) -> str:
    """Which seed to build: 'auto' | 'cheap' | 'cmx'."""
    mode = str(_cfg(agent, "_autopilot_recycle_seed", "AUTOPILOT_SESSION_RECYCLE_SEED",
                    _DEFAULT_SEED)).strip().lower()
    return mode if mode in {"auto", "cheap", "cmx"} else "auto"


def _split_leading_system(messages: list) -> tuple[list, list]:
    """Return (system_prefix_msgs, conversation_msgs).

    Seam B's ``messages`` is normally the conversation only (the system prompt
    is prepended separately downstream). But if a leading system message is ever
    present we preserve it verbatim so recycle never disturbs the cached prefix.
    """
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        return [messages[0]], messages[1:]
    return [], list(messages)


def _verbatim_tail(messages: list, tail_turns: int) -> list:
    """The last ``tail_turns`` user/assistant turns, verbatim, role-alternation
    safe. Tool-role messages orphaned from their assistant tool_calls are
    dropped to keep a clean role sequence in the fresh list."""
    if tail_turns <= 0:
        return []
    _sys, conv = _split_leading_system(messages)
    picked: list = []
    turns = 0
    for msg in reversed(conv):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "tool":
            # A tool result is only valid right after its assistant tool_calls
            # turn; carrying it into a trimmed list would orphan it. Skip.
            continue
        if role not in ("user", "assistant"):
            continue
        # Drop assistant turns that carry tool_calls: their matching tool
        # results won't be in the trimmed list, which breaks the pairing.
        if role == "assistant" and msg.get("tool_calls"):
            picked.append({"role": "assistant", "content": msg.get("content") or "(tool calls omitted)"})
        else:
            picked.append(msg)
        turns += 1
        if turns >= tail_turns:
            break
    picked.reverse()
    return picked


def _compose_seed_text(
    agent: Any,
    goal: str,
    *,
    seed_mode: str,
    engine_name: str,
    tail_turns: int,
    messages: list,
) -> tuple[str, str]:
    """Build the resume-seed text. Returns (seed_text, source_label)."""
    parts: list[str] = []
    parts.append(
        "[Autopilot · session recycle] This is a FRESH working context for an "
        "IN-FLIGHT autonomous run. The full conversation history was trimmed to "
        "keep the context small; nothing about the goal was dropped. Continue "
        "exactly this work from where it left off — do not restart, do not "
        "re-plan from scratch, and do not treat this as a new task."
    )
    if goal:
        parts.append(f"--- GOAL / CONTRACT ---\n{goal.strip()}")

    used_cmx = False
    if seed_mode in ("auto", "cmx") and engine_name == "cmx":
        briefing = _cmx_briefing(agent, goal)
        if briefing:
            parts.append(f"--- CMX BRIEFING ---\n{briefing}")
            used_cmx = True

    # Cheap seed (always included as the durable spine; also the full seed when
    # CMX is unavailable / not selected).
    ledger = _ledger_digest(agent, goal)
    if ledger:
        parts.append(f"--- RUN LEDGER (durable milestones/progress) ---\n{ledger}")
    adr = _adr_digest(agent, goal)
    if adr:
        parts.append(f"--- RECENT DECISIONS (ADR) ---\n{adr}")

    tail = summarize_session_tail(messages, turns=tail_turns)
    if tail:
        parts.append(
            "--- CURRENT SESSION, LAST TURNS (verbatim tail) ---\n"
            f"{tail}\n--- end tail ---"
        )

    parts.append(
        "Ground yourself in the ledger, decisions, and transcript tail above — "
        "NOT in a memory/keyword search — then take the next concrete action to "
        "advance this goal. Do not stop, summarize-and-wait, or ask the user."
    )
    source = "cmx" if used_cmx else "cheap"
    return "\n\n".join(p for p in parts if p.strip()), source


def maybe_recycle(agent: Any, messages: list) -> Optional[list]:
    """Return a fresh trimmed message list when a recycle should fire, else None.

    Fires only when ALL hold:
      * autopilot is active for this agent,
      * ``autopilot.session_recycle.enabled`` is opted in,
      * context utilization is computable AND >= the configured threshold_pct.

    The returned list is: [ <leading system msg if any, preserved verbatim>,
    <role:user resume-seed turn>, <last tail_turns verbatim turns> ]. The caller
    REPLACES messages with it (fresh session) and then appends its continuation
    directive as usual. The durable goal/ledger/ADR/budget state is untouched.

    Fail-open: returns None on any error or when the threshold isn't crossed, so
    the caller behaves exactly as today.
    """
    try:
        if not is_autopilot_active(agent):
            return None
        if not recycle_enabled(agent):
            return None

        threshold = _cfg_int(agent, "_autopilot_recycle_threshold_pct",
                             "AUTOPILOT_SESSION_RECYCLE_THRESHOLD_PCT", _DEFAULT_THRESHOLD_PCT)
        util = _utilization_pct(agent)
        if util is None or util < threshold:
            return None

        tail_turns = _cfg_int(agent, "_autopilot_recycle_tail_turns",
                             "AUTOPILOT_SESSION_RECYCLE_TAIL_TURNS", _DEFAULT_TAIL_TURNS)
        tail_turns = max(0, min(tail_turns, 20))
        seed_mode = _resolve_seed_mode(agent)
        engine_name = _context_engine_name(agent)

        goal = ""
        try:
            goal = resolve_goal(agent, None) or ""
        except Exception:  # noqa: BLE001
            goal = ""

        seed_text, source = _compose_seed_text(
            agent, goal, seed_mode=seed_mode, engine_name=engine_name,
            tail_turns=tail_turns, messages=messages,
        )
        if not seed_text.strip():
            return None

        system_prefix, _conv = _split_leading_system(messages)
        tail_msgs = _verbatim_tail(messages, tail_turns)

        fresh: list = list(system_prefix)
        fresh.append({
            "role": "user",
            "content": seed_text,
            "_autopilot_synthetic": True,
            "_autopilot_recycle_seed": True,
        })
        # The verbatim tail must not start with a bare tool result and must
        # alternate cleanly after the seed user turn. _verbatim_tail already
        # dropped tool-role and unpaired tool_calls turns. If the first carried
        # turn is a user turn (adjacent to our seed user turn), collapse it into
        # a preceding assistant sentinel so the sequence stays valid.
        if tail_msgs and isinstance(tail_msgs[0], dict) and tail_msgs[0].get("role") == "user":
            fresh.append({"role": "assistant", "content": "(continuing)"})
        fresh.extend(tail_msgs)

        try:
            _ledger.record_progress(
                agent, goal=goal, summary=(
                    f"session recycled ({source} seed) at ~{util:.0f}% context "
                    f"utilization: {len(messages)} → {len(fresh)} messages "
                    "(goal/ledger/ADR preserved)"
                ),
            ) if hasattr(_ledger, "record_progress") else None
        except Exception:  # noqa: BLE001
            pass

        logger.info(
            "autopilot: session recycled (seed=%s engine=%s util=%.0f%% thr=%d%%) "
            "%d → %d messages",
            source, engine_name, util, threshold, len(messages), len(fresh),
        )
        _emit_recycle(agent, source, util, len(messages), len(fresh))
        return fresh
    except Exception as exc:  # noqa: BLE001 — recycle must NEVER break a run
        logger.debug("autopilot: maybe_recycle failed, falling open (%s)", exc)
        return None


def _emit_recycle(agent: Any, source: str, util: float, before: int, after: int) -> None:
    """Best-effort status line; never raises."""
    text = (f"♻️  Autopilot: recycled session ({source} seed) at ~{util:.0f}% "
            f"context — {before} → {after} messages; goal/ledger/ADR preserved.")
    try:
        from agent.autopilot.driver import _emit as _driver_emit

        _driver_emit(agent, text)
    except Exception:  # noqa: BLE001
        pass
