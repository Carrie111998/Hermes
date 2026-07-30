from __future__ import annotations

import base64
import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_service as service
from scripts.canary import passkey_v2_upstream_sync as upstream
from scripts.canary.passkey_v2_signer import ReceiptSigner


NOW = 2_000_000_000
RELEASE = "a" * 40


def _plan() -> dict:
    return dict(
        upstream.build_activation_plan(
            release_revision=RELEASE,
            sender_revision="b" * 40,
            package_manifest_sha256="1" * 64,
            activation_runtime_sha256="2" * 64,
            first_catch_up_receipt_sha256="3" * 64,
            candidate_upstream_sha="c" * 40,
            fork_main_after_sha="d" * 40,
            unit_digests={
                name: str(index) * 64
                for index, name in enumerate(
                    upstream.UNIT_NAMES,
                    start=4,
                )
            },
            legacy_cron_source_definition_sha256="8" * 64,
            legacy_cron_retired_definition_sha256="9" * 64,
            legacy_collector_timer_prestate="enabled_active",
            legacy_collector_timer_fragment_path=(
                upstream.LEGACY_TIMER_FRAGMENT_PATH
            ),
            legacy_collector_timer_fragment_sha256="a" * 64,
        )
    )


def _action(plan: dict) -> dict:
    return dict(
        upstream.build_upstream_sync_action_envelope(
            activation_plan=plan,
            authorization_nonce_sha256="b" * 64,
            authority_manifest_sha256="c" * 64,
            authority_host_receipt_sha256="d" * 64,
            external_iam_receipt_sha256="e" * 64,
            prior_authoritative_receipt_sha256="f" * 64,
            prior_event_head_sha256=protocol.GENESIS_JOURNAL_HEAD_SHA256,
            issued_at_unix=NOW,
        )
    )


def _signed_bundle() -> tuple[dict, dict, ReceiptSigner]:
    plan = _plan()
    action = _action(plan)
    challenge = protocol.build_challenge_record(
        envelope=action,
        challenge_id="C" * 32,
        challenge_b64url=base64.urlsafe_b64encode(b"x" * 32)
        .rstrip(b"=")
        .decode("ascii"),
        rp_id=protocol.PRODUCTION_RP_ID,
        origin=protocol.PRODUCTION_ORIGIN,
        created_at_unix=NOW + 1,
    )
    grant = protocol.build_passkey_grant(
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        approver_discord_user_id=upstream.OWNER_DISCORD_USER_ID,
        credential_id_sha256="1" * 64,
        credential_record_sha256="2" * 64,
        credential_migration_receipt_sha256="3" * 64,
        assertion_verification_sha256="4" * 64,
        credential_sign_count=8,
        credential_backed_up=True,
        granted_at_unix=NOW + 2,
    )
    runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["activation_plan_sha256"],
        executor_binary_sha256=plan["activation_runtime_sha256"],
        mutation_wrapper_sha256=plan["package_manifest_sha256"],
        remote_transport_sha256="5" * 64,
    )
    signer = ReceiptSigner(Ed25519PrivateKey.generate())
    receipt = signer.sign(
        protocol.build_authorization_receipt_unsigned(
            envelope=action,
            grant=grant,
            challenge=challenge,
            runtime_binding=runtime,
            consume_attempt_id="6" * 64,
            consumed_at_unix=NOW + 3,
            prior_journal_head_sha256="7" * 64,
            receipt_public_key_id=signer.key_id,
        )
    )
    bundle = upstream.build_authorization_bundle(
        activation_plan=plan,
        action_envelope=action,
        challenge_record=challenge,
        grant_record=grant,
        authorization_receipt=receipt,
        receipt_public_key=signer.public_key,
    )
    return dict(bundle), plan, signer


def _rehash_action(action: dict) -> None:
    action["action_payload_sha256"] = protocol.sha256_json(
        action["action_payload"]
    )
    action["envelope_sha256"] = protocol.sha256_json(
        {
            name: item
            for name, item in action.items()
            if name != "envelope_sha256"
        }
    )


def _intake_frame(operation: str, document: dict) -> dict:
    unsigned = {
        "schema": service.storage.REMOTE_FRAME_SCHEMA,
        "operation": operation,
        "release_sha": RELEASE,
        "document": document,
    }
    return {
        **unsigned,
        "frame_sha256": protocol.sha256_json(unsigned),
    }


class _UnusedExecutor:
    def call(self, *_args, **_kwargs) -> dict:
        raise AssertionError("upstream owner-gate route reached executor")


