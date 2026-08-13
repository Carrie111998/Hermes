"""Tests for the OAG boundary (enterprise/oag.py)."""

from __future__ import annotations

import time

import pytest

from enterprise.errors import AdmissionError
from enterprise.oag import OAGVerifier, TrustConfig, mint_test_token

SECRET = "test-secret-material"


def make_config(**overrides):
    defaults = dict(
        issuer="https://idp.example.com",
        audience="hermes-enterprise",
        installation="prod-install",
        hs256_secret=SECRET,
        tenant_claim="org",
        tenant_map={"acme": "ns-acme", "globex": "ns-globex"},
    )
    defaults.update(overrides)
    return TrustConfig(**defaults)


def test_valid_token_admits_installation_scope():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "org": "acme"}, SECRET)
    ident = OAGVerifier(cfg).verify(token)
    assert ident.subject == "user-1"
    assert ident.installation == "prod-install"
    assert ident.issuer == cfg.issuer
    assert ident.namespace is None


def test_namespace_admission_via_tenant_map():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "org": "acme"}, SECRET)
    ident = OAGVerifier(cfg).verify(token, require_namespace="ns-acme")
    assert ident.namespace == "ns-acme"
    # admit() mirrors verify()
    ident2 = OAGVerifier(cfg).admit(token, namespace="ns-acme")
    assert ident2.namespace == "ns-acme"


def test_alg_none_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1"}, SECRET, header={"alg": "none"})
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_wrong_signature_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1"}, "wrong-secret")
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_tampered_payload_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1"}, SECRET)
    good = mint_test_token(cfg, {"sub": "admin"}, "wrong-secret")
    frankentoken = ".".join([token.split(".")[0], good.split(".")[1], token.split(".")[2]])
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(frankentoken)


def test_expired_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "exp": int(time.time()) - 3600}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_not_yet_valid_rejected():
    cfg = make_config()
    token = mint_test_token(
        cfg, {"sub": "user-1", "nbf": int(time.time()) + 3600}, SECRET
    )
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_wrong_audience_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "aud": "other-audience"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_wrong_issuer_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "iss": "https://evil.example.com"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)


def test_missing_sub_rejected():
    cfg = make_config()
    token = mint_test_token(cfg, {"org": "acme"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token)
    empty = mint_test_token(cfg, {"sub": "   "}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(empty)


def test_tenant_mismatch_rejected_for_namespace_scope():
    cfg = make_config()
    # tenant maps to ns-globex, requesting ns-acme
    token = mint_test_token(cfg, {"sub": "user-1", "org": "globex"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(token, require_namespace="ns-acme")
    # tenant claim missing entirely
    no_tenant = mint_test_token(cfg, {"sub": "user-1"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(no_tenant, require_namespace="ns-acme")
    # tenant not in the map at all
    unknown = mint_test_token(cfg, {"sub": "user-1", "org": "initech"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(unknown, require_namespace="ns-acme")


def test_caller_claim_cannot_override_installation():
    cfg = make_config()
    token = mint_test_token(
        cfg, {"sub": "user-1", "installation": "attacker-install"}, SECRET
    )
    ident = OAGVerifier(cfg).verify(token)
    assert ident.installation == "prod-install"


def test_required_claims_enforced():
    cfg = make_config(required_claims={"tier": "enterprise"})
    missing = mint_test_token(cfg, {"sub": "user-1"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(missing)
    wrong = mint_test_token(cfg, {"sub": "user-1", "tier": "free"}, SECRET)
    with pytest.raises(AdmissionError):
        OAGVerifier(cfg).verify(wrong)
    ok = mint_test_token(cfg, {"sub": "user-1", "tier": "enterprise"}, SECRET)
    assert OAGVerifier(cfg).verify(ok).subject == "user-1"


def test_malformed_token_rejected():
    cfg = make_config()
    verifier = OAGVerifier(cfg)
    for bad in ("", "a.b", "not-a-jwt", "a.b.c.d"):
        with pytest.raises(AdmissionError):
            verifier.verify(bad)


def test_error_messages_never_leak_token_contents():
    cfg = make_config()
    token = mint_test_token(cfg, {"sub": "user-1", "iss": "https://evil.example.com"}, SECRET)
    with pytest.raises(AdmissionError) as exc:
        OAGVerifier(cfg).verify(token)
    msg = str(exc.value)
    assert "evil.example.com" not in msg
    assert "user-1" not in msg
    assert token.split(".")[1] not in msg
