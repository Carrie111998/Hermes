"""What standing a verdict needs, and what it must keep refusing.

`strong_fit` required a domain classified `official` — a page on the company's
own site. That classification is only ever produced by fetching a domain the
candidate row already carried, so a company whose website was not in the corpus
had no path to it at all: 161 of the 201 TED-derived rows could never be a
strong fit however much evidence they accumulated. The verdict was capped by
corpus metadata rather than by evidence.

An authoritative registry now satisfies that leg. It is declared per source in
the provider catalog, because "the EU's Publications Office is authoritative"
is a judgement about a publisher, not something to infer from a URL.

What this must *not* do is lower the bar. One publisher is still one source,
and third-party mentions with no authority behind them are still `review`.
"""
from __future__ import annotations

from server.lead_research.models import Claim, LeadScore
from server.lead_research.qualification import EligibilityResult
from server.lead_research.registry import build_registry
from server.lead_research.verdicts import SourceCoverage, evaluate_verdict


TED = "ted.europa.eu"


def _claims() -> list[Claim]:
    return [
        Claim(field="product_sector_fit", value=95, status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_1"]),
        Claim(field="buyer_channel_fit", value=85, status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_2"]),
    ]


def _score(band: str = "A") -> LeadScore:
    return LeadScore(
        fit_score=88, evidence_confidence=.8, priority_band=band,
        dimensions={"product_sector_fit": 95.0}, confidence_factors={},
    )


def _verdict(coverage: SourceCoverage, band: str = "A"):
    return evaluate_verdict(
        {}, _claims(), _score(band), EligibilityResult(True, {}, []), coverage,
    )


# ── the block this lifts ──────────────────────────────────────────────────────

def test_a_registry_notice_and_a_second_publisher_reach_a_strong_fit():
    """The regression this file exists for.

    A company with no website in the corpus: an EU award notice naming it, plus
    two independent pages. Every path to `official` was closed to it, so this
    used to be permanently `review`.
    """
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={TED, "trade-press.example", "registry.example"},
        registry_domains={TED},
    ))

    assert verdict.kind == "strong_fit"
    assert verdict.reasons == ["a_band_with_registry_and_corroborating_evidence"]


def test_a_strong_fit_still_reports_what_it_never_saw():
    """A verdict that hides its own gaps stops being auditable."""
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={TED, "trade-press.example"},
        registry_domains={TED},
    ))

    assert verdict.kind == "strong_fit"
    assert "official_source" in verdict.missing_evidence, (
        "the company's own page was never read, and the record has to say so"
    )
    assert "authoritative_source" not in verdict.missing_evidence


def test_the_company_own_page_route_is_unchanged():
    verdict = _verdict(SourceCoverage(
        official_domains={"buyer.example"},
        independent_domains={"registry.example"},
    ))

    assert verdict.kind == "strong_fit"
    assert verdict.reasons == ["a_band_with_official_and_corroborating_evidence"]
    assert verdict.missing_evidence == []


def test_an_official_page_and_a_registry_notice_corroborate_each_other():
    """Two publishers, both with standing. Nothing further is required."""
    verdict = _verdict(SourceCoverage(
        official_domains={"buyer.example"},
        independent_domains=set(),
        registry_domains={TED},
    ))

    assert verdict.kind == "strong_fit"
    assert "independent_source" in verdict.missing_evidence


# ── the bar this must not lower ───────────────────────────────────────────────

def test_a_registry_notice_alone_is_not_corroboration():
    """One publisher is one source, whoever the publisher is.

    This is the case the deployed tenant is actually in: TED, and a corpus whose
    rows cite the same TED notice, are one domain between them.
    """
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={TED},
        registry_domains={TED},
    ))

    assert verdict.kind == "review"
    assert "second_source" in verdict.missing_evidence


def test_third_party_mentions_with_no_authority_stay_under_review():
    """Two directory listings are not a strong fit."""
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={"directory-a.example", "directory-b.example"},
    ))

    assert verdict.kind == "review"
    assert verdict.missing_evidence.count("authoritative_source") == 1


def test_nothing_at_all_is_still_review_with_every_gap_named():
    verdict = _verdict(SourceCoverage(set(), set()))

    assert verdict.kind == "review"
    assert set(verdict.missing_evidence) == {
        "authoritative_source", "official_source", "independent_source", "second_source",
    }


def test_a_lower_band_is_never_upgraded_by_authority():
    verdict = _verdict(
        SourceCoverage(set(), {TED, "press.example"}, {TED}), band="B",
    )

    assert verdict.kind == "review"


