"""Parse Telegram/WhatsApp reply commands like '/approve job-XXX reason=...'."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


VALID_VERBS = ("approve", "reject", "archive")


@dataclass(frozen=True)
class CommandIntent:
    verb: str
    job_id: str
    reason: Optional[str] = None


class ParseError(Exception):
    """Raised when a recognised verb is malformed (missing job_id, etc.)."""


_PATTERN = re.compile(
    r"^/(?P<verb>[A-Za-z]+)(?:\s+(?P<job_id>\S+)(?:\s+reason=(?P<reason>.+))?)?$"
)


def parse(text: str) -> Optional[CommandIntent]:
    """Return a CommandIntent if `text` is one of our recognised slash commands.

    Returns None for non-command text, unknown slash commands (let other handlers
    take them), or empty input. Raises ParseError if a recognised verb appears
    without a job_id.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    m = _PATTERN.match(stripped)
    if not m:
        return None
    verb = m.group("verb").lower()
    if verb not in VALID_VERBS:
        return None
    job_id = m.group("job_id")
    if not job_id:
        raise ParseError(
            f"/{verb} requires a job_id (usage: /{verb} <job_id> [reason=...])"
        )
    reason = (m.group("reason") or "").strip() or None
    return CommandIntent(verb=verb, job_id=job_id, reason=reason)
