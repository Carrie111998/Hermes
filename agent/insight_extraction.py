"""LLM-salience extraction of durable facts from session context.

Two entry points, one shared parser/normalizer:

* :data:`DURABLE_FACTS_PROMPT_SECTION` is appended to the handoff prompt so the
  *single* handoff LLM call also emits a machine-parseable durable-facts block
  — **zero extra LLM calls** for the handoff triggers.
  :func:`parse_facts_block` then recovers the JSON from that response.

* :func:`extract_facts_from_messages` is a standalone extractor (its own
  auxiliary-LLM call) for callers that want facts without generating a full
  handoff — e.g. at session end or before a compaction where no handoff is
  produced. It is unthrottled; callers are responsible for only invoking it on
  session boundaries, never per turn.

This module only *extracts and normalizes* candidate facts. Any hard guarantee
that secrets never reach long-term memory must come from the consuming memory
store's write path, not from this module; the prompt merely *asks* the model to
omit them.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = {"user_pref", "project", "tool", "general"}
_VALID_SCOPES = {"user", "project"}

DEFAULT_MAX_FACTS = 8
DEFAULT_MIN_CHARS = 12
MAX_FACT_CHARS = 200

# Appended verbatim after the handoff sections.
DURABLE_FACTS_PROMPT_SECTION = """

---
## Durable facts (JSON, for long-term memory)
Extract ONLY facts that will be useful in FUTURE sessions. Include:
user preferences and rules, decisions made, facts about the environment/project,
corrections ("do not do it this way"), stable information about people.
Do NOT include: task progress, transient state, things that are easily
rediscovered, or secrets/tokens/passwords.
Each fact must be a single self-contained sentence (<=200 characters).
Return an ARRAY (an empty array [] if there are no facts) strictly inside a
fenced ```json block:
```json
[{"content":"...","category":"user_pref|project|tool|general","scope":"user|project"}]
```"""

# Greedy-but-bounded: grab the contents of a ```json ... ``` fence.
_JSON_FENCE_RE = re.compile(r"```json\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)
# Fallback: a bare top-level JSON array somewhere in the text.
_BARE_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)


def _coerce_to_list(raw: Any) -> list[dict]:
    """Tolerate the model returning a bare object or a {"facts": [...]} wrapper."""
    if isinstance(raw, dict):
        if isinstance(raw.get("facts"), list):
            raw = raw["facts"]
        elif "content" in raw:
            raw = [raw]
        else:
            return []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def parse_facts_block(text: str) -> list[dict]:
    """Recover a durable-facts list from an LLM response. Never raises."""
    if not text or not isinstance(text, str):
        return []
    candidates: list[str] = list(_JSON_FENCE_RE.findall(text))
    if not candidates:
        m = _BARE_ARRAY_RE.search(text)
        if m:
            candidates.append(m.group(0))
    for blob in candidates:
        try:
            return _coerce_to_list(json.loads(blob))
        except Exception:
            continue
    return []


def normalize_facts(
    raw_facts: Iterable[Any] | None,
    *,
    max_facts: int = DEFAULT_MAX_FACTS,
    min_chars: int = DEFAULT_MIN_CHARS,
    default_origin: str = "agent",
) -> list[dict]:
    """Validate, clamp and de-duplicate extracted facts.

    Returns a list of ``{content, category, scope, origin}`` dicts. Invalid or
    too-short items are dropped; content is clamped to ``MAX_FACT_CHARS``;
    category/scope are coerced to the allowed enums and kept mutually
    consistent. Case-insensitive content de-dup within the batch.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw_facts or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "") or "").strip()
        if len(content) < int(min_chars):
            continue
        content = content[:MAX_FACT_CHARS]
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)

        scope = item.get("scope") if item.get("scope") in _VALID_SCOPES else None
        category = item.get("category") if item.get("category") in _VALID_CATEGORIES else None
        if category is None:
            category = "user_pref" if scope == "user" else "general"
        if scope is None:
            scope = "user" if category == "user_pref" else "project"

        out.append(
            {
                "content": content,
                "category": category,
                "scope": scope,
                "origin": str(item.get("origin") or default_origin),
            }
        )
        if len(out) >= int(max_facts):
            break
    return out


def _build_extraction_prompt(transcript_excerpt: str) -> str:
    return (
        "Analyze the session transcript and extract long-lived facts for the "
        "agent's memory."
        + DURABLE_FACTS_PROMPT_SECTION
        + "\n\nTranscript:\n"
        + transcript_excerpt
    )


def extract_facts_from_messages(
    messages: list[dict] | None,
    *,
    main_runtime: dict | None = None,
    max_facts: int = DEFAULT_MAX_FACTS,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_transcript_chars: int = 18_000,
) -> list[dict]:
    """Standalone extraction over a transcript. Returns [] on any failure.

    Uses the auxiliary "compression" model (same as the handoff summarizer);
    callers are responsible for throttling so this is not called per turn.
    """
    try:
        from agent.handoff import _message_excerpt, EMPTY_EXCERPT_SENTINEL

        excerpt = _message_excerpt(
            messages or [], max_messages=60, max_chars=max_transcript_chars
        )
        if not excerpt or excerpt == EMPTY_EXCERPT_SENTINEL:
            return []

        from agent.auxiliary_client import call_llm

        response = call_llm(
            task="compression",
            messages=[{"role": "user", "content": _build_extraction_prompt(excerpt)}],
            max_tokens=1200,
            temperature=0.2,
            main_runtime=main_runtime,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
        return normalize_facts(
            parse_facts_block(content), max_facts=max_facts, min_chars=min_chars
        )
    except Exception:
        logger.debug("standalone insight extraction failed", exc_info=True)
        return []
