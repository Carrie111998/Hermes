"""Workflow analyst — LLM-backed auxiliary for pipeline inference tasks.

Three analysis modes, one auxiliary key. Used by the workflow engine
when a task benefits from reasoning rather than mechanical processing.

Invoked via ``get_text_auxiliary_client("workflow_analyst")`` with
a mode-specific system prompt and structured JSON output schema.

Design notes
------------
* Mirrors ``hermes_cli/kanban_decompose.py``: lazy aux client import,
  lenient JSON parse, never raises on expected failure modes.
* Configured under ``auxiliary.workflow_analyst`` in config.yaml.
  Falls back to the auto provider when not explicitly set.
* System prompt defines the output schema — each mode produces
  a different JSON shape documented inline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


# ── Mode: escalation analysis ─────────────────────────────────────

_ESCALATION_SYSTEM = """You are the workflow escalation analyst for the Hermes Agent fleet.

A workflow engine revision loop has exhausted its 3-cycle limit. You are given
the full history of a review gate (the original spec/section, each round of
rejection, each round of revision, and the final state). Your job is to
produce a structured brief so the orchestrator (Sherlock) can resolve the
deadlock without re-reading all the raw card bodies.

Output a single JSON object:

  {
    "deadlock_type": "spec_disagreement" | "security_blocker" | "incomplete_decomposition" | "other",
    "summary": "<two-sentence summary of what's happening>",
    "rounds": [
      {"round": 1, "rejection": "<what was rejected>", "revision": "<what changed>"},
      ...
    ],
    "sticking_point": "<the one issue that never got resolved across all rounds>",
    "suggested_actions": ["<option A>", "<option B>", "<option C>"],
    "recommended_escalation": "sherlock_can_resolve" | "needs_randy"
  }

Rules:
- Be specific. Name the exact section/file/requirement in dispute.
- If the same issue appeared in all 3 rounds, that's the sticking point.
- suggested_actions should be concrete next steps, not vague guidance.
- If the deadlock is a genuine design trade-off (not a misunderstanding),
  recommended_escalation should be "needs_randy".
- No preamble, no code fences. Output only the JSON object."""


_ESCALATION_USER = """Project: {project}
Gate: {gate}
Verify node: {verify_node}

Loop history:
{loop_history}
"""


# ── Mode: pipeline status summary ─────────────────────────────────

_STATUS_SYSTEM = """You are the workflow status summariser for the Hermes Agent fleet.

You are given the raw state of a running pipeline (JSON from the engine's
state file). Produce a concise, human-readable status summary for the
orchestrator (Sherlock).

Output a single JSON object:

  {
    "pipeline": "<name>",
    "current_layer": <N>,
    "total_layers": <M>,
    "overall_status": "running" | "blocked" | "completed" | "failed",
    "layer_summary": [
      {
        "layer": <N>,
        "nodes": [
          {"node": "<id>", "agent": "<name>", "status": "<status>", "error": "<if any>"},
          ...
        ]
      },
      ...
    ],
    "attention_needed": [
      "<human-readable alert for any blocked/failed/timed_out node>"
    ],
    "estimated_completion": "<best guess, e.g. '~2 hours if unblocked'>"
  }

Rules:
- List ALL layers, not just the current one. Skip layers with all statuses "pending".
- For each node, include agent name, status, and error if present.
- attention_needed is empty array if nothing is blocked/failed/timed_out.
- Be concrete about estimated completion based on remaining timeout windows.
- No preamble, no code fences. Output only the JSON object."""


_STATUS_USER = """Pipeline: {pipeline_name}

Raw engine state:
{state_json}
"""


# ── Mode: failure diagnosis ────────────────────────────────────────

_FAILURE_SYSTEM = """You are the workflow failure diagnostician for the Hermes Agent fleet.

A node in the pipeline has failed or timed out. You are given:
- The node's task description (what it was supposed to do)
- The node's agent (which profile was working on it)
- The error or timeout details
- How long it ran before failing

Produce a structured diagnosis:

  {
    "likely_cause": "<most probable explanation>",
    "cause_category": "timeout" | "agent_error" | "config_missing" | "dependency_failure" | "resource_exhaustion" | "unknown",
    "evidence": ["<fact 1 supporting the diagnosis>", "<fact 2>", ...],
    "suggested_fix": "<concrete next step for Sherlock>",
    "should_retry": true | false,
    "retry_instructions": "<if should_retry: what to change before retrying, else empty string>"
  }

