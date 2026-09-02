#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

Supports both single-select (radio) and multi-select (checkbox) modes via the
``multi_select`` parameter, and two option shapes:

1. **Simple** -- provide ``choices`` (up to 4 strings). Auto-appends
   'Other (type your answer)'.
2. **Rich** -- provide ``options`` (up to 25 objects with label, value,
   style, and optional modal forms). The caller controls the full options
   array -- no synthetic 'Other' is appended.

``choices`` and ``options`` are mutually exclusive.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import Any, Callable, Dict, List, Optional


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4

# Maximum number of independent questions in one batch clarify call.
MAX_QUESTIONS = 5

# Canonical timeout sentinel returned to the agent when the user never
# answers. The CLI has always returned this exact text; the batch fallback
# loop also recognises it (alongside ``None``) as "the user walked away",
# which aborts the remaining questions instead of pestering one by one.
TIMEOUT_RESPONSE = (
    "The user did not provide a response within the time limit. "
    "Use your best judgement to make the choice and proceed."
)

# Suffix appended to the first choice so the user can see, at a glance, which
# option the agent actually recommends. Applied here rather than per-surface so
# CLI, TUI, desktop, and messaging adapters all render the same label.
RECOMMENDED_LABEL = "(Recommended)"

# -- Rich-option validation constants ----------------------------------------

MAX_OPTIONS = 25
MAX_LABEL_LEN = 80
MAX_VALUE_LEN = 100
MAX_DESC_LEN = 100
MAX_MODAL_TITLE_LEN = 45
MIN_MODAL_FIELDS = 1
MAX_MODAL_FIELDS = 5
MAX_QUESTION_LEN = 2000

VALID_DISPLAY_TYPES = {"buttons"}
VALID_AUTH_POLICIES = {
    "session_owner_only",
    "any_allowed_user",
    "any_allowed_role",
    "any_allowed_user_or_role",
}
VALID_FIELD_TYPES = {"text", "select", "radio", "checkbox", "file_upload"}
VALID_STYLES = {"primary", "secondary", "success", "danger"}
VALID_ACTIONS = {"return", "modal"}


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


def mark_recommended(choices: List[str]) -> List[str]:
    """Label the first choice as the agent's recommendation.

    The schema tells the model to order ``choices`` best-first, so element 0 is
    always the option it would pick itself. Tagging it here — the one
    platform-agnostic entry point — means every surface (CLI panel, TUI,
    desktop card, Telegram buttons) reads the same way without four copies of
    the same string concatenation, and the label can never drift between them.

    Idempotent: a model that writes its own "(recommended)" into the choice is
    left alone rather than getting the suffix twice. A lone choice isn't a
    recommendation — there's nothing to prefer it over — so single-choice lists
    pass through untouched.
    """
    if len(choices) < 2:
        return choices
    first = str(choices[0]).strip()
    if first != strip_recommended(first):
        return choices
    return [f"{first} {RECOMMENDED_LABEL}"] + list(choices[1:])


def strip_recommended(text: str) -> str:
    """Remove the recommendation label from a resolved answer.

    The user picks the decorated string, but the agent asked about the bare
    option — returning "Rebase onto main (Recommended)" as ``user_response``
    would leak presentation into the answer the model reasons about and into
    anything it echoes back.
    """
    stripped = str(text).strip()
    if stripped.casefold().endswith(RECOMMENDED_LABEL.casefold()):
        return stripped[: -len(RECOMMENDED_LABEL)].strip()
    return stripped


def _invoke_callback(callback, question, choices, multi_select):
    """Invoke the platform callback, passing multi_select if supported.

    Uses signature inspection (not a ``TypeError`` retry) to decide whether
    the callback accepts the ``multi_select`` keyword — a retry-on-TypeError
    approach would re-invoke a *compatible* callback that raised TypeError
    internally, potentially prompting the user twice.
    """
    import inspect

    accepts_multi = False
    try:
        sig = inspect.signature(callback)
        params = sig.parameters
        accepts_multi = "multi_select" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        # Builtins / C callables without introspectable signatures:
        # be conservative and use the legacy 2-arg form.
        accepts_multi = False

    if accepts_multi:
        return callback(question, choices, multi_select=multi_select)
    return callback(question, choices)


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


# =============================================================================
# Batch (multi-question) support — issue #18450
# =============================================================================