class _AuthorityClient:
    def __init__(self, signer: ReceiptSigner) -> None:
        self.signer = signer
        self.action: dict | None = None
        self.challenge: dict | None = None
        self.grant: dict | None = None
        self.preview_raw: str | None = None

    def call(self, operation: str, document: dict) -> dict:
        if operation == "create_request":
            self.action = dict(document["action_envelope"])
            self.challenge = dict(
                protocol.build_challenge_record(
                    envelope=self.action,
                    challenge_id="C" * 32,
                    challenge_b64url=base64.urlsafe_b64encode(b"z" * 32)
                    .rstrip(b"=")
                    .decode("ascii"),
                    rp_id=protocol.PRODUCTION_RP_ID,
                    origin=protocol.PRODUCTION_ORIGIN,
                    created_at_unix=NOW + 1,
                )
            )
            self.grant = dict(
                protocol.build_passkey_grant(
                    envelope=self.action,
                    challenge=self.challenge,
                    grant_id="G" * 32,
                    approver_discord_user_id=(
                        upstream.OWNER_DISCORD_USER_ID
                    ),
                    credential_id_sha256="1" * 64,
                    credential_record_sha256="2" * 64,
                    credential_migration_receipt_sha256="3" * 64,
                    assertion_verification_sha256="4" * 64,
                    credential_sign_count=8,
                    credential_backed_up=True,
                    granted_at_unix=NOW + 2,
                )
            )
            return {
                "request_id": self.action["request_id"],
                "action_envelope_sha256": self.action[
                    "envelope_sha256"
                ],
                "challenge_record_sha256": self.challenge[
                    "challenge_record_sha256"
                ],
                "expires_at_unix": self.action["expires_at_unix"],
            }
        if operation == "render":
            assert self.action is not None
            rendered = service._render_authority_action(self.action)
            self.preview_raw = rendered[
                "exact_action_envelope_canonical_json"
            ]
            return dict(rendered)
        if operation == "consume":
            assert self.action is not None
            assert self.challenge is not None
            assert self.grant is not None
            receipt = self.signer.sign(
                protocol.build_authorization_receipt_unsigned(
                    envelope=self.action,
                    grant=self.grant,
                    challenge=self.challenge,
                    runtime_binding=document["runtime_binding"],
                    consume_attempt_id=document["consume_attempt_id"],
                    consumed_at_unix=NOW + 3,
                    prior_journal_head_sha256="7" * 64,
                    receipt_public_key_id=self.signer.key_id,
                )
            )
            return {
                "disposition": "authorized_once",
                "authorization_receipt": receipt,
                "action_envelope": self.action,
                "challenge_record": self.challenge,
                "grant_record": self.grant,
            }
        raise AssertionError(f"unexpected authority operation {operation}")


def test_service_dispatch_accepts_only_exact_upstream_sync_action() -> None:
    plan = _plan()
    action = _action(plan)

    assert service._validate_authority_action(action) == action
    facts = service._mechanical_authority_facts(action)
    assert facts["activation_plan_sha256"] == (
        plan["activation_plan_sha256"]
    )
    assert facts["exact_allowed_operations"] == [
        upstream.OPERATION
    ]

    forbidden = copy.deepcopy(action)
    forbidden["action_payload"]["schema"] = "caller-action.v1"
    _rehash_action(forbidden)
    with pytest.raises(
        service.PasskeyV2ServiceError,
        match="passkey_v2_action_schema_forbidden",
    ):
        service._validate_authority_action(forbidden)


def test_authorization_bundle_binds_signed_one_shot_nonce_and_digests() -> None:
    bundle, plan, signer = _signed_bundle()

    checked = upstream.validate_authorization_bundle(
        bundle,
        activation_plan=plan,
        receipt_public_key=signer.public_key,
        now_unix=NOW + 4,
    )

    action = checked["action_envelope"]
    receipt = checked["authorization_receipt"]
    assert action["action_payload"]["authorization_nonce_sha256"] == (
        "b" * 64
    )
    assert action["executor_plan_sha256"] == (
        plan["activation_plan_sha256"]
    )
    assert receipt["authorization_disposition"] == "authorized_once"
    assert receipt["consume_attempt_id"] == "6" * 64
    assert receipt["runtime_binding"]["executor_binary_sha256"] == (
        plan["activation_runtime_sha256"]
    )


def test_authorization_bundle_rejects_tamper_and_changed_replay_attempt() -> None:
    bundle, plan, signer = _signed_bundle()

    tampered = copy.deepcopy(bundle)
    tampered["authorization_receipt"]["consume_attempt_id"] = "8" * 64
    tampered["bundle_sha256"] = protocol.sha256_json(
        {
            name: item
            for name, item in tampered.items()
            if name != "bundle_sha256"
        }
    )

    with pytest.raises(
        upstream.UpstreamSyncPasskeyError,
        match="upstream_sync_passkey_authorization_invalid",
    ):
        upstream.validate_authorization_bundle(
            tampered,
            activation_plan=plan,
            receipt_public_key=signer.public_key,
            now_unix=NOW + 4,
        )