Rules:
- Be specific. If the error mentions a missing file, name it.
- If the node timed out after running the full timeout window, cause_category is likely "timeout" or "resource_exhaustion".
- suggested_fix should be one concrete action Sherlock can take.
- If the error is clearly transient (network timeout, API rate limit), should_retry should be true.
- No preamble, no code fences. Output only the JSON object."""


_FAILURE_USER = """Node: {node_id}
Agent: {agent}
Task: {task}
Timeout: {timeout_minutes}min
Elapsed before failure: {elapsed}
Error: {error}
"""


# ── Outcome dataclass ──────────────────────────────────────────────

@dataclass
class AnalystOutcome:
    """Result of an analyst invocation."""
    mode: str
    success: bool
    result: Optional[dict] = None
    error: Optional[str] = None
    raw_response: Optional[str] = None


# ── Public API ─────────────────────────────────────────────────────

def analyze_escalation(
    *,
    project: str = "",
    gate: str = "",
    verify_node: str = "",
    loop_history: str = "",
    timeout: Optional[int] = None,
) -> AnalystOutcome:
    """Analyze a deadlocked revision loop and produce a structured brief."""
    user_msg = _ESCALATION_USER.format(
        project=project,
        gate=gate,
        verify_node=verify_node,
        loop_history=loop_history,
    )
    return _invoke(
        mode="escalation",
        system_prompt=_ESCALATION_SYSTEM,
        user_message=user_msg,
        timeout=timeout,
    )


def analyze_status(
    *,
    pipeline_name: str = "",
    state_json: str = "",
    timeout: Optional[int] = None,
) -> AnalystOutcome:
    """Summarize engine state into a human-readable status report."""
    user_msg = _STATUS_USER.format(
        pipeline_name=pipeline_name,
        state_json=state_json,
    )
    return _invoke(
        mode="status",
        system_prompt=_STATUS_SYSTEM,
        user_message=user_msg,
        timeout=timeout,
    )


def analyze_failure(
    *,
    node_id: str = "",
    agent: str = "",
    task: str = "",
    timeout_minutes: int = 30,
    elapsed: str = "",
    error: str = "",
    timeout: Optional[int] = None,
) -> AnalystOutcome:
    """Diagnose a node failure and suggest remediation."""
    user_msg = _FAILURE_USER.format(
        node_id=node_id,
        agent=agent,
        task=task,
        timeout_minutes=timeout_minutes,
        elapsed=elapsed,
        error=error,
    )
    return _invoke(
        mode="failure",
        system_prompt=_FAILURE_SYSTEM,
        user_message=user_msg,
        timeout=timeout,
    )


# ── Internal ───────────────────────────────────────────────────────

def _invoke(
    *,
    mode: str,
    system_prompt: str,
    user_message: str,
    timeout: Optional[int] = None,
) -> AnalystOutcome:
    """Call the auxiliary LLM and return a structured outcome."""
    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("workflow_analyst: aux client import failed: %s", exc)
        return AnalystOutcome(mode=mode, success=False, error="auxiliary client unavailable")

    try:
        client, model = get_text_auxiliary_client("workflow_analyst")
    except Exception as exc:
        logger.debug("workflow_analyst: get_text_auxiliary_client failed: %s", exc)
        return AnalystOutcome(mode=mode, success=False, error="auxiliary client unavailable")

    if client is None or not model:
        return AnalystOutcome(mode=mode, success=False, error="no auxiliary client configured")

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
            max_tokens=2000,
            timeout=timeout or 180,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info("workflow_analyst: API call failed for mode=%s (%s)", mode, exc)
        return AnalystOutcome(mode=mode, success=False, error=f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        return AnalystOutcome(
            mode=mode, success=False,
            error="LLM returned malformed JSON",
            raw_response=raw,
        )

    return AnalystOutcome(mode=mode, success=True, result=parsed, raw_response=raw)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """Extract a JSON object from an LLM response, tolerating fences."""
    stripped = raw.strip()
    # Strip ```json / ``` fences
    stripped = _FENCE_RE.sub("", stripped).strip()
    # Find the outermost { ... }
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = stripped[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None
