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

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from jobflow_dispatch.contracts import Activation
from jobflow_dispatch.quarantine_control import default_control_store

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
    # Intentional asymmetry: a legacy record with no "enabled" key is treated
    # as not-enabled HERE (fails closed), even though the due scan defaults
    # missing "enabled" to True (`job.get("enabled", True)`). A record like
    # that is runnable by the scheduler but permanently refused by this path.
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


#: Attribution for every activation this module performs. Reaches the
#: cron_triggered event and therefore ~/.hermes/events/audit.jsonl, which is
#: the ONLY durable record of a reconcile activation: a wakeAgent:false run has
#: its stdout replaced by the scheduler's silent_doc, and _run_job_script
#: discards stderr entirely on exit 0.
CALLER = "cron:jobflow-reconcile"
REASON = "reconcile"


@dataclass(frozen=True)
class ActivationReport:
    """What one reconcile pass did, in counts the wake gate can read.

    The three failure buckets are kept apart because they mean different
    things to whoever reads the report: ``unresolved`` is a broken
    activity-to-job mapping, ``refused`` is a job that was disabled between the
    scan and the activation, and ``errors`` is a fault in this code or the cron
    store. Collapsing them into one number would hide which.
    """

    activations: int
    activities: int
    activated: tuple[str, ...]
    unresolved: tuple[str, ...]
    refused: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def needs_agent(self) -> bool:
        """True when something needs a human-legible diagnosis.

        A pass that activated ten workers cleanly needs no agent; a pass that
        could not resolve one activity does.
        """
        return bool(self.unresolved or self.refused or self.errors)


def activate_pending(
    activations: Sequence[Activation],
    *,
    resolve: Callable[[str], Optional[str]] = resolve_job_id_for_activity,
    request_run: Optional[Callable[..., Optional[dict]]] = None,
    caller: str = CALLER,
    reason: str = REASON,
) -> ActivationReport:
    """Resolve each pending activity to an enabled job and schedule it.

    Fail-closed twice over: ``resolve`` refuses anything that does not map to
    exactly one enabled job, and ``request_run`` independently refuses a job
    that is not enabled at the moment of the write. Neither can revive a
    disabled worker.

    Every activity is isolated — a resolver that raises, or a cron store that
    fails one write, costs that one activity and not the rest.
    """
    if request_run is None:
        from cron.jobs import request_run as _default_request_run

        request_run = _default_request_run

    # Dedupe activities while preserving scan order. There are only four routed
    # activities in ROUTES, so this list is bounded by construction and needs no
    # display cap.
    activity_ids: list[str] = []
    seen: set[str] = set()
    for activation in activations:
        activity_id = activation.activity_id
        if activity_id not in seen:
            seen.add(activity_id)
            activity_ids.append(activity_id)

    activated: list[str] = []
    unresolved: list[str] = []
    refused: list[str] = []
    errors: list[str] = []
    woken: set[str] = set()

    # One retained section closes the scan result's resolve-through-request race.
    # Per-activity failures remain isolated inside it.
    with default_control_store().dispatch_section(boundary="jobflow-reconciler"):
        for activity_id in activity_ids:
            try:
                job_id = resolve(activity_id)
            except Exception:
                logger.exception("activate: resolving %s failed", activity_id)
                errors.append(activity_id)
                continue

            if not job_id:
                unresolved.append(activity_id)
                continue

            if job_id in woken:
                continue  # one wake per job per run, however many activities map to it

            try:
                result = request_run(job_id, caller=caller, reason=reason)
            except Exception:
                logger.exception(
                    "activate: request_run failed for %s (%s)", job_id, activity_id
                )
                errors.append(activity_id)
                continue

            if result is None:
                # Enabled at scan time, not enabled now — or gone. Fail closed and
                # let the next reconcile catch it.
                logger.warning(
                    "activate: %s (%s) refused — not enabled at activation time",
                    job_id, activity_id,
                )
                refused.append(activity_id)
                continue

            woken.add(job_id)
            activated.append(job_id)

    return ActivationReport(
        activations=len(activations),
        activities=len(activity_ids),
        activated=tuple(activated),
        unresolved=tuple(unresolved),
        refused=tuple(refused),
        errors=tuple(errors),
    )


def render_report(report: ActivationReport) -> str:
    """Format one pass for the cron script slot's stdout.

    The LAST non-empty line MUST be the wake gate — the scheduler parses only
    that line and treats anything unparseable as "wake the agent". Keeping the
    gate here, rather than in the wrapper script, is what lets a unit test hold
    that invariant.

    Only activity IDs, job IDs and counts appear. Message bodies and mailbox
    paths never reach stdout: for an agent-path job this text is injected into
    the prompt verbatim.
    """
    lines = [
        "jobflow-reconcile: "
        f"activations={report.activations} "
        f"activities={report.activities} "
        f"activated={len(report.activated)} "
        f"unresolved={len(report.unresolved)} "
        f"refused={len(report.refused)} "
        f"errors={len(report.errors)}"
    ]
    for job_id in report.activated:
        lines.append(f"- activated: {job_id}")
    for activity_id in report.unresolved:
        lines.append(
            f"- UNRESOLVED: {activity_id} did not map to exactly one enabled job"
        )
    for activity_id in report.refused:
        lines.append(
            f"- REFUSED: {activity_id} resolved to a job that was not enabled "
            "at activation time"
        )
    for activity_id in report.errors:
        lines.append(f"- ERROR: {activity_id} raised during activation")

    # MUST be last.
    lines.append(json.dumps({"wakeAgent": report.needs_agent}))
    return "\n".join(lines)
