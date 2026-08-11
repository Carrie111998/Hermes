"""Turning a scanned activation into a woken worker, in code.

The reconciler used to hand its scan output to an LLM and instruct it, in
prose, to resolve each activity to exactly one ENABLED cron job and trigger it
with ``hermes cron run``. That command re-enables whatever it triggers, so the
only thing standing between a mis-resolving agent and a revived worker was a
sentence in a prompt. This module is that sentence, as code.

``resolve_job_id_for_activity`` lives here rather than in the event subscriber
because it now has two consumers — the dispatcher and the reconciler — and the
dispatcher's own docstring already requires that routing, the claim ledger, and
the availability predicate each have exactly one implementation. Resolution
belongs in that set.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_job_id_for_activity(activity_id: str) -> Optional[str]:
    """Map a policy activity ID to exactly one enabled cron job ID.

    Fails closed on zero or multiple matches: activating the wrong worker is
    worse than not activating one, because the reconciler will catch the miss.
    """
    from activity_policy.registry import ActivityRegistry
    from cron.jobs import load_jobs

    registry = ActivityRegistry.load_default()
    policy = registry.policies.get(activity_id)
    if policy is None or not policy.aliases:
        logger.warning("dispatch: no policy/alias for activity %s", activity_id)
        return None

    names = {alias for alias in policy.aliases}
    matches = [
        job for job in load_jobs()
        if job.get("name") in names and job.get("enabled")
    ]
    if len(matches) != 1:
        logger.warning(
            "dispatch: activity %s resolved %d enabled jobs — refusing to guess",
            activity_id, len(matches),
        )
        return None
    return matches[0].get("id")
