"""Deterministic QA + run-output parsing checks (Turkish-operator product)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.agent_service import extract_json
from server.quality import normalize_name, preflight_message


BASE = {"to": "anna@example.com", "cc": []}


def test_german_umlauts_pass_preflight():
    # PRODUCT.md demo contact: Anna Müller, Germany campaign.
    result = preflight_message({
        **BASE, "language": "de",
        "subject": "Partnerschaft im Bereich Küchengeräte",
        "body": "Sehr geehrte Frau Müller, wir würden uns über eine Zusammenarbeit freuen.",
    })
    assert result.passed, result.failures


def test_turkish_leak_into_foreign_email_fails():
    result = preflight_message({
        **BASE, "language": "de",
        "subject": "Partnership",
        "body": "Dear Ms. Weber, ürünlerimizi tanıtmak isteriz.",
    })
    assert "operator_language_contamination" in result.failures


def test_turkish_language_email_passes():
    result = preflight_message({
        **BASE, "language": "tr",
        "subject": "İş birliği fırsatı",
        "body": "Sayın yetkili, ürünlerimizi tanıtmak isteriz. Saygılarımızla.",
    })
    assert result.passed, result.failures


def test_null_cc_does_not_crash():
    result = preflight_message({"to": "a@b.co", "cc": None, "language": "en",
                                "subject": "Hi", "body": "Hello there."})
    assert result.passed, result.failures


def test_extract_json_takes_last_top_level_object():
    stdout = (
        'Loading skill\nPAYLOAD:\n{"lead_id": "lead_1", "to": "x@y.com"}\n'
        'log line\n'
        '{"subject": "Hi", "body": "Final", "qa_verdict": {"pass": true, "failures": []}}\n'
    )
    result = extract_json(stdout)
    assert result["subject"] == "Hi"
    # Nested qa_verdict object must not be mistaken for the answer.
    assert result["qa_verdict"] == {"pass": True, "failures": []}


def test_normalize_name_folds_turkish_and_diacritics():
    assert normalize_name("İSTANBUL Mutfak") == normalize_name("istanbul mutfak")
    assert normalize_name("ISPARTA Gıda") == normalize_name("ısparta gida")
    assert normalize_name("Müller  GmbH") == normalize_name("muller gmbh")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} quality checks passed")
