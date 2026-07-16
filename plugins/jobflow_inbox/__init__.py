"""jobflow_inbox — /job <url> adds a forwarded job posting to the JobFlow pipeline.

Registers a single in-session slash command available on every gateway surface.
The handler is deterministic and never invokes the LLM: it queues a
USER_SUBMITTED_JOB message into the tracker's inbox, which the tracker's
inbox-sweep applies to the canonical pipeline.json.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_USAGE = "That doesn't look like a job URL. Usage: /job <url>"


async def _handle_job(raw_args: str) -> str:
    # Task 7 replaces this body with a call to ingest.ingest_job(...).
    text = (raw_args or "").strip()
    if not text:
        return _USAGE
    return _USAGE


def register(ctx) -> None:
    ctx.register_command(
        "job",
        handler=_handle_job,
        description="Add a forwarded job posting to your JobFlow pipeline.",
        args_hint="<url>",
    )
