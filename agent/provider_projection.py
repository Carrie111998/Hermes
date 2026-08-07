"""Fold an agent-as-provider's own activity back into Hermes' turn state.

Most providers are models: they ask Hermes to run a tool and Hermes runs it, so
the transcript and the loop's counters see every tool iteration. Some providers
are *agents* (the ACP integrations — junie-acp today; the codex app-server takes
an analogous path in ``agent/codex_runtime.py``): they execute their own
read/edit/execute tools inside their own session, and by the time Hermes sees the
response that work is already done.

Those calls must never come back as pending ``tool_calls`` — Hermes would re-run
finished work. But two subsystems go blind if they're merely summarised into the
``reasoning`` field:

* the **self-improvement loop**, which distils memories and skills by replaying
  ``messages`` — a one-line activity feed teaches it nothing;
* the **skill-review nudge**, whose counter (``_iters_since_skill``) only moves
  on Hermes tool iterations, of which there are none.

So the provider client hands both back on the completion object and this helper
applies them: ``hermes_projected_messages`` (already-completed
``assistant(tool_calls=[…])`` + ``tool(result)`` history rows) and
``hermes_provider_tool_iterations`` (how many tool iterations happened inside the
provider). Clients that set neither are unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["splice_provider_projection"]


def splice_provider_projection(
    agent: Any, response: Any, messages: list[dict[str, Any]]
) -> int:
    """Append the provider's projected history rows and tick the nudge counter.

    Returns the number of rows spliced. Tolerates absent/garbage attributes so a
    third-party OpenAI-compatible client can't break the turn.
    """
    projected = getattr(response, "hermes_projected_messages", None)
    rows = [m for m in projected if isinstance(m, dict)] if isinstance(projected, list) else []
    if rows:
        messages.extend(rows)
        logger.debug(
            "spliced %d provider-projected transcript row(s) from %s",
            len(rows),
            getattr(agent, "provider", "?"),
        )

    raw_iters = getattr(response, "hermes_provider_tool_iterations", 0)
    try:
        iterations = int(raw_iters or 0)
    except (TypeError, ValueError):
        iterations = 0
    if iterations > 0:
        agent._iters_since_skill = getattr(agent, "_iters_since_skill", 0) + iterations

    return len(rows)
