#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

Supports both single-select (radio) and multi-select (checkbox) modes via the
``multi_select`` parameter.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
import re
from typing import List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


_AUTONOMY_PROCEED_RESPONSE = (
    "[Autonomy policy: this is a routine engineering decision, not a user "
    "decision. Proceed now using your best technical judgment. Prefer the "
    "safest reversible approach, run the relevant checks, and report the "
    "result. Do not ask for another micro-approval.]"
)

_SAFE_ARTIFACT_QUALIFIER = (
    r"(?:local|failing|error|contact|email|form|validation|api|http|"
    r"post|get|put|patch|delete)"
)
_SAFE_INSPECTABLE_ARTIFACT = (
    rf"(?:{_SAFE_ARTIFACT_QUALIFIER}\s+){{0,3}}"
    r"(?:repository|repo|codebase|code|patch|diff|logs?|workspace|"
    r"tests?|failures?|message|request|form|helper|module|function|validation)"
)
_SAFE_LOCAL_ACTION = (
    r"(?:"
    r"(?:inspect|review|check|analy[sz]e|diagnose)\s+(?:the\s+)?"
    rf"{_SAFE_INSPECTABLE_ARTIFACT}"
    r"|(?:run|rerun|re-run|execute)\s+(?:the\s+)?"
    r"(?:(?:focused|relevant|unit|integration|e2e|full)\s+)?"
    r"(?:tests?|pytest|vitest|lint|ruff|typecheck|type check|build)"
    r"|(?:test|validate)\s+(?:the\s+)?"
    rf"(?:{_SAFE_ARTIFACT_QUALIFIER}\s+){{0,3}}"
    r"(?:code|patch|helper|module|function|form|request|validation)"
    r"(?:\s+locally)?"
    r"|(?:apply|prepare|implement|make)\s+(?:the\s+|a\s+)?"
    r"local\s+(?:patch|change|fix|edit)"
    r"|(?:fix|update|edit|refactor)\s+(?:the\s+)?local\s+"
    r"(?:code|patch|implementation|tests?)"
    r")"
)
_SAFE_ACTION_SEPARATOR = r"(?:\s*(?:,?\s+and(?:\s+then)?|,?\s+then|,)\s*)"
_ROUTINE_LOCAL_PROMPT_PATTERNS = (
    re.compile(
        rf"^(?:(?:should|can|may|shall)\s+i|"
        rf"(?:would you like|do you want)\s+me\s+to)\s+"
        rf"{_SAFE_LOCAL_ACTION}"
        rf"(?:{_SAFE_ACTION_SEPARATOR}{_SAFE_LOCAL_ACTION})*"
        r"(?:\s+without\s+(?:push|deploy|push or deploy|push and deploy|"
        r"push/deploy))?\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^should i implement and test the local patch "
        r"without push or deploy\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:which|what)\s+(?:technical\s+)?"
        r"(?:implementation|architecture|algorithm|library|framework|"
        r"dependency|approach|option)\s+should\s+i\s+"
        r"(?:choose|use|select)\s+(?:for\s+)?(?:the\s+)?"
        r"(?:local\s+)?(?:code|patch|implementation)\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^ко(?:я|й|е)\s+(?:техническ\w+|архитектурн\w+|"
        r"алгоритъм|библиотек\w+|framework\w*)\s+"
        r"(?:реализация|подход|вариант)?\s*да\s+"
        r"(?:избера|използвам)\s+(?:за\s+)?(?:локалн\w+\s+)?"
        r"(?:код|кода|пач|реализация)\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^да\s+(?:пусна|изпълня)\s+ли\s+"
        r"(?:(?:фокусираните|нужните|релевантните)\s+)?"
        r"(?:тестове|pytest|lint|typecheck|build)\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^да\s+(?:проверя|прегледам|анализирам)\s+ли\s+"
        r"(?:локалн\w+\s+)?"
        r"(?:репозитори[яй]|репото|код|кода|пача|diff-а|логовете|"
        r"грешката|съобщението за грешка)\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^да\s+(?:проверя|прегледам|анализирам)\s+"
        r"(?:локалн\w+\s+)?"
        r"(?:репозитори[яй]|репото|код|кода|пача|diff-а|логовете|"
        r"грешката|съобщението за грешка)\s+и\s+да\s+"
        r"(?:пусна|изпълня)\s+"
        r"(?:(?:фокусираните|нужните|релевантните)\s+)?"
        r"(?:тестове|тестовете|pytest|lint|typecheck|build)\s*\?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^да\s+(?:приложа|направя|подготвя|имплементирам)\s+ли\s+"
        r"локалн\w+\s+(?:пач|промяна|поправка)\s*\?$",
        re.IGNORECASE,
    ),
)
_SAFE_AUTONOMY_CHOICE = re.compile(
    r"^(?:proceed|continue|go ahead|stop|cancel|"
    r"run(?: them| it)?|skip(?: them| it)?|yes|no|"
    r"продължи|действай|спри|откажи|да|не|"
    r"изпълни|пропусни)$",
    re.IGNORECASE,
)


def _flatten_policy_choice(choice) -> str:
    """Return a string choice for authorization checks.

    The public tool schema only permits strings. Structured choices are still
    normalized for display compatibility, but are never eligible for silent
    authorization: nested metadata can carry an external action that is not
    visible in the rendered label.
    """
    if not isinstance(choice, str):
        return ""
    return choice.strip()


