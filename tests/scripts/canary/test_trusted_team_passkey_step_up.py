from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


ISOLATED_RUNTIME_ENV = "MUNCHO_OWNER_GATE_ISOLATED_TEST_RUNTIME"
if os.environ.get(ISOLATED_RUNTIME_ENV) != "1":
    pytest.skip(
        "runs under the exact owner-gate WebAuthn dependency boundary",
        allow_module_level=True,
    )

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from gateway.operational_edge_protocol import (
    OperationalCapability,
    OperationalIntent,
    sha256_json,
)
from scripts.canary import passkey_v2_enrollment as enrollment
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_sensitive_report as sensitive
from scripts.canary import passkey_v2_service as service
from scripts.canary import passkey_v2_webauthn as webauthn
from scripts.canary.passkey_v2_signer import ReceiptSigner
from scripts.canary.passkey_v2_sqlite import PasskeyV2AuthorityDatabase
from scripts.canary.passkey_v2_sqlite import bootstrap_authority_database


NOW = 1_785_000_000
IVS = "1391703330711142472"
EMIL = "1279454038731264061"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _intent() -> OperationalIntent:
    arguments = {
        "db": "skyvisio_fp",
        "query": "SELECT email, total FROM orders_new LIMIT 10;",
        "case_id": "case:ivs-daily-report",
        "requester": "Ivs",
        "requester_id": IVS,
        "purpose": "Daily customer payment reconciliation",
        "max_rows": 10,
        "timeout_seconds": 15,
        "expected_result_shape": "bounded_rows",
        "redact_field": ["email"],
    }
    return OperationalIntent(
        operation_id=sensitive.OPERATION_ID,
        arguments=arguments,
        arguments_sha256=sha256_json(arguments),
        idempotency_key="case:ivs-daily-report:query:1",
    )


def _capability(intent: OperationalIntent) -> OperationalCapability:
    return OperationalCapability(
        authority_kind="canonical_plan",
        authority_ref="canonical-plan:" + "a" * 64,
        operation_id=intent.operation_id,
        arguments_sha256=intent.arguments_sha256,
        idempotency_key=intent.idempotency_key,
        issued_at_unix_ms=NOW * 1000,
        expires_at_unix_ms=(NOW + 300) * 1000,
        subject_discord_user_id=IVS,
        case_id="case:ivs-daily-report",
        operator_tier="top",
    )


def _action(
    intent: OperationalIntent,
    capability: OperationalCapability,
    token: bytes,
) -> Mapping[str, Any]:
    return sensitive.build_action_envelope(
        capability=capability,
        intent=intent,
        retrieval_token=token,
        request_id="R" * 32,
        executor_release_sha="a" * 40,
        authority_release_sha="b" * 40,
        authority_manifest_sha256="c" * 64,
        authority_host_receipt_sha256="d" * 64,
        source_preflight_sha256="e" * 64,
        live_projection_sha256="f" * 64,
        prior_authoritative_receipt_sha256="1" * 64,
        prior_event_head_sha256="2" * 64,
        issued_at_unix=NOW,
        approval_ttl_seconds=300,
    )


