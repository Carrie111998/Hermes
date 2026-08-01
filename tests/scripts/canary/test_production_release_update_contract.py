from __future__ import annotations

from copy import deepcopy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.canary import production_release_update_contract as contract


NOW = 1_900_000_000
PREDECESSOR = "1" * 40
TARGET = "2" * 40
SHA = "a" * 64


def _authority() -> tuple[Ed25519PrivateKey, dict[str, object]]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted = contract.build_predecessor_trust(
        release_revision=PREDECESSOR,
        authority_plan_sha256="01" * 32,
        authority_approval_sha256="02" * 32,
        fixed_inputs_sha256="03" * 32,
        activation_receipt_sha256="04" * 32,
        owner_subject_sha256="b" * 64,
        owner_public_key_ed25519_hex=public.hex(),
        owner_key_id=contract.sha256_bytes(public),
    )
    return private, dict(trusted)


def _plan_values() -> dict[str, object]:
    values: dict[str, object] = {
        "predecessor_revision": PREDECESSOR,
        "predecessor_authority_plan_sha256": "01" * 32,
        "predecessor_authority_approval_sha256": "02" * 32,
        "predecessor_fixed_inputs_sha256": "03" * 32,
        "predecessor_activation_receipt_sha256": "04" * 32,
        "release_revision": TARGET,
        "release_root": contract.expected_release_root(TARGET),
        "source_tree_oid": "3" * 40,
        "source_v3_manifest_sha256": "05" * 32,
        "builder_request_sha256": "06" * 32,
        "builder_terminal_receipt_sha256": "07" * 32,
        "candidate_seal_receipt_sha256": "08" * 32,
        "whole_tree_manifest_sha256": "09" * 32,
        "runtime_dependency_manifest_sha256": "0a" * 32,
        "uv_sha256": "0b" * 32,
        "interpreter_relative_path": ".venv/bin/python",
        "interpreter_sha256": "0c" * 32,
        "entrypoint_relative_path": (
            "scripts/canary/production_release_update_entrypoint.py"
        ),
        "entrypoint_sha256": "0d" * 32,
        "host_inventory_sha256": "0e" * 32,
        "release_consumer_set_sha256": "0f" * 32,
        "host_artifact_manifest_sha256": "10" * 32,
        "cron_artifact_index_sha256": "11" * 32,
        "alias_artifact_index_sha256": "12" * 32,
        "successor_unit_input_publication_sha256": "13" * 32,
        "activation_plan_sha256": "14" * 32,
        "rollback_plan_sha256": "15" * 32,
        "builder_identity": {
            "user": "muncho-release-builder",
            "group": "muncho-release-builder",
            "uid": 29104,
            "gid": 29104,
        },
        "release_owner": {"uid": 0, "gid": 0},
        "reserved_runtime_uids": list(
            range(1001, 1001 + contract.EXPECTED_RUNTIME_UID_COUNT)
        ),
        "reserved_runtime_gids": list(
            range(2001, 2001 + contract.EXPECTED_RESERVED_GID_COUNT)
        ),
        "created_at_unix": NOW - 30,
    }
    return values


