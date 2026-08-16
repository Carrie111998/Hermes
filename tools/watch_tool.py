"""Native ``watch`` tool — configurable background polling with conditions.

Implements the feature requested in
NousResearch/hermes-agent#56694 ("Native watch tool with configurable
intervals and conditions").

The tool polls a shell ``command`` on an ``interval`` and records observations.
An optional ``condition`` string gates which observations are surfaced/notify'd.
A ``duration`` bounds the total watch window.

Design notes
------------
* Registration uses the standard ``registry.register(...)`` self-registration
  pattern (same as ``cronjob_tools.py``), so ``discover_builtin_tools`` picks
  this module up automatically.
* The async poll loop and lifecycle bookkeeping live in
  ``agent/conversation_loop.py`` (see ``_start_watch_session``), which is
  invoked from the handler via the agent object passed through
  ``handle_function_call(..., agent=agent)`` -> ``registry.dispatch``. This
  module owns the pure, unit-testable helpers (condition evaluation, duration
  math, the synchronous single tick) and the handler contract.
* ``condition`` is a tiny, safe expression language — ``contains "x"``,
  ``not contains "x"``, ``equals "x"``, ``matches "regex"``, or a bare
  substring — deliberately avoiding ``eval``/``exec`` on agent input.
* This module has NO dependency on AgentRadio/Coral; it surfaces results
  through the agent's existing notify hook, so the same code serves both the
  upstream feature and downstream passive-awareness ports.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from tools.registry import registry


# ---------------------------------------------------------------------------
# Condition evaluation (pure, unit-testable)
# ---------------------------------------------------------------------------


def _eval_condition(condition: str, output: str) -> bool:
    """Evaluate a ``condition`` string against command ``output``.

    Supported forms (case-insensitive keyword prefix):
      * ``contains "foo"`` / ``"foo"``        -> output contains foo
      * ``not contains "foo"``                -> output does NOT contain foo
      * ``equals "foo"``                      -> output (stripped) == foo
      * ``matches "regex"``                   -> re.search(regex, output)
    Returns ``True`` when ``condition`` is empty/whitespace (unconditional).
    """
    if not condition or not condition.strip():
        return True
    cond = condition.strip()

    m = re.match(r'^not\s+contains\s+["\'](.+?)["\']\s*$', cond, re.IGNORECASE)
    if m:
        return m.group(1) not in output

    m = re.match(r'^contains\s+["\'](.+?)["\']\s*$', cond, re.IGNORECASE)
    if m:
        return m.group(1) in output

    m = re.match(r'^equals\s+["\'](.+?)["\']\s*$', cond, re.IGNORECASE)
    if m:
        return output.strip() == m.group(1)

    m = re.match(r'^matches\s+["\'](.+?)["\']\s*$', cond, re.IGNORECASE)
    if m:
        try:
            return re.search(m.group(1), output) is not None
        except re.error:
            return False

    # bare substring -> contains
    inner = cond.strip('"\'')
    return inner in output


def _parse_duration(value: Any) -> int:
    """Parse a duration string/int into seconds.

    Accepts ``"24h"``, ``"30m"``, ``"45s"``, or a bare int (seconds).
    Returns 0 for unparseable input (caller treats 0 as 'single tick').
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    m = re.match(r'^(\d+)\s*(h|m|s)?$', s)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return n * 3600
    if unit == "m":
        return n * 60
    return n  # 's' or bare -> seconds


