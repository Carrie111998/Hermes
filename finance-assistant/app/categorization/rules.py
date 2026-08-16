from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class CategoryResult:
    category: str
    subcategory: str | None
    source: str
    confidence: float


def _rules_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "categories.yaml"


def normalize_merchant(value: str) -> str:
    text = value.upper().replace("İ", "I")
    text = re.sub(r"\b(TR|TURKIYE|A\.S\.|AS|ANKARA|ISTANBUL)\b", " ", text)
    text = re.sub(r"\b\d{3,}\b", " ", text)
    text = re.sub(r"[^A-ZÇĞÖŞÜ0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {"MIGROS TICARET": "MIGROS", "MIGROS SANAL MARKET": "MIGROS"}
    for alias, canonical in aliases.items():
        if text.startswith(alias):
            return canonical
    return text


def _load_rules() -> dict:
    try:
        with _rules_path().open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}


def categorize(merchant_normalized: str, description: str = "") -> CategoryResult:
    haystack = f"{merchant_normalized} {description}".upper()
    for category, entries in _load_rules().get("categories", {}).items():
        if isinstance(entries, dict):
            for subcategory, keywords in entries.items():
                if any(str(keyword).upper() in haystack for keyword in keywords):
                    return CategoryResult(category, subcategory, "rule", 1.0)
        elif any(str(keyword).upper() in haystack for keyword in entries):
            return CategoryResult(category, None, "rule", 1.0)
    return CategoryResult("Diğer", None, "rule", 0.25)
