from datetime import datetime, timezone

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import (
    ClaimStatus,
    EvidenceFabricService,
    EvidenceScope,
    EvidenceValidationError,
    ResearchRunStatus,
    canonicalize_uri,
    content_sha256,
)


def _service(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", "agent")
    return db, EvidenceFabricService(db, scope)


def test_hash_and_uri_helpers_are_deterministic():
    assert content_sha256("café") == content_sha256("cafe\u0301")
    assert content_sha256(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert canonicalize_uri("HTTP://Example.TEST:80") == "http://example.test/"
    assert canonicalize_uri("https://Example.TEST:443/a#fragment") == "https://example.test/a"
    assert canonicalize_uri("https://example.test/a?x=1&x=2&Track=Yes") == "https://example.test/a?x=1&x=2&Track=Yes"


def test_service_creates_run_evidence_claim_link_and_status_provenance(tmp_path):
    db, service = _service(tmp_path)
    try:
        run = service.create_research_run("Find the answer")
        evidence = service.add_evidence(
            run.id,
            source_type="WEB_PAGE",
            retrieval_method="DIRECT_HTTP",
            content="source text",
            source_uri="https://Example.test#x",
        ).evidence
        claim = service.create_claim(run.id, "The answer is supported")
        link = service.link_evidence_to_claim(claim.id, evidence.id, "SUPPORTS")
        updated = service.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
        assert run.status is ResearchRunStatus.OPEN
        assert link.created_by_agent == "agent"
        assert updated.status is ClaimStatus.SUPPORTED
        assert updated.updated_by_agent == "agent"
    finally:
        db.close()


def test_validation_rejects_bad_hash_uri_and_terminal_mutation(tmp_path):
    db, service = _service(tmp_path)
    try:
        run = service.create_research_run("objective")
        with pytest.raises(EvidenceValidationError):
            service.add_evidence(run.id, source_type="WEB_PAGE", retrieval_method="DIRECT_HTTP", content="x", expected_content_hash="x")
        with pytest.raises(EvidenceValidationError):
            canonicalize_uri("relative/path")
        service.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(Exception):
            service.create_claim(run.id, "too late")
    finally:
        db.close()
