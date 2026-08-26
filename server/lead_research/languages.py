"""Market-language discovery terms and stable display translations.

English is the fact/storage language.  This module deliberately keeps the
opposite direction used by discovery separate: providers search the language
of the target market, while the canonical term never changes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Literal

import yaml

from ..db import json_dump, new_id, now
from .models import CompanyResearchProfile, MarketTermSet
from .sectors import REFERENCE_DIR, load_sectors


DisplayLocale = Literal["en", "tr"]
TranslationFunction = Callable[[str, str, str], str]

# The search language for a market, which is not the same question as its
# official languages: this picks the one language a buyer's own site is most
# likely written in. Anglophone markets are listed explicitly rather than left
# out, because a missing entry is reported as an unmapped market — and an
# English-language market is served perfectly by the canonical terms, so a UK
# campaign warning about "missing local mapping" is a false alarm.
#
# Genuinely split markets (BE, CH, CA, MA) are deliberately absent: guessing
# one half wrong searches the wrong language, and being listed as unmapped is
# the honest answer until a playbook covers both.
COUNTRY_LANGUAGE = {
    "AE": "ar",
    "AT": "de",
    "AU": "en",
    "BH": "ar",
    "DE": "de",
    "EG": "ar",
    "FR": "fr",
    "GB": "en",
    "IE": "en",
    "KW": "ar",
    "NL": "nl",
    "NZ": "en",
    "OM": "ar",
    "PL": "pl",
    "QA": "ar",
    "RO": "ro",
    "SA": "ar",
    "TR": "tr",
    "US": "en",
}

# Fixed product vocabulary is dictionary-backed. Free text goes through the
# injected model-backed translator and is persisted below.
FIXED_TR = {
    "public procurement award": "kamu ihalesi kazanımı",
    "distributor": "distribütör",
    "importer": "ithalatçı",
    "procurement organization": "satın alma kuruluşu",
    "strong fit": "güçlü eşleşme",
    "review": "inceleme",
    "reject": "reddet",
}


def _dedupe_clean(values) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = " ".join(str(value).split())
        key = text.casefold()
        if text and key not in seen:
            output.append(text)
            seen.add(key)
    return output


def _ordered_languages(preferences: dict, target_countries: list[str]) -> list[str]:
    values = [COUNTRY_LANGUAGE.get(country.upper()) for country in target_countries]
    values.extend(str(value).lower() for value in preferences.get("languages", []))
    values.append("en")
    return _dedupe_clean(value for value in values if value)


def _equivalents(mapping: dict, language: str) -> list[str]:
    terms: list[str] = []
    for equivalents in (mapping.get(language) or {}).values():
        if isinstance(equivalents, str):
            terms.append(equivalents)
        else:
            terms.extend(equivalents or [])
    return _dedupe_clean(terms)


def build_market_terms(scope: dict, profile: CompanyResearchProfile) -> MarketTermSet:
    """Build local search vocabulary without mutating canonical English."""
    canonical = _dedupe_clean(scope.get("product_terms", []))
    sector_ids = set(scope.get("sector_ids", []))
    selected_products = set(scope.get("product_ids", []))
    for product in profile.products:
        if not selected_products or str(product.get("id")) in selected_products:
            sector_ids.update(str(value) for value in product.get("sector_ids", []))
    targets = [str(value).upper() for value in scope.get(
        "target_countries", profile.market_preferences.get("target_countries", [])
    )]
    languages = _ordered_languages(profile.market_preferences, targets)
    sectors = [sector for sector in load_sectors() if sector.sector_id in sector_ids]
    playbooks = yaml.safe_load(
        (REFERENCE_DIR / "feature-playbooks.yaml").read_text(encoding="utf-8")
    ) or {}
    confirmed = profile.market_preferences.get("product_term_translations", {})

    by_language: dict[str, list[str]] = {}
    mapped_languages: set[str] = set()
    for language in languages:
        local: list[str] = []
        for sector in sectors:
            sector_values = _equivalents(sector.market_terms, language)
            local.extend(sector_values)
            if sector_values:
                mapped_languages.add(language)
            playbook_values = _equivalents(
                (playbooks.get(sector.sector_id) or {}).get("market_terms", {}), language
            )
            local.extend(playbook_values)
            if playbook_values:
                mapped_languages.add(language)
        local.extend(_equivalents(confirmed, language))
        if _equivalents(confirmed, language):
            mapped_languages.add(language)
        by_language[language] = _dedupe_clean([*canonical, *local])

    unmapped = [
        country for country in targets
        if COUNTRY_LANGUAGE.get(country) not in mapped_languages
        and COUNTRY_LANGUAGE.get(country) != "en"
    ]
    return MarketTermSet(
        canonical=canonical,
        by_language=by_language,
        unmapped_markets=_dedupe_clean(unmapped),
    )


class TranslationCache:
    """Tenant-scoped, content-addressed display translations."""

    def __init__(self, db, *, translate: TranslationFunction | None = None) -> None:
        self.db = db
        self.translate = translate

    @staticmethod
    def _content_hash(value_en: str, source_language: str) -> str:
        material = json.dumps(
            {"value_en": value_en, "source_language": source_language},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
        return hashlib.sha256(material).hexdigest()

    def get_or_generate(
        self,
        company_id: str,
        fact_key: str,
        value_en: str,
        source_language: str,
        locale: DisplayLocale,
    ) -> str:
        content_hash = self._content_hash(value_en, source_language)
        row = self.db.one(
            "SELECT display_value FROM research_translations "
            "WHERE company_id=? AND fact_key=? AND content_hash=? "
            "AND source_language=? AND display_locale=?",
            (company_id, fact_key, content_hash, source_language, locale),
        )
        if row:
            return row["display_value"]
        if self.translate is not None:
            display = " ".join(self.translate(value_en, source_language, locale).split())
        elif locale == "tr":
            display = FIXED_TR.get(value_en.casefold(), value_en)
        else:  # guarded by DisplayLocale, retained for defensive runtime callers
            display = value_en
        stamp = now()
        self.db.execute(
            "INSERT INTO research_translations("
            "id,company_id,fact_key,content_hash,source_language,display_locale,"
            "value_en,display_value,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_id("trn"), company_id, fact_key, content_hash, source_language,
             locale, value_en, display, stamp, stamp),
        )
        return display

    def evidence_value(
        self,
        *,
        company_id: str,
        fact_key: str,
        value_en: str,
        original: str,
        language: str,
        locale: DisplayLocale,
    ) -> dict[str, str]:
        display = value_en if locale == "en" else self.get_or_generate(
            company_id, fact_key, value_en, language, locale
        )
        return {
            "canonical": value_en,
            "original": original,
            "display": display,
            "source_language": language,
        }
