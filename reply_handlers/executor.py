"""Execute parsed reply commands: write to PipelineManager, optionally resume HITL graph."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from pipeline_state import PipelineManager

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
    "reject": "rejected_by_user",
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
    manager: Optional[PipelineManager] = None,
    resume_full: Optional[Callable[[str, dict], object]] = None,
) -> CommandResult:
    """Apply a CommandIntent: update pipeline.json, optionally resume LangGraph thread.

    Args:
        intent: parsed command (verb + job_id + reason)
        actor: human/agent identifier ("diego", or sender JID/user_id)
        source: 'telegram' or 'whatsapp' (must be in pipeline_state.KNOWN_SOURCES)
        manager: optional injected PipelineManager (default: new instance reading
                 the canonical workspace path)
        resume_full: optional callable; if given, called as
                     resume_full(thread_id, {"approval": ...}) after a successful
                     stage write. Failures are logged but do not affect the
                     CommandResult — the pipeline write is the source of truth.
    """
    new_stage = VERB_TO_STAGE.get(intent.verb)
    if new_stage is None:
        return CommandResult(ok=False, message=f"Unknown command: /{intent.verb}")

    mgr = manager or PipelineManager()

    job = mgr.get_job(intent.job_id)
    if job is None:
        return CommandResult(
            ok=False,
            message=f"Job {intent.job_id} not found in pipeline.",
            job_id=intent.job_id,
        )

    notes = f"{source}: {actor} {intent.verb}"
    if intent.reason:
        notes += f" — {intent.reason}"

    mgr.update_stage(
        job_id=intent.job_id,
        new_stage=new_stage,
        actor=actor,
        source=source,
        notes=notes,
    )
    logger.info(
        "reply_handlers: %s %s -> %s (actor=%s source=%s)",
        intent.verb, intent.job_id, new_stage, actor, source,
    )

    if resume_full is not None:
        thread_id = f"job-{intent.job_id}"
        approval = VERB_TO_APPROVAL[intent.verb]
        try:
            resume_full(thread_id, {"approval": approval})
            logger.info(
                "reply_handlers: resumed thread %s with approval=%s",
                thread_id, approval,
            )
        except Exception as exc:
            logger.info(
                "reply_handlers: resume_full(%s) skipped (%s: %s)",
                thread_id, exc.__class__.__name__, exc,
            )

    return CommandResult(
        ok=True,
        message=f"Job {intent.job_id} → {new_stage}.",
        job_id=intent.job_id,
        new_stage=new_stage,
    )
