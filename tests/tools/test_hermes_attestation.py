"""A2 Hermes attestation fixed vectors.

The expected canonical strings mirror Backend B's dps-hermes-attestation.ts:
code-point key sorting, JSON-compatible finite values only, and HMAC over the
canonical payload bytes.
"""

from decimal import Decimal
import hashlib
import hmac

import pytest


def test_canonical_hermes_json_cross_language_vectors():
    from tools.hermes_attestation import canonical_hermes_json

    assert canonical_hermes_json({
        "\U0001f600": 1,
        "\uffff": 2,
        "a": [{"z": -0.0, "A": Decimal("1.25")}],
    }) == '{"a":[{"A":1.25,"z":0}],"\uffff":2,"\U0001f600":1}'

    assert canonical_hermes_json({
        "null": None,
        "bools": [True, False],
        "ints": [0, -7, 42],
        "decimals": [Decimal("12.50"), Decimal("0.125")],
    }) == '{"bools":[true,false],"decimals":[12.5,0.125],"ints":[0,-7,42],"null":null}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_canonical_hermes_json_rejects_non_finite_and_unsupported(value):
    from tools.hermes_attestation import HermesAttestationError, canonical_hermes_json

    with pytest.raises(HermesAttestationError):
        canonical_hermes_json({"bad": value})


@pytest.mark.parametrize(
    "value",
    [
        2**53 - 1,
        -(2**53 - 1),
        {"nested": [2**53 - 1, {"proposal": -(2**53 - 1)}]},
    ],
)
def test_canonical_hermes_json_accepts_js_safe_integer_boundaries(value):
    from tools.hermes_attestation import canonical_hermes_json

    assert canonical_hermes_json(value)


@pytest.mark.parametrize(
    "value",
    [
        2**53,
        -(2**53),
        {"args": {"amount": 2**53}},
        {"proposal": [{"target_version": -(2**53)}]},
    ],
)
def test_canonical_hermes_json_rejects_ints_outside_js_safe_range(value):
    from tools.hermes_attestation import HermesAttestationError, canonical_hermes_json

    with pytest.raises(HermesAttestationError, match="safe integer"):
        canonical_hermes_json(value)


def test_attestation_policy_requires_scrubbable_key_env_name():
    from tools.hermes_attestation import HermesAttestationError, HermesAttestationPolicy

    with pytest.raises(HermesAttestationError, match="must end with _KEY"):
        HermesAttestationPolicy.from_mapping({
            "enabled": True,
            "kid_env": "A2_KID",
            "key_env": "A2_SECRET",
            "oauth_client_id_env": "A2_CLIENT",
            "operator_subject_env": "A2_SUBJECT",
        })


def test_hmac_attestation_token_fixed_vector(monkeypatch):
    from tools.hermes_attestation import HermesAttestationPolicy, canonical_hermes_json, sign_attestation

    monkeypatch.setenv("A2_KID", "h1")
    monkeypatch.setenv("A2_KEY", "h" * 64)
    monkeypatch.setenv("A2_CLIENT", "hermes-client")
    monkeypatch.setenv("A2_SUBJECT", "auth0|marcial")
    monkeypatch.setattr("tools.hermes_attestation.secrets.token_urlsafe", lambda _n: "nonce-fixed")

    policy = HermesAttestationPolicy.from_mapping({
        "enabled": True,
        "kid_env": "A2_KID",
        "key_env": "A2_KEY",
        "oauth_client_id_env": "A2_CLIENT",
        "operator_subject_env": "A2_SUBJECT",
    })
    ctx = {
        "platform": "telegram",
        "user_id": "u_123",
        "chat_id": "chat_123",
        "thread_id": "thread_123",
        "session_id": "session_123",
        "session_key": "key_123",
        "message_id": "msg_123",
    }
    token = sign_attestation(
        policy=policy,
        phase="prepare",
        tool_name="dps_prepare_project_note",
        args={"b": 1, "a": Decimal("2.5")},
        ctx=ctx,
        now_seconds=1000,
    )
    payload = {
        "v": 1,
        "kid": "h1",
        "phase": "prepare",
        "tool_name": "dps_prepare_project_note",
        "oauth_client_id": "hermes-client",
        "operator_subject": "auth0|marcial",
        "platform": "telegram",
        "platform_user_id": "u_123",
        "chat_id": "chat_123",
        "thread_id": "thread_123",
        "session_id": "session_123",
        "originating_user_message_id": "msg_123",
        "iat": 1000,
        "exp": 1300,
        "nonce": "nonce-fixed",
        "tool_args_digest": hashlib.sha256(b'{"a":2.5,"b":1}').hexdigest(),
    }
    canonical = canonical_hermes_json(payload)
    signature = hmac.new(b"h" * 64, canonical.encode("utf-8"), hashlib.sha256).digest()
    import base64

    encoded = base64.urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    assert token == f"hmac-sha256-v1.{encoded}.{sig}"
