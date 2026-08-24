from __future__ import annotations

from server.db import Database, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.discovery import CandidateDiscoveryService
from server.lead_research.languages import TranslationCache, build_market_terms
from server.lead_research.models import CompanyResearchProfile, DiscoveryQuery
from server.lead_research.registry import ProviderRegistry


def _profile(*, target_countries=None, languages=None):
    return CompanyResearchProfile(
        identity={"name": "Acme", "website": "https://acme.test"},
        seller_countries=["TR"],
        products=[{
            "id": "prd_1",
            "name": "Vana",
            "english_name": "Valve",
            "sector_ids": ["industrial-machinery"],
        }],
        market_preferences={
            "target_countries": target_countries or ["DE"],
            "languages": languages or ["de", "en"],
        },
    )


def test_market_terms_expand_product_and_buyer_language_without_changing_canonical_value():
    terms = build_market_terms(
        {"product_terms": ["industrial valve"], "sector_ids": ["industrial-machinery"]},
        _profile(),
    )

    assert terms.canonical == ["industrial valve"]
    assert "Industriearmatur" in terms.by_language["de"]
    assert "Einkaufsleiter" in terms.by_language["de"]
    assert terms.unmapped_markets == []


def test_missing_market_mapping_is_visible_and_canonical_term_remains_searchable():
    terms = build_market_terms(
        {"product_terms": ["industrial valve"], "sector_ids": ["industrial-machinery"]},
        _profile(target_countries=["JP"], languages=["ja", "en"]),
    )

    assert terms.by_language["ja"] == ["industrial valve"]
    assert terms.unmapped_markets == ["JP"]


def test_indexed_candidate_selection_uses_local_terms(tmp_path):
    db = Database(tmp_path / "local-selection.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "Acme", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "german-buyers",
        "1",
        "buyers.jsonl",
        b'{"source_record_id":"de-1","company_name":"Armaturen Einkauf GmbH",'
        b'"country":"DE","categories":["Industriearmatur"]}',
    )
    terms = build_market_terms(
        {"product_terms": ["industrial valve"], "sector_ids": ["industrial-machinery"]},
        _profile(),
    )
    query = DiscoveryQuery(
        campaign_id="rc_1",
        seller_countries=["TR"],
        target_countries=["DE"],
        sector_ids=["industrial-machinery"],
        product_terms=terms.canonical,
        market_terms=terms.by_language,
    )

    supply = CandidateDiscoveryService(db, ProviderRegistry([], {})).supply(
        "cmp_a", query, 10,
    )

    assert [candidate.source_record_id for candidate in supply.candidates] == ["de-1"]


def test_display_translation_retains_original_and_canonical(tmp_path):
    cache = TranslationCache(Database(tmp_path / "translations.db"))

    rendered = cache.evidence_value(
        company_id="cmp_a",
        fact_key="tf_1",
        value_en="public procurement award",
        original="kamu ihalesi",
        language="tr",
        locale="en",
    )

    assert rendered == {
        "canonical": "public procurement award",
        "original": "kamu ihalesi",
        "display": "public procurement award",
        "source_language": "tr",
    }


def test_free_text_translation_is_generated_once_per_tenant_and_content(tmp_path):
    db = Database(tmp_path / "translation-cache.db")
    stamp = now()
    for company_id in ("cmp_a", "cmp_b"):
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (company_id, company_id, "active", "{}", stamp, stamp),
        )
    calls: list[tuple[str, str, str]] = []

    def translate(value_en: str, source_language: str, locale: str) -> str:
        calls.append((value_en, source_language, locale))
        return "kamu ihalesi kazanimi"

    cache = TranslationCache(db, translate=translate)
    arguments = {
        "fact_key": "fact_1",
        "value_en": "public procurement award",
        "original": "zamowienie publiczne",
        "language": "pl",
        "locale": "tr",
    }

    first = cache.evidence_value(company_id="cmp_a", **arguments)
    second = cache.evidence_value(company_id="cmp_a", **arguments)
    other_tenant = cache.evidence_value(company_id="cmp_b", **arguments)

    assert first == second
    assert first["display"] == other_tenant["display"] == "kamu ihalesi kazanimi"
    assert calls == [
        ("public procurement award", "pl", "tr"),
        ("public procurement award", "pl", "tr"),
    ]