def _normalize_questions(questions) -> tuple:
    """Validate and normalize the ``questions`` batch parameter.

    Returns ``(normalized, error)`` where exactly one is non-None, except the
    empty-list case which returns ``(None, None)`` — an empty array is not an
    error, it just means "no batch here" and the caller falls back to the
    single-question path.

    Each normalized entry carries:
      - ``qid``: stable wire id (``q0``..``qN``, index order). Surfaces key
        their per-question answers by this; a model-supplied ``id`` is NOT
        used on the wire (it's unvalidated text) and only echoed in results.
      - ``id``: the model's optional identifier, or None.
      - ``question``: stripped question text.
      - ``choices``: decorated choice list (recommended label applied), or
        None for open-ended.
      - ``choices_offered``: the bare list as offered, for the result JSON.
      - ``multi_select``: honored only when choices exist.
    """
    if not isinstance(questions, list):
        return None, "questions must be an array of question objects."
    if not questions:
        return None, None
    if len(questions) > MAX_QUESTIONS:
        return None, f"questions supports at most {MAX_QUESTIONS} items."

    normalized = []
    for index, item in enumerate(questions):
        if isinstance(item, str):
            # Tolerate bare-string items: LLMs sometimes send ["Q1?", "Q2?"].
            item = {"question": item}
        if not isinstance(item, dict):
            return None, f"questions[{index}] must be an object with a 'question'."

        text = str(item.get("question") or "").strip()
        if not text:
            return None, f"questions[{index}].question must be non-empty text."

        choices = item.get("choices")
        if choices is not None:
            if not isinstance(choices, list):
                return None, f"questions[{index}].choices must be a list."
            choices = [s for s in (_flatten_choice(c) for c in choices) if s]
            if len(choices) > MAX_CHOICES:
                choices = choices[:MAX_CHOICES]
            if not choices:
                choices = None

        model_id = str(item.get("id") or "").strip() or None

        normalized.append({
            "qid": f"q{index}",
            "id": model_id,
            "question": text,
            "choices": mark_recommended(list(choices)) if choices else None,
            "choices_offered": list(choices) if choices else None,
            "multi_select": bool(item.get("multi_select")) and bool(choices),
        })

    return normalized, None


def _callback_accepts_questions(callback) -> bool:
    """True when the platform callback understands the ``questions`` kwarg.

    Same signature-inspection approach as ``_invoke_callback`` (never a
    TypeError retry — that would re-prompt the user on an internal bug).
    """
    import inspect

    try:
        params = inspect.signature(callback).parameters
        return "questions" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        return False


def _clean_batch_answer(entry: dict, raw) -> object:
    """Strip presentation from one locked answer (label, multi-select JSON)."""
    if entry["multi_select"]:
        return [strip_recommended(r) for r in _parse_multi_select_response(raw)]
    return strip_recommended(raw)


def _batch_result(normalized: List[dict], answers: dict, timed_out: bool) -> str:
    """Assemble the batch result JSON from per-qid answers.

    Unanswered questions surface as empty ``user_response`` — with the
    top-level ``timed_out`` flag (present only when true) telling the agent
    whether those blanks are deliberate skips or the user walking away.
    """
    responses = []
    for entry in normalized:
        row = {}
        if entry["id"]:
            row["id"] = entry["id"]
        row["question"] = entry["question"]
        row["choices_offered"] = entry["choices_offered"]
        raw = answers.get(entry["qid"])
        row["user_response"] = _clean_batch_answer(entry, raw) if raw else ""
        responses.append(row)

    result: Dict[str, object] = {"responses": responses}
    if timed_out:
        result["timed_out"] = True
    return json.dumps(result, ensure_ascii=False)


