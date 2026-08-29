"""Durable session handoff files for compaction and restarts.

Handoffs are an operational continuity layer separate from the in-context
compression summary: they are written to disk before context is discarded so a
future continuation can recover goals, decisions, next steps, and changed files.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from agent.insight_extraction import (
    DURABLE_FACTS_PROMPT_SECTION as _DURABLE_FACTS_SECTION,
    normalize_facts,
    parse_facts_block,
)

logger = logging.getLogger(__name__)

HANDOFFS_DIRNAME = "handoffs"
_DEFAULT_EXCERPT_CHARS_PER_TOKEN = 4

# Sentinel returned by _message_excerpt when a transcript has no usable text.
# Exposed so callers (e.g. insight_extraction) can detect an empty excerpt by
# identity instead of matching on a localized substring.
EMPTY_EXCERPT_SENTINEL = "- No textual context available."


def _safe_session_id(session_id: str | None) -> str:
    raw = str(session_id or "unknown-session")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    return safe or "unknown-session"


def _handoffs_dir(hermes_home: str | Path | None = None) -> Path:
    root = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return root / HANDOFFS_DIRNAME


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _message_excerpt(messages: Iterable[dict[str, Any]], *, max_messages: int = 40, max_chars: int = 12_000) -> str:
    """Return an extractive transcript excerpt suitable for fallback handoffs."""
    items = list(messages or [])
    if len(items) > max_messages:
        head = items[: max_messages // 4]
        tail = items[-(max_messages - len(head)) :]
        items = head + [{"role": "system", "content": "... middle messages omitted ..."}] + tail

    lines: list[str] = []
    used = 0
    for msg in items:
        role = msg.get("role", "unknown") if isinstance(msg, dict) else "unknown"
        content = _stringify_content(msg.get("content") if isinstance(msg, dict) else msg).strip()
        if not content and isinstance(msg, dict) and msg.get("tool_calls"):
            content = f"tool_calls: {_stringify_content(msg.get('tool_calls'))}"
        if not content:
            continue
        block = f"- **{role}:** {content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[: max(0, remaining - 20)] + "… [truncated]"
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines) or EMPTY_EXCERPT_SENTINEL


def _write_latest_pointer(handoffs_dir: Path, target: Path) -> None:
    latest = handoffs_dir / "latest.md"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(target.name)
    except Exception:
        latest.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")


def _update_index(handoffs_dir: Path, entry: dict[str, Any]) -> None:
    index_path = handoffs_dir / "index.json"
    data: list[dict[str, Any]] = []
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            data = [x for x in raw if isinstance(x, dict)]
    except Exception:
        data = []

    data = [x for x in data if x.get("session_id") != entry.get("session_id")]
    data.append(entry)
    index_path.write_text(
        json.dumps(data[-500:], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _gather_git_context(hermes_home: str | Path | None = None) -> str:
    """Collect recent git log and status from the hermes home directory.

    Returns a combined string of recent commits and status, or an empty
    string on any failure (e.g. when the directory is not a git repository).
    """
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    parts: list[str] = []

    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True,
            text=True,
            cwd=str(home),
            timeout=10,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            parts.append("=== git log --oneline -20 ===")
            parts.append(log_result.stdout.strip())
    except Exception:
        pass

    try:
        status_result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=str(home),
            timeout=10,
        )
        if status_result.returncode == 0 and status_result.stdout.strip():
            parts.append("=== git status --short ===")
            parts.append(status_result.stdout.strip())
    except Exception:
        pass

    return "\n".join(parts)


def _build_llm_handoff_prompt(
    *,
    safe_id: str,
    now: str,
    source_info: str,
    model: str | None,
    session_id: str | None,
    reason: str,
    git_context: str,
    transcript_excerpt: str,
) -> str:
    """Build the LLM prompt for generating a structured handoff summary."""
    return f"""Analyze the session transcript and git context below. Fill in EVERY section based on what actually happened. Do not write generic filler — only concrete facts.

# Handoff: {safe_id}

**Date:** {now}
**Source:** {source_info}
**Model:** {model or ''}
**Session ID:** {session_id or ''}
**Reason:** {reason}

## Goal
[1-2 sentences: what the user was trying to do]

## Done
- [x] [concrete task — what was completed]

## In progress / Next steps
- [ ] [task with file paths and specific areas]
- [ ] [blocked tasks with an explanation]

## Key decisions
- **[Decision]**: [What was chosen] — [Why, including rejected alternatives]

## Dead ends (do not repeat)
- [Approach that did not work] — [Why]

## Changed files
- `path/to/file` — [what changed, one line]

## Current state
- **Tests/checks:** [status]
- **Manual verification:** [what was verified]

## Context for the next session
[2-4 sentences: the most important things for the next agent]

