from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gateway import canonical_writer_schema_reconciliation_runtime as reconciliation_runtime
from gateway import canonical_writer_schema_upgrade_runtime as runtime
from gateway.canonical_writer_foundation import _load_source_artifacts_for_tests
from gateway.canonical_writer_schema_reconciliation import SchemaContractAsset
from gateway.canonical_writer_schema_reconciliation import SchemaReconciliationError
from gateway.canonical_writer_schema_upgrade import SchemaUpgradePlan


ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "gateway/assets/canonical_writer_schema_contract_v1.json"
REVISION = "a" * 40
OWNER = reconciliation_runtime.OWNER_SUBJECT_SHA256
FAKE_SIGNATURE = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "ZmFrZQ==\n"
    "-----END SSH SIGNATURE-----\n"
)


def _plan() -> SchemaUpgradePlan:
    target = SchemaContractAsset.from_bytes(ASSET.read_bytes()).contract
    return SchemaUpgradePlan.build(
        release_revision=REVISION,
        target=target,
        artifact=_load_source_artifacts_for_tests()["base_migration"],
    )


def test_apply_error_code_preserves_only_stable_upgrade_invariants() -> None:
    exact = SchemaReconciliationError(
        "schema_upgrade_post_apply_public_routines_invalid"
    )
    unrelated = SchemaReconciliationError(
        "schema_reconciliation_contract_invalid"
    )
    assert runtime._schema_upgrade_apply_error_code(exact) == exact.code
    assert (
        runtime._schema_upgrade_apply_error_code(unrelated)
        == "schema_upgrade_apply_failed"
    )
    assert (
        runtime._schema_upgrade_apply_error_code(RuntimeError("secret text"))
        == "schema_upgrade_apply_failed"
    )


def _gate() -> dict[str, object]:
    plan = _plan()
    username = "muncho_canary_reconciler_" + plan.sha256[:16]
    unsigned = {
        "schema": runtime.GATE_SCHEMA,
        "ok": True,
        "state": "exact_source_stopped_upgrade_ready",
        "release_revision": REVISION,
        "release_manifest_sha256": "1" * 64,
        "stopped_release_receipt_file_sha256": "2" * 64,
        "stopped_release_receipt_sha256": "3" * 64,
        "release_artifact_sha256": "4" * 64,
        "python_version": reconciliation_runtime.EXPECTED_PYTHON_VERSION,
        "interpreter_sha256": "5" * 64,
        "activation_inventory_sha256": "6" * 64,
        "plan_sha256": plan.sha256,
        "source_schema_revision": plan.value["source_schema_revision"],
        "source_base_artifact_sha256": plan.value[
            "source_base_artifact_sha256"
        ],
        "source_contract_sha256": plan.value["source_contract_sha256"],
        "target_contract_sha256": plan.value["target_contract_sha256"],
        "target_base_artifact_sha256": plan.value[
            "target_base_artifact_sha256"
        ],
        "transactional_migration_body_sha256": plan.value[
            "transactional_migration_body_sha256"
        ],
        "initial_control_observation_sha256": "7" * 64,
        "initial_writer_managed_hba_receipt_sha256": "8" * 64,
        "host_identity_sha256": "9" * 64,
        "services_stopped_sha256": "a" * 64,
        "project": "adventico-ai-platform",
        "sql_instance": "muncho-canary-pg18-v2",
        "database": "muncho_canary_brain",
        "postgresql_major": 18,
        "tls_server_name": runtime.foundation.SQL_TLS_SERVER_NAME,
        "temporary_schema_upgrade_admin_username": username,
        "temporary_schema_upgrade_admin_username_sha256": runtime.hashlib.sha256(
            username.encode("ascii")
        ).hexdigest(),
        "database_roles_requested": [
            "canonical_brain_schema_reconciler",
            "cloudsqlsuperuser",
        ],
        "owner_subject_sha256": OWNER,
        "owner_public_key_ed25519_hex": (
            reconciliation_runtime.OWNER_PUBLIC_KEY_ED25519_HEX
        ),
        "owner_key_id": reconciliation_runtime.OWNER_KEY_ID,
        "owner_public_fingerprint": (
            reconciliation_runtime.OWNER_PUBLIC_FINGERPRINT
        ),
        "run_nonce_sha256": "b" * 64,
        "issued_at_unix": 90,
        "expires_at_unix": 500,
        "services_stopped": True,
        "secret_material_recorded": False,
    }
    return dict(runtime._hashed(unsigned, "gate_sha256"))