def _credential_and_assertion(
    *,
    owner: str,
    action: Mapping[str, Any],
    challenge: Mapping[str, Any],
    credential_id: bytes,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    public_key = cbor2.dumps({
        1: 2,
        3: -7,
        -1: 1,
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    })
    credential = webauthn.build_migrated_credential(
        owner_discord_user_id=owner,
        credential_id=credential_id,
        public_key_cose=public_key,
        rp_id=protocol.PRODUCTION_RP_ID,
        origin=protocol.PRODUCTION_ORIGIN,
        imported_at_unix=NOW - 10,
        migration_receipt_sha256=hashlib.sha256(owner.encode()).hexdigest(),
        initial_sign_count=0,
        initial_credential_backed_up=True,
        expected_user_handle=owner.encode("ascii"),
    )
    client_data = json.dumps(
        {
            "type": "webauthn.get",
            "challenge": challenge["challenge_b64url"],
            "origin": protocol.PRODUCTION_ORIGIN,
            "crossOrigin": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    authenticator_data = (
        hashlib.sha256(protocol.PRODUCTION_RP_ID.encode("ascii")).digest()
        + bytes([0x1D])
        + (0).to_bytes(4, "big")
    )
    signature = private_key.sign(
        authenticator_data + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    assertion = {
        "schema": webauthn.ASSERTION_SCHEMA,
        "credential": {
            "id": _b64(credential_id),
            "rawId": _b64(credential_id),
            "response": {
                "clientDataJSON": _b64(client_data),
                "authenticatorData": _b64(authenticator_data),
                "signature": _b64(signature),
                "userHandle": _b64(owner.encode("ascii")),
            },
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        },
    }
    return credential, assertion


def test_ivs_passkey_authorizes_only_her_exact_query_and_replays_safely(
    tmp_path: Path,
) -> None:
    intent = _intent()
    capability = _capability(intent)
    retrieval_token = b"r" * 32
    action = _action(intent, capability, retrieval_token)
    bootstrap_authority_database(
        tmp_path / "authority.sqlite3",
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        now_unix=NOW - 1,
        require_root=False,
    )
    authority = PasskeyV2AuthorityDatabase(
        tmp_path / "authority.sqlite3",
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )
    authority.create_request(action)
    challenge = protocol.build_challenge_record(
        envelope=action,
        challenge_id="C" * 32,
        challenge_b64url=_b64(b"challenge" * 4),
        rp_id=protocol.PRODUCTION_RP_ID,
        origin=protocol.PRODUCTION_ORIGIN,
        created_at_unix=NOW + 1,
    )
    authority.create_challenge(challenge, envelope=action)
    ivs_credential, ivs_assertion = _credential_and_assertion(
        owner=IVS,
        action=action,
        challenge=challenge,
        credential_id=b"ivs-phone-passkey-credential",
    )
    emil_credential, _ = _credential_and_assertion(
        owner=EMIL,
        action=action,
        challenge=challenge,
        credential_id=b"emil-phone-passkey-credential",
    )
    authority.import_migrated_credential(ivs_credential)
    authority.import_migrated_credential(emil_credential)

    options = service._authority_options(authority, action["request_id"])
    assert [row["id"] for row in options["publicKey"]["allowCredentials"]] == [
        ivs_credential["credential_id_b64url"]
    ]
    grant = authority.verify_assertion_and_record_grant(
        assertion=ivs_assertion,
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        now_unix=NOW + 2,
    )
    signer = ReceiptSigner(ed25519.Ed25519PrivateKey.generate())
    runtime = protocol.build_runtime_binding(
        executor_release_sha="a" * 40,
        executor_plan_sha256=action["executor_plan_sha256"],
        executor_binary_sha256="3" * 64,
        mutation_wrapper_sha256="4" * 64,
        remote_transport_sha256="5" * 64,
    )
    consumed = authority.consume_or_replay(
        envelope=action,
        runtime_binding=runtime,
        consume_attempt_id="6" * 64,
        signer=signer,
        now_unix=NOW + 3,
    )
    receipt = sensitive.validate_authorization_receipt(
        receipt=consumed.receipt,
        envelope=action,
        grant=grant,
        challenge=challenge,
        receipt_public_key=signer.public_key,
        intent=intent,
        capability=capability,
        now_unix=NOW + 4,
    )
    assert sensitive.require_retrieval_token(
        action, retrieval_token
    )["requester_discord_user_id"] == IVS

    journal = sensitive.SensitiveReportAuthorizationJournal(
        tmp_path / "edge" / "journal.sqlite3"
    )
    assert journal.consume_once(
        receipt_sha256=receipt["receipt_sha256"],
        intent=intent,
        now_unix=NOW + 4,
    ) == "consumed"
    assert journal.consume_once(
        receipt_sha256=receipt["receipt_sha256"],
        intent=intent,
        now_unix=NOW + 5,
    ) == "replayed_same_intent"
    changed = OperationalIntent(
        operation_id=intent.operation_id,
        arguments={**intent.arguments, "query": "SELECT id FROM orders_new LIMIT 1"},
        arguments_sha256=sha256_json(
            {**intent.arguments, "query": "SELECT id FROM orders_new LIMIT 1"}
        ),
        idempotency_key="case:ivs-daily-report:query:2",
    )
    with pytest.raises(
        sensitive.SensitiveReportPasskeyError,
        match="authorization_replay_forbidden",
    ):
        journal.consume_once(
            receipt_sha256=receipt["receipt_sha256"],
            intent=changed,
            now_unix=NOW + 5,
        )
    journal.close()


def test_cross_user_and_query_drift_fail_closed() -> None:
    intent = _intent()
    wrong_intent = OperationalIntent(
        operation_id=intent.operation_id,
        arguments={**intent.arguments, "requester_id": EMIL},
        arguments_sha256=sha256_json(
            {**intent.arguments, "requester_id": EMIL}
        ),
        idempotency_key=intent.idempotency_key,
    )
    capability = OperationalCapability(
        authority_kind="canonical_plan",
        authority_ref="canonical-plan:" + "b" * 64,
        operation_id=wrong_intent.operation_id,
        arguments_sha256=wrong_intent.arguments_sha256,
        idempotency_key=wrong_intent.idempotency_key,
        issued_at_unix_ms=NOW * 1000,
        expires_at_unix_ms=(NOW + 300) * 1000,
        subject_discord_user_id=IVS,
        case_id="case:ivs-daily-report",
        operator_tier="top",
    )
    with pytest.raises(
        sensitive.SensitiveReportPasskeyError,
        match="identity_binding_invalid",
    ):
        sensitive.build_action_envelope(
            capability=capability,
            intent=wrong_intent,
            retrieval_token=b"r" * 32,
            request_id="R" * 32,
            executor_release_sha="a" * 40,
            authority_release_sha="b" * 40,
            authority_manifest_sha256="c" * 64,
            authority_host_receipt_sha256="d" * 64,
            source_preflight_sha256="e" * 64,
            live_projection_sha256="f" * 64,
            prior_authoritative_receipt_sha256="1" * 64,
            prior_event_head_sha256="2" * 64,
            issued_at_unix=NOW,
        )
    capability = _capability(intent)
    action = _action(intent, capability, b"r" * 32)
    with pytest.raises(
        sensitive.SensitiveReportPasskeyError,
        match="retrieval_token_invalid",
    ):
        sensitive.require_retrieval_token(action, b"x" * 32)


def test_owner_invite_registers_one_phone_passkey_without_report_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation, token = enrollment.create_invitation(
        root=tmp_path,
        owner_discord_user_id=IVS,
        user_label="Ivs",
        now_unix=NOW,
        ttl_seconds=3600,
    )
    options = enrollment.registration_options(
        root=tmp_path,
        invitation_id=invitation["invitation_id"],
        token=token,
        now_unix=NOW + 1,
    )
    assert options["publicKey"]["rp"]["id"] == protocol.PRODUCTION_RP_ID
    assert options["publicKey"]["user"]["name"] == "Ivs"
    assert sensitive.FACTS_SCHEMA.encode("ascii") in service._APPROVAL_JS

    verified = SimpleNamespace(
        credential_id=b"ivs-new-phone-passkey",
        credential_public_key=cbor2.dumps({1: 2, 3: -7, -1: 1, -2: b"x" * 32, -3: b"y" * 32}),
        sign_count=0,
        credential_backed_up=True,
    )
    monkeypatch.setattr(
        enrollment,
        "_load_registration_verifier",
        lambda: (lambda **_kwargs: verified, ValueError),
    )
    credential = enrollment.complete_enrollment(
        root=tmp_path,
        invitation_id=invitation["invitation_id"],
        token=token,
        credential={"browser": "registration"},
        now_unix=NOW + 2,
    )
    assert credential["owner_discord_user_id"] == IVS
    assert credential["migration_receipt_sha256"]
    replay = enrollment.complete_enrollment(
        root=tmp_path,
        invitation_id=invitation["invitation_id"],
        token=token,
        credential={"ignored": "after create-only receipt"},
        now_unix=NOW + 3,
    )
    assert replay == credential
    with pytest.raises(
        enrollment.PasskeyV2EnrollmentError,
        match="invitation_denied",
    ):
        enrollment.registration_options(
            root=tmp_path,
            invitation_id=invitation["invitation_id"],
            token=b"w" * 32,
            now_unix=NOW + 3,
        )


def test_enrollment_state_and_runtime_mismatch_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation, token = enrollment.create_invitation(
        root=tmp_path,
        owner_discord_user_id=IVS,
        user_label="Ivs",
        now_unix=NOW,
    )
    invitation_path = (
        tmp_path / "invitations" / f"{invitation['invitation_id']}.json"
    )
    invitation_path.chmod(0o600)
    with pytest.raises(
        enrollment.PasskeyV2EnrollmentError,
        match="state_invalid",
    ):
        enrollment.registration_options(
            root=tmp_path,
            invitation_id=invitation["invitation_id"],
            token=token,
            now_unix=NOW + 1,
        )

    monkeypatch.setattr(
        enrollment.importlib.metadata,
        "version",
        lambda package: "0.0.0" if package == "webauthn" else {
            "cbor2": "6.1.3",
            "cryptography": "49.0.0",
        }[package],
    )
    with pytest.raises(
        enrollment.PasskeyV2EnrollmentError,
        match="runtime_mismatch",
    ):
        enrollment._require_selected_runtime()
