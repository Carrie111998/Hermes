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

from gateway import operational_edge_service as edge_service
from gateway.operational_edge_protocol import (
    OperationalCapability,
    OperationalIntent,
    OperationalRequest,
    SignedEnvelope,
    sha256_json,
    sign_envelope,
)
from gateway.operational_edge_service import (
    OperationalEdgeService,
    OperationalEdgeServiceError,
)
from scripts.canary import passkey_v2_enrollment as enrollment
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_sensitive_report as sensitive
from scripts.canary import passkey_v2_sensitive_report_transport as transport
from scripts.canary import passkey_v2_service as service
from scripts.canary import passkey_v2_webauthn as webauthn
from scripts.canary.passkey_v2_signer import ReceiptSigner
from scripts.canary.passkey_v2_sqlite import PasskeyV2AuthorityDatabase
from scripts.canary.passkey_v2_sqlite import bootstrap_authority_database
from ops.muncho.runtime import operational_edge_cli


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
        "discord_guild_id": "1504852355588423801",
        "discord_channel_id": "1531409183163813970",
        "discord_thread_id": "1534518002307829850",
        "discord_message_id": "1534518002307829851",
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


def _capability(
    intent: OperationalIntent,
    *,
    issued_at_unix: int = NOW,
    ttl_seconds: int = 300,
) -> OperationalCapability:
    return OperationalCapability(
        authority_kind="canonical_plan",
        authority_ref="canonical-plan:" + "a" * 64,
        operation_id=intent.operation_id,
        arguments_sha256=intent.arguments_sha256,
        idempotency_key=intent.idempotency_key,
        issued_at_unix_ms=issued_at_unix * 1000,
        expires_at_unix_ms=(issued_at_unix + ttl_seconds) * 1000,
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


def test_cloud_relay_to_phone_passkey_to_exact_query_bundle_is_real_and_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the disposable HTTPS payload through the sealed authority."""

    intent = _intent()
    capability = _capability(intent)
    writer_private = ed25519.Ed25519PrivateKey.generate()
    writer_key_id = hashlib.sha256(
        writer_private.public_key().public_bytes_raw()
    ).hexdigest()
    capability_envelope = sign_envelope(
        capability.to_mapping(),
        key_id=writer_key_id,
        private_key=writer_private,
    ).to_mapping()
    create_frame = transport.build_frame(
        operation="create",
        capability_envelope=capability_envelope,
        intent=intent,
    )

    database_path = tmp_path / "authority.sqlite3"
    bootstrap_authority_database(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        now_unix=NOW - 1,
        require_root=False,
    )
    authority = PasskeyV2AuthorityDatabase(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )
    receipt_signer = ReceiptSigner(ed25519.Ed25519PrivateKey.generate())
    monkeypatch.setattr(service, "_release_revision", lambda: "a" * 40)
    monkeypatch.setattr(
        service,
        "_local_cutover_authority_binding",
        lambda _revision, _plan: ({}, "b" * 64, "c" * 64, {}),
    )

    create_response = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame("sensitive_create", create_frame),
            authority=authority,
            signer=receipt_signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW,
            writer_public_key=writer_private.public_key(),
        ),
        expected_operation="sensitive_create",
    )
    assert create_response["state"] == "pending"
    assert create_response["approval_url"] == (
        f"{protocol.PRODUCTION_ORIGIN}/approve/{create_frame['request_id']}"
    )
    create_json = json.dumps(create_response, sort_keys=True)
    assert _b64(
        transport.retrieval_token(capability_envelope, intent)
    ) not in create_json
    assert "retrieval_token_b64" not in create_json

    action = create_response["action_envelope"]
    challenge = create_response["challenge_record"]
    credential, assertion = _credential_and_assertion(
        owner=IVS,
        action=action,
        challenge=challenge,
        credential_id=b"ivs-cloud-phone-passkey",
    )
    authority.import_migrated_credential(credential)
    grant = authority.verify_assertion_and_record_grant(
        assertion=assertion,
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        now_unix=NOW + 1,
    )
    assert grant["approver_discord_user_id"] == IVS

    runtime = transport.build_runtime_binding(
        action_envelope=action,
        capability_envelope=capability_envelope,
        intent=intent,
    )
    consume_frame = transport.build_frame(
        operation="consume",
        capability_envelope=capability_envelope,
        intent=intent,
        runtime_binding=runtime,
    )
    consume_response = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame("sensitive_consume", consume_frame),
            authority=authority,
            signer=receipt_signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW + 2,
            writer_public_key=writer_private.public_key(),
        ),
        expected_operation="sensitive_consume",
    )
    bundle = transport.step_up_bundle(
        response=consume_response,
        capability_envelope=capability_envelope,
        intent=intent,
    )
    receipt = sensitive.validate_authorization_receipt(
        receipt=bundle["authorization_receipt"],
        envelope=bundle["action_envelope"],
        grant=bundle["grant_record"],
        challenge=bundle["challenge_record"],
        receipt_public_key=receipt_signer.public_key,
        intent=intent,
        capability=capability,
        now_unix=NOW + 3,
    )
    assert receipt["approver_discord_user_id"] == IVS
    assert sensitive.require_retrieval_token(
        bundle["action_envelope"],
        base64.b64decode(bundle["retrieval_token_b64"], validate=True),
    )["action_payload"]["normalized_query_sha256"] == (
        sensitive.normalized_query_sha256(intent.arguments["query"])
    )

    edge = object.__new__(OperationalEdgeService)
    edge.config = SimpleNamespace(release_revision="a" * 40)
    edge.owner_gate_receipt_public_key = receipt_signer.public_key
    edge.sensitive_report_journal = sensitive.SensitiveReportAuthorizationJournal(
        tmp_path / "edge" / "journal.sqlite3"
    )
    operational_request = OperationalRequest(
        request_id="90ddf0ef-5919-46f4-b39a-c61f28e582cd",
        sequence=0,
        deadline_unix_ms=(NOW + 30) * 1000,
        intent=intent,
        capability=SignedEnvelope.from_mapping(
            capability_envelope,
            code="test_capability_invalid",
        ),
        step_up_authorization=bundle,
    )
    monkeypatch.setattr(edge_service.time, "time", lambda: NOW + 3)
    edge._authorize_sensitive_report(operational_request, capability)
    assert edge.sensitive_report_journal.consume_once(
        receipt_sha256=receipt["receipt_sha256"],
        intent=intent,
        now_unix=NOW + 4,
    ) == "replayed_same_intent"

    changed_arguments = {**intent.arguments, "query": "SELECT 2"}
    changed_intent = OperationalIntent(
        operation_id=intent.operation_id,
        arguments=changed_arguments,
        arguments_sha256=sha256_json(changed_arguments),
        idempotency_key=intent.idempotency_key,
    )
    with pytest.raises(
        transport.SensitiveReportTransportError,
        match="operation_invalid",
    ):
        transport.validate_frame(
            transport.build_frame(
                operation="create",
                capability_envelope=capability_envelope,
                intent=changed_intent,
            ),
            writer_key_id=writer_key_id,
            writer_public_key=writer_private.public_key(),
            now_unix=NOW,
        )
    changed_request = OperationalRequest(
        request_id="af8f2ec3-e1e6-40c4-8d37-b8e37f9c2c68",
        sequence=1,
        deadline_unix_ms=(NOW + 30) * 1000,
        intent=changed_intent,
        capability=operational_request.capability,
        step_up_authorization=bundle,
    )
    with pytest.raises(
        OperationalEdgeServiceError,
        match="sensitive_report_step_up_invalid",
    ):
        edge._authorize_sensitive_report(changed_request, capability)
    edge.sensitive_report_journal.close()


def test_phone_action_survives_fresh_short_capabilities_at_16_and_300_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    writer_private = ed25519.Ed25519PrivateKey.generate()
    writer_key_id = hashlib.sha256(
        writer_private.public_key().public_bytes_raw()
    ).hexdigest()

    def signed_capability(at: int) -> tuple[OperationalCapability, Mapping[str, Any]]:
        capability = _capability(
            intent,
            issued_at_unix=at,
            ttl_seconds=15,
        )
        envelope = sign_envelope(
            capability.to_mapping(),
            key_id=writer_key_id,
            private_key=writer_private,
        ).to_mapping()
        return capability, envelope

    database_path = tmp_path / "authority.sqlite3"
    bootstrap_authority_database(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        now_unix=NOW - 1,
        require_root=False,
    )
    authority = PasskeyV2AuthorityDatabase(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )
    signer = ReceiptSigner(ed25519.Ed25519PrivateKey.generate())
    monkeypatch.setattr(service, "_release_revision", lambda: "a" * 40)
    monkeypatch.setattr(
        service,
        "_local_cutover_authority_binding",
        lambda _revision, _plan: ({}, "b" * 64, "c" * 64, {}),
    )

    _cap0, envelope0 = signed_capability(NOW)
    frame0 = transport.build_frame(
        operation="create", capability_envelope=envelope0, intent=intent
    )
    created = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame("sensitive_create", frame0),
            authority=authority,
            signer=signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW,
            writer_public_key=writer_private.public_key(),
        ),
        expected_operation="sensitive_create",
    )

    _cap16, envelope16 = signed_capability(NOW + 16)
    frame16 = transport.build_frame(
        operation="create", capability_envelope=envelope16, intent=intent
    )
    refreshed = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame("sensitive_create", frame16),
            authority=authority,
            signer=signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW + 16,
            writer_public_key=writer_private.public_key(),
        ),
        expected_operation="sensitive_create",
    )
    assert frame16["request_id"] == frame0["request_id"]
    assert refreshed["action_envelope"] == created["action_envelope"]
    assert refreshed["approval_url"] == created["approval_url"]

    action = created["action_envelope"]
    challenge = created["challenge_record"]
    credential, assertion = _credential_and_assertion(
        owner=IVS,
        action=action,
        challenge=challenge,
        credential_id=b"ivs-stable-step-up-passkey",
    )
    authority.import_migrated_credential(credential)
    authority.verify_assertion_and_record_grant(
        assertion=assertion,
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        now_unix=NOW + 20,
    )

    capability300, envelope300 = signed_capability(NOW + 300)
    runtime = transport.build_runtime_binding(
        action_envelope=action,
        capability_envelope=envelope300,
        intent=intent,
    )
    frame300 = transport.build_frame(
        operation="consume",
        capability_envelope=envelope300,
        intent=intent,
        runtime_binding=runtime,
    )
    consumed = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame("sensitive_consume", frame300),
            authority=authority,
            signer=signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW + 300,
            writer_public_key=writer_private.public_key(),
        ),
        expected_operation="sensitive_consume",
    )
    bundle = transport.step_up_bundle(
        response=consumed,
        capability_envelope=envelope300,
        intent=intent,
    )
    assert consumed["state"] == "authorized"
    assert consumed["request_id"] == frame0["request_id"]
    sensitive.validate_authorization_receipt(
        receipt=bundle["authorization_receipt"],
        envelope=bundle["action_envelope"],
        grant=bundle["grant_record"],
        challenge=bundle["challenge_record"],
        receipt_public_key=signer.public_key,
        intent=intent,
        capability=capability300,
        now_unix=NOW + 300,
    )


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


def test_sensitive_report_phone_ttl_exception_is_scope_bound() -> None:
    intent = _intent()
    capability = _capability(intent)
    sensitive_action = sensitive.build_action_envelope(
        capability=capability,
        intent=intent,
        retrieval_token=b"r" * 32,
        request_id="S" * 32,
        executor_release_sha="a" * 40,
        authority_release_sha="b" * 40,
        authority_manifest_sha256="c" * 64,
        authority_host_receipt_sha256="d" * 64,
        source_preflight_sha256="e" * 64,
        live_projection_sha256="f" * 64,
        prior_authoritative_receipt_sha256="1" * 64,
        prior_event_head_sha256="2" * 64,
        issued_at_unix=NOW,
        approval_ttl_seconds=360,
    )
    assert sensitive_action["expires_at_unix"] == NOW + 360

    ordinary = dict(sensitive_action)
    ordinary["scope"] = "cloud_secret_change"
    ordinary["requester_discord_user_id"] = EMIL
    ordinary["required_approver_discord_user_id"] = EMIL
    ordinary["envelope_sha256"] = protocol.sha256_json(
        {key: value for key, value in ordinary.items() if key != "envelope_sha256"}
    )
    with pytest.raises(protocol.PasskeyV2ProtocolError, match="ttl_invalid"):
        protocol.validate_action_envelope(ordinary)


@pytest.mark.parametrize(
    "url",
    (
        "https://evil.example/approve/{request_id}",
        "https://auth.lomliev.com.evil.example/approve/{request_id}",
        "https://auth.lomliev.com/approve/wrong",
        "https://auth.lomliev.com/approve/{request_id}?next=evil",
        "https://auth.lomliev.com:444/approve/{request_id}",
    ),
)
def test_owner_gate_client_rejects_non_exact_approval_url(url: str) -> None:
    intent = _intent()
    capability = _capability(intent)
    writer = ed25519.Ed25519PrivateKey.generate()
    key_id = hashlib.sha256(writer.public_key().public_bytes_raw()).hexdigest()
    envelope = sign_envelope(
        capability.to_mapping(), key_id=key_id, private_key=writer
    ).to_mapping()
    frame = transport.build_frame(
        operation="create", capability_envelope=envelope, intent=intent
    )
    response = {
        "schema": transport.RESPONSE_SCHEMA,
        "operation": "create",
        "state": "pending",
        "request_id": frame["request_id"],
        "approval_url": url.format(request_id=frame["request_id"]),
        "action_envelope": {},
        "challenge_record": {},
        "grant_record": None,
        "authorization_receipt": None,
    }
    client = transport.SensitiveReportOwnerGateClient(
        requester=lambda _raw: (
            200,
            json.dumps(response).encode("utf-8"),
        )
    )
    with pytest.raises(
        transport.SensitiveReportTransportError,
        match="approval_url_invalid",
    ):
        client.call(frame)


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


def test_installed_enrollment_cli_emits_seed_once_only_to_operator_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert enrollment.main(
        [
            "create",
            "--owner-discord-user-id",
            IVS,
            "--user-label",
            "Ivs",
            "--ttl-seconds",
            "3600",
            "--root",
            str(tmp_path),
        ]
    ) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(
        f"{protocol.PRODUCTION_ORIGIN}/enroll/"
    )
    assert captured.out.count("#") == 1
    seed = captured.out.rstrip().split("#", 1)[1]
    assert seed
    assert caplog.text == ""
    assert seed not in "".join(
        path.read_text(encoding="ascii")
        for path in tmp_path.rglob("*.json")
    )


def test_phone_enrollment_web_wire_imports_one_credential_create_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation, token = enrollment.create_invitation(
        root=tmp_path,
        owner_discord_user_id=IVS,
        user_label="Ivs",
        now_unix=NOW,
    )
    monkeypatch.setattr(service, "ENROLLMENT_ROOT", tmp_path)
    verified = SimpleNamespace(
        credential_id=b"ivs-production-phone-passkey",
        credential_public_key=cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: b"x" * 32, -3: b"y" * 32}
        ),
        sign_count=0,
        credential_backed_up=True,
    )
    monkeypatch.setattr(
        enrollment,
        "_load_registration_verifier",
        lambda: (lambda **_kwargs: verified, ValueError),
    )
    database_path = tmp_path / "authority.sqlite3"
    bootstrap_authority_database(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
        now_unix=NOW - 1,
        require_root=False,
    )
    authority = PasskeyV2AuthorityDatabase(
        database_path,
        authority_uid=os.getuid(),
        authority_gid=os.getgid(),
    )
    signer = ReceiptSigner(ed25519.Ed25519PrivateKey.generate())
    encoded_token = enrollment._b64(token)
    options = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame(
                "enrollment_options",
                {
                    "invitation_id": invitation["invitation_id"],
                    "token_b64url": encoded_token,
                },
            ),
            authority=authority,
            signer=signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW + 1,
        ),
        expected_operation="enrollment_options",
    )
    assert options["publicKey"]["rp"]["id"] == protocol.PRODUCTION_RP_ID
    completed = service.validate_service_response(
        service.handle_authority_frame(
            service.build_service_frame(
                "enrollment_complete",
                {
                    "invitation_id": invitation["invitation_id"],
                    "token_b64url": encoded_token,
                    "credential": {"browser": "registration"},
                },
            ),
            authority=authority,
            signer=signer,
            peer_uid=service.WEB_UID,
            now_unix=NOW + 2,
        ),
        expected_operation="enrollment_complete",
    )
    assert completed["state"] == "enrolled"
    assert completed["owner_discord_user_id"] == IVS

    csrf = "C" * 43
    route, request_id, document = service.validate_web_request(
        method="POST",
        path=f"/enroll/{invitation['invitation_id']}/options",
        headers={
            "host": "auth.lomliev.com",
            "origin": protocol.PRODUCTION_ORIGIN,
            "content-type": "application/json",
            "x-muncho-csrf": csrf,
        },
        body=json.dumps({
            "schema": "muncho-passkey-v2-web-enrollment-options.v1",
            "token_b64url": encoded_token,
        }).encode("utf-8"),
        csrf_cookie=csrf,
    )
    assert (route, request_id) == (
        "enrollment_options",
        invitation["invitation_id"],
    )
    assert document["token_b64url"] == encoded_token


def test_sensitive_result_routes_only_to_signed_source_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = _intent()
    capability = _capability(intent)
    action = _action(intent, capability, b"r" * 32)
    observed: dict[str, Any] = {}

    def route(**kwargs: Any) -> str:
        observed.update(kwargs)
        return json.dumps({"success": True, "status": "ROUTE_BACK_EXECUTE_SENT"})

    monkeypatch.setattr(operational_edge_cli, "_route_back_execute", route)
    output = {
        "ok": True,
        "status": "OK",
        "rows": [{"email": "customer@example.com", "total": "42.00"}],
    }
    receipt = {
        "stdout_b64": base64.b64encode(
            json.dumps(output).encode("utf-8")
        ).decode("ascii"),
        "idempotency_key": intent.idempotency_key,
    }
    result = operational_edge_cli._route_sensitive_result(
        intent=intent,
        receipt=receipt,
        lease=action["action_payload"]["step_up_lease"],
    )
    assert result["success"] is True
    assert observed["target_ref"]["thread_id"] == (
        intent.arguments["discord_thread_id"]
    )
    assert observed["source_refs"]["message_id"] == (
        intent.arguments["discord_message_id"]
    )
    assert "customer@example.com" in observed["message"]


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