def _authority(gate: dict[str, object]) -> dict[str, object]:
    baseline = ["baseline-op"]
    baseline_rows = [["baseline-op", "CREATE_USER", "DONE", OWNER, True]]
    unsigned = {
        "schema": runtime.CLOUD_AUTHORITY_SCHEMA,
        "project": gate["project"],
        "instance": gate["sql_instance"],
        "username_sha256": gate[
            "temporary_schema_upgrade_admin_username_sha256"
        ],
        "host": "",
        "type": "BUILT_IN",
        "user_present": True,
        "owner_subject_sha256": OWNER,
        "mutation_context_sha256": gate["gate_sha256"],
        "baseline_operation_names": baseline,
        "baseline_user_operations": baseline_rows,
        "authority_operation": [
            "create-upgrade-admin",
            "CREATE_USER",
            "DONE",
            OWNER,
            True,
        ],
        "broad_schema_upgrade_authority": True,
        "database_roles_requested": gate["database_roles_requested"],
        "normal_reconciliation_executor": False,
        "resource_etag_sha256": "c" * 64,
    }
    return dict(runtime._hashed(unsigned, "receipt_sha256"))


def _apply(gate: dict[str, object]) -> dict[str, object]:
    signed = runtime.build_owner_apply_unsigned(
        gate=gate,
        cloud_sql_authority_receipt=_authority(gate),
        issued_at_unix=95,
        expires_at_unix=200,
        nonce_sha256="d" * 64,
    )
    return {**signed, "signature_sshsig": FAKE_SIGNATURE}


def _upgrade_receipt(
    gate: dict[str, object],
    apply: dict[str, object],
    *,
    observed_at_unix: int = 100,
) -> dict[str, object]:
    replay = gate["state"] == "exact_target_stopped_upgrade_replay_ready"
    initial = (
        gate["target_contract_sha256"]
        if replay
        else gate["source_contract_sha256"]
    )
    unsigned = {
        "schema": runtime.UPGRADE_TERMINAL_SCHEMA,
        "ok": True,
        "state": "already_exact_target" if replay else "exact_target_committed",
        "release_revision": gate["release_revision"],
        "plan_sha256": gate["plan_sha256"],
        "authorization_sha256": apply["apply_claim_sha256"],
        "initial_contract_sha256": initial,
        "final_contract_sha256": gate["target_contract_sha256"],
        "canonical_truth_receipt_sha256": "1" * 64,
        "initial_observation_sha256": "2" * 64,
        "final_observation_sha256": "2" * 64 if replay else "3" * 64,
        "writer_managed_hba_receipt_sha256": "4" * 64,
        "admin_managed_hba_receipt_sha256": "5" * 64,
        "mutation_applied": not replay,
        "deployment_lock_key": runtime.CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY,
        "started_at_unix": observed_at_unix,
        "secret_material_recorded": False,
    }
    return dict(runtime._hashed(unsigned, "receipt_sha256"))


def _intermediate(
    gate: dict[str, object],
    apply: dict[str, object],
    *,
    observed_at_unix: int = 100,
) -> dict[str, object]:
    receipt = _upgrade_receipt(
        gate,
        apply,
        observed_at_unix=observed_at_unix,
    )
    return dict(
        runtime._hashed(
            {
                "schema": runtime.INTERMEDIATE_SCHEMA,
                "ok": True,
                "state": "upgrade_committed_session_closed_awaiting_cloud_cleanup",
                "gate_sha256": gate["gate_sha256"],
                "release_revision": gate["release_revision"],
                "plan_sha256": gate["plan_sha256"],
                "apply_claim_sha256": apply["apply_claim_sha256"],
                "before_admin_authority_receipt_sha256": "6" * 64,
                "after_admin_authority_receipt_sha256": "7" * 64,
                "upgrade_receipt": receipt,
                "upgrade_receipt_sha256": receipt["receipt_sha256"],
                "database_session_closed": True,
                "database_capability_terminated": True,
                "services_stopped_sha256": gate["services_stopped_sha256"],
                "observed_at_unix": observed_at_unix,
                "secret_material_recorded": False,
            },
            "intermediate_sha256",
        )
    )


