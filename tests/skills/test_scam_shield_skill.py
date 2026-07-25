"""Tests for the scam-shield skill. Stdlib + pytest only, no network."""
import importlib.util
from pathlib import Path

import pytest

# Locate the skill's scanner relative to the repo root (tests/skills/ -> repo).
_REPO = Path(__file__).resolve().parents[2]
_SCAN = _REPO / "skills" / "security" / "scam-shield" / "scripts" / "scan.py"


def _load_scan():
    spec = importlib.util.spec_from_file_location("scam_shield_scan", _SCAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()
PATTERNS = scan.load_patterns(_SCAN.parent.parent / "references" / "patterns.json")


def test_patterns_file_loads_and_has_signals():
    assert PATTERNS["signals"]
    for sig in PATTERNS["signals"]:
        assert 0.0 <= sig["weight"] <= 1.0
        assert sig["id"] and sig["scheme_tag"] and sig["safe_action"]


def test_seed_phrase_scam_scores_high():
    rep = scan.build_report(
        "Служба безопасности. Срочно подтверди кошелёк и введи сид фразу "
        "на https://metamask-verify.top иначе аккаунт будет заблокирован",
        PATTERNS,
    )
    assert rep["risk_score"] >= 80
    assert rep["risk_band"] in ("высокий", "очень высокий")
    assert "credential_theft" in rep["scheme_tags"]
    assert rep["safe_actions"]


def test_benign_message_scores_low():
    rep = scan.build_report("Привет, скинь презентацию с прошлой встречи, спасибо", PATTERNS)
    assert rep["risk_score"] < 20
    assert rep["risk_band"] == "низкий"
    assert rep["reasons"] == []


def test_score_never_exceeds_100():
    stacked = " ".join(s["keywords"][0] for s in PATTERNS["signals"])
    rep = scan.build_report(stacked + " https://binance-login.xyz", PATTERNS)
    assert 0 <= rep["risk_score"] <= 100


def test_otp_request_flags_account_takeover():
    rep = scan.build_report("Это банк, продиктуй код из смс", PATTERNS)
    assert "account_takeover" in rep["scheme_tags"]


def test_lookalike_domain_detected_but_legit_subdomain_is_not():
    fake = scan.build_report("open https://metamask-verify.top now", PATTERNS)
    assert any("маскируется" in n for n in fake["url_findings"])
    legit = scan.build_report("see https://support.metamask.io/help", PATTERNS)
    assert not any("маскируется" in n for n in legit["url_findings"])


def test_report_always_has_disclaimer():
    rep = scan.build_report("", PATTERNS)
    assert rep["disclaimer"]
    assert rep["risk_score"] == 0


def test_noisy_or_is_monotonic_and_bounded():
    assert scan._noisy_or([]) == 0.0
    assert 0.0 < scan._noisy_or([0.5]) < 1.0
    assert scan._noisy_or([0.9, 0.9, 0.9]) < 1.0
    assert scan._noisy_or([0.5, 0.5]) > scan._noisy_or([0.5])
