from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from server.lead_research.models import (
    EvidenceEnvelope, EvidenceSpan, ResearchFact, VerificationBundle, VerificationSource,
)
from server.lead_research.quotes import EvidenceRejected, accept_fact, validate_span
from server.lead_research.storage import EvidenceRepository


JAN_2022 = datetime(2022, 1, 15, tzinfo=timezone.utc)
AUG_2026 = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _envelope(content: str, *, archive_snapshot_at=None):
    return EvidenceEnvelope(
        evidence_id="ev_1",
        source_id="official-site",
        source_record_id="page-1",
        snapshot_id="snap_1",
        record_type="company_signal",
        provenance_url="https://acme.example/about",
        raw_hash="a" * 64,
        method="observed",
        confidence=.95,
        snapshot_content=content,
        source_language="tr",
        archive_snapshot_at=archive_snapshot_at,
        retrieved_at=AUG_2026,
        payload={},
    )


def _fact(content: str, original: str, **updates):
    start = content.index(original)
    values = {
        "organization_id": "org_1",
        "field": "facility_event",
        "value_en": "opened a new distribution center in Germany",
        "original_text": original,
        "source_language": "tr",
        "derivation_kind": "translated",
        "confidence": .9,
        "validation_basis": "pending exact-span validation",
        "evidence_id": "ev_1",
        "span": EvidenceSpan(original=original, start=start, end=start + len(original)),
        "source_class": "official",
        "visibility": "public",
        "mechanically_validated": False,
        "observed_at": AUG_2026.timestamp(),
        "retrieved_at": AUG_2026.timestamp(),
        "expires_at": AUG_2026.timestamp() + 86400,
    }
    values.update(updates)
    return ResearchFact(**values)


def test_quote_must_be_exact_substring_of_immutable_snapshot():
    page = "Şirket 2024 yılında Almanya'da yeni bir dağıtım merkezi açtı."
    original = "Almanya'da yeni bir dağıtım merkezi"
    start = page.index(original)

    accepted = validate_span(
        page, EvidenceSpan(original=original, start=start, end=start + len(original))
    )
    rejected = validate_span(
        page,
        EvidenceSpan(
            original="opened a new distribution center in Germany", start=0, end=43
        ),
    )

    assert accepted.valid is True
    assert accepted.exact == original
    assert rejected.valid is False


def test_archive_snapshot_does_not_claim_current_observation():
    page = "Şirket Almanya'da yeni bir dağıtım merkezi açtı."
    original = "Almanya'da yeni bir dağıtım merkezi"

    accepted = accept_fact(
        _envelope(page, archive_snapshot_at=JAN_2022),
        _fact(page, original),
    )

    assert accepted.observed_at == JAN_2022.timestamp()
    assert accepted.retrieved_at == AUG_2026.timestamp()
    assert accepted.mechanically_validated is True
    assert accepted.original_text == original


def test_observed_fact_value_must_be_present_but_translation_may_differ():
    page = "Çalışan sayısı 42 kişidir."
    translated = _fact(
        page,
        "Çalışan sayısı 42 kişidir",
        field="employee_count",
        value_en=42,
        derivation_kind="translated",
    )
    observed = translated.model_copy(update={"derivation_kind": "observed"})

    assert accept_fact(_envelope(page), translated).value_en == 42
    with pytest.raises(EvidenceRejected, match="absent"):
        accept_fact(_envelope(page), observed.model_copy(update={"value_en": 99}))


def test_identity_tokens_are_not_translated_or_normalized():
    page = "Ticaret unvanı: İleri Dış Ticaret A.Ş."
    fact = _fact(
        page,
        "İleri Dış Ticaret A.Ş.",
        field="company_name",
        value_en="Ileri Dis Ticaret AS",
        derivation_kind="translated",
    )

    with pytest.raises(EvidenceRejected, match="identity tokens"):
        accept_fact(_envelope(page), fact)


def test_prepare_verification_drops_fact_whose_value_is_not_in_declared_span():
    content = "Acme reports 42 employees."
    span = EvidenceSpan(original="42", start=13, end=15)
    bundle = VerificationBundle(
        candidate_source_record_id="acme",
        sources=[VerificationSource(
            provenance_url="https://acme.example/about",
            raw_hash=hashlib.sha256(content.encode()).hexdigest(),
            classification="official",
            retrieved_via="https://acme.example/about",
            facts={"employee_count": ["99"]},
            snapshot_content=content,
            fact_spans={"employee_count": [span]},
        )],
    )

    prepared = EvidenceRepository(None, "cmp_a").prepare_verification(bundle, "official-site")

    assert prepared == []
