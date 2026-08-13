"""Verification-loop helpers for the ``pre_verify`` round-end gate.

When the agent has edited code and is about to verify/finish, the loop fires the
``pre_verify`` hook (user directives resolved by
:func:`hermes_cli.plugins.get_pre_verify_continue_message`). A directive keeps
the agent going one more turn — run a check, defer it, tidy the diff — instead of
stopping immediately.

The shipped coding guidance lives on the evidence-based verification-stop nudge
(``agent/verification_stop.py``), not as a second default stop gate. That keeps
the default token cost tied to the existing "missing verification evidence"
decision while preserving ``pre_verify`` for user/plugin policy.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from utils import is_truthy_value

DEFAULT_MAX_VERIFY_NUDGES = 3

# Rubric verify-on-stop (Gap 3, docs/agents-loops-graphs.md): verify-on-stop
# covers code (file mutations + verify evidence); this covers NON-code
# deliverables (research/writing/analysis) with a *mechanical* rubric — a check
# that can actually fail, not the model grading its own homework. Off by default;
# a deployment opts in by naming an active rubric in `agent.verify_rubric`.
DEFAULT_RUBRIC_MAX_NUDGES = 2

# Shipped guidance appended to the verification-stop nudge when code lacks fresh
# verification evidence. Wording mirrors the user-facing "clean your work"
# workflow, but does not create its own extra model turn.
CODING_VERIFY_GUIDANCE = (
    "[Coding] Before you run tests/linters or call this done: if this is "
    "creative UI/visual work, hold off on tests and linters until the user says "
    "they like the result or you're about to commit. And before every commit, "
    "clean your work: keep it KISS/DRY, match the surrounding code style, and be "
    "elitist, shorthand, clever, concise, efficient, and elegant."
)


def max_verify_nudges(config: Optional[dict[str, Any]] = None) -> int:
    """Bound on consecutive ``pre_verify`` continue directives per turn (>= 0)."""
    agent_cfg = _agent_cfg(config)
    raw = agent_cfg.get("max_verify_nudges")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_VERIFY_NUDGES


def coding_verify_guidance(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return the optional guidance appended to verification-stop nudges."""
    if not is_truthy_value(_agent_cfg(config).get("verify_guidance", True), default=True):
        return None
    return CODING_VERIFY_GUIDANCE


def active_verify_rubric(config: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Return the configured active non-code deliverable rubric, or ``None``.

    Selection is by name: ``agent.verify_rubric`` names one entry of the
    ``agent.verify_rubrics`` map. Empty/absent name (the default) disables the
    gate entirely, so the default agent behavior is unchanged.
    """
    cfg = _agent_cfg(config)
    name = cfg.get("verify_rubric")
    if not isinstance(name, str) or not name.strip():
        return None
    rubrics = cfg.get("verify_rubrics")
    if not isinstance(rubrics, dict):
        return None
    rubric = rubrics.get(name.strip())
    return rubric if isinstance(rubric, dict) else None


def _rubric_max_nudges(rubric: dict[str, Any]) -> int:
    raw = rubric.get("max_nudges", DEFAULT_RUBRIC_MAX_NUDGES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RUBRIC_MAX_NUDGES


def unmet_rubric_criteria(text: str, rubric: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the rubric criteria not satisfied by *text*.

    A criterion is a mapping with ``any_of`` (a marker or list of markers); it is
    *met* when at least one marker is present in *text*. Markers are plain
    substrings by default, or regexes when the criterion sets ``regex: true``.
    Matching is case-insensitive unless the rubric sets ``case_insensitive:
    false``. A criterion with no usable markers is skipped (cannot be evaluated),
    never reported as unmet — the gate only fires on checks it can actually run.
    """
    case_insensitive = is_truthy_value(rubric.get("case_insensitive", True), default=True)
    haystack = text.lower() if case_insensitive else text
    unmet: list[dict[str, Any]] = []
    for criterion in rubric.get("criteria") or []:
        if not isinstance(criterion, dict):
            continue
        markers = criterion.get("any_of")
        if isinstance(markers, str):
            markers = [markers]
        if not isinstance(markers, list):
            continue
        use_regex = is_truthy_value(criterion.get("regex", False), default=False)
        usable = False
        present = False
        for marker in markers:
            if not isinstance(marker, str) or not marker:
                continue
            usable = True
            if use_regex:
                try:
                    if re.search(marker, text, re.IGNORECASE if case_insensitive else 0):
                        present = True
                        break
                except re.error:
                    # A bad pattern is a config bug, not a deliverable failure —
                    # skip it rather than blocking the turn forever.
                    usable = False
                    continue
            else:
                needle = marker.lower() if case_insensitive else marker
                if needle in haystack:
                    present = True
                    break
        if usable and not present:
            unmet.append(criterion)
    return unmet


def build_rubric_verify_nudge(
    *,
    final_response: Optional[str],
    rubric: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
    attempt: int = 0,
    coding: Optional[bool] = None,
    changed_paths: Optional[list[str]] = None,
) -> Optional[str]:
    """Return a continue-nudge if a non-code deliverable misses its rubric, else None.

    Returns ``None`` (finish normally) when: no rubric is active (default),
    the turn is a coding turn or mutated files (the code evidence gate owns
    those), the nudge cap is reached, the response is empty, or every criterion
    is satisfied. Otherwise returns a short message listing the unmet criteria so
    the agent completes the deliverable before finishing.
    """
    # Code turns are handled by verify-on-stop / pre_verify — never double-gate.
    if coding:
        return None
    if changed_paths:
        return None
    if rubric is None:
        rubric = active_verify_rubric(config)
    if not rubric:
        return None
    if attempt >= _rubric_max_nudges(rubric):
        return None
    text = final_response or ""
    if not text.strip():
        # Nothing produced yet — forcing content is not this gate's job.
        return None
    unmet = unmet_rubric_criteria(text, rubric)
    if not unmet:
        return None
    intro = rubric.get("nudge_intro")
    if not isinstance(intro, str) or not intro.strip():
        intro = (
            "Before you finish: this deliverable is missing required elements "
            "from its rubric. Add them, then finish."
        )
    lines = [intro]
    for criterion in unmet:
        name = str(criterion.get("name") or "criterion")
        hint = criterion.get("hint")
        hint = hint if isinstance(hint, str) and hint.strip() else f"include {name}"
        lines.append(f"  - {name}: {hint}")
    lines.append(
        "If a criterion genuinely does not apply, say so explicitly in the "
        "deliverable rather than omitting it silently."
    )
    return "\n".join(lines)


def _agent_cfg(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else None
    return agent_cfg if isinstance(agent_cfg, dict) else {}


__all__ = [
    "CODING_VERIFY_GUIDANCE",
    "DEFAULT_MAX_VERIFY_NUDGES",
    "DEFAULT_RUBRIC_MAX_NUDGES",
    "active_verify_rubric",
    "build_rubric_verify_nudge",
    "coding_verify_guidance",
    "max_verify_nudges",
    "unmet_rubric_criteria",
]
