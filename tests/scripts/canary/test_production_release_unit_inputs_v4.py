from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.canary import package_production_cutover_artifacts as v3
from scripts.canary import production_release_unit_inputs_v4 as unit_v4
from scripts.canary import production_release_update_contract as release_update


NOW = 1_900_000_000
PREDECESSOR = "1" * 40
TARGET = "2" * 40


def _v3_payload() -> dict[str, Any]:
    domains = sorted(v3.CREDENTIALS_BY_DOMAIN)
    operational_identities = {
        domain: {
            "user": f"muncho-edge-{domain}",
            "group": f"muncho-edge-{domain}",
            "uid": 1100 + index,
            "gid": 2100 + index,
        }
        for index, domain in enumerate(domains)
    }
    operational_socket_groups = {
        domain: {
            "group": f"muncho-edge-{domain}-c",
            "gid": 2200 + index,
        }
        for index, domain in enumerate(domains)
    }
    receipt_keys = {
        domain: f"{index:064x}"
        for index, domain in enumerate(domains, start=1)
    }
    return {
        "schema": v3.UNIT_INPUT_PAYLOAD_SCHEMA,
        "database_ip": "10.20.30.40",
        "target": {
            "project": "adventico-ai-platform",
            "zone": "europe-west3-a",
            "vm": "ai-platform-runtime-01",
            "database": "ai_platform_brain",
            "sql_instance": "production-pg18",
            "sql_host": "10.20.30.40",
            "tls_server_name": "production.example.internal",
            "port": 5432,
            "writer_login": "muncho_production_writer_login",
        },
        "gateway": {
            "user": "ai-platform-brain",
            "group": "ai-platform-brain",
            "uid": 1001,
            "gid": 2001,
        },
        "writer": {
            "user": "muncho-canonical-writer",
            "group": "muncho-canonical-writer",
            "uid": 1002,
            "gid": 2002,
        },
        "projector": {
            "user": "muncho-projector",
            "group": "muncho-projector",
            "uid": 1003,
            "gid": 2003,
        },
        "routeback": {
            "user": "muncho-discord-egress",
            "group": "muncho-discord-egress",
            "uid": 1004,
            "gid": 2004,
        },
        "connector": {
            "user": "muncho-discord-connector",
            "group": "muncho-discord-connector",
            "uid": 1005,
            "gid": 2005,
        },
        "mac_ops": {
            "user": "muncho-mac-ops-edge",
            "group": "muncho-mac-ops-edge",
            "uid": 1006,
            "gid": 2006,
        },
        "browser": {
            "user": "muncho-capability-browser",
            "group": "muncho-capability-browser",
            "uid": 1007,
            "gid": 2007,
        },
        "worker": {
            "user": "muncho-worker",
            "group": "muncho-worker",
            "uid": 1008,
            "gid": 2008,
        },
        "writer_client_group": {
            "group": "muncho-writer-client",
            "gid": 2300,
        },
        "worker_client_group": {
            "group": "muncho-worker-clients",
            "gid": 2301,
        },
        "operational_edge_identities": operational_identities,
        "operational_edge_socket_groups": operational_socket_groups,
        "writer_capability_public_key_id": "a" * 64,
        "discord_edge_receipt_public_key_id": "b" * 64,
        "operational_edge_key_foundation_sha256": "c" * 64,
        "operational_edge_receipt_public_key_ids": receipt_keys,
        "discord_reconciliation_intent": {
            "schema": v3.DISCORD_RECONCILIATION_INTENT_SCHEMA,
            "purpose": v3.DISCORD_RECONCILIATION_INTENT_PURPOSE,
            "release_revision": TARGET,
            "legacy_public_policy_sha256": "d" * 64,
            "target_public_policy_sha256": "e" * 64,
            "reviewed_reconciliation": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "release_owner_uid": 1001,
        "release_owner_gid": 2001,
        "bwrap_sha256": "f" * 64,
        "shell_sha256": "0" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def _authority() -> tuple[Ed25519PrivateKey, dict[str, Any]]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted = release_update.build_predecessor_trust(
        release_revision=PREDECESSOR,
        authority_plan_sha256="01" * 32,
        authority_approval_sha256="02" * 32,
        fixed_inputs_sha256="03" * 32,
        activation_receipt_sha256="04" * 32,
        owner_subject_sha256="9" * 64,
        owner_public_key_ed25519_hex=public.hex(),
        owner_key_id=release_update.sha256_bytes(public),
    )
    return private, dict(trusted)


def _payload(v3_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return dict(
        unit_v4.build_payload(
            v3_payload=v3_payload or _v3_payload(),
            builder_identity={
                "user": "muncho-release-builder",
                "group": "muncho-release-builder",
                "uid": 29104,
                "gid": 29104,
            },
            builder_terminal_receipt_sha256="07" * 32,
            whole_tree_manifest_sha256="08" * 32,
            candidate_seal_receipt_sha256="09" * 32,
            runtime_dependency_manifest_sha256="0a" * 32,
        )
    )


def _unit_documents(
    private: Ed25519PrivateKey,
    trusted: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    created_at_unix: int = NOW - 30,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = dict(
        unit_v4.build_plan(
            release_revision=TARGET,
            unit_inputs=payload,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            created_at_unix=created_at_unix,
        )
    )
    approval = dict(
        unit_v4.build_approval(
            plan=plan,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            nonce_sha256="11" * 32,
            issued_at_unix=NOW - 10,
            expires_at_unix=NOW + 300,
            now_unix=NOW,
            signer=private.sign,
        )
    )
    publication = dict(
        unit_v4.build_publication(
            plan=plan,
            approval=approval,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )
    )
    return plan, approval, publication


def _release_update_values(
    *,
    payload: Mapping[str, Any],
    unit_publication_sha256: str,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "release_revision": TARGET,
        "release_root": release_update.expected_release_root(TARGET),
        "source_tree_oid": "3" * 40,
        "source_v3_manifest_sha256": "05" * 32,
        "builder_request_sha256": "06" * 32,
        "builder_terminal_receipt_sha256": payload[
            "builder_terminal_receipt_sha256"
        ],
        "candidate_seal_receipt_sha256": payload[
            "candidate_seal_receipt_sha256"
        ],
        "whole_tree_manifest_sha256": payload[
            "whole_tree_manifest_sha256"
        ],
        "runtime_dependency_manifest_sha256": payload[
            "runtime_dependency_manifest_sha256"
        ],
        "uv_sha256": "0b" * 32,
        "interpreter_relative_path": (
            release_update.INTERPRETER_RELATIVE_PATH
        ),
        "interpreter_sha256": "0c" * 32,
        "entrypoint_relative_path": release_update.ENTRYPOINT_RELATIVE_PATH,
        "entrypoint_sha256": "0d" * 32,
        "host_inventory_sha256": "0e" * 32,
        "release_consumer_set_sha256": "0f" * 32,
        "host_artifact_manifest_sha256": "10" * 32,
        "cron_artifact_index_sha256": "12" * 32,
        "alias_artifact_index_sha256": "13" * 32,
        "successor_unit_input_publication_sha256": (
            unit_publication_sha256
        ),
        "activation_plan_sha256": "14" * 32,
        "rollback_plan_sha256": "15" * 32,
        "builder_identity": dict(payload["builder_identity"]),
        "release_owner": {
            "uid": payload["release_owner_uid"],
            "gid": payload["release_owner_gid"],
        },
        "reserved_runtime_uids": list(payload["reserved_runtime_uids"]),
        "reserved_runtime_gids": list(payload["reserved_runtime_gids"]),
        "created_at_unix": NOW - 25,
    }
    values.update(overrides or {})
    return values


def _release_update_documents(
    private: Ed25519PrivateKey,
    trusted: Mapping[str, Any],
    payload: Mapping[str, Any],
    unit_publication: Mapping[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = dict(
        release_update.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            values=_release_update_values(
                payload=payload,
                unit_publication_sha256=str(
                    unit_publication["publication_sha256"]
                ),
                overrides=overrides,
            ),
        )
    )
    approval_unsigned = {
        "schema": release_update.APPROVAL_SCHEMA,
        "purpose": release_update.APPROVAL_PURPOSE,
        "plan_sha256": plan["plan_sha256"],
        "predecessor_revision": plan["predecessor_revision"],
        "release_revision": plan["release_revision"],
        "owner_subject_sha256": plan["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": plan[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": plan["owner_key_id"],
        "nonce_sha256": "16" * 32,
        "issued_at_unix": NOW - 5,
        "expires_at_unix": NOW + 300,
        "approved": True,
    }
    approval_signed = {
        **approval_unsigned,
        "signature_ed25519_hex": private.sign(
            release_update.canonical_bytes(approval_unsigned)
        ).hex(),
    }
    approval = {
        **approval_signed,
        "approval_sha256": release_update.sha256_bytes(
            release_update.canonical_bytes(approval_signed)
        ),
    }
    publication = dict(
        release_update.build_publication(
            plan=plan,
            approval=approval,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )
    )
    return plan, approval, publication


def _documents() -> dict[str, Any]:
    private, trusted = _authority()
    original_v3 = _v3_payload()
    payload = _payload(original_v3)
    unit_plan, unit_approval, unit_publication = _unit_documents(
        private,
        trusted,
        payload,
    )
    update_plan, update_approval, update_publication = (
        _release_update_documents(
            private,
            trusted,
            payload,
            unit_publication,
        )
    )
    fixed = dict(
        unit_v4.derive_fixed_inputs(
            unit_input_publication=unit_publication,
            release_update_publication=update_publication,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )
    )
    return {
        "private": private,
        "trusted": trusted,
        "original_v3": original_v3,
        "payload": payload,
        "unit_plan": unit_plan,
        "unit_approval": unit_approval,
        "unit_publication": unit_publication,
        "update_plan": update_plan,
        "update_approval": update_approval,
        "update_publication": update_publication,
        "fixed": fixed,
    }


def _rehash(value: dict[str, Any], digest_field: str) -> None:
    unsigned = {
        key: item for key, item in value.items() if key != digest_field
    }
    value[digest_field] = unit_v4.sha256_bytes(
        unit_v4.canonical_bytes(unsigned)
    )


def test_v3_projection_is_exact_and_fixed_inputs_cross_bind_authorities() -> None:
    documents = _documents()
    original = v3._unit_input_payload(documents["original_v3"])
    projected = unit_v4.project_payload_to_v3(documents["payload"])

    assert projected == original
    assert documents["payload"]["release_owner_uid"] == 0
    assert documents["payload"]["release_owner_gid"] == 0
    assert projected["release_owner_uid"] == original["gateway"]["uid"]
    assert projected["release_owner_gid"] == original["gateway"]["gid"]

    fixed = unit_v4.validate_fixed_inputs(
        documents["fixed"],
        unit_input_publication=documents["unit_publication"],
        release_update_publication=documents["update_publication"],
        trusted_predecessor=documents["trusted"],
        expected_predecessor_trust_sha256=str(
            documents["trusted"]["trust_sha256"]
        ),
        now_unix=NOW,
    )
    assert fixed["unit_input_authority_publication_sha256"] == (
        documents["unit_publication"]["publication_sha256"]
    )
    assert fixed["release_update_plan_sha256"] == (
        documents["update_plan"]["plan_sha256"]
    )
    assert fixed["release_update_publication_sha256"] == (
        documents["update_publication"]["publication_sha256"]
    )
    assert fixed["predecessor_authority_plan_sha256"] == "01" * 32
    assert fixed["builder_terminal_receipt_sha256"] == "07" * 32
    assert fixed["whole_tree_manifest_sha256"] == "08" * 32
    assert fixed["candidate_seal_receipt_sha256"] == "09" * 32
    assert fixed["runtime_dependency_manifest_sha256"] == "0a" * 32


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("release_owner_uid", 1001),
        ("release_owner_gid", 2001),
    ),
)
def test_v4_requires_physical_root_ownership(
    field: str,
    value: int,
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_payload_invalid",
    ):
        unit_v4.validate_payload(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("user", "muncho-release-builder-alias"),
        ("group", "muncho-release-builder-alias"),
        ("uid", 1002),
        ("uid", 2002),
        ("gid", 1003),
        ("gid", 2003),
        ("uid", 0),
        ("gid", 0),
    ),
)
def test_builder_identity_cannot_alias_runtime_or_reserved_ids(
    field: str,
    value: str | int,
) -> None:
    payload = _payload()
    payload["builder_identity"][field] = value

    with pytest.raises(unit_v4.ProductionReleaseUnitInputsV4Error):
        unit_v4.validate_payload(payload)


def test_signature_forgery_and_signed_document_tamper_fail_closed() -> None:
    documents = _documents()
    forged_private = Ed25519PrivateKey.generate()
    approval = deepcopy(documents["unit_approval"])
    unsigned = {
        key: item
        for key, item in approval.items()
        if key not in {"signature_ed25519_hex", "approval_sha256"}
    }
    approval["signature_ed25519_hex"] = forged_private.sign(
        unit_v4.canonical_bytes(unsigned)
    ).hex()
    _rehash(approval, "approval_sha256")

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_approval_invalid",
    ):
        unit_v4.validate_approval(
            approval,
            plan=documents["unit_plan"],
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW,
        )

    tampered_plan = deepcopy(documents["unit_plan"])
    tampered_plan["unit_inputs"]["whole_tree_manifest_sha256"] = "ab" * 32
    _rehash(tampered_plan, "plan_sha256")
    tampered_publication = deepcopy(documents["unit_publication"])
    tampered_publication["plan"] = tampered_plan
    _rehash(tampered_publication, "publication_sha256")

    with pytest.raises(unit_v4.ProductionReleaseUnitInputsV4Error):
        unit_v4.validate_publication(
            tampered_publication,
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW,
        )


def test_approval_replay_across_plan_or_outside_fresh_window_fails() -> None:
    documents = _documents()

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_approval_invalid",
    ):
        unit_v4.validate_approval(
            documents["unit_approval"],
            plan=documents["unit_plan"],
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW + 301,
        )

    replay_target = dict(
        unit_v4.build_plan(
            release_revision=TARGET,
            unit_inputs=documents["payload"],
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            created_at_unix=NOW - 29,
        )
    )
    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_approval_invalid",
    ):
        unit_v4.validate_approval(
            documents["unit_approval"],
            plan=replay_target,
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW,
        )


def test_externally_pinned_predecessor_envelope_cannot_be_substituted() -> None:
    documents = _documents()

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_predecessor_trust_invalid",
    ):
        unit_v4.validate_plan(
            documents["unit_plan"],
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256="ff" * 32,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("successor_unit_input_publication_sha256", "20" * 32),
        ("builder_terminal_receipt_sha256", "21" * 32),
        ("whole_tree_manifest_sha256", "22" * 32),
        ("candidate_seal_receipt_sha256", "23" * 32),
        ("runtime_dependency_manifest_sha256", "24" * 32),
    ),
)
def test_cross_binding_rejects_independently_valid_update_authority(
    field: str,
    value: str,
) -> None:
    documents = _documents()
    _plan, _approval, publication = _release_update_documents(
        documents["private"],
        documents["trusted"],
        documents["payload"],
        documents["unit_publication"],
        overrides={field: value},
    )

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_cross_binding_invalid",
    ):
        unit_v4.derive_fixed_inputs(
            unit_input_publication=documents["unit_publication"],
            release_update_publication=publication,
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW,
        )


def test_fixed_input_tamper_fails_even_with_recomputed_self_hash() -> None:
    documents = _documents()
    fixed = deepcopy(documents["fixed"])
    fixed["release_update_publication_sha256"] = "25" * 32
    _rehash(fixed, "fixed_inputs_sha256")

    with pytest.raises(
        unit_v4.ProductionReleaseUnitInputsV4Error,
        match="release_unit_inputs_v4_fixed_inputs_invalid",
    ):
        unit_v4.validate_fixed_inputs(
            fixed,
            unit_input_publication=documents["unit_publication"],
            release_update_publication=documents["update_publication"],
            trusted_predecessor=documents["trusted"],
            expected_predecessor_trust_sha256=str(
                documents["trusted"]["trust_sha256"]
            ),
            now_unix=NOW,
        )