def _terminal(
    gate: dict[str, object],
    apply: dict[str, object],
    intermediate: dict[str, object],
    cleanup: dict[str, object],
) -> dict[str, object]:
    return dict(
        runtime._hashed(
            {
                "schema": runtime.TERMINAL_SCHEMA,
                "ok": True,
                "state": "exact_target_admin_absent_services_stopped",
                "gate_sha256": gate["gate_sha256"],
                "release_revision": gate["release_revision"],
                "plan_sha256": gate["plan_sha256"],
                "apply_claim_sha256": apply["apply_claim_sha256"],
                "intermediate_sha256": intermediate["intermediate_sha256"],
                "cleanup_claim_sha256": cleanup["cleanup_claim_sha256"],
                "upgrade_receipt_sha256": intermediate[
                    "upgrade_receipt_sha256"
                ],
                "target_contract_sha256": gate["target_contract_sha256"],
                "writer_managed_hba_receipt_sha256": "8" * 64,
                "canonical_truth_receipt_sha256": intermediate[
                    "upgrade_receipt"
                ]["canonical_truth_receipt_sha256"],
                "temporary_schema_upgrade_admin_absent": True,
                "database_admin_absence_exact": True,
                "services_stopped_sha256": gate["services_stopped_sha256"],
                "completed_at_unix": 100,
                "secret_material_recorded": False,
            },
            "terminal_sha256",
        )
    )


def _absence(
    gate: dict[str, object],
    authority: dict[str, object],
) -> dict[str, object]:
    delete = ["delete-upgrade-admin", "DELETE_USER", "DONE", OWNER, True]
    baseline_rows = authority["baseline_user_operations"]
    authority_row = authority["authority_operation"]
    unsigned = {
        "schema": runtime.CLOUD_ABSENCE_SCHEMA,
        "temporary_schema_upgrade_admin_absent": True,
        "project": gate["project"],
        "instance": gate["sql_instance"],
        "username_sha256": gate[
            "temporary_schema_upgrade_admin_username_sha256"
        ],
        "owner_subject_sha256": OWNER,
        "mutation_context_sha256": gate["gate_sha256"],
        "user_absent": True,
        "baseline_operation_names": authority["baseline_operation_names"],
        "baseline_user_operations": baseline_rows,
        "known_operation_names": ["create-upgrade-admin", "delete-upgrade-admin"],
        "response_known_authority_operation_names": ["create-upgrade-admin"],
        "response_known_delete_operation_names": ["delete-upgrade-admin"],
        "post_baseline_authority_operations": [authority_row],
        "response_known_candidate_observed": True,
        "post_baseline_authority_operation_count": 1,
        "terminal_user_operations": sorted(
            [*baseline_rows, authority_row, delete], key=lambda row: row[0]
        ),
        "mutation_ambiguity_observed": False,
        "quiet_window_seconds": 180.0,
    }
    return dict(runtime._hashed(unsigned, "evidence_sha256"))


def test_gate_and_owner_claims_are_exact_and_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_verify_signature", lambda *args, **kwargs: None)
    gate = _gate()
    validated = runtime.validate_gate_for_owner(
        gate,
        expected_release_revision=REVISION,
        expected_owner_subject_sha256=OWNER,
        owner_public_key_ed25519_hex=(
            reconciliation_runtime.OWNER_PUBLIC_KEY_ED25519_HEX
        ),
        owner_public_fingerprint=reconciliation_runtime.OWNER_PUBLIC_FINGERPRINT,
        now_unix=100,
    )
    apply = runtime._validate_apply_claim(_apply(gate), gate=gate, now_unix=100)
    intermediate = runtime._hashed(
        {
            "schema": runtime.INTERMEDIATE_SCHEMA,
            "gate_sha256": gate["gate_sha256"],
            "apply_claim_sha256": apply["apply_claim_sha256"],
            "database_session_closed": True,
            "database_capability_terminated": True,
        },
        "intermediate_sha256",
    )
    cleanup = runtime.build_owner_cleanup_unsigned(
        gate=gate,
        apply_claim=apply,
        intermediate=intermediate,
        cloud_sql_absence_receipt=_absence(
            gate,
            apply["cloud_sql_authority_receipt"],
        ),
        issued_at_unix=100,
        expires_at_unix=200,
        nonce_sha256="e" * 64,
    )
    assert validated["source_contract_sha256"] == _plan().value[
        "source_contract_sha256"
    ]
    assert cleanup["intermediate_sha256"] == intermediate["intermediate_sha256"]


