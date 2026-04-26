"""Execute parsed reply commands by recording an intent in the JobOps API.

Migration note (2026-04-25, Phase 2 → tracker-intent-applier):
  Earlier this module called PipelineManager.update_stage directly. It now
  records intents through JobOps API; the tracker-intent-applier subscriber
  consumes the intent message and writes both pipeline.json and Postgres
  in canonical-first order. See spec at
  docs/superpowers/specs/2026-04-25-tracker-intent-applier-design.md.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from intent_applier import (
    JobOpsClient,
    JobOpsClientPermanentError,
    JobOpsClientTransientError,
)

from .parser import CommandIntent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    job_id: Optional[str] = None
    new_stage: Optional[str] = None


VERB_TO_STAGE = {
    "approve": "approved",
    # Use 'rejected' (in JobOps's LEGACY_PIPELINE_STAGES); 'rejected_by_user'
    # is in PipelineManager's VALID_STAGES but NOT in JobOps's contract, so
    # post_intent would get a 400. Aligns with dashboard's skipJob target.
    "reject": "rejected",
    "archive": "archived",
}

VERB_TO_APPROVAL = {
    "approve": "yes",
    "reject": "no",
    "archive": "no",
}


def execute(
    intent: CommandIntent,
    *,
    actor: str,
    source: str,
    jobops_client: Optional[JobOpsClient] = None,
    thread_id: Optional[str] = None,
) -> CommandResult:
    """Record an intent for the given verb. The tracker-intent-applier inside
    the gateway will eventually write pipeline.json + Postgres.
    """
    new_stage = VERB_TO_STAGE.get(intent.verb)
    if new_stage is None:
        return CommandResult(ok=False, message=f"Unknown command: /{intent.verb}")

    client = jobops_client or JobOpsClient(
        base_url=os.environ.get("HERMES_JOBOPS_URL", "http://127.0.0.1:4100"),
    )
    metadata = {"original_source": source}
    if thread_id:
        metadata["thread_id"] = thread_id

    if intent.reason:
        notes = f"{source}: {actor} {intent.verb} — {intent.reason}"
    else:
        notes = f"{source}: {actor} {intent.verb}"

    try:
        client.post_intent(
            job_id=intent.job_id,
            stage=new_stage,
            actor_id=actor,
            source=source,
            notes=notes,
            metadata=metadata,
        )
    except JobOpsClientPermanentError as exc:
        logger.warning("reply-handler intent rejected: %s", exc)
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower():
            return CommandResult(
                ok=False,
                message=f"Job {intent.job_id} not found in pipeline.",
                job_id=intent.job_id,
            )
        return CommandResult(ok=False, message=f"Intent rejected: {msg}")
    except JobOpsClientTransientError as exc:
        logger.warning("reply-handler intent transient failure: %s", exc)
        return CommandResult(
            ok=False,
            message="Pipeline service is unreachable; please try again in a moment.",
            job_id=intent.job_id,
        )

    logger.info(
        "reply-handler: queued intent verb=%s job=%s stage=%s actor=%s source=%s",
        intent.verb, intent.job_id, new_stage, actor, source,
    )
    return CommandResult(
        ok=True,
        message=f"Job {intent.job_id} → {new_stage} (queued).",
        job_id=intent.job_id,
        new_stage=new_stage,
    )