def test_authorization_bundle_rejects_expired_execution_window() -> None:
    bundle, plan, signer = _signed_bundle()
    expiry = bundle["authorization_receipt"][
        "execution_window_expires_at_unix"
    ]

    with pytest.raises(
        upstream.UpstreamSyncPasskeyError,
        match="upstream_sync_passkey_authorization_invalid",
    ):
        upstream.validate_authorization_bundle(
            bundle,
            activation_plan=plan,
            receipt_public_key=signer.public_key,
            now_unix=expiry,
        )


def test_action_rejects_wrong_operation_even_when_rehashed() -> None:
    action = _action(_plan())
    action["action_payload"]["operation"] = (
        "activate_caller_selected_target.v1"
    )
    _rehash_action(action)

    with pytest.raises(
        upstream.UpstreamSyncPasskeyError,
        match="upstream_sync_passkey_action_invalid",
    ):
        upstream.validate_upstream_sync_action_envelope(action)


def test_bundle_rejects_different_digest_bound_plan() -> None:
    bundle, _plan_value, signer = _signed_bundle()
    different = _plan()
    different["package_manifest_sha256"] = "0" * 64
    different["activation_plan_sha256"] = protocol.sha256_json(
        {
            name: item
            for name, item in different.items()
            if name != "activation_plan_sha256"
        }
    )
    upstream.validate_activation_plan(different)

    with pytest.raises(
        upstream.UpstreamSyncPasskeyError,
        match="upstream_sync_passkey_authorization_invalid",
    ):
        upstream.validate_authorization_bundle(
            bundle,
            activation_plan=different,
            receipt_public_key=signer.public_key,
            now_unix=NOW + 4,
        )


def test_upstream_facts_render_and_browser_allowset_are_exact() -> None:
    action = _action(_plan())
    rendered = service._render_authority_action(action)

    assert rendered["mechanical_facts"]["schema"] == (
        upstream.UPSTREAM_SYNC_FACTS_SCHEMA
    )
    assert rendered["exact_action_envelope_canonical_json"] == (
        protocol.canonical_json_bytes(action).decode("utf-8")
    )
    assert (
        upstream.UPSTREAM_SYNC_FACTS_SCHEMA.encode("ascii")
        in service._APPROVAL_JS
    )


def test_owner_facing_request_and_consume_emit_exact_authorization_bundle() -> None:
    plan = _plan()
    signer = ReceiptSigner(Ed25519PrivateKey.generate())
    authority = _AuthorityClient(signer)
    runtime = protocol.build_runtime_binding(
        executor_release_sha=RELEASE,
        executor_plan_sha256=plan["activation_plan_sha256"],
        executor_binary_sha256=plan["activation_runtime_sha256"],
        mutation_wrapper_sha256=plan["package_manifest_sha256"],
        remote_transport_sha256="5" * 64,
    )

    def binding_loader(
        release_revision: str,
        supplied_plan: dict,
    ) -> tuple:
        assert release_revision == RELEASE
        assert supplied_plan == plan
        return (
            runtime,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            signer.public_key,
        )

    class Transport:
        calls = 0

        def invoke_owner_gate(self, raw: bytes) -> bytes:
            self.calls += 1
            frame = protocol.decode_canonical_json(raw)
            response = service.handle_intake_frame(
                frame,
                authority_client=authority,
                executor_client=_UnusedExecutor(),
                release_revision=RELEASE,
                now_unix=NOW if self.calls == 1 else NOW + 4,
                upstream_sync_binding_loader=binding_loader,
            )
            return protocol.canonical_json_bytes(response)

    transport = Transport()
    boundary = upstream.UpstreamSyncPasskeyBoundary(RELEASE, transport)
    requested = boundary.request(
        activation_plan=plan,
        authorization_nonce_sha256="b" * 64,
    )
    consumed = boundary.consume(
        activation_plan=plan,
        request_id=requested["request_id"],
        consume_attempt_id="6" * 64,
    )

    checked = upstream.validate_authorization_bundle(
        consumed["authorization_bundle"],
        activation_plan=plan,
        receipt_public_key=signer.public_key,
        now_unix=NOW + 4,
    )
    assert authority.preview_raw == protocol.canonical_json_bytes(
        checked["action_envelope"]
    ).decode("utf-8")
    assert requested["plan_sha256"] == plan["activation_plan_sha256"]
    assert consumed["plan_sha256"] == plan["activation_plan_sha256"]
    assert consumed["disposition"] == "authorized_once"
    assert consumed["production_host_mutation_performed"] is False
    assert transport.calls == 2
