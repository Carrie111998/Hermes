import hashlib
import json

import pytest

from server.db import Database, json_dump, now
from server.lead_research.candidates import CandidateRecord, CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    Claim,
    DatasetDefinition,
    LeadScore,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.qualification import EligibilityResult
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from server.lead_research.verdicts import SourceCoverage, evaluate_verdict


def _candidate() -> CandidateRecord:
    return CandidateRecord(
        dataset_id="buyers",
        version="2026-08",
        source_record_id="buyer-de-1",
        company_name="Atlas DE",
        normalized_name="atlas de",
        country="DE",
        domain="atlas-de.example.test",
        data={"buyer_types": ["distributor"], "categories": ["household-appliances"]},
    )


def _claim(field: str, evidence_id: str) -> Claim:
    return Claim(
        field=field,
        value=100,
        status="observed",
        confidence=.95,
        method="observed",
        evidence_ids=[evidence_id],
    )


def _score(band: str = "A") -> LeadScore:
    return LeadScore(
        fit_score=92,
        evidence_confidence=.86,
        priority_band=band,
        dimensions={"product_sector_fit": 100.0},
        confidence_factors={},
    )


def _eligible() -> EligibilityResult:
    return EligibilityResult(True, {"resolved_identity": "pass"}, [])


def test_strong_fit_requires_two_sources_and_one_official():
    coverage = SourceCoverage(
        official_domains={"official.example"},
        independent_domains={"registry.example"},
    )

    verdict = evaluate_verdict(
        _candidate(),
        [_claim("product_sector_fit", "ev_official"), _claim("buyer_channel_fit", "ev_registry")],
        _score(),
        _eligible(),
        coverage,
    )

    assert verdict.kind == "strong_fit"
    assert verdict.missing_evidence == []


@pytest.mark.parametrize(
    ("coverage", "missing"),
    [
        (SourceCoverage(official_domains=set(), independent_domains={"registry.example"}), "official_source"),
        (SourceCoverage(official_domains={"official.example"}, independent_domains=set()), "independent_source"),
    ],
)
def test_a_score_without_required_source_coverage_is_review(coverage, missing):
    verdict = evaluate_verdict(_candidate(), [_claim("product_sector_fit", "ev_1")], _score(), _eligible(), coverage)

    assert verdict.kind == "review"
    assert missing in verdict.missing_evidence


def test_ineligible_candidate_is_rejected_even_with_a_score_and_source_coverage():
    verdict = evaluate_verdict(
        _candidate(),
        [_claim("product_sector_fit", "ev_1")],
        _score(),
        EligibilityResult(False, {"buyer_role": "fail"}, ["buyer_role"]),
        SourceCoverage({"official.example"}, {"registry.example"}),
    )

    assert verdict.kind == "reject"
    assert "buyer_role" in verdict.reasons


class RejectingVerifier:
    def __init__(self, definition):
        self.definition = definition

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        markdown = f"Independent identity record for {candidate.company_name}"
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[VerificationSource(
                provenance_url="https://registry.example.test/atlas",
                raw_hash=hashlib.sha256(markdown.encode()).hexdigest(),
                classification="independent",
                retrieved_via="https://search.example.test",
                facts={"company_name": [candidate.company_name]},
            )],
            independent_source_count=1,
        )


class ReviewingVerifier(RejectingVerifier):
    def verify(self, query, candidate):
        bundle = super().verify(query, candidate)
        source = bundle.sources[0]
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[source.model_copy(update={"facts": {
                "company_name": [candidate.company_name],
                "buyer_role": ["distributor"],
                "product_term": ["household-appliances"],
            }})],
            independent_source_count=1,
        )


class StrongEvidenceVerifier(RejectingVerifier):
    """What a genuinely strong lead looks like: an official page and two
    independent records agreeing, across several product terms and roles.

    Scoring by degree means the top band has to be earned, so something has to
    prove it is still reachable through the real pipeline.
    """

    def verify(self, query, candidate):
        del query
        terms = ["white goods", "built-in ovens", "household-appliances"]
        roles = ["distributor", "wholesaler"]
        shared = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": roles,
            "product_term": terms,
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                VerificationSource(
                    provenance_url=f"https://{candidate.domain}/about",
                    raw_hash=hashlib.sha256(b"official").hexdigest(),
                    classification="official",
                    retrieved_via=f"https://{candidate.domain}",
                    facts={**shared, "domain": [candidate.domain]},
                ),
                VerificationSource(
                    provenance_url="https://registry.example.test/atlas",
                    raw_hash=hashlib.sha256(b"registry").hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example.test",
                    facts=shared,
                ),
                VerificationSource(
                    provenance_url="https://trade-press.example.test/atlas",
                    raw_hash=hashlib.sha256(b"press").hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example.test",
                    facts=shared,
                ),
            ],
            independent_source_count=2,
        )


