"""Model routing policy guardrails for sensitive scheduled work.

This module is intentionally small and dependency-free so cron job creation and
updates can fail closed before a privileged workload is persisted with an unsafe
provider/model override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


PRIVILEGED_WORKLOAD_TAGS = {"legal", "finance"}

# Short-retention / no-extra-logging routes allowed for privileged workloads.
# Keep these exact tuples conservative; expand only when a provider's retention
# posture has been reviewed.
PRIVILEGED_MODEL_ALLOWLIST: set[tuple[str, str]] = {
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("nvidia", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("ollama", "local"),
    ("local", "local"),
}

# Explicit denylist for known prompt-logging / unsuitable routes. Matching is
# substring-based after normalization because OpenRouter model identifiers can
# include prefixes/suffixes and config aliases.
PRIVILEGED_MODEL_DENYLIST = {
    "openrouter/owl-alpha",
    "owl-alpha",
}

LEGAL_KEYWORDS = {
    "legal",
    "zheng",
    "hhs",
    "cbca",
    "settlement",
    "mediation",
    "privileged",
    "attorney",
    "counsel",
}

FINANCE_KEYWORDS = {
    "finance",
    "financial",
    "accounting",
    "quickbooks",
    "qbo",
    "books",
    "reconciliation",
    "reconcile",
    "journal entry",
    "je",
    "ustc",
    "usti",
    "jhj",
    "pii",
}

LEGAL_SKILLS = {
    "legal",
    "zheng",
    "hhs",
}

FINANCE_SKILLS = {
    "accounting-department",
    "quickbooks automation",
    "quickbooks-automation",
    "excel-author",
    "3-statement-model",
    "dcf-model",
}

# Keywords that are acronyms rather than words. These must match as whole
# tokens: a plain substring scan for "je" (journal entry) fires on "project",
# "subject", "rejected" and "Jenkins", which would tag most ordinary cron jobs
# as finance work and — once the route check runs — refuse any of them that
# pins a model. Acronyms don't take suffixes, so both edges are anchored.
ACRONYM_KEYWORDS = {"je", "qbo", "pii", "ustc", "usti", "jhj", "hhs", "cbca"}


def _keyword_pattern(keywords: Iterable[str]) -> re.Pattern[str]:
    """Compile *keywords* into one alternation anchored at a token start.

    Ordinary keywords are anchored at the start only, so inflections still
    match ("reconciliation" -> "reconciliations") while a keyword buried
    inside an unrelated word does not ("books" must not fire on
    "notebooks"). Acronyms are anchored at both ends — see
    ``ACRONYM_KEYWORDS``.

    Longest-first so an alternation never settles for a shorter prefix.
    """
    parts = []
    for keyword in sorted(keywords, key=len, reverse=True):
        suffix = r"(?!\w)" if keyword in ACRONYM_KEYWORDS else ""
        parts.append(re.escape(keyword) + suffix)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")")


_LEGAL_PATTERN = _keyword_pattern(LEGAL_KEYWORDS)
_FINANCE_PATTERN = _keyword_pattern(FINANCE_KEYWORDS)


@dataclass(frozen=True)
class ModelPolicyDecision:
    allowed: bool
    tags: tuple[str, ...]
    reason: str = ""


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _iter_text_fields(job: dict[str, Any]) -> Iterable[str]:
    for key in ("name", "prompt", "workdir", "profile"):
        value = job.get(key)
        if value:
            yield str(value)

    skills = job.get("skills") or []
    if isinstance(skills, str):
        yield skills
    else:
        for skill in skills:
            if skill:
                yield str(skill)

    skill = job.get("skill")
    if skill:
        yield str(skill)


def detect_privileged_workload_tags(job: dict[str, Any]) -> tuple[str, ...]:
    """Return privileged tags inferred from cron job metadata.

    The detector intentionally errs on the side of tagging legal/finance jobs:
    false positives merely restrict model choices, while false negatives can leak
    privileged prompts to unsafe providers.
    """
    haystack = "\n".join(_iter_text_fields(job)).lower()
    skills = job.get("skills") or []
    if isinstance(skills, str):
        skill_values = {_norm(skills)}
    else:
        skill_values = {_norm(s) for s in skills if s}
    if job.get("skill"):
        skill_values.add(_norm(job.get("skill")))

    tags: set[str] = set()
    if _LEGAL_PATTERN.search(haystack) or skill_values.intersection(LEGAL_SKILLS):
        tags.add("legal")
    if _FINANCE_PATTERN.search(haystack) or skill_values.intersection(FINANCE_SKILLS):
        tags.add("finance")
    return tuple(sorted(tags))


def _has_explicit_model_route(job: dict[str, Any]) -> bool:
    return bool(_norm(job.get("model")) or _norm(job.get("provider")) or _norm(job.get("base_url")))


def validate_privileged_model_route(job: dict[str, Any]) -> ModelPolicyDecision:
    """Validate a cron job's explicit model route against privileged policy.

    Jobs with no privileged tags are unaffected. Privileged no-agent jobs are
    unaffected because they do not send prompts to an LLM. Privileged agent jobs
    without explicit overrides are allowed to inherit the deployment default;
    explicit overrides must be allowlisted and never denylisted.
    """
    tags = detect_privileged_workload_tags(job)
    if not tags:
        return ModelPolicyDecision(True, ())
    if bool(job.get("no_agent")):
        return ModelPolicyDecision(True, tags)
    if not _has_explicit_model_route(job):
        return ModelPolicyDecision(True, tags)

    provider = _norm(job.get("provider"))
    model = _norm(job.get("model"))
    route = f"{provider}/{model}" if provider else model
    base_url = _norm(job.get("base_url"))

    if any(denied in route or denied in model or denied in provider for denied in PRIVILEGED_MODEL_DENYLIST):
        return ModelPolicyDecision(
            False,
            tags,
            f"privileged cron job ({', '.join(tags)}) cannot use denied model route '{route or base_url}'",
        )

    # base_url custom endpoints are not considered safe unless paired with an
    # explicit allowlisted provider/model tuple. This is fail-closed by design.
    if (provider, model) not in PRIVILEGED_MODEL_ALLOWLIST:
        allowed = ", ".join(f"{p}/{m}" for p, m in sorted(PRIVILEGED_MODEL_ALLOWLIST))
        return ModelPolicyDecision(
            False,
            tags,
            "privileged cron job "
            f"({', '.join(tags)}) model route '{route or base_url or '<unset>'}' is not allowlisted; "
            f"allowed routes: {allowed}",
        )

    return ModelPolicyDecision(True, tags)


def enforce_privileged_model_route(job: dict[str, Any]) -> None:
    """Raise ValueError if *job* violates privileged model policy."""
    decision = validate_privileged_model_route(job)
    if not decision.allowed:
        raise ValueError(decision.reason)
