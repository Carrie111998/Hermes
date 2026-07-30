"""Tests for optional-skills/blockchain/onchain-safety/scripts/decode_action.py

Regression guards for the four high-risk selectors. Covers:
- fail-closed on malformed/truncated calldata
- permit deadline=0 -> NO-GO
- swap deadline=0 -> NO-GO
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DECODER = (
    _REPO / "optional-skills" / "blockchain" / "onchain-safety" / "scripts" / "decode_action.py"
)

# Load the stdlib-only decoder as a module (no package import needed).
_spec = importlib.util.spec_from_file_location("onchain_safety_decoder", _DECODER)
decoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decoder)


def _word(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def test_unlimited_approve_is_no_go():
    data = "0x095ea7b3" + _word("0xDeadBeef") + "f" * 64
    r = decoder.decode(data)
    assert r["action"] == "approve"
    assert r["unlimited"] is True
    assert r["risk"] == "NO-GO"


def test_bounded_approve_is_caution():
    data = "0x095ea7b3" + _word("0xDeadBeef") + "0" * 56 + "3b9aca00"
    r = decoder.decode(data)
    assert r["unlimited"] is False
    assert r["risk"] == "CAUTION"


def test_truncated_approve_is_no_go():
    # selector only, no ABI body -> fail-closed
    r = decoder.decode("0x095ea7b3")
    assert r["risk"] == "NO-GO"
    assert "malformed" in r["reason"]


def test_set_approval_for_all_true_is_no_go():
    data = "0xa22cb465" + _word("0xOperator") + "0" * 63 + "1"
    r = decoder.decode(data)
    assert r["approved"] is True
    assert r["risk"] == "NO-GO"


def test_set_approval_for_all_false_is_ok():
    data = "0xa22cb465" + _word("0xOperator") + "0" * 64
    r = decoder.decode(data)
    assert r["approved"] is False
    assert r["risk"] == "ok"


def test_permit_deadline_zero_is_no_go():
    body = _word("0xToken") + _word("0xOwner") + _word("0xSpender") + _word("0") + "0" * 64 + _word("0") + _word("0") + _word("0")
    r = decoder.decode("0xd505accf" + body)
    assert r["risk"] == "NO-GO"
    assert r["deadline"] == "0"


def test_permit_deadline_nonzero_is_caution():
    body = _word("0xToken") + _word("0xOwner") + _word("0xSpender") + _word("0") + ("0" * 63 + "1") + _word("0") + _word("0") + _word("0")
    r = decoder.decode("0xd505accf" + body)
    assert r["risk"] == "CAUTION"
    assert r["deadline"] == "1"


def test_swap_deadline_zero_is_no_go():
    body = _word("0x1") + _word("0x0") + _word("0x0") + _word("0x0") + "0" * 64
    r = decoder.decode("0x38ed1739" + body)
    assert r["risk"] == "NO-GO"
    assert r["deadline"] == "0"


def test_unknown_selector_is_unknown():
    r = decoder.decode("0x12345678" + "0" * 64)
    assert r["action"] == "unknown"
    assert r["risk"] == "unknown"