def autonomy_clarify_response(
    question: str,
    choices: Optional[List[str]] = None,
    *,
    policy: str = "interactive",
    multi_select: bool = False,
) -> Optional[str]:
    """Auto-resolve routine engineering prompts under ``blockers_only``.

    Questions that require user-only information, business judgment, or
    authorization for an external/high-impact action remain interactive.
    """
    if str(policy or "").strip().lower() != "blockers_only":
        return None
    if multi_select:
        # The canned response is an instruction to the model, not one or more
        # selected values. Parsing it as a multi-select answer would split the
        # prose on commas and corrupt the tool result.
        return None

    normalized = " ".join(str(question or "").casefold().split())
    if not normalized:
        return None

    raw_choices = choices or []
    # Treat the schema as part of the authorization boundary. Do not silently
    # authorize malformed or over-limit choices even though the display layer
    # remains lenient for compatibility.
    if len(raw_choices) > MAX_CHOICES:
        return None
    if any(not isinstance(choice, str) for choice in raw_choices):
        return None

    flattened_choices = [
        value
        for value in (
            _flatten_policy_choice(choice).casefold() for choice in raw_choices
        )
        if value
    ]
    # This is an authorization boundary, not a general natural-language
    # classifier. Only prompts whose ENTIRE question matches a known local,
    # reversible grammar are eligible. Every supplied choice must also be a
    # generic proceed/stop or opaque variant label; descriptive/unclassified
    # choices stay interactive. This positive/full-text contract avoids
    # fail-open behavior such as "run tests and upload to S3".
    if not any(
        pattern.fullmatch(normalized)
        for pattern in _ROUTINE_LOCAL_PROMPT_PATTERNS
    ):
        return None
    if flattened_choices and not all(
        _SAFE_AUTONOMY_CHOICE.fullmatch(choice)
        for choice in flattened_choices
    ):
        return None
    return _AUTONOMY_PROCEED_RESPONSE


def _invoke_callback(
    callback,
    question,
    choices,
    multi_select,
    *,
    raw_choices=None,
):
    """Invoke the platform callback, passing multi_select if supported.

    Uses signature inspection (not a ``TypeError`` retry) to decide whether
    the callback accepts the ``multi_select`` keyword — a retry-on-TypeError
    approach would re-invoke a *compatible* callback that raised TypeError
    internally, potentially prompting the user twice.
    """
    import inspect

    accepts_multi = False
    accepts_raw_choices = False
    try:
        sig = inspect.signature(callback)
        params = sig.parameters
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        accepts_multi = "multi_select" in params or accepts_kwargs
        accepts_raw_choices = "raw_choices" in params or accepts_kwargs
    except (TypeError, ValueError):
        # Builtins / C callables without introspectable signatures:
        # be conservative and use the legacy 2-arg form.
        accepts_multi = False
        accepts_raw_choices = False

    kwargs = {}
    if accepts_multi:
        kwargs["multi_select"] = multi_select
    if accepts_raw_choices:
        # Security classifiers need the unflattened choice object so hidden
        # dict metadata cannot disappear behind a benign display label.
        kwargs["raw_choices"] = raw_choices
    return callback(question, choices, **kwargs)


def _parse_multi_select_response(raw_response) -> List[str]:
    """Parse a multi-select response into a list of cleaned choice strings.

    Handles three forms:
      - Already a list  →  stringify + strip each element
      - JSON array      →  parse and strip
      - Comma-separated →  split, strip, drop empties
    """
    if isinstance(raw_response, list):
        return [str(r).strip() for r in raw_response if str(r).strip()]

    raw = str(raw_response).strip()

    # Try JSON array
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except json.JSONDecodeError:
            pass

    # Fall back to comma-separated
    return [s.strip() for s in raw.split(",") if s.strip()]


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    multi_select: bool = False,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question:     The question text to present.
        choices:      Up to 4 predefined answer choices. When omitted the
                      question is purely open-ended.
        multi_select: When True, the user can select multiple choices
                      (checkboxes).  The ``user_response`` in the output JSON
                      will be a list of strings instead of a single string.
                      Has no effect when ``choices`` is omitted.
        callback:     Platform-provided function that handles the actual UI
                      interaction.  Signature:
                      ``callback(question, choices, multi_select=False) -> str``.
                      The optional ``multi_select`` keyword is passed so the
                      platform can render checkboxes instead of radio buttons.
                      Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    raw_choices = None
    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # Keep the complete original list for the policy callback. Display
        # normalization may trim to MAX_CHOICES, but authorization must see
        # every supplied option so an unsafe tail item cannot disappear.
        raw_choices = list(choices)
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        raw_response = _invoke_callback(
            callback,
            question,
            choices,
            multi_select,
            raw_choices=raw_choices,
        )
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    if multi_select and choices is not None:
        user_response = _parse_multi_select_response(raw_response)
    else:
        user_response = str(raw_response).strip()

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": user_response,
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports three modes:\n\n"
        "1. **Single-select multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Multi-select multiple choice** — set multi_select=true. The user can select "
        "multiple options via checkboxes. user_response will be a list of selected choices.\n"
        "3. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes. NEVER use "
        "this tool to ask permission for in-scope read-only discovery or "
        "dependency preflight. When the user has asked you to implement, change, "
        "build, or fix something, also do not ask permission for the local code "
        "edits, tests, lint, typecheck, build, or ordinary engineering choices "
        "needed within that requested scope. For answer, explanation, analysis, "
        "diagnosis, review, planning, or status requests, inspect and report "
        "without making edits unless the user also asked for a change."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
            "multi_select": {
                "type": "boolean",
                "description": (
                    "When true, the user can select MULTIPLE options (like checkboxes). "
                    "The user_response will be a list of selected choices. "
                    "When false (default), single selection (radio). "
                    "Has no effect when choices is omitted (open-ended question)."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        multi_select=args.get("multi_select", False),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
