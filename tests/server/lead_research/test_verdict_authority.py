"""What a strong fit actually requires, and what it must keep refusing.

`strong_fit` used to require an authoritative publisher plus a second one
agreeing. That is a statement about publishers, and it made the verdict a
function of who filed the evidence rather than of what the evidence says: a
company whose website was not in the corpus, or whose facts came from a curated
customer list with no public page at all, could never be a strong fit however
complete and consistent its evidence was.

What replaces it is an absolute quality floor on the evidence itself — enough
answered scoring weight, enough evidence confidence, both scored dimensions
present, no material conflict — applied after the terminal and eligibility
checks that already reject. Authority is still computed and still reported in
`missing_evidence`, because a strong fit that hides its own gaps stops being
auditable; it just no longer decides the verdict.

What this must *not* do is pad the list. The floor is a floor: below it a
candidate is `review`, and no review candidate is ever promoted to reach a
target.
"""
from __future__ import annotations

import pytest

from server.lead_research.models import Claim, LeadScore
from server.lead_research.qualification import EligibilityResult
from server.lead_research.registry import build_registry
from server.lead_research.verdicts import SourceCoverage, evaluate_verdict
from tests.server.lead_research.fakes import cited_source


TED = "ted.europa.eu"


def _product_claim() -> Claim:
    return Claim(field="product_sector_fit", value=95, status="observed", confidence=.9,
                 method="observed", evidence_ids=["ev_1"])


def _claims() -> list[Claim]:
    return [
        _product_claim(),
        Claim(field="buyer_channel_fit", value=85, status="observed", confidence=.9,
              method="observed", evidence_ids=["ev_2"]),
    ]


def _score(band: str = "A", **overrides) -> LeadScore:
    """A band-A score that clears the floor, unless a test lowers one input."""
    values = {
        "fit_score": 88,
        "evidence_confidence": .8,
        "priority_band": band,
        "known_weight": 60,
        "dimensions": {"product_sector_fit": 95.0, "buyer_channel_fit": 85.0},
        "confidence_factors": {},
    }
    return LeadScore(**{**values, **overrides})


def _verdict(coverage: SourceCoverage, band: str = "A"):
    return evaluate_verdict(
        {}, _claims(), _score(band), EligibilityResult(True, {}, []), coverage,
    )


# ── the block this lifts ──────────────────────────────────────────────────────

def test_a_band_curated_evidence_can_be_strong_without_a_public_url():
    """The regression this file exists for.

    A curated buyer list states the dimensions and cites an internal dataset
    reference. There is no domain of any kind, so every publisher-based route to
    `strong_fit` was closed to it permanently.
    """
    verdict = evaluate_verdict(
        {}, _claims(), _score(), EligibilityResult(True, {}, []),
        SourceCoverage(set(), set()),
    )

    assert verdict.kind == "strong_fit"
    assert verdict.reasons == ["a_band_above_absolute_quality_floor"]
    assert "official_source" in verdict.missing_evidence


def test_a_registry_notice_and_a_second_publisher_reach_a_strong_fit():
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={TED, "trade-press.example", "registry.example"},
        registry_domains={TED},
    ))

    assert verdict.kind == "strong_fit"
    assert verdict.reasons == ["a_band_above_absolute_quality_floor"]


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


def test_the_company_own_page_route_still_reaches_a_strong_fit():
    verdict = _verdict(SourceCoverage(
        official_domains={"buyer.example"},
        independent_domains={"registry.example"},
    ))

    assert verdict.kind == "strong_fit"
    assert verdict.missing_evidence == []


def test_a_single_publisher_is_reported_but_no_longer_blocking():
    """One publisher is still one source, and the record says so.

    It is the *floor* that decides now, so this is a strong fit with
    `second_source` named as a gap rather than a review with no way forward.
    """
    verdict = _verdict(SourceCoverage(
        official_domains=set(),
        independent_domains={TED},
        registry_domains={TED},
    ))

    assert verdict.kind == "strong_fit"
    assert "second_source" in verdict.missing_evidence


# ── the bar this must not lower ───────────────────────────────────────────────

@pytest.mark.parametrize(
    ("score_update", "reason"),
    [
        ({"known_weight": 45}, "insufficient_answered_weight"),
        ({"evidence_confidence": .59}, "insufficient_evidence_confidence"),
    ],
)
def test_absolute_floor_never_pads_a_band(score_update, reason):
    """A band-A fit computed from too little is still not a strong fit.

    `fit_score` is a weighted mean over the dimensions a lead actually has, so
    one dimension answered out of seven can read 95. Answered weight is what
    stops that from being a lead.
    """
    verdict = evaluate_verdict(
        {}, _claims(), _score(**score_update),
        EligibilityResult(True, {}, []), SourceCoverage(set(), set()),
    )

    assert verdict.kind == "review"
    assert reason in verdict.reasons


