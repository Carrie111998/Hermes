"""Shared command entrypoint for checkpointed ``/learn`` requests."""

from __future__ import annotations

import re

from agent.learn_checkpoint import prepare_learn_checkpoint
from agent.learn_prompt import build_learn_prompt


_LEARN_COMMAND_RE = re.compile(r"^/learn(?:\s+|$)", re.IGNORECASE)


def build_learn_request(user_request: str) -> str:
    """Prepare a ``/learn`` prompt after creating any local-source checkpoint."""
    checkpoint = prepare_learn_checkpoint(user_request)
    note = checkpoint.message if checkpoint.status != "skipped" else ""
    return build_learn_prompt(user_request, preflight_note=note)


def normalize_learn_query(query: str) -> str:
    """Expand a raw ``/learn`` query for single-query CLI execution."""
    if not isinstance(query, str) or not _LEARN_COMMAND_RE.match(query.strip()):
        return query
    request = _LEARN_COMMAND_RE.sub("", query.strip(), count=1).strip()
    return build_learn_request(request)