def _approval(
    private: Ed25519PrivateKey,
    plan: dict[str, object],
    *,
    issued_at_unix: int = NOW - 10,
    expires_at_unix: int = NOW + 300,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema": contract.APPROVAL_SCHEMA,
        "purpose": contract.APPROVAL_PURPOSE,
        "plan_sha256": plan["plan_sha256"],
        "predecessor_revision": plan["predecessor_revision"],
        "release_revision": plan["release_revision"],
        "owner_subject_sha256": plan["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": plan[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": plan["owner_key_id"],
        "nonce_sha256": "16" * 32,
        "issued_at_unix": issued_at_unix,
        "expires_at_unix": expires_at_unix,
        "approved": True,
    }
    signature = private.sign(contract.canonical_bytes(unsigned)).hex()
    signed = {**unsigned, "signature_ed25519_hex": signature}
    return {
        **signed,
        "approval_sha256": contract.sha256_bytes(
            contract.canonical_bytes(signed)
        ),
    }


def _documents() -> tuple[
    Ed25519PrivateKey,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    private, trusted = _authority()
    plan = dict(
        contract.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=_plan_values(),
        )
    )
    approval = _approval(private, plan)
    publication = dict(
        contract.build_publication(
            plan=plan,
            approval=approval,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            now_unix=NOW,
        )
    )
    return private, trusted, plan, approval, publication


def _rehash(
    value: dict[str, object],
    digest_field: str,
) -> dict[str, object]:
    unsigned = {
        key: item for key, item in value.items() if key != digest_field
    }
    value[digest_field] = contract.sha256_bytes(
        contract.canonical_bytes(unsigned)
    )
    return value


def test_owner_signed_publication_binds_exact_pinned_release() -> None:
    _private, trusted, plan, _approval_value, publication = _documents()

    validated = contract.validate_publication(
        publication,
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
        now_unix=NOW,
    )

    assert validated["release_revision"] == TARGET
    assert validated["predecessor_revision"] == PREDECESSOR
    assert validated["plan"]["release_root"] == (
        f"/opt/adventico-ai-platform/hermes-agent-releases/"
        f"hermes-agent-{TARGET[:12]}"
    )
    assert validated["plan"]["release_owner"] == {"uid": 0, "gid": 0}
    assert validated["plan"]["builder_identity"]["uid"] == 29104
    assert validated["plan"]["plan_sha256"] == plan["plan_sha256"]
    assert validated["plan"]["schema"] == (
        "muncho-production-release-update-plan.v5"
    )


def test_prior_dormant_plan_schema_is_not_accepted_as_current_authority() -> None:
    _private, trusted, plan, _approval_value, _publication = _documents()
    legacy = deepcopy(plan)
    legacy["schema"] = "muncho-production-release-update-plan.v4"
    _rehash(legacy, "plan_sha256")

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_plan_invalid",
    ):
        contract.validate_plan(
            legacy,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
        )


def test_target_cannot_replace_predecessor_trust_key() -> None:
    _private, _trusted, _plan, _approval_value, publication = _documents()
    _other_private, other_trusted = _authority()

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_plan_invalid",
    ):
        contract.validate_publication(
            publication,
            trusted_predecessor=other_trusted,
            expected_predecessor_trust_sha256=str(
                other_trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )


def test_forged_publication_and_forged_envelope_fail_the_external_pin() -> None:
    _trusted_private, trusted = _authority()
    forged_private, forged_trust = _authority()
    forged_plan = dict(
        contract.build_plan(
            trusted_predecessor=forged_trust,
            expected_predecessor_trust_sha256=str(
                forged_trust["trust_sha256"]
            ),
            values=_plan_values(),
        )
    )
    forged_approval = _approval(forged_private, forged_plan)
    forged_publication = contract.build_publication(
        plan=forged_plan,
        approval=forged_approval,
        trusted_predecessor=forged_trust,
        expected_predecessor_trust_sha256=str(
            forged_trust["trust_sha256"]
        ),
        now_unix=NOW,
    )

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_predecessor_trust_invalid",
    ):
        contract.validate_publication(
            forged_publication,
            trusted_predecessor=forged_trust,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )


def test_signature_tamper_fails_even_when_hashes_are_recomputed() -> None:
    _private, trusted, _plan, _approval_value, publication = _documents()
    tampered = deepcopy(publication)
    approval = dict(tampered["approval"])
    approval["nonce_sha256"] = "17" * 32
    _rehash(approval, "approval_sha256")
    tampered["approval"] = approval
    _rehash(tampered, "publication_sha256")

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_approval_invalid",
    ):
        contract.validate_publication(
            tampered,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            now_unix=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("release_root", "/tmp/hermes-agent"),
        ("interpreter_relative_path", "../python"),
        ("interpreter_relative_path", "bin/python"),
        ("entrypoint_relative_path", "/tmp/entrypoint.py"),
        ("entrypoint_relative_path", "scripts/canary/other.py"),
        ("release_owner", {"uid": 991, "gid": 991}),
        (
            "builder_identity",
            {
                "user": "ai-platform-brain",
                "group": "ai-platform-brain",
                "uid": 1001,
                "gid": 1001,
            },
        ),
    ),
)
def test_plan_rejects_untrusted_path_or_identity_boundary(
    field: str,
    value: object,
) -> None:
    _private, trusted = _authority()
    values = _plan_values()
    values[field] = value

    with pytest.raises(contract.ProductionReleaseUpdateContractError):
        contract.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=values,
        )