def test_two_frame_protocol_zeroizes_credential_and_finishes_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_verify_signature", lambda *args, **kwargs: None)
    gate = _gate()
    apply = _apply(gate)
    intermediate_holder: dict[str, object] = {}

    def apply_callback(gate_value, claim, credential):
        assert bytes(credential) == b"x" * 64
        value = _intermediate(gate_value, claim)
        intermediate_holder.update(value)
        return value

    preliminary = apply_callback(gate, apply, bytearray(b"x" * 64))
    intermediate_holder.clear()
    cleanup_unsigned = runtime.build_owner_cleanup_unsigned(
        gate=gate,
        apply_claim=apply,
        intermediate=preliminary,
        cloud_sql_absence_receipt=_absence(
            gate,
            apply["cloud_sql_authority_receipt"],
        ),
        issued_at_unix=100,
        expires_at_unix=200,
        nonce_sha256="e" * 64,
    )
    cleanup = {**cleanup_unsigned, "signature_sshsig": FAKE_SIGNATURE}
    source = io.BytesIO(
        runtime.build_frame(
            runtime.APPLY_MAGIC,
            apply,
            credential=b"x" * 64,
        )
        + runtime.build_frame(runtime.CLEANUP_MAGIC, cleanup)
    )
    output = io.BytesIO()

    def cleanup_callback(gate_value, claim, intermediate, cleanup_claim):
        assert intermediate == intermediate_holder
        return _terminal(gate_value, claim, intermediate, cleanup_claim)

    terminal = runtime.run_protocol(
        gate,
        apply_callback=apply_callback,
        cleanup_callback=cleanup_callback,
        input_stream=source,
        output_stream=output,
        now=lambda: 100,
    )
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["schema"] for message in messages] == [
        runtime.GATE_SCHEMA,
        runtime.INTERMEDIATE_SCHEMA,
        runtime.TERMINAL_SCHEMA,
    ]
    assert messages[-1] == terminal


def test_exact_target_gate_is_a_crash_recovery_replay() -> None:
    gate = _gate()
    gate["state"] = "exact_target_stopped_upgrade_replay_ready"
    unsigned = dict(gate)
    unsigned.pop("gate_sha256")
    gate["gate_sha256"] = runtime._sha256_json(unsigned)
    apply = _apply(gate)
    intermediate = _intermediate(gate, apply)

    validated = runtime.validate_gate_for_owner(
        gate,
        expected_release_revision=REVISION,
        expected_owner_subject_sha256=OWNER,
        owner_public_key_ed25519_hex=(
            reconciliation_runtime.OWNER_PUBLIC_KEY_ED25519_HEX
        ),
        owner_public_fingerprint=reconciliation_runtime.OWNER_PUBLIC_FINGERPRINT,
        now_unix=100,
    )
    receipt = runtime.validate_intermediate_for_owner(
        intermediate,
        gate=gate,
        apply_claim=apply,
        now_unix=100,
    )["upgrade_receipt"]

    assert validated["state"] == "exact_target_stopped_upgrade_replay_ready"
    assert receipt["state"] == "already_exact_target"
    assert receipt["mutation_applied"] is False


def test_cleanup_rejects_operation_ledger_not_causally_bound() -> None:
    gate = _gate()
    apply = _apply(gate)
    intermediate = runtime._hashed(
        {
            "schema": runtime.INTERMEDIATE_SCHEMA,
            "gate_sha256": gate["gate_sha256"],
            "apply_claim_sha256": apply["apply_claim_sha256"],
            "database_session_closed": True,
            "database_capability_terminated": True,
        },
        "intermediate_sha256",
    )
    absence = _absence(gate, apply["cloud_sql_authority_receipt"])
    absence["post_baseline_authority_operation_count"] = 2
    unsigned = dict(absence)
    unsigned.pop("evidence_sha256")
    absence["evidence_sha256"] = runtime._sha256_json(unsigned)
    with pytest.raises(runtime.SchemaUpgradeRuntimeError):
        runtime.build_owner_cleanup_unsigned(
            gate=gate,
            apply_claim=apply,
            intermediate=intermediate,
            cloud_sql_absence_receipt=absence,
            issued_at_unix=100,
            expires_at_unix=200,
            nonce_sha256="e" * 64,
        )