## Recommended first action
[Exact command or step]
{_DURABLE_FACTS_SECTION}

---
Git log:
{git_context}

Transcript:
{transcript_excerpt}"""


class HandoffLLMError(Exception):
    """The auxiliary LLM could not produce a structured handoff.

    Raised only in strict mode (e.g. an explicit manual handoff request). In
    non-strict mode (auto-compaction) an LLM failure silently falls back to an
    extractive handoff, which is a lossless raw record — not context loss — so no
    error is surfaced. ``str(self)`` carries the (already-string) cause so a
    caller can display it; callers should sanitize it before sending anywhere.
    """


def _call_auxiliary_llm(
    prompt: str, *, main_runtime: dict[str, Any] | None = None, strict: bool = False
) -> str | None:
    """Call the auxiliary LLM with a handoff prompt.

    Returns the LLM response text, or None on any failure. When *strict* is True,
    an exception is re-raised as :class:`HandoffLLMError` instead of being
    swallowed — a manual handoff request needs the failure surfaced, not hidden
    behind a silent extractive fallback.
    """
    try:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task="compression",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
            main_runtime=main_runtime,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
        return content.strip() if content else None
    except Exception as exc:
        logger.debug("LLM handoff summarization failed, falling back to extractive mode", exc_info=True)
        if strict:
            raise HandoffLLMError(str(exc)) from exc
        return None


def generate_handoff(
    *,
    session_id: str | None,
    messages: Iterable[dict[str, Any]] | None,
    reason: str,
    platform: str | None = None,
    model: str | None = None,
    parent_session_id: str | None = None,
    current_session_id: str | None = None,
    focus_topic: str | None = None,
    hermes_home: str | Path | None = None,
    extra_metadata: dict[str, Any] | None = None,
    llm_summarize: bool = False,
    strict_llm: bool = False,
    session_name: str | None = None,
    gateway_name: str | None = None,
    main_runtime: dict[str, Any] | None = None,
    memory_manager: Any | None = None,
) -> Path:
    """Write a durable markdown handoff and return its path.

    The extractive path is deliberately dependency-free. It must keep working
    when auxiliary LLMs are unavailable during shutdown or while a compression
    failure is already in progress.

    When *llm_summarize* is True, an auxiliary LLM is used to produce a
    structured summary of the session. If the LLM call fails, the function
    falls back to the extractive (non-LLM) handoff automatically — UNLESS
    *strict_llm* is True, in which case the failure raises
    :class:`HandoffLLMError` instead of silently degrading (the intended
    behavior for an explicit manual handoff request). *strict_llm* has no
    effect when *llm_summarize* is False.

    *memory_manager* is optional. When provided and it exposes
    ``ingest_extracted_facts(facts, session_id=...)``, the durable-facts block
    emitted by the same handoff LLM call is routed to it — no extra LLM calls.
    A parse/ingest failure there is never fatal to the handoff write itself.
    """

    safe_id = _safe_session_id(session_id)
    handoffs_dir = _handoffs_dir(hermes_home)
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / f"handoff-{safe_id}.md"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current = current_session_id or session_id or ""

    # --- LLM summarization path ---
    if llm_summarize:
        source_info = (
            f"{session_name or 'unnamed'} | "
            f"{'Gateway: ' + gateway_name if gateway_name else 'Terminal'} | "
            f"Session: {session_id or ''}"
        )
        git_context = _gather_git_context(hermes_home)
        transcript_excerpt = _message_excerpt(
            messages or [],
            max_messages=80,
            max_chars=25_000,
        )
        prompt = _build_llm_handoff_prompt(
            safe_id=safe_id,
            now=now,
            source_info=source_info,
            model=model,
            session_id=session_id,
            reason=reason,
            git_context=git_context,
            transcript_excerpt=transcript_excerpt,
        )
        # strict_llm re-raises an LLM exception as HandoffLLMError (manual
        # handoff request). Non-strict keeps the silent extractive fallback.
        llm_content = _call_auxiliary_llm(
            prompt, main_runtime=main_runtime, strict=strict_llm
        )

        if llm_content:
            # The single handoff LLM call also emits a durable-facts JSON block
            # (see DURABLE_FACTS_PROMPT_SECTION). Recover it and route the facts
            # to the optional memory manager — zero extra LLM calls. Never fatal:
            # a parse/ingest failure must not break the handoff write itself.
            if memory_manager is not None:
                try:
                    facts = normalize_facts(parse_facts_block(llm_content))
                    if facts and hasattr(memory_manager, "ingest_extracted_facts"):
                        n = memory_manager.ingest_extracted_facts(
                            facts, session_id=session_id or ""
                        )
                        if n:
                            logger.info(
                                "handoff insight extraction: routed %d durable fact(s) "
                                "(session=%s, reason=%s)",
                                n, session_id, reason,
                            )
                except Exception:
                    logger.debug("handoff insight ingestion failed", exc_info=True)

            # The extractive path below folds extra_metadata (e.g. chat_id/
            # thread_id) and ``platform`` into metadata_lines, but the LLM writes
            # llm_content verbatim from its own prompt — which mentions neither —
            # so those coordinates would be dropped on the LLM-summary path.
            # Append them deterministically instead of asking the LLM to
            # reproduce them (it would not reliably). Additive and guarded:
            # callers that pass platform=None and extra_metadata=None see no
            # change to llm_content.
            _llm_meta = {"platform": platform, **(extra_metadata or {})}
            _meta_lines = [
                f"{key}: {value}"
                for key, value in sorted(_llm_meta.items())
                if value is not None
            ]
            if _meta_lines:
                llm_content = (
                    f"{llm_content.rstrip()}\n\n" + "\n".join(_meta_lines) + "\n"
                )

            path.write_text(llm_content, encoding="utf-8")
            _write_latest_pointer(handoffs_dir, path)
            _update_index(
                handoffs_dir,
                {
                    "session_id": session_id,
                    "safe_session_id": safe_id,
                    "path": str(path),
                    "created_at": now,
                    "reason": reason,
                    "platform": platform,
                    "model": model,
                    "parent_session_id": parent_session_id,
                    "current_session_id": current,
                    "llm_summarized": True,
                },
            )
            return path
        # LLM returned empty content. In strict mode surface it rather than
        # silently degrading; otherwise fall through to the extractive path.
        if strict_llm:
            raise HandoffLLMError("auxiliary LLM returned no handoff content")
        logger.info("LLM handoff summarization failed, using extractive fallback")

    # --- Extractive (default / fallback) path ---
    excerpt = _message_excerpt(messages or [])

    metadata_lines = [
        f"Date: {now}",
        f"Platform: {platform or ''}",
        f"Model: {model or ''}",
        f"Parent session: {parent_session_id or ''}",
        f"Current session: {current}",
        f"Reason: {reason}",
    ]
    if focus_topic:
        metadata_lines.append(f"Focus: {focus_topic}")
    if extra_metadata:
        for key, value in sorted(extra_metadata.items()):
            if value is not None:
                metadata_lines.append(f"{key}: {value}")

    content = f"""# Handoff: {safe_id}

