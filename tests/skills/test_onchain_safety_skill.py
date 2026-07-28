"""Tests for optional-skills/blockchain/onchain-safety/scripts/decode_action.py

Regression guards for the four high-risk calldata selectors. Mirrors the
GO / CAUTION / NO-GO verdict logic documented in SKILL.md.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DECODER = (
    _REPO
    / "optional-skills"
    / "blockchain"
    / "onchain-safety"
    / "scripts"
    / "decode_action.py"
)

# Load the stdlib-only decoder as a module (no package import needed).
_spec = importlib.util.spec_from_file_location("onchain_safety_decoder", _DECODER)
decoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decoder)


def _pad(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def _max_uint() -> str:
    return "f" * 64


def test_unlimited_approve_is_no_go():
    data = "0x095ea7b3" + _pad("0xDeadBeef") + _max_uint()
    r = decoder.decode(data)
    assert r["action"] == "approve"
    assert r["unlimited"] is True
    assert r["risk"] == "NO-GO"


def test_bounded_approve_is_caution():
    data = "0x095ea7b3" + _pad("0xDeadBeef") + "0" * 56 + "3b9aca00"  # 1000e6
    r = decoder.decode(data)
    assert r["action"] == "approve"
    assert r["unlimited"] is False
    assert r["risk"] == "CAUTION"


def test_set_approval_for_all_true_is_no_go():
    data = "0xa22cb465" + _pad("0xOperator") + "0" * 63 + "1"
    r = decoder.decode(data)
    assert r["action"] == "setApprovalForAll"
    assert r["approved"] is True
    assert r["risk"] == "NO-GO"


def test_set_approval_for_all_false_is_ok():
    data = "0xa22cb465" + _pad("0xOperator") + "0" * 64
    r = decoder.decode(data)
    assert r["approved"] is False
    assert r["risk"] == "ok"


def test_unknown_selector_is_unknown():
    data = "0x12345678" + "0" * 64
    r = decoder.decode(data)
    assert r["action"] == "unknown"
    assert r["risk"] == "unknown"


def test_short_calldata_is_safe_unknown():
    # < 8 hex chars after 0x → selector cannot be read → unknown
    r = decoder.decode("0x095e")
    assert r["action"] == "unknown"
