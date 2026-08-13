"""The premium review that runs only after the deterministic gate passes.

The deterministic half (:mod:`jobflow_quality.qc`) settles what a fact check
can: a fabricated surname, an unfilled ``[Company Name]``, a PDF older than the
markdown it was rendered from. What it cannot settle is a resume bullet
asserting something the master resume never says — the claim most likely to
reach an employer and the hardest to walk back.

Ordering is the same economy the rest of this workstream uses: cheap
deterministic checks first, and the expensive review never runs on a package
that already failed them. Measured 2026-08-13, a review costs ~14.2k input
tokens — $0.0072 on ``deepseek-v4-pro``, $0.107 on ``gpt-5.6-sol``.

Everything here except the model call is pure. ``build_qc_prompt`` and
``parse_qc_response`` are unit-tested; the call itself is an injected
``invoke`` seam. That keeps the expensive part small and the tested part real.

**A review that did not happen is `unknown`, never `pass`.** A timeout, a
malformed response, a refusal, an empty body — every one of them returns
UNKNOWN. Treating any of those as "no problems found" is exactly how an
unreviewed document would acquire an approval, and the whole point of this
module is that it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Callable

# Bounded categories. A model is free to write prose; only these survive.
_CATEGORIES = frozenset({
    "unsupported_claim",      # asserts something the master resume does not support
    "strategic_mismatch",     # positioning contradicts the brief or the role
    "missing_required_content",
})
_OTHER = "other"

# Per-source budgets, in characters. The master resume and the artifacts are
# the evidence; the JD is context and is cut hardest.
_LIMITS = {
    "master_resume": 24_000,
    "job_description": 12_000,
    "resume": 16_000,
    "cover_letter": 8_000,
}
_MAX_DETAIL_CHARS = 500

_INSTRUCTIONS = """\
You are reviewing a tailored job application before it is sent to an employer.

The MASTER RESUME is the only source of candidate facts. A statement in the
resume or cover letter that the master resume does not support is an
unsupported claim, however plausible it sounds — do not resolve it in the
candidate's favour.

Report only what you can point at. Return JSON and nothing else:

{"findings": [{"category": "...", "artifact": "resume.md", "detail": "..."}]}

category is one of: unsupported_claim, strategic_mismatch,
missing_required_content. artifact is the file the finding is in. detail is one
sentence naming the specific text at issue.

An application with no problems returns {"findings": []}.
"""


class SemanticStatus(str, Enum):
    PASS = "pass"
    FINDINGS = "findings"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SemanticFinding:
    category: str
    artifact: str
    detail: str


@dataclass(frozen=True)
class SemanticResult:
    status: SemanticStatus
    findings: tuple[SemanticFinding, ...] = ()
    reason: str = ""


def _section(title: str, body: Any, limit: int) -> str:
    """One labelled section. Absence is stated, never left blank.

    A blank section reads to a model as "nothing to flag here"; saying the
    source is unavailable keeps a missing input from becoming a silent pass.
    """
    if not isinstance(body, str) or not body.strip():
        return f"## {title}\n(not available)\n"
    text = body.strip()
    if len(text) > limit:
        text = text[:limit] + "\n… [truncated]"
    return f"## {title}\n{text}\n"


def build_qc_prompt(
    *,
    master_resume: str,
    job_description: str,
    resume: str,
    cover_letter: str,
) -> str:
    """Assemble the review prompt. Deterministic and bounded."""
    return "\n".join((
        _INSTRUCTIONS,
        _section("MASTER RESUME (the only source of candidate facts)",
                 master_resume, _LIMITS["master_resume"]),
        _section("JOB DESCRIPTION", job_description, _LIMITS["job_description"]),
        _section("GENERATED resume.md", resume, _LIMITS["resume"]),
        _section("GENERATED cover-letter.md", cover_letter, _LIMITS["cover_letter"]),
    ))


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _normalise_category(raw: Any) -> str:
    if not isinstance(raw, str):
        return _OTHER
    slug = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return slug if slug in _CATEGORIES else _OTHER


def parse_qc_response(raw: Any) -> SemanticResult:
    """Read a model response, resolving every ambiguity to UNKNOWN.

    Models wrap JSON in prose and code fences, so the first ``{...}`` span is
    extracted rather than requiring a bare document. Anything that does not
    yield a ``findings`` list is unknown — including ``[]``, which is a list
    where an object was asked for and cannot be distinguished from a truncated
    reply.
    """
    if not isinstance(raw, str) or not raw.strip():
        return SemanticResult(SemanticStatus.UNKNOWN, reason="empty_response")

    match = _JSON_BLOCK.search(raw)
    if not match:
        return SemanticResult(SemanticStatus.UNKNOWN, reason="no_json_object")

    try:
        payload = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return SemanticResult(SemanticStatus.UNKNOWN, reason="unparseable_json")

    if not isinstance(payload, dict) or "findings" not in payload:
        return SemanticResult(SemanticStatus.UNKNOWN, reason="unexpected_shape")

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        return SemanticResult(SemanticStatus.UNKNOWN, reason="findings_not_a_list")

    findings = tuple(
        SemanticFinding(
            category=_normalise_category(entry.get("category")),
            artifact=str(entry.get("artifact") or "unknown")[:120],
            detail=str(entry.get("detail") or "")[:_MAX_DETAIL_CHARS],
        )
        for entry in raw_findings
        if isinstance(entry, dict)
    )
    status = SemanticStatus.PASS if not findings else SemanticStatus.FINDINGS
    return SemanticResult(status, findings)


def review(
    *,
    master_resume: str,
    job_description: str,
    resume: str,
    cover_letter: str,
    invoke: Callable[[str], Any],
) -> SemanticResult:
    """Run one review. ``invoke`` takes the prompt and returns the raw text.

    Any exception from ``invoke`` becomes UNKNOWN carrying only the exception
    class — a provider message could quote the documents back, and findings
    travel in mailbox messages.
    """
    prompt = build_qc_prompt(
        master_resume=master_resume,
        job_description=job_description,
        resume=resume,
        cover_letter=cover_letter,
    )
    try:
        raw = invoke(prompt)
    except Exception as exc:
        return SemanticResult(SemanticStatus.UNKNOWN, reason=type(exc).__name__)
    return parse_qc_response(raw)