def test_missing_product_or_buyer_dimension_stays_review():
    verdict = evaluate_verdict(
        {}, [_product_claim()],
        _score(dimensions={"product_sector_fit": 95.0}),
        EligibilityResult(True, {}, []), SourceCoverage(set(), set()),
    )

    assert verdict.kind == "review"
    assert "buyer_channel_fit_required" in verdict.reasons


def test_a_lower_band_is_never_upgraded_by_a_cleared_floor():
    verdict = _verdict(
        SourceCoverage(set(), {TED, "press.example"}, {TED}), band="B",
    )

    assert verdict.kind == "review"


def test_nothing_at_all_still_names_every_gap():
    verdict = evaluate_verdict(
        {}, _claims(), _score(known_weight=10),
        EligibilityResult(True, {}, []), SourceCoverage(set(), set()),
    )

    assert verdict.kind == "review"
    assert set(verdict.missing_evidence) == {
        "authoritative_source", "official_source", "independent_source", "second_source",
    }


def test_a_material_conflict_still_blocks_a_strong_fit():
    conflicted = Claim(
        field="domain", value=["a.example", "b.example"], status="conflicted",
        confidence=.9, method="observed", evidence_ids=["ev_1"],
    )

    verdict = evaluate_verdict(
        {}, [*_claims(), conflicted], _score(), EligibilityResult(True, {}, []),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "review"
    assert "material_conflict" in verdict.reasons
    assert verdict.conflicting_claims == ["domain"]


def test_an_immaterial_conflict_does_not_block_a_strong_fit():
    """A disagreement about something the verdict does not rest on.

    Blocking on any conflicted field at all made an argument about a phone
    number as disqualifying as one about the company's country.
    """
    conflicted = Claim(
        field="phone", value=["+1 1", "+1 2"], status="conflicted",
        confidence=.9, method="observed", evidence_ids=["ev_1"],
    )

    verdict = evaluate_verdict(
        {}, [*_claims(), conflicted], _score(), EligibilityResult(True, {}, []),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "strong_fit"
    assert verdict.conflicting_claims == ["phone"]


def test_a_closed_company_is_still_rejected_however_well_it_scores():
    closed = Claim(
        field="lifecycle_status", value="dissolved", status="observed",
        confidence=.9, method="observed", evidence_ids=["ev_1"],
    )

    verdict = evaluate_verdict(
        {}, [*_claims(), closed], _score(), EligibilityResult(True, {}, []),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "reject"
    assert verdict.reasons == ["lifecycle_status_dissolved"]


def test_an_ineligible_company_is_still_rejected_however_authoritative():
    verdict = evaluate_verdict(
        {}, _claims(), _score(),
        EligibilityResult(False, {"buyer_role": "fail"}, ["buyer_role"]),
        SourceCoverage({"buyer.example"}, {TED}, {TED}),
    )

    assert verdict.kind == "reject"


def test_a_rejected_band_is_never_a_strong_fit():
    verdict = _verdict(SourceCoverage({"buyer.example"}, {TED}, {TED}), band="Rejected")

    assert verdict.kind == "reject"
    assert verdict.reasons == ["below_scoring_threshold"]


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
        from server.lead_research.models import VerificationBundle
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            # A notice naming where the contract was performed is presence in
            # the requested market, which is a third answered dimension. Without
            # it this fixture answers 45 of 100 weighted points and sits below
            # the floor on arithmetic rather than on authority.
            "locations": [candidate.country],
            "buyer_role": ["public procurement supplier", "distributor"],
            "product_term": ["white goods", "built-in ovens", "household-appliances"],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[cited_source(
                provenance_url=f"https://{TED}/en/notice/-/detail/255023-2024",
                classification="independent",
                retrieved_via=f"https://{TED}/",
                facts=facts,
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
        from server.lead_research.models import VerificationBundle
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "locations": [candidate.country],
            "buyer_role": ["distributor", "wholesaler"],
            "product_term": ["white goods", "built-in ovens", "household-appliances"],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                cited_source(
                    provenance_url=f"https://trade-press.example/{candidate.source_record_id}",
                    classification="independent",
                    retrieved_via="https://search.example/",
                    facts=facts,
                ),
                cited_source(
                    provenance_url=f"https://directory.example/{candidate.source_record_id}",
                    classification="independent",
                    retrieved_via="https://search.example/",
                    facts=facts,
                ),
                cited_source(
                    provenance_url=f"https://chamber.example/{candidate.source_record_id}",
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
    notice plus corroborating third-party pages now reaches it — on answered
    weight and confidence, which is what the floor measures.

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
        "INSERT INTO research_campaigns(id,company_id,name,status,version,config,estimate,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
