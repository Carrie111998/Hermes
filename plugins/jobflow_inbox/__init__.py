"""jobflow_inbox — /job <url> adds a forwarded job posting to the JobFlow pipeline.

Registers a single in-session slash command available on every gateway surface.
The handler is deterministic and never invokes the LLM: it queues a
USER_SUBMITTED_JOB message into the tracker's inbox, which the tracker's
inbox-sweep applies to the canonical pipeline.json.
"""

from __future__ import annotations

import asyncio
import logging

from . import ingest

logger = logging.getLogger(__name__)

_USAGE = "That doesn't look like a job URL. Usage: /job <url>"


async def _handle_job(raw_args: str) -> str:
    text = (raw_args or "").strip()
    if not text:
        return _USAGE
    try:
        result = await asyncio.to_thread(ingest.ingest_job, text)
        return result.reply
    except Exception:  # noqa: BLE001 — never raise to the gateway
        logger.warning("jobflow_inbox: handler failed", exc_info=True)
        return "Couldn't queue that job — please retry."


def register(ctx) -> None:
    ctx.register_command(
        "job",
        handler=_handle_job,
        description="Add a forwarded job posting to your JobFlow pipeline.",
        args_hint="<url>",
    )