def _run_campaign(tmp_path, verifier_type):
    db = Database(tmp_path / "verdicts.db")
    company_id, campaign_id = "company_one", "campaign_one"
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (company_id, "Company One", "active", "{}", stamp, stamp),
    )
    definition = DatasetDefinition(
        source_id="verifier-fixture",
        display_name="Verifier fixture",
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        adapter_mode="live",
        default_enabled=True,
    )
    config = CampaignConfig(
        name="German appliance distributors",
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=[definition.source_id],
    )
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, config.name, "draft", 1, json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers",
        "2026-08",
        "candidates.jsonl",
        json.dumps({
            "source_record_id": "buyer-de-1",
            "company_name": "Atlas DE",
            "country": "DE",
            "domain": "https://atlas-de.example.test",
            "categories": ["household-appliances"],
            "buyer_types": ["distributor"],
        }).encode(),
    )
    registry = ProviderRegistry([definition], {definition.source_id: verifier_type(definition)})

    result = LeadResearchService(db, registry=registry).run(company_id, campaign_id)

    return db, company_id, campaign_id, result


def test_reject_is_persisted_without_creating_a_lead(tmp_path):
    db, company_id, campaign_id, result = _run_campaign(tmp_path, RejectingVerifier)

    assert result["status"] == "succeeded"
    research_result = db.one(
        "SELECT verdict,lead_id FROM research_results WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    )
    assert dict(research_result) == {"verdict": "reject", "lead_id": None}
    assert db.one("SELECT COUNT(*) AS n FROM leads WHERE company_id=?", (company_id,))["n"] == 0


def test_review_is_persisted_and_creates_a_lead(tmp_path):
    db, company_id, campaign_id, result = _run_campaign(tmp_path, ReviewingVerifier)

    assert result["status"] == "succeeded"
    research_result = db.one(
        "SELECT verdict,lead_id FROM research_results WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    )
    assert research_result["verdict"] == "review"
    assert research_result["lead_id"]
    lead = db.one("SELECT status FROM leads WHERE id=?", (research_result["lead_id"],))
    assert lead["status"] == "review"


class AbstainingVerifier(RejectingVerifier):
    """A source with nothing to say about this candidate.

    `CorpusProvider` does exactly this for a row carrying no citation, which is
    most rows in a plain contact-list import.
    """

    def verify(self, query, candidate):
        del query
        return VerificationBundle(candidate_source_record_id=candidate.source_record_id)


def test_an_abstaining_source_reports_missing_evidence_not_a_broken_identity(tmp_path):
    """An empty bundle is not a verification.

    Counting it as one carried the candidate to the identity stage to fail
    there on "verification returned no evidence-backed identity" — an
    internal-sounding error for the ordinary case that no source could vouch
    for the company. It also produced a result row and a verified count for a
    candidate nothing had verified.
    """
    db, company_id, campaign_id, result = _run_campaign(tmp_path, AbstainingVerifier)

    assert result["metrics"]["resolved_organizations"] == 0
    assert db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    )["n"] == 0

    issue = db.one(
        "SELECT issue_type,data FROM research_issues WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    )
    assert issue["issue_type"] == "candidate_processing_failed"
    detail = json.loads(issue["data"])
    assert detail["stage"] == "verification"
    assert detail["messages"] == ["no evidence from verifier-fixture"]


def test_a_rerun_with_a_domainless_verifier_does_not_duplicate_the_identity(tmp_path):
    """The identity duplication A3 fixes, at campaign level.

    `ReviewingVerifier` names the company and states nothing that links it — no
    domain, no registry id — which is the ordinary shape of a TED notice or a
    search snippet. Resolution could only match on an identifier, so every run
    created another organization for the same company, and every organization
    carried its own lead.
    """
    db = Database(tmp_path / "verdicts.db")
    company_id, campaign_id = "company_one", "campaign_one"
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (company_id, "Company One", "active", "{}", stamp, stamp),
    )
    definition = DatasetDefinition(
        source_id="verifier-fixture",
        display_name="Verifier fixture",
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        adapter_mode="live",
        default_enabled=True,
    )
    config = CampaignConfig(
        name="German appliance distributors",
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=[definition.source_id],
    )
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "2026-08", "candidates.jsonl",
        json.dumps({
            "source_record_id": "buyer-de-1",
            "company_name": "Atlas DE",
            "country": "DE",
            "categories": ["household-appliances"],
            "buyer_types": ["distributor"],
        }).encode(),
    )
    registry = ProviderRegistry(
        [definition], {definition.source_id: ReviewingVerifier(definition)}
    )
    service = LeadResearchService(db, registry=registry)

    service.run(company_id, campaign_id)
    service.run(company_id, campaign_id)

    assert db.one(
        "SELECT COUNT(*) AS n FROM organizations WHERE company_id=?", (company_id,)
    )["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM leads WHERE company_id=?", (company_id,))["n"] == 1


def test_strong_evidence_still_earns_a_strong_fit_end_to_end(tmp_path):
    """The canary for the top band.

    The shared fixture states one term and one role, which is corroborated but
    narrow, and now correctly lands on `review`. Without this test nothing would
    prove the pipeline can still produce a `strong_fit` at all.
    """
    db, company_id, campaign_id, result = _run_campaign(tmp_path, StrongEvidenceVerifier)

    assert result["status"] == "succeeded"
    research_result = db.one(
        "SELECT verdict,fit_score,lead_id FROM research_results WHERE company_id=? AND campaign_id=?",
        (company_id, campaign_id),
    )
    assert research_result["verdict"] == "strong_fit"
    assert research_result["fit_score"] >= 80
    assert db.one(
        "SELECT status FROM leads WHERE id=?", (research_result["lead_id"],)
    )["status"] == "qualified"
