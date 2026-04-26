"""Regression test for shadow-coverage outage 2026-04-25.

The placeholder string `access-new` made it past _jwt_is_expired() because the
parser raised IndexError and the bare `except: return False` treated it as a
healthy token. Codex then 401'd on every shadow LLM call.
"""
from __future__ import annotations

import base64
import json
import time

from obs.oauth_llm import _jwt_is_expired


def _make_jwt(exp: int) -> str:
    """Minimal unsigned JWT with given exp claim. Header.payload.sig shape."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    sig = "deadbeef"
    return f"{header}.{body}.{sig}"


def test_valid_unexpired_jwt_returns_false():
    tok = _make_jwt(int(time.time()) + 3600)  # expires in 1h
    assert _jwt_is_expired(tok) is False


def test_expired_jwt_returns_true():
    tok = _make_jwt(int(time.time()) - 3600)  # expired 1h ago
    assert _jwt_is_expired(tok) is True


def test_jwt_within_skew_returns_true():
    tok = _make_jwt(int(time.time()) + 30)  # expires in 30s, skew is 60s
    assert _jwt_is_expired(tok) is True


def test_malformed_no_dots_returns_true():
    """The bug: 'access-new' has no dots, so split('.')[1] raises.
    Old code returned False; new code must return True so the caller falls
    through to the credential_pool fallback.
    """
    assert _jwt_is_expired("access-new") is True


def test_empty_string_returns_true():
    assert _jwt_is_expired("") is True


def test_one_segment_returns_true():
    assert _jwt_is_expired("notajwt") is True


def test_two_segments_returns_true():
    assert _jwt_is_expired("only.two") is True


def test_unparseable_payload_returns_true():
    """Three segments but middle isn't valid base64-JSON."""
    assert _jwt_is_expired("a.b.c") is True


def _make_jwt_with_payload(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.deadbeef"


def test_string_exp_returns_true():
    """Defensive: if a malformed token sets exp to a string, int() would
    accept it but the value isn't trustworthy. Reject as expired."""
    tok = _make_jwt_with_payload({"exp": "9999999999"})
    assert _jwt_is_expired(tok) is True


def test_missing_exp_returns_true():
    tok = _make_jwt_with_payload({"sub": "abc"})
    assert _jwt_is_expired(tok) is True


def test_null_payload_returns_true():
    """Three segments but middle decodes to JSON null, not a dict."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b'null').rstrip(b"=").decode()
    tok = f"{header}.{body}.deadbeef"
    assert _jwt_is_expired(tok) is True
