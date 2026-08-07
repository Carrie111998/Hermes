"""Quality and safety gates for data written to Honcho.

Honcho is a durable, semantic memory system, not a transcript archive.  This
module deliberately keeps the default policy conservative: a turn must carry
an explicit durable signal or clearly reusable project/technical context before
it is sent to Honcho.  Hard exclusions cannot be bypassed by a positive signal
because they represent data that should never become long-term memory for the
URecruit profiles (for example football chatter and credentials).

The functions are pure and do not make network calls.  Keeping the decision
logic here makes it testable and gives migrations/backfills the same gate as
live conversation writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


POLICY_VERSION = "curated-v1"

# These are intentionally broad.  The purpose is to prevent accidental
# retention, not to infer that a topic is valuable from its existence in chat.
# An explicit durable fact about a blocked topic is still rejected by design.
DEFAULT_HARD_DENY_TERMS: tuple[str, ...] = (
    # Sports / football chatter (explicitly excluded for this deployment).
    "football",
    "soccer",
    "premier league",
    "champions league",
    "arsenal",
    "chelsea",
    "liverpool",
    "manchester united",
    "manchester city",
    "tottenham",
    "fifa",
    "world cup",
    "transfer window",
    "matchday",
    "goalkeeper",
    "cricket",
    "rugby",
    "nba",
    "nfl",
    "baseball",
    # Current-event / lifestyle chatter that has no durable product value.
    "weather forecast",
    "today's weather",
    "breaking news",
    "today's news",
    "latest headlines",
    "celebrity gossip",
    "election results",
    "stock price",
    "crypto price",
    "live score",
)

# Phrases that indicate the user is intentionally establishing reusable
# context.  These are signal phrases, not instructions to the agent.
_EXPLICIT_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"remember|don't forget|do not forget|keep in mind|important context|"
    r"my preference|i prefer|i always|i never|from now on|standing rule|"
    r"we decided|the decision is|decision:|policy:|constraint:|"
    r"save this|store this|make a note|going forward|next time|"
    r"do not|don't|never|always"
    r")\b",
    re.IGNORECASE,
)

# Reusable project/engineering context.  This is deliberately not enough on
# its own for a turn: it needs a future/reuse signal or an explicit durable
# marker so ordinary operational chatter is not retained.
_PROJECT_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"architecture|design decision|technical decision|root cause|"
    r"implemented|implementation|refactor(?:ed)?|workflow|runbook|"
    r"convention|codebase|repository|deployment|integration|api|schema|"
    r"database|configuration|configured|test strategy|lesson learned|"
    r"build|builder|implement|fix(?:ed)?|bug|migration|backfill"
    r")\b",
    re.IGNORECASE,
)

_BUILD_INTENT_RE = re.compile(
    r"\b(?:build|implement|design|refactor|fix|configure|create|develop)\b",
    re.IGNORECASE,
)

_FUTURE_SIGNAL_RE = re.compile(
    r"\b(?:for future|future use|reuse|reusable|will help|help us later|"
    r"build on this|going forward|next time|subsequent|long[- ]term|"
    r"standing|durable|persistent|context)\b",
    re.IGNORECASE,
)

# Secrets are rejected even if the surrounding message says "remember".
_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|"
    r"client[_ -]?secret|bearer)\s*[:=]\s*\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[0-9A-Z]{12,})\b"
    r")",
    re.IGNORECASE,
)

# External text is data, not an instruction to the memory subsystem.  Do not
# retain obvious prompt-injection payloads as durable context.
_INJECTION_RE = re.compile(
    r"\b(?:ignore (?:all )?(?:previous|prior) instructions|"
    r"system message|developer message|you are chatgpt|"
    r"follow these instructions|override your instructions)\b",
    re.IGNORECASE,
)

_CASUAL_RE = re.compile(
    r"^(?:ok(?:ay)?|thanks?|thank you|cheers|great|cool|nice|lol|haha|"
    r"good morning|good afternoon|good evening|how are you|same|yes|no|"
    r"sounds good|got it|understood)[.!\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IngestionDecision:
    """A deterministic, explainable write decision."""

    accepted: bool
    reason: str
    tags: tuple[str, ...] = ()
    explicit_signal: bool = False
    project_signal: bool = False
    future_signal: bool = False
    hard_denial: bool = False

    def as_metadata(self) -> dict[str, object]:
        """Return non-sensitive provenance suitable for Honcho metadata."""
        return {
            "memory_policy": POLICY_VERSION,
            "decision": "accept" if self.accepted else "reject",
            "decision_reason": self.reason,
            "tags": list(self.tags),
        }


def _terms(extra_terms: Iterable[str] | None) -> tuple[str, ...]:
    """Normalize extra literal deny terms without weakening built-ins."""
    result = list(DEFAULT_HARD_DENY_TERMS)
    for term in extra_terms or ():
        value = str(term).strip().lower()
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _blocked_topic(text: str, extra_terms: Iterable[str] | None) -> str | None:
    lowered = text.lower()
    for term in _terms(extra_terms):
        if term in lowered:
            return term
    return None


def evaluate_message(
    text: str,
    *,
    role: str = "user",
    extra_deny_terms: Iterable[str] | None = None,
) -> IngestionDecision:
    """Classify one message without deciding whether a whole turn is useful."""
    content = (text or "").strip()
    if not content:
        return IngestionDecision(False, "empty")

    if _SECRET_RE.search(content):
        return IngestionDecision(False, "secret_or_credential", hard_denial=True)
    if _INJECTION_RE.search(content):
        return IngestionDecision(False, "prompt_injection_like_text", hard_denial=True)

    blocked = _blocked_topic(content, extra_deny_terms)
    if blocked:
        return IngestionDecision(
            False,
            f"blocked_topic:{blocked}",
            tags=("blocked", "non_durable"),
            hard_denial=True,
        )

    if _CASUAL_RE.fullmatch(content) or len(content) < 20:
        return IngestionDecision(False, "casual_or_too_short", tags=("transient",))

    explicit = bool(_EXPLICIT_SIGNAL_RE.search(content))
    project = bool(_PROJECT_SIGNAL_RE.search(content))
    future = bool(_FUTURE_SIGNAL_RE.search(content))
    tags: list[str] = []
    if explicit:
        tags.append("explicit_durable_signal")
    if project:
        tags.append("project_context")
    if future:
        tags.append("future_reuse_signal")
    if role == "assistant":
        tags.append("assistant_context")

    return IngestionDecision(
        accepted=bool(explicit or (project and future)),
        reason="message_signal" if (explicit or (project and future)) else "no_durable_signal",
        tags=tuple(tags),
        explicit_signal=explicit,
        project_signal=project,
        future_signal=future,
    )


def decide_turn(
    user_content: str,
    assistant_content: str,
    *,
    mode: str = "curated",
    require_signal: bool = True,
    extra_deny_terms: Iterable[str] | None = None,
) -> IngestionDecision:
    """Decide whether a conversation turn belongs in durable memory.

    ``all`` is retained only as a compatibility escape hatch for isolated
    development/testing.  It still cannot bypass hard safety/topic exclusions.
    Production profiles should use ``curated`` (the default) or ``off``.
    """
    normalized_mode = str(mode or "curated").strip().lower()
    if normalized_mode not in {"curated", "all", "off"}:
        normalized_mode = "curated"
    if normalized_mode == "off":
        return IngestionDecision(False, "ingestion_disabled")

    user = evaluate_message(user_content, role="user", extra_deny_terms=extra_deny_terms)
    assistant = evaluate_message(
        assistant_content,
        role="assistant",
        extra_deny_terms=extra_deny_terms,
    )

    # A hard denial in either half rejects the whole pair.  Never retain a
    # football/credential-bearing assistant reply merely because the user half
    # happened to contain a useful preference.
    if user.hard_denial or assistant.hard_denial:
        hard = user if user.hard_denial else assistant
        return IngestionDecision(
            False,
            hard.reason,
            tags=hard.tags,
            hard_denial=True,
        )

    if normalized_mode == "all":
        return IngestionDecision(
            True,
            "compatibility_all_mode",
            tags=("legacy_all_mode",),
        )

    explicit = user.explicit_signal or assistant.explicit_signal
    project = user.project_signal or assistant.project_signal
    future = user.future_signal or assistant.future_signal
    technical_pair = (
        user.project_signal
        and assistant.project_signal
        and (
            future
            or user.explicit_signal
            or assistant.explicit_signal
            or bool(_BUILD_INTENT_RE.search(user_content or ""))
        )
    )

    accepted = bool(explicit or technical_pair)
    if not require_signal:
        accepted = bool(user_content.strip() or assistant_content.strip())

    if not accepted:
        reason = "no_durable_signal"
    elif explicit:
        reason = "explicit_durable_signal"
    else:
        reason = "reusable_project_context"

    tags = tuple(
        tag
        for tag in (
            "explicit_durable_signal" if explicit else "",
            "project_context" if project else "",
            "future_reuse_signal" if future else "",
        )
        if tag
    )
    return IngestionDecision(
        accepted=accepted,
        reason=reason,
        tags=tags,
        explicit_signal=explicit,
        project_signal=project,
        future_signal=future,
    )


def decide_conclusion(
    content: str,
    *,
    extra_deny_terms: Iterable[str] | None = None,
) -> IngestionDecision:
    """Apply the hard safety gate to an explicit ``honcho_conclude`` write.

    A conclusion is already an intentional write, so it does not require the
    turn-level signal.  It still must be non-trivial and must not contain a
    blocked topic, credential, or prompt-injection payload.
    """
    evaluated = evaluate_message(
        content,
        role="user",
        extra_deny_terms=extra_deny_terms,
    )
    if evaluated.hard_denial:
        return evaluated
    if not content or len(content.strip()) < 20:
        return IngestionDecision(False, "conclusion_too_short", tags=("transient",))
    return IngestionDecision(
        True,
        "explicit_conclusion",
        tags=tuple(dict.fromkeys((*evaluated.tags, "explicit_conclusion"))),
        explicit_signal=True,
        project_signal=evaluated.project_signal,
        future_signal=evaluated.future_signal,
    )


def decide_card_fact(
    content: str,
    *,
    extra_deny_terms: Iterable[str] | None = None,
) -> IngestionDecision:
    """Validate a short peer-card fact without imposing a length minimum."""
    evaluated = evaluate_message(
        content,
        role="user",
        extra_deny_terms=extra_deny_terms,
    )
    if evaluated.hard_denial:
        return evaluated
    if not content or not content.strip():
        return IngestionDecision(False, "card_fact_empty")
    return IngestionDecision(
        True,
        "explicit_card_fact",
        tags=tuple(dict.fromkeys((*evaluated.tags, "peer_card"))),
        explicit_signal=True,
    )