def test_expired_approval_cannot_start_a_new_transaction() -> None:
    private, trusted = _authority()
    plan = dict(
        contract.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=_plan_values(),
        )
    )
    expired = _approval(
        private,
        plan,
        issued_at_unix=NOW - 100,
        expires_at_unix=NOW,
    )

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_approval_invalid",
    ):
        contract.build_publication(
            plan=plan,
            approval=expired,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            now_unix=NOW,
        )


def test_publication_cannot_rebind_a_signed_plan_to_another_release() -> None:
    _private, trusted, _plan, _approval_value, publication = _documents()
    rebound = deepcopy(publication)
    rebound["release_revision"] = "4" * 40
    _rehash(rebound, "publication_sha256")

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_publication_invalid",
    ):
        contract.validate_publication(
            rebound,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            now_unix=NOW,
        )


def test_plan_is_compare_and_swap_bound_to_predecessor_evidence() -> None:
    _private, trusted, plan, _approval_value, _publication = _documents()
    for field in (
        "predecessor_revision",
        "predecessor_authority_plan_sha256",
        "predecessor_authority_approval_sha256",
        "predecessor_fixed_inputs_sha256",
        "predecessor_activation_receipt_sha256",
    ):
        tampered = deepcopy(plan)
        tampered[field] = (
            "f" * 40 if field == "predecessor_revision" else "f" * 64
        )
        _rehash(tampered, "plan_sha256")
        with pytest.raises(
            contract.ProductionReleaseUpdateContractError,
            match="release_update_plan_invalid",
        ):
            contract.validate_plan(
                tampered,
                trusted_predecessor=trusted,
                expected_predecessor_trust_sha256=str(
                    trusted["trust_sha256"]
                ),
            )


def test_release_prefix_collision_is_rejected() -> None:
    _private, trusted = _authority()
    values = _plan_values()
    collision = PREDECESSOR[:12] + "f" * 28
    values["release_revision"] = collision
    values["release_root"] = contract.expected_release_root(collision)

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_plan_invalid",
    ):
        contract.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=values,
        )


@pytest.mark.parametrize(("field", "value"), (("uid", 1001), ("gid", 2001)))
def test_builder_numeric_identity_must_match_reserved_sysusers_identity(
    field: str,
    value: int,
) -> None:
    _private, trusted = _authority()
    values = _plan_values()
    builder = dict(values["builder_identity"])
    builder[field] = value
    values["builder_identity"] = builder

    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_builder_identity_invalid",
    ):
        contract.build_plan(
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=values,
        )


def test_malformed_predecessor_envelope_cannot_be_a_trust_anchor() -> None:
    _private, trusted = _authority()
    malformed = dict(trusted)
    malformed.pop("activation_receipt_sha256")
    with pytest.raises(
        contract.ProductionReleaseUpdateContractError,
        match="release_update_predecessor_trust_invalid",
    ):
        contract.build_plan(
            trusted_predecessor=malformed,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            values=_plan_values(),
        )


def test_future_or_stale_plan_is_not_approvable() -> None:
    private, trusted = _authority()
    for created_at in (
        NOW + 1,
        NOW - contract.MAX_PLAN_AGE_AT_APPROVAL_SECONDS - 100,
    ):
        values = _plan_values()
        values["created_at_unix"] = created_at
        plan = dict(
            contract.build_plan(
                trusted_predecessor=trusted,
                expected_predecessor_trust_sha256=str(
                    trusted["trust_sha256"]
                ),
                values=values,
            )
        )
        approval = _approval(private, plan)
        with pytest.raises(
            contract.ProductionReleaseUpdateContractError,
            match="release_update_approval_invalid",
        ):
            contract.validate_approval(
                approval,
                plan=plan,
                now_unix=NOW,
            )
