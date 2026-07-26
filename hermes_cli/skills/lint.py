"""Strip routing intent from skill bodies at write time.

Skills describe what to do. Provider, model, mode, cost, and transport
selection belongs to the routing doctrine, so those directives are replaced
with explicit markers before a ``SKILL.md`` reaches disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class LintFinding:
    """One forbidden routing-intent match in the original skill body."""

    category: str
    pattern_label: str
    matched_text: str
    line_number: int
    replacement: str


@dataclass(frozen=True)
class LintResult:
    original_body: str
    linted_body: str
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def any_strips(self) -> bool:
        return bool(self.findings)


MODEL_SLUG_PATTERNS: list[tuple[str, str]] = [
    (r"openai/gpt-5\.6-[a-z]+", "openai model slug"),
    (r"openai-codex", "chatgpt pro bridge slug"),
    (
        r"anthropic/claude-(opus|sonnet|haiku)-[0-9.]+",
        "anthropic model slug",
    ),
    (r"google/gemini-[0-9.]+(?:-[a-z]+)?", "google model slug"),
    (r"moonshotai/kimi-k[0-9]+", "moonshot model slug"),
    (r"z-ai/glm-[0-9.]+", "zhipu model slug"),
    (r"zhipu/glm-[0-9.]+", "zhipu model slug (legacy)"),
    (r"deepseek/deepseek-v[0-9]+", "deepseek model slug"),
    (r"openrouter/fusion", "fusion slug"),
    (r"meta-llama/[a-z0-9-]+", "meta model slug"),
    (r"mistralai/[a-z0-9-]+", "mistral model slug"),
]

PROVIDER_DIRECTIVE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(?:use|call|route(?:\s+to)?|via|through)\s+"
        r"(openrouter|anthropic\s+direct|openai\s+direct|chatgpt\s+pro)\b",
        "provider directive",
    ),
    (
        r"\b(?:use|call|route(?:\s+to)?|via|through)\s+"
        r"(opus|sonnet|sol|luna|terra|kimi|glm|gemini|fusion)\b",
        "model family directive",
    ),
]

MODE_DIRECTIVE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\bmode\s*:\s*(single|single_with_critic|moa|panel|fusion)\b",
        "mode directive",
    ),
    (
        r"\b(?:use|apply|switch\s+to)\s+"
        r"(mixture[-\s]of[-\s]agents|moa|fusion|critic\s+mode)\b",
        "mode directive",
    ),
    # Include digits after the rung prefix because the required acceptance
    # case is ``r4_opus5_single``.
    (r"\brung\s*:\s*r[0-9]+_[a-z0-9_]+\b", "rung directive"),
]

COST_DIRECTIVE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(?:spend|budget|cap)\s+(?:up\s+to\s+)?"
        r"(?:AUD|USD|\$)\s*[0-9]+(?:\.[0-9]+)?\b",
        "cost directive",
    ),
    (
        r"\bescalate(?:\s+to)?\s+"
        r"(opus|sonnet|sol|fusion|higher\s+tier|frontier)\b",
        "escalation directive",
    ),
]

TRANSPORT_DIRECTIVE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(?:gateway|transport)\s*:\s*"
        r"(openrouter|anthropic_direct|openai_direct)\b",
        "transport pin",
    ),
    (r":nitro\b", "nitro mode pin"),
]

ALL_PATTERN_GROUPS = [
    ("model_slug", MODEL_SLUG_PATTERNS),
    ("provider_directive", PROVIDER_DIRECTIVE_PATTERNS),
    ("mode_directive", MODE_DIRECTIVE_PATTERNS),
    ("cost_directive", COST_DIRECTIVE_PATTERNS),
    ("transport_pin", TRANSPORT_DIRECTIVE_PATTERNS),
]


@dataclass(frozen=True)
class _Pattern:
    category: str
    label: str
    expression: re.Pattern[str]


_PATTERNS = tuple(
    _Pattern(category, label, re.compile(expression, re.IGNORECASE))
    for category, patterns in ALL_PATTERN_GROUPS
    for expression, label in patterns
)

_LINT_NOTE_RE = re.compile(
    r"^> \*\*Lint note \(CS-11a\):\*\* This skill had routing intent stripped "
    r"at write time\..*$",
    re.MULTILINE,
)
_PITFALLS_HEADER_RE = re.compile(
    r"^##[ \t]+Pitfalls[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_skill_document(content: str) -> tuple[str, str]:
    """Return ``(frontmatter_prefix, body)`` without changing either string.

    The prefix includes the opening delimiter, the entire YAML payload, the
    closing delimiter, and its trailing newline when present. Files without
    valid leading frontmatter are treated as body-only input.
    """

    opening = re.match(r"\A\ufeff?---[ \t]*(?:\r\n|\n|\r)", content)
    if opening is None:
        return "", content
    closing = re.search(
        r"^---[ \t]*(?:\r\n|\n|\r|$)",
        content[opening.end() :],
        re.MULTILINE,
    )
    if closing is None:
        return "", content
    end = opening.end() + closing.end()
    return content[:end], content[end:]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _collect_matches(body: str) -> list[tuple[int, int, _Pattern, re.Match[str]]]:
    candidates: list[tuple[int, int, _Pattern, re.Match[str]]] = []
    for pattern in _PATTERNS:
        for match in pattern.expression.finditer(body):
            candidates.append((match.start(), match.end(), pattern, match))

    # Longest match wins when categories overlap at the same position. Other
    # overlaps are skipped so replacements are deterministic and never nested.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, _Pattern, re.Match[str]]] = []
    occupied_until = -1
    for candidate in candidates:
        start, end, _pattern_def, _match = candidate
        if start < occupied_until:
            continue
        accepted.append(candidate)
        occupied_until = end
    return accepted


def _lint_note(categories: Iterable[str]) -> str:
    ordered = sorted(set(categories))
    return (
        "> **Lint note (CS-11a):** This skill had routing intent stripped at "
        f"write time. Categories: {', '.join(ordered)}. Skills describe *what* "
        "to do; the routing doctrine decides *what runs it*. If you need a "
        "specific model or mode, edit `~/.hermes/routing-doctrine.yaml`, not "
        "this skill."
    )


def _append_pitfalls_note(body: str, categories: Iterable[str]) -> str:
    note = _lint_note(categories)
    prior_notes = list(_LINT_NOTE_RE.finditer(body))
    if prior_notes:
        pieces: list[str] = []
        cursor = 0
        for index, match in enumerate(prior_notes):
            pieces.append(body[cursor : match.start()])
            if index == 0:
                pieces.append(note)
            cursor = match.end()
        pieces.append(body[cursor:])
        return "".join(pieces)

    header = _PITFALLS_HEADER_RE.search(body)
    if header is not None:
        newline = "\r\n" if "\r\n" in body else "\n"
        return body[: header.end()] + newline + newline + note + body[header.end() :]

    newline = "\r\n" if "\r\n" in body else "\n"
    separator = "" if not body or body.endswith(("\n", "\r")) else newline
    return (
        body
        + separator
        + (newline if body else "")
        + "## Pitfalls"
        + newline
        + newline
        + note
        + newline
    )


def lint_skill_body(body: str) -> LintResult:
    """Strip forbidden routing intent and return the cleaned body plus findings."""

    matches = _collect_matches(body)
    if not matches:
        return LintResult(original_body=body, linted_body=body)

    findings: list[LintFinding] = []
    pieces: list[str] = []
    cursor = 0
    for start, end, pattern, match in matches:
        replacement = f"[STRIPPED: {pattern.category}]"
        pieces.append(body[cursor:start])
        pieces.append(replacement)
        findings.append(
            LintFinding(
                category=pattern.category,
                pattern_label=pattern.label,
                matched_text=match.group(0),
                line_number=_line_number(body, start),
                replacement=replacement,
            )
        )
        cursor = end
    pieces.append(body[cursor:])
    cleaned = "".join(pieces)
    cleaned = _append_pitfalls_note(cleaned, (item.category for item in findings))
    return LintResult(
        original_body=body,
        linted_body=cleaned,
        findings=findings,
    )


# Compatibility alias for callers written during the initial CS-11a rollout.
Finding = LintFinding

__all__ = [
    "ALL_PATTERN_GROUPS",
    "COST_DIRECTIVE_PATTERNS",
    "Finding",
    "LintFinding",
    "LintResult",
    "MODEL_SLUG_PATTERNS",
    "MODE_DIRECTIVE_PATTERNS",
    "PROVIDER_DIRECTIVE_PATTERNS",
    "TRANSPORT_DIRECTIVE_PATTERNS",
    "lint_skill_body",
    "split_skill_document",
]