{chr(10).join(metadata_lines)}

## Goal

See the extracted context below. If the user's goal is not obvious, first infer
it from the most recent messages.

## Done

- Recorded a handoff for reason `{reason}`.

## In progress / Next steps

- Recover the current goal from the "Context for the next session" section and
  the most recent messages.
- Verify actual state with tools before making claims about services, files, or
  configuration.

## Key decisions

- The handoff was created as a durable continuity artifact; it complements the
  compaction summary and does not replace verifying the current state.

## Dead ends — do not repeat

- Do not rely on the old transcript alone after compaction/restart; use this
  handoff as the starting map of the state.

## Changed files / external artifacts

- Not detected automatically. See the transcript excerpt and git/status checks.

## Checks

- Not run automatically when the handoff was created.

## Context for the next session

{excerpt}

## Recommended first action

Read this handoff, then verify the live state with tools before continuing.
"""
    path.write_text(content, encoding="utf-8")
    _write_latest_pointer(handoffs_dir, path)
    _update_index(
        handoffs_dir,
        {
            "session_id": session_id,
            "safe_session_id": safe_id,
            "path": str(path),
            "created_at": now,
            "reason": reason,
            "platform": platform,
            "model": model,
            "parent_session_id": parent_session_id,
            "current_session_id": current,
        },
    )
    return path


def read_handoff_excerpt(path: str | Path, *, max_tokens: int = 12_000) -> str:
    """Read a handoff file with a rough token budget cap."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    max_chars = max(1, int(max_tokens) * _DEFAULT_EXCERPT_CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 80)] + "\n\n[... handoff truncated to configured max_tokens budget ...]\n"


def write_gateway_active_handoff(entry: dict[str, Any], *, hermes_home: str | Path | None = None) -> Path:
    """Append/update the gateway-active handoff mapping."""
    handoffs_dir = _handoffs_dir(hermes_home)
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / "gateway-active.json"
    data: list[dict[str, Any]] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            data = [x for x in raw if isinstance(x, dict)]
    except Exception:
        data = []

    key = entry.get("session_key") or entry.get("session_id") or entry.get("handoff_path")
    if key:
        data = [x for x in data if (x.get("session_key") or x.get("session_id") or x.get("handoff_path")) != key]
    data.append({**entry, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    path.write_text(json.dumps(data[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