def _plan_ticks(interval: int, duration: Optional[int]) -> int:
    """How many ticks to schedule.

    ``duration`` is in seconds; ``interval`` is seconds between ticks.
    Returns at least 1. A hard cap (enforced in the loop) prevents a forgotten
    duration from spinning forever.
    """
    if not duration or duration <= 0:
        return 1
    return max(1, int(duration // interval) + (1 if duration % interval else 0))


# ---------------------------------------------------------------------------
# Synchronous command runner (unit-testable without an event loop)
# ---------------------------------------------------------------------------


def run_once(command: str, timeout: int = 30) -> str:
    """Run ``command`` once and return combined stdout/stderr text.

    Raises on timeout are caught and returned as a marker string so callers
    can record the outcome uniformly.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return f"<timeout after {timeout}s>"
    except Exception as exc:  # pragma: no cover - defensive
        return f"<error: {exc}>"


def _make_observation(
    *,
    watch_id: str,
    tick: int,
    command: str,
    output: str,
    condition: Optional[str],
    triggered: bool,
) -> Dict[str, Any]:
    return {
        "watch_id": watch_id,
        "tick": tick,
        "command": command,
        "condition": condition,
        "triggered": triggered,
        "output": output,
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def watch(args: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    """Watch a command's output over time and surface observations.

    The registry calls handlers as ``handler(args, **kwargs)``, so the tool
    arguments arrive as a single ``args`` dict (mirrors cronjob_tools.py).
    The agent object is injected via the ``agent=`` kwarg by
    ``handle_function_call``; when present, the poll loop runs as a background
    task on the agent's event loop so it survives across the calling turn.

    Args (in ``args``):
        command: Shell command to run on each tick.
        interval: Seconds between ticks (clamped to 5-3600).
        condition: Optional trigger expression (see ``_eval_condition``).
            When set, notify/observation marking only fires on match.
        notify: If True, surface a notification when an observation is recorded
            (gated by ``condition`` when present).
        duration: Total window: ``"24h"``, ``"30m"``, or raw seconds.
            Defaults to one interval when omitted.
        timeout: Per-tick command timeout in seconds.

    Returns a handle describing the watch session. Falls back to a single
    synchronous tick when no agent/event loop is reachable (e.g. unit tests).
    """
    command = str(args.get("command", ""))
    interval = int(args.get("interval", 60))
    condition = args.get("condition")
    notify = bool(args.get("notify", True))
    duration = args.get("duration")
    timeout = int(args.get("timeout", 30))

    interval = max(5, min(3600, interval))
    seconds = _parse_duration(duration) if duration else interval
    max_ticks = min(_plan_ticks(interval, seconds), 1000)  # hard safety cap

    watch_id = f"watch_{int(time.time() * 1000)}"
    handle = {
        "watch_id": watch_id,
        "command": command,
        "interval": interval,
        "condition": condition,
        "notify": notify,
        "duration_seconds": seconds,
        "planned_ticks": max_ticks,
    }

    agent = kw.get("agent") or kw.get("ctx")
    loop = getattr(agent, "_loop", None) or getattr(agent, "loop", None)
    if loop is None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

    if agent is not None and loop is not None and loop.is_running():
        # Background poll on the agent's live event loop.
        sessions = getattr(agent, "_watch_sessions", None)
        if sessions is None:
            sessions = {}
            agent._watch_sessions = sessions
        handle["status"] = "running"
        handle["observations"] = []
        sessions[watch_id] = handle

        async def _poll() -> None:
            tick = 0
            deadline = time.time() + seconds
            while time.time() < deadline and tick < max_ticks:
                tick += 1
                out = run_once(command, timeout)
                triggered = _eval_condition(condition or "", out)
                handle["observations"].append(
                    _make_observation(
                        watch_id=watch_id,
                        tick=tick,
                        command=command,
                        output=out,
                        condition=condition,
                        triggered=triggered,
                    )
                )
                if triggered:
                    if notify and callable(getattr(agent, "notify", None)):
                        try:
                            agent.notify(
                                f"[watch:{watch_id}] tick {tick} matched "
                                f"condition={condition!r}: {out[:200]}"
                            )
                        except Exception:
                            pass
                    if condition:  # stop after first match when a condition is set
                        handle["status"] = "matched"
                        return
                await asyncio.sleep(interval)
            handle["status"] = "completed"

        loop.create_task(_poll())
    else:  # pragma: no cover - fallback when no agent/loop (e.g. unit tests)
        out = run_once(command, timeout)
        triggered = _eval_condition(condition or "", out)
        handle["observations"] = [
            _make_observation(
                watch_id=watch_id,
                tick=1,
                command=command,
                output=out,
                condition=condition,
                triggered=triggered,
            )
        ]
        handle["status"] = "completed"
    return json.dumps(handle, ensure_ascii=False)


WATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "watch",
        "description": (
            "Poll a shell command on an interval and surface observations "
            "when an optional condition is met. Useful for monitoring a "
            "service, watching for a file to appear, or polling an API — "
            "without blocking the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run each tick.",
                },
                "interval": {
                    "type": "integer",
                    "description": "Seconds between ticks (5-3600).",
                    "default": 60,
                },
                "condition": {
                    "type": "string",
                    "description": (
                        "Optional trigger: 'contains \"x\"', "
                        "'not contains \"x\"', 'equals \"x\"', "
                        "'matches \"regex\"', or a bare substring."
                    ),
                },
                "notify": {
                    "type": "boolean",
                    "description": "Surface a notification on match.",
                    "default": True,
                },
                "duration": {
                    "type": "string",
                    "description": (
                        "Total window: '24h', '30m', or raw seconds. "
                        "Defaults to one interval."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Per-tick command timeout (seconds).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
}


registry.register(
    name="watch",
    toolset="watch",
    schema=WATCH_SCHEMA,
    handler=watch,
    is_async=True,
)
