"""Token-budget estimators and compression constants extracted from context_compressor.

Owns the char/token budget math (chars-per-token, image equivalents, fallback
and auto-focus caps, tail floors, small-context thresholds) plus the
HISTORICAL_TASK_HEADING constant used across summary templates.

Part of #78645 + #78647.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from agent.model_metadata import estimate_tokens_rough


HISTORICAL_TASK_HEADING = "## Historical Task Snapshot"

_CHARS_PER_TOKEN = 4
# Flat token cost per attached image part.  Real cost varies by provider and
# dimensions (Anthropic ≈ width×height/750, GPT-4o up to ~1700 for
# high-detail 2048×2048, Gemini 258/tile), but 1600 is a realistic ceiling
# that keeps compression budgeting honest for multi-image conversations.
# Matches Claude Code's IMAGE_TOKEN_ESTIMATE constant.
_IMAGE_TOKEN_ESTIMATE = 1600
# Same figure expressed in the char-budget currency the rest of the
# compressor speaks in.  Used when accumulating message "content length"
# for tail-cut decisions.
_IMAGE_CHAR_EQUIVALENT = _IMAGE_TOKEN_ESTIMATE * _CHARS_PER_TOKEN
_SUMMARY_FAILURE_COOLDOWN_SECONDS = 600

# Hard ceiling for the deterministic summary-failure handoff.  The fallback is
# only meant to preserve continuity anchors from the dropped window, not to
# become another unbounded transcript copy after the LLM summarizer failed.
_FALLBACK_SUMMARY_MAX_CHARS = 8_000
_FALLBACK_PREVIOUS_SUMMARY_MAX_CHARS = 3_000
_FALLBACK_TURN_MAX_CHARS = 700
_AUTO_FOCUS_MAX_TURNS = 3
_AUTO_FOCUS_TURN_MAX_CHARS = 260
_AUTO_FOCUS_MAX_CHARS = 700
_ACTIVE_TASK_MAX_CHARS = 1400
# Keep a short run of recent messages verbatim even when the token budget is
# already exhausted.  The public ``protect_last_n`` default is intentionally
# high for small/light tails, but using all 20 as a hard floor here would bring
# back the old large-tool-output case where nothing can be compacted.
_MAX_TAIL_MESSAGE_FLOOR = 8

# Pre-LLM feasibility skip (#60451): when the compressible middle is below
# this fraction of threshold_tokens (and a prior real-usage ineffectiveness
# strike exists), skip the LLM summary call — deterministic dropping alone
# recovers the negligible savings such a summary could deliver.
_FEASIBILITY_SKIP_MIDDLE_FRACTION = 0.10
# Under context pressure (protected-tail tool bodies alone exceed the soft
# tail budget), demote large completed tool/file outputs even inside the
# protected region — but always keep this many trailing messages verbatim so
# the active user ask / latest tool pair remain readable.  Issue #61932.
_PRESSURE_KEEP_RECENT_MESSAGES = 3

# Models with context windows below this get their compression threshold
# floored at ``_SMALL_CTX_THRESHOLD_PERCENT`` (raise-only — an explicitly
# higher user/model threshold always wins).  At the default 50% trigger a
# 128K-262K model compacts with only ~64-131K consumed; the incompressible
# floor (system prompt + tool schemas + protected tail + rolling summary)
# eats most of the reclaimed headroom, so compaction re-fires every 1-2
# turns and the session spends most of its wall-clock summarizing.
_SMALL_CTX_WINDOW_LIMIT = 512_000
_SMALL_CTX_THRESHOLD_PERCENT = 0.75


_PATH_MENTION_RE = re.compile(r"(?:/|~/?|[A-Za-z]:\\)[^\s`'\")\]}<>]+")

# MEDIA delivery directives must not reach the summarizer — if one leaks into
# the summary, the downstream model may re-emit it as an active directive on
# the next turn, triggering bogus attachment sends (#14665).
_MEDIA_DIRECTIVE_RE = re.compile(r"MEDIA:\S+")
_HISTORICAL_TASK_SECTION_RE = re.compile(
    rf"(?ms)^{re.escape(HISTORICAL_TASK_HEADING)}\s*\n.*?(?=^## |\Z)"
)


def _dedupe_append(items: list[str], value: str, *, limit: int) -> None:
    value = value.strip()
    if value and value not in items and len(items) < limit:
        items.append(value)


def _extract_tool_call_name_and_args(tool_call: Any) -> tuple[str, str]:
    """Return a best-effort ``(name, arguments)`` pair for dict/object tool calls."""
    if isinstance(tool_call, dict):
        fn = tool_call.get("function") or {}
        return str(fn.get("name") or "unknown"), str(fn.get("arguments") or "")

    fn = getattr(tool_call, "function", None)
    if fn is None:
        return "unknown", ""
    return str(getattr(fn, "name", None) or "unknown"), str(getattr(fn, "arguments", None) or "")


def _extract_tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _collect_path_mentions(text: str, relevant_files: list[str], *, limit: int = 12) -> None:
    for match in _PATH_MENTION_RE.findall(text):
        _dedupe_append(relevant_files, match.rstrip(".,:;"), limit=limit)


def _content_length_for_budget(raw_content: Any) -> int:
    """Return the effective char-length of a message's content for token budgeting.

    Plain strings: ``len(content)``. Multimodal lists: sum of text-part
    ``len(text)`` plus a flat ``_IMAGE_CHAR_EQUIVALENT`` per image part
    (``image_url`` / ``input_image`` / Anthropic-style ``image``). This
    keeps the compressor from treating a turn with 5 attached images as
    near-zero tokens just because the text part is empty.
    """
    if isinstance(raw_content, str):
        return len(raw_content)
    if not isinstance(raw_content, list):
        return len(str(raw_content or ""))

    total = 0
    for p in raw_content:
        if isinstance(p, str):
            total += len(p)
            continue
        if not isinstance(p, dict):
            total += len(str(p))
            continue
        ptype = p.get("type")
        if ptype in {"image_url", "input_image", "image"}:
            total += _IMAGE_CHAR_EQUIVALENT
        else:
            # text / input_text / tool_result-with-text / anything else with
            # a text field.  Ignore the raw base64 payload inside image_url
            # dicts — dimensions don't matter, only whether it's an image.
            total += len(p.get("text", "") or "")
    return total


def _serialized_length_for_budget(value: Any) -> int:
    """Return a stable char-length for non-content replay/metadata fields."""
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return len(str(value))


# Provider replay/metadata fields that ride the wire on every request but are
# invisible to ``msg["content"]``/``msg["tool_calls"]`` accounting.  Codex
# Responses sessions in particular carry ``codex_reasoning_items`` blobs of
# ``encrypted_content`` that can dominate the serialized session (a measured
# 214-turn session held ~115K tokens / 27% of its payload there — #55572).
#
# ``reasoning_details`` is handled separately (see
# ``_reasoning_details_text_chars``): its signed/base64 envelope is excluded
# from the budget, mirroring the preflight estimator's exclusion in
# ``model_metadata._estimate_message_tokens_without_images`` (#73298).
_REPLAY_BUDGET_KEYS = (
    "reasoning",
    "reasoning_content",
    "codex_reasoning_items",
    "codex_message_items",
)


def _reasoning_details_text_chars(value: Any) -> int:
    """Textual thinking chars inside a ``reasoning_details`` envelope.

    ``reasoning_details`` carries provider thinking blocks: the actual
    thinking TEXT plus opaque signed/base64 envelope blobs (Anthropic
    ``signature``, redacted ``data``, encrypted payloads).  The envelope is
    never billed at anything near chars/4 by the provider and — on every
    transport except Codex Responses — is replayed for at most the newest
    assistant turn, so charging it on every message inflated the tail-budget
    walk and silently shrank the surviving tail (#73298, second site).

    Count only the thinking text (the #51800 lesson: real reasoning text
    MUST stay visible to the budget), skip everything else.
    """
    if not value:
        return 0
    if isinstance(value, str):
        return len(value)
    total = 0
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        for part in value:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                for text_key in ("thinking", "text", "summary"):
                    text = part.get(text_key)
                    if isinstance(text, str):
                        total += len(text)
    return total


def _estimate_msg_budget_tokens(msg: dict) -> int:
    """Token estimate for one message in the tail-protection budget walks.

    Counts the message content plus the **full** ``tool_call`` envelope —
    ``id``, ``type``, ``function.name`` and JSON structure — not just
    ``function.arguments``.  Counting only the arguments string undercounted
    assistant turns that fan out into parallel tool calls by 2-15x (a
    4-tool-call turn measures ~73 vs ~1,090 real tokens), so the protected
    tail overshot ``tail_token_budget`` and compression became ineffective.
    See issue #28053.

    Also counts provider replay fields (``codex_reasoning_items`` etc. —
    see ``_REPLAY_BUDGET_KEYS``).  The preflight "should I compress?"
    estimator sees the full message shape, so the tail walk must use the
    same size class; otherwise an assistant message with tiny visible
    content but large hidden replay blobs is protected as if it were small,
    the post-compression session stays near the context limit, and
    compaction re-fires continuously (#55572).  Accounting-only: replay
    fields are never mutated or pruned here.
    """
    content = msg.get("content") or ""
    if isinstance(content, str):
        tokens = estimate_tokens_rough(content) + 10  # +10 for role/key overhead
    else:
        content_len = _content_length_for_budget(content)
        tokens = content_len // _CHARS_PER_TOKEN + 10
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            tokens += estimate_tokens_rough(str(tc))
    for key in _REPLAY_BUDGET_KEYS:
        tokens += _serialized_length_for_budget(msg.get(key)) // _CHARS_PER_TOKEN
    # reasoning_details: charge only the thinking TEXT, never the signed /
    # base64 envelope (#73298 second site; mirrors the preflight estimator's
    # exclusion in model_metadata).  When the same thinking text already rides
    # in ``reasoning``/``reasoning_content`` (measured byte-identical on
    # Anthropic-wire sessions), skip it here entirely so the prose is not
    # charged twice on top of the envelope exclusion.
    if not (msg.get("reasoning") or msg.get("reasoning_content")):
        tokens += (
            _reasoning_details_text_chars(msg.get("reasoning_details"))
            // _CHARS_PER_TOKEN
        )
    return tokens
