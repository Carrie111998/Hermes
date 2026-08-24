"""Single acceptance gate for exact source spans and archive semantics."""
from __future__ import annotations

import re
from datetime import datetime

from .models import ApiModel, EvidenceEnvelope, EvidenceSpan, ResearchFact


class EvidenceRejected(ValueError):
    """A proposed fact is not mechanically supported by its snapshot."""


class SpanValidation(ApiModel):
    valid: bool
    exact: str


def validate_span(content: str, span: EvidenceSpan) -> SpanValidation:
    if span.end > len(content):
        return SpanValidation(valid=False, exact=content[span.start:])
    exact = content[span.start:span.end]
    return SpanValidation(valid=exact == span.original, exact=exact)


def spans_for_facts(content: str, facts: dict[str, list[str]]) -> dict[str, list[EvidenceSpan]]:
    """Locate literal fact values without changing the published characters.

    A missing entry is meaningful: the provider may retain a derived hint for
    compatibility, but it cannot pass ``accept_fact`` as an observed value.
    """
    result: dict[str, list[EvidenceSpan]] = {}
    for field, values in facts.items():
        spans: list[EvidenceSpan] = []
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            match = re.search(re.escape(text), content, flags=re.IGNORECASE)
            if match:
                original = content[match.start():match.end()]
                spans.append(EvidenceSpan(
                    original=original, start=match.start(), end=match.end(),
                ))
        if spans:
            result[field] = spans
    return result


def _timestamp(value: datetime | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _literal_value_present(value, original: str) -> bool:
    values = value if isinstance(value, list) else [value]
    folded = original.casefold()
    return all(str(item).strip().casefold() in folded for item in values if item is not None)


def accept_fact(envelope: EvidenceEnvelope, proposed: ResearchFact) -> ResearchFact:
    validation = validate_span(envelope.snapshot_content, proposed.span)
    if not validation.valid:
        raise EvidenceRejected("source span is not an exact snapshot substring")
    if proposed.original_text != proposed.span.original:
        raise EvidenceRejected("original text differs from the exact source span")
    if proposed.derivation_kind == "observed" and not _literal_value_present(
        proposed.value_en, proposed.span.original
    ):
        raise EvidenceRejected("observed value is absent from its source span")
    if proposed.field in {"company_name", "registry_id", "domain"}:
        if str(proposed.value_en).strip() not in proposed.span.original:
            raise EvidenceRejected("identity tokens must remain unchanged")
    observed = _timestamp(proposed.observed_at)
    archive = _timestamp(envelope.archive_snapshot_at)
    if archive is not None:
        observed = min(value for value in (observed, archive) if value is not None)
    retrieved = _timestamp(envelope.retrieved_at)
    return proposed.model_copy(update={
        "observed_at": observed,
        "retrieved_at": retrieved,
        "mechanically_validated": True,
        "validation_basis": "exact snapshot substring",
    })

