"""Context-local provenance for autonomous learning proposals.

Background review binds one bounded evidence envelope before its tool loop.
Memory and skill staging consume it without changing model-visible tool schemas.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from agent.redact import redact_sensitive_text

_MAX_EXCERPT_CHARS = 500
_learning_metadata: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "learning_metadata", default={}
)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") in {None, "text"}
        )
    return ""


def _trigger_for(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("remember", "i prefer", "please always", "please never", "my preference")):
        return "preference"
    if any(marker in lowered for marker in ("that's wrong", "that is wrong", "instead", "correction", "don't do that")):
        return "correction"
    return "workflow_observation"


def build_learning_metadata(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str = "",
    platform: str = "",
) -> dict[str, Any]:
    excerpt = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            excerpt = _message_text(message).strip()
            if excerpt:
                break
    excerpt = redact_sensitive_text(excerpt, force=True)[:_MAX_EXCERPT_CHARS]
    return {
        "source": {
            "session_id": session_id,
            "platform": platform,
            "trust": "user_supplied_unverified",
        },
        "evidence": {
            "status": "captured" if excerpt else "missing",
            "trigger": _trigger_for(excerpt) if excerpt else "unknown",
            "source_trust": "user_supplied_unverified",
            "excerpt": excerpt,
            "hypothesis": "Applying this proposal should reduce repeated correction on similar future tasks.",
            "risk": "unknown",
            "confidence": "unknown",
        },
    }


def current_learning_metadata() -> dict[str, Any]:
    return dict(_learning_metadata.get())


@contextmanager
def learning_metadata_scope(metadata: Mapping[str, Any]) -> Iterator[None]:
    token = _learning_metadata.set(dict(metadata))
    try:
        yield
    finally:
        _learning_metadata.reset(token)