def test_a_conflicting_claim_still_blocks_a_strong_fit():
    conflicted = Claim(
        field="domain", value=["a.example", "b.example"], status="conflicted",
        confidence=.9, method="observed", evidence_ids=["ev_1"],
    )

    verdict = evaluate_verdict(
        {}, [*_claims(), conflicted], _score(), EligibilityResult(True, {}, []),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "review"
    assert verdict.conflicting_claims == ["domain"]


def test_an_ineligible_company_is_still_rejected_however_authoritative():
    verdict = evaluate_verdict(
        {}, _claims(), _score(),
        EligibilityResult(False, {"buyer_role": "fail"}, ["buyer_role"]),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "reject"


# ── the declaration itself ───────────────────────────────────────────────────

def test_only_ted_claims_registry_standing_in_the_catalog():
    """Authority is declared, so the declaration is the thing to pin.

    A web verifier must never claim it: it reports whatever page it reached, and
    the whole point of the split is that reaching a page is not standing.
    """
    registry = build_registry()
    declared = {
        source_id for source_id, definition in registry.definitions.items()
        if "authoritative_registry" in definition.capabilities
    }

    assert declared == {"ted"}


# ── end to end, on the shape the deployed tenant actually has ─────────────────

class _RegistryNotice:
    """A TED-shaped verifier: authoritative publisher, no company page."""

    def __init__(self, definition):
        self.definition = definition

    def health(self):
        from server.lead_research.models import ProviderHealth
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        from server.lead_research.models import VerificationBundle, VerificationSource
        import hashlib
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[VerificationSource(
                provenance_url=f"https://{TED}/en/notice/-/detail/255023-2024",
                raw_hash=hashlib.sha256(b"notice").hexdigest(),
                classification="independent",
                retrieved_via=f"https://{TED}/",
                facts={
                    "company_name": [candidate.company_name],
                    "country": [candidate.country],
                    "buyer_role": ["public procurement supplier", "distributor"],
                    "product_term": ["white goods", "built-in ovens", "household-appliances"],
                },
            )],
            independent_source_count=1,
        )


class _WebMentions:
    """A web verifier that finds the company on two third-party pages."""

    def __init__(self, definition):
        self.definition = definition

    def health(self):
        from server.lead_research.models import ProviderHealth
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        from server.lead_research.models import VerificationBundle, VerificationSource
        import hashlib
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": ["distributor", "wholesaler"],
            "product_term": ["white goods", "built-in ovens", "household-appliances"],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                VerificationSource(
                    provenance_url=f"https://trade-press.example/{candidate.source_record_id}",
                    raw_hash=hashlib.sha256(b"press").hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example/",
                    facts=facts,
                ),
                VerificationSource(
                    provenance_url=f"https://directory.example/{candidate.source_record_id}",
                    raw_hash=hashlib.sha256(b"directory").hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example/",
                    facts=facts,
                ),
                VerificationSource(
                    provenance_url=f"https://chamber.example/{candidate.source_record_id}",
                    raw_hash=hashlib.sha256(b"chamber").hexdigest(),
                    classification="independent",
                    retrieved_via="https://search.example/",
                    facts=facts,
                ),
            ],
            independent_source_count=3,
        )


def test_a_company_with_no_website_can_now_be_a_strong_fit_end_to_end(tmp_path):
    """The whole point of B2, through the real pipeline.

    No domain on the candidate row, so nothing can ever be classified
    `official`, and this was permanently `review` at any evidence level. An EU
    notice plus corroborating third-party pages now reaches it.

    The fixtures here declare what they emit, so `completeness` is measured
    against what these two sources could establish rather than against all seven
    scoring dimensions — see test_fit_scoring.py for that on its own.
    """
    import json
    from server.db import Database, json_dump, now
    from server.lead_research.candidates import CandidateRepository
    from server.lead_research.models import CampaignConfig, DatasetDefinition
    from server.lead_research.registry import ProviderRegistry
    from server.lead_research.service import LeadResearchService

    db = Database(tmp_path / "authority.db")
    company_id, campaign_id, stamp = "cmp_1", "camp_1", now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (company_id, "Tenant", "active", "{}", stamp, stamp),
    )
    def _definition(source_id, capabilities):
        return DatasetDefinition(
            source_id=source_id, display_name=source_id, publisher="Tests",
            access_tier="public", entity_levels=["named_company"],
            capabilities=capabilities, adapter_mode="live", default_enabled=True,
        )
    notice = _definition("ted-like", ["candidate_verification", "authoritative_registry"])
    web = _definition("web-like", ["candidate_verification", "web_evidence"])
    config = CampaignConfig(
        name="Romanian catering suppliers",
        target_countries=["RO"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=[notice.source_id, web.source_id],
    )
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, company_id, config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "ted-appliances", "3", "candidates.jsonl",
        json.dumps({
            "source_record_id": "pro-horeca-ro",
            "company_name": "Pro Horeca SRL",
            "country": "RO",
            "categories": ["household-appliances"],
        }).encode(),
    )
    service = LeadResearchService(db, registry=ProviderRegistry(
        [notice, web],
        {notice.source_id: _RegistryNotice(notice), web.source_id: _WebMentions(web)},
    ))

    result = service.run(company_id, campaign_id)

    assert result["status"] == "succeeded", result
    row = db.one(
        "SELECT verdict,fit_score,data FROM research_results WHERE campaign_id=?", (campaign_id,)
    )
    assert row["verdict"] == "strong_fit", dict(row)
    # And the record still says the company's own page was never read.
    import json as _json
    assert "official_source" in _json.loads(row["data"])["missing_evidence"]


def test_a_dimension_no_source_can_reach_no_longer_caps_this_verdict():
    """The ceiling that used to sit behind this fix.

    `completeness` divided by all seven scoring dimensions, and four of them
    have no field any shipped verifier emits, so a domain-less company could not
    exceed 2/7 and could not clear band A however well corroborated. Fixing the
    verdict alone would have left it blocked one step later.
    """
    from server.lead_research.registry import build_registry
    from server.lead_research.scoring import attainable_dimensions

    definitions = build_registry().definitions
    reachable = attainable_dimensions({
        field for definition in definitions.values() for field in definition.emits
    })

    assert reachable == {"product_sector_fit", "buyer_channel_fit", "contactability"}
    assert "commercial_scale" not in reachable, (
        "if a verifier starts emitting revenue or store counts, this widens on "
        "its own — that is the point of declaring emitted fields per source"
    )