def _run_batch(normalized: List[dict], callback, question: str) -> str:
    """Dispatch a validated batch to the platform callback.

    Batch-capable callbacks (a ``questions`` kwarg, detected by signature)
    get the whole list once and reply with ``{"answers": {qid: raw}}`` plus
    an optional ``timed_out`` flag — as a dict or a JSON string (the
    tui_gateway ``_block`` bridge can only carry strings).

    Legacy callbacks are looped one question at a time (messaging adapters,
    older plugins). An explicit empty answer is a skip and the loop
    continues; a timeout (``None`` or the ``TIMEOUT_RESPONSE`` sentinel)
    means the user walked away, so the loop aborts instead of pestering
    them with the remaining questions. Answers collected before the abort
    are kept either way.
    """
    if _callback_accepts_questions(callback):
        raw = callback(question, None, questions=normalized)

        answers: dict = {}
        timed_out = False
        if raw is None or (isinstance(raw, str) and raw.strip() == TIMEOUT_RESPONSE):
            timed_out = True
        elif isinstance(raw, dict):
            answers = dict(raw.get("answers") or {})
            timed_out = bool(raw.get("timed_out"))
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                answers = dict(parsed.get("answers") or {})
                timed_out = bool(parsed.get("timed_out"))
        # Any other falsy/unparseable reply is a cancel-all: every answer
        # empty, no timeout flag (mirrors the single-question skip).
        return _batch_result(normalized, answers, timed_out)

    answers = {}
    timed_out = False
    for entry in normalized:
        raw = _invoke_callback(
            callback, entry["question"], entry["choices"], entry["multi_select"],
        )
        if raw is None or (isinstance(raw, str) and raw.strip() == TIMEOUT_RESPONSE):
            timed_out = True
            break
        answers[entry["qid"]] = raw
    return _batch_result(normalized, answers, timed_out)


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    multi_select: bool = False,
    questions: Optional[List[dict]] = None,
    options: Optional[List[Dict[str, Any]]] = None,
    display_type: str = "buttons",
    auth_policy: str = "session_owner_only",
    timeout_seconds: Optional[float] = None,
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
        questions:    Up to 5 independent questions asked as one batch
                      (issue #18450). Each item: ``{id?, question, choices?,
                      multi_select?}``. When present (non-empty), the single
                      ``question``/``choices``/``multi_select`` parameters
                      are ignored and the result JSON is ``{"responses":
                      [...]}`` (plus ``"timed_out": true`` when the user
                      stopped answering partway).
        callback:     Platform-provided function that handles the actual UI
                      interaction.  Signature:
                      ``callback(question, choices, multi_select=False) -> str``.
                      Batch-capable platforms additionally accept a
                      ``questions`` keyword and receive the normalized list
                      in one call; platforms without it are looped one
                      question at a time.
                      Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response(s).
    """
    if questions is not None:
        normalized, error = _normalize_questions(questions)
        if error:
            return tool_error(error)
        if normalized:
            if callback is None:
                return tool_error(
                    "Clarify tool is not available in this execution context."
                )
            try:
                return _run_batch(normalized, callback, str(question or "").strip())
            except Exception as exc:
                return tool_error(f"Failed to get user input: {exc}")
        # Empty questions array → fall through to the single-question path.

    if not question or not question.strip():
        return tool_error(
            "No question provided. Pass questions=[{question: '...', "
            "choices?: [...], multi_select?: bool}, ...] — a single question "
            "is a one-entry array."
        )

    question = question.strip()

    # -- Rich options path (mutually exclusive with choices) --
    if len(question) > MAX_QUESTION_LEN:
        return tool_error(
            f"Question text too long ({len(question)} chars, max {MAX_QUESTION_LEN})."
        )
    if options is not None and choices is not None:
        return tool_error("Use either 'choices' (simple) or 'options' (rich), not both.")
    if options is not None:
        err = _validate_options(options)
        if err:
            return tool_error(err)
        if display_type not in VALID_DISPLAY_TYPES:
            return tool_error(
                f"Unsupported display_type '{display_type}'. "
                f"Must be one of {sorted(VALID_DISPLAY_TYPES)}."
            )
        if auth_policy not in VALID_AUTH_POLICIES:
            return tool_error(
                f"Unsupported auth_policy '{auth_policy}'. "
                f"Must be one of {sorted(VALID_AUTH_POLICIES)}."
            )
        if timeout_seconds is not None:
            if not isinstance(timeout_seconds, (int, float)):
                return tool_error("timeout_seconds must be a number.")
            timeout_seconds = max(60, min(3600, int(timeout_seconds)))

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
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
        return tool_error("Clarify tool is not available in this execution context.")

    # The first choice is the agent's pick (the schema says order best-first),
    # so it reaches every surface carrying the "(Recommended)" label. The bare
    # list is what goes back to the agent — the label is presentation only.
    offered = choices
    if choices is not None:
        choices = mark_recommended(choices)

    try:
        if options is not None:
            # Rich path -- delegate to callback with structured params.
            raw_response = callback(
                question, choices=None, options=options,
                display_type=display_type, auth_policy=auth_policy,
                timeout_seconds=timeout_seconds,
            )
            # Rich callbacks return a JSON string (ClarifyResult.to_dict());
            # pass through to avoid double-encoding.
            if isinstance(raw_response, str):
                try:
                    parsed = json.loads(raw_response)
                    if isinstance(parsed, dict) and "status" in parsed:
                        return raw_response
                except (json.JSONDecodeError, ValueError):
                    pass
        else:
            raw_response = _invoke_callback(callback, question, choices, multi_select)
    except Exception as exc:
        return tool_error(f"Failed to get user input: {exc}")

    if multi_select and choices is not None:
        user_response = [strip_recommended(r) for r in _parse_multi_select_response(raw_response)]
    else:
        user_response = strip_recommended(raw_response)

    return json.dumps({
        "question": question,
        "choices_offered": offered,
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
        "Ask the user one or more questions when you need a decision, "
        "clarification, or feedback before proceeding. Pass every question "
        f"in `questions` (1-{MAX_QUESTIONS} entries) — a single question is a "
        "one-entry array, and several INDEPENDENT questions belong in ONE "
        "call (one form beats a chain of clarify calls; if one answer would "
        "change another question, ask separately). Per question: "
        f"single-select (up to {MAX_CHOICES} choices — put your recommended "
        "option FIRST, the UI marks it '(Recommended)' and auto-appends an "
        "'Other' free-text row), multi-select (multi_select=true), or "
        "open-ended (omit choices). Options go ONLY in `choices`, never "
        "enumerated inside the question text (choices render as pickable "
        "rows; options written into the question are dead prose the user "
        "can't click). Result: {responses: [...]} in question order (plus "
        "timed_out=true if the user stopped part-way). Prefer deciding "
        "low-stakes questions yourself; don't use this for dangerous-command "
        "confirmation (the terminal tool handles that). Alternatively, for a "
        "single rich question provide `options` (up to 25 objects with "
        "label, value, style, and optional modal forms) — the caller "
        "controls the full options array and no synthetic 'Other' is "
        "appended. `choices` and `options` are mutually exclusive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    "The question(s). Each: question text (options excluded), "
                    "optional choices (recommended first; omit for free-text), "
                    "optional multi_select. Responses come back in question "
                    "order with the question text echoed."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_CHOICES,
                        },
                        "multi_select": {"type": "boolean"},
                    },
                    "required": ["question"],
                },
            },
            # NOTE: the handler also accepts (unadvertised): a per-question
            # `id` (echoed in the matching response — redundant since rows
            # carry the question text and preserve order), and the legacy
            # single-question shape (`question` + `choices` + `multi_select`
            # at top level; a top-level `question` beside `questions` is the
            # batch form's title). One documented way to call.
            "options": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OPTIONS,
                "description": (
                    "Rich option objects (1-25). Each has at least `label` "
                    "and `value`; may include `description`, `style` "
                    "(primary/secondary/success/danger), `action` "
                    "(return/modal), and `modal` (form spec). Mutually "
                    "exclusive with `choices`."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label":       {"type": "string", "maxLength": MAX_LABEL_LEN},
                        "value":       {"type": "string", "maxLength": MAX_VALUE_LEN},
                        "description": {"type": "string", "maxLength": MAX_DESC_LEN},
                        "style":       {"type": "string", "enum": sorted(VALID_STYLES)},
                        "action":      {"type": "string", "enum": sorted(VALID_ACTIONS)},
                        "modal": {
                            "type": "object",
                            "properties": {
                                "title":  {"type": "string", "maxLength": MAX_MODAL_TITLE_LEN},
                                "fields": {
                                    "type": "array", "minItems": MIN_MODAL_FIELDS, "maxItems": MAX_MODAL_FIELDS,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "key":         {"type": "string"},
                                            "label":       {"type": "string"},
                                            "description": {"type": "string"},
                                            "type":        {"type": "string", "enum": sorted(VALID_FIELD_TYPES)},
                                            "required":    {"type": "boolean"},
                                            "placeholder": {"type": "string"},
                                            "options":     {"type": "array", "items": {"type": "string"}},
                                            "min_length":  {"type": "integer"},
                                            "max_length":  {"type": "integer"},
                                            "multiline":   {"type": "boolean"},
                                            "file_policy": {
                                                "type": "object",
                                                "properties": {
                                                    "max_files":         {"type": "integer", "minimum": 1, "maximum": 10},
                                                    "min_files":         {"type": "integer", "minimum": 0, "maximum": 10},
                                                },
                                            },
                                        },
                                        "required": ["key", "label", "type"],
                                    },
                                },
                            },
                            "required": ["title", "fields"],
                        },
                    },
                    "required": ["label", "value", "action"],
                },
            },
            "display_type":    {"type": "string", "enum": sorted(VALID_DISPLAY_TYPES), "default": "buttons"},
            "auth_policy":     {"type": "string", "enum": sorted(VALID_AUTH_POLICIES), "default": "session_owner_only"},
            "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
        },
        "required": ["questions"],
    },
}


# =============================================================================
# Validation helpers (rich path)
# =============================================================================

def _validate_options(options: list) -> Optional[str]:
    """Validate the rich ``options`` array.

    Returns an error message string on failure, or ``None`` if valid.
    """
    if not options or not isinstance(options, list):
        return "options must be a non-empty list of option objects."

    if len(options) > MAX_OPTIONS:
        return f"Too many options ({len(options)}). Maximum is {MAX_OPTIONS}."

    for idx, opt in enumerate(options):
        if not isinstance(opt, dict):
            return f"Option {idx} must be a dict."

        label = opt.get("label")
        value = opt.get("value")

        if not label or not isinstance(label, str) or not label.strip():
            return f"Option {idx} is missing a non-empty 'label'."
        if not value or not isinstance(value, str) or not value.strip():
            return f"Option {idx} is missing a non-empty 'value'."

        if len(label) > MAX_LABEL_LEN:
            return f"Option {idx} label exceeds {MAX_LABEL_LEN} characters ({len(label)})."
        if len(value) > MAX_VALUE_LEN:
            return f"Option {idx} value exceeds {MAX_VALUE_LEN} characters ({len(value)})."

        desc = opt.get("description")
        if desc is not None and len(str(desc)) > MAX_DESC_LEN:
            return f"Option {idx} description exceeds {MAX_DESC_LEN} characters ({len(str(desc))})."

        style = opt.get("style", "secondary")
        if style not in VALID_STYLES:
            return f"Option {idx} has invalid style '{style}'. Must be one of {sorted(VALID_STYLES)}."

        action = opt.get("action", "return")
        if action not in VALID_ACTIONS:
            return f"Option {idx} has invalid action '{action}'. Must be one of {sorted(VALID_ACTIONS)}."

        if action == "modal":
            modal = opt.get("modal")
            if not isinstance(modal, dict):
                return f"Option {idx} has action='modal' but no valid 'modal' object."
            title = modal.get("title")
            if not title or not isinstance(title, str) or not title.strip():
                return f"Option {idx} modal is missing a non-empty 'title'."
            if len(title) > MAX_MODAL_TITLE_LEN:
                return f"Option {idx} modal title exceeds {MAX_MODAL_TITLE_LEN} characters ({len(title)})."
            fields = modal.get("fields")
            if not isinstance(fields, list):
                return f"Option {idx} modal 'fields' must be a list."
            if len(fields) < MIN_MODAL_FIELDS or len(fields) > MAX_MODAL_FIELDS:
                return (
                    f"Option {idx} modal must have {MIN_MODAL_FIELDS}-"
                    f"{MAX_MODAL_FIELDS} fields (got {len(fields)})."
                )

            seen_keys: set = set()
            for fi, fld in enumerate(fields):
                if not isinstance(fld, dict):
                    return f"Option {idx} modal field {fi} must be a dict."
                key = fld.get("key", "")
                if not key or not isinstance(key, str) or not key.strip():
                    return f"Option {idx} modal field {fi} is missing a non-empty 'key'."
                if key in seen_keys:
                    return f"Option {idx} modal field {fi} has duplicate key '{key}'."
                seen_keys.add(key)
                lbl = fld.get("label")
                if not lbl or not isinstance(lbl, str) or not lbl.strip():
                    return f"Option {idx} modal field {fi} is missing a non-empty 'label'."
                field_type = fld.get("type", "text")
                if field_type not in VALID_FIELD_TYPES:
                    return (
                        f"Option {idx} modal field {fi} has invalid type "
                        f"'{field_type}'. Must be one of {sorted(VALID_FIELD_TYPES)}."
                    )

    return None


# =============================================================================
# Tool handler
# =============================================================================

# Note: rich-options support (options/display_type/auth_policy/timeout_seconds)
# is merged into clarify_tool above -- validated then dispatched through the
# same callback contract, keeping one canonical function.


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# Registry
# =============================================================================

from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        multi_select=args.get("multi_select", False),
        questions=args.get("questions"),
        options=args.get("options"),
        display_type=args.get("display_type", "buttons"),
        auth_policy=args.get("auth_policy", "session_owner_only"),
        timeout_seconds=args.get("timeout_seconds"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
