from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.canonical_writer_foundation import _load_source_artifacts_for_tests
from gateway.canonical_writer_db import (
    CredentialSource,
    ManagedCloudSQLAdminHBAReceipt,
    QueryResult,
    WriterDBConfig,
)
from gateway.canonical_writer_schema_reconciliation import (
    MISSING_HELPER_SIGNATURE,
    SchemaContract,
    SchemaContractAsset,
    SchemaReconciliationError,
)
from gateway import canonical_writer_schema_upgrade as upgrade
from gateway.canonical_writer_schema_upgrade import (
    SOURCE_ROUTINE_DEFINITION_SHA256,
    UPGRADE_MIGRATION_CHUNK_SHA256,
    SchemaUpgradePlan,
    collect_upgrade_admin_authority_receipt,
    execute_atomic_schema_upgrade,
    preflight_schema_upgrade,
    source_contract_value,
    transactional_migration_body,
    transactional_migration_chunks,
)


ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "gateway/assets/canonical_writer_schema_contract_v1.json"


def _target() -> SchemaContract:
    return SchemaContractAsset.from_bytes(ASSET.read_bytes()).contract


def _source_contract() -> SchemaContract:
    return SchemaContract.from_mapping(source_contract_value(_target()))


def _plan() -> SchemaUpgradePlan:
    return SchemaUpgradePlan.build(
        release_revision="a" * 40,
        target=_target(),
        artifact=_load_source_artifacts_for_tests()["base_migration"],
    )


def _config(user: str = "muncho_canary_writer_login") -> WriterDBConfig:
    return WriterDBConfig(
        host="127.0.0.1",
        tls_server_name="localhost",
        port=5432,
        database="muncho_canonical_canary",
        user=user,
        ca_file=Path("/tmp/muncho-schema-upgrade-test-ca.pem"),
        credential=CredentialSource(fd=0, expected_uid=os.getuid()),
        application_name="muncho-schema-upgrade-test",
    )


def _managed_hba(
    config: WriterDBConfig,
    *,
    user: str,
) -> ManagedCloudSQLAdminHBAReceipt:
    return ManagedCloudSQLAdminHBAReceipt(
        version="managed-cloudsqladmin-hba-rejection-v2",
        host=config.host,
        tls_server_name=config.tls_server_name,
        port=config.port,
        server_certificate_sha256="e" * 64,
        database="cloudsqladmin",
        user=user,
        observed_at_unix=90,
        expires_at_unix=190,
        sqlstate="28000",
        server_message=(
            f'no pg_hba.conf entry for host "{config.host}", user '
            f'"{user}", database "cloudsqladmin", SSL encryption'
        ),
        result="pg_hba_rejected",
        tls_peer_verified=True,
    )


class _Session:
    username = "muncho_canary_reconciler_" + "a" * 16

    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str, *, maximum_rows: int) -> SimpleNamespace:
        self.sql.append(sql)
        if sql == "BEGIN ISOLATION LEVEL SERIALIZABLE":
            tag = "BEGIN"
        elif sql.startswith("SET LOCAL "):
            tag = "SET"
        elif sql == "ROLLBACK":
            tag = "ROLLBACK"
        elif sql == "COMMIT":
            tag = "COMMIT"
        elif sql.startswith("-- Privileged Canonical Writer v1"):
            return SimpleNamespace(
                command_tag="CREATE FUNCTION",
                rows=(("",),),
                columns=("pg_advisory_xact_lock",),
            )
        elif sql.startswith("-- Fixed public routine 2/17"):
            tag = "CREATE FUNCTION"
        elif sql.startswith("-- Fixed public routine 12/17"):
            tag = "DO"
        else:
            tag = "SELECT 1"
        return SimpleNamespace(command_tag=tag, rows=(), columns=())


def test_source_projection_is_exact_bounded_generation() -> None:
    target = _target()
    source = _source_contract()
    assert source.sha256 != target.sha256
    assert source.helper_catalog_identity is None
    assert MISSING_HELPER_SIGNATURE not in {
        item.signature for item in source.attestation.dependency_routine_identities
    }
    target_values = target.value["attestation"]
    source_values = source.value["attestation"]
    observed_changes: dict[str, str] = {}
    for field in ("routine_identities", "helper_routine_identities"):
        target_by_signature = {
            item["signature"]: item for item in target_values[field]
        }
        for item in source_values[field]:
            expected = target_by_signature[item["signature"]]
            if item != expected:
                changed_fields = {
                    key for key in item if item[key] != expected[key]
                }
                assert changed_fields == {"definition_sha256"}
                observed_changes[item["signature"]] = item["definition_sha256"]
    assert observed_changes == dict(SOURCE_ROUTINE_DEFINITION_SHA256)


def test_transactional_body_is_exact_derivation() -> None:
    artifact = _load_source_artifacts_for_tests()["base_migration"]
    body = transactional_migration_body(artifact)
    assert body.startswith(b"-- Privileged Canonical Writer v1")
    assert b"\nBEGIN;\n" not in body
    assert b"\nCOMMIT;\n" not in body
    assert body.endswith(b"\n")
    assert hashlib.sha256(body).hexdigest() == _plan().value[
        "transactional_migration_body_sha256"
    ]
    chunks = transactional_migration_chunks(artifact)
    assert b"".join(chunks) == body
    assert all(len(chunk) <= 120 * 1024 for chunk in chunks)
    assert tuple(hashlib.sha256(chunk).hexdigest() for chunk in chunks) == (
        UPGRADE_MIGRATION_CHUNK_SHA256
    )


def test_preflight_accepts_only_source_or_target() -> None:
    plan = _plan()
    source = preflight_schema_upgrade(
        plan,
        observed=_source_contract(),
        observed_at_unix=1,
    )
    assert source["state"] == "exact_source_1ef981b4"
    assert source["mutation_required"] is True
    target = preflight_schema_upgrade(
        plan,
        observed=_target(),
        observed_at_unix=2,
    )
    assert target["state"] == "exact_target"
    assert target["mutation_required"] is False

    drifted = copy.deepcopy(_source_contract().value)
    drifted["attestation"]["routine_identities"][0][
        "definition_sha256"
    ] = "f" * 64
    with pytest.raises(
        SchemaReconciliationError,
        match="schema_upgrade_unreviewed_database_drift",
    ):
        preflight_schema_upgrade(
            plan,
            observed=SchemaContract.from_mapping(drifted),
            observed_at_unix=3,
        )


def test_plan_is_canonical_and_binds_both_generations() -> None:
    plan = _plan()
    unsigned = dict(plan.value)
    digest = unsigned.pop("plan_sha256")
    assert digest == hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert plan.value["source_contract_sha256"] == _source_contract().sha256
    assert plan.value["target_contract_sha256"] == _target().sha256


def test_plan_rejects_noncanonical_or_semantically_mutated_values() -> None:
    value = copy.deepcopy(_plan().value)
    value["services_stopped_required"] = False
    with pytest.raises(SchemaReconciliationError, match="schema_upgrade_plan_invalid"):
        SchemaUpgradePlan.from_mapping(value)

    value = copy.deepcopy(_plan().value)
    value["unreviewed"] = True
    with pytest.raises(SchemaReconciliationError, match="schema_upgrade_plan_invalid"):
        SchemaUpgradePlan.from_mapping(value)


def test_execute_atomic_upgrade_locks_truth_and_commits_exact_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    config = _config()
    contracts = iter((_source_contract(), _target()))
    truth = SimpleNamespace(sha256="b" * 64)
    observations = iter(
        (
            SimpleNamespace(truth=truth, observation_sha256="c" * 64),
            SimpleNamespace(truth=truth, observation_sha256="d" * 64),
        )
    )
    monkeypatch.setattr(
        upgrade,
        "collect_schema_contract",
        lambda *args, **kwargs: next(contracts),
    )
    monkeypatch.setattr(
        upgrade,
        "parse_control_observation",
        lambda result: next(observations),
    )

    receipt = execute_atomic_schema_upgrade(
        _plan(),
        target=_target(),
        artifact=_load_source_artifacts_for_tests()["base_migration"],
        session=session,
        writer_config=config,
        writer_managed_hba_receipt=_managed_hba(config, user=config.user),
        admin_managed_hba_receipt=_managed_hba(
            config,
            user=session.username,
        ),
        authorization_sha256="f" * 64,
        started_at_unix=100,
    )

    assert receipt["state"] == "exact_target_committed"
    assert receipt["mutation_applied"] is True
    assert session.sql[0] == "BEGIN ISOLATION LEVEL SERIALIZABLE"
    assert session.sql[-1] == "COMMIT"
    assert "ROLLBACK" not in session.sql
    assert not any(sql.startswith("RESET ") for sql in session.sql)
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_sha256")
    assert digest == hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_execute_atomic_upgrade_replay_rolls_back_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    config = _config()
    truth = SimpleNamespace(sha256="b" * 64)
    monkeypatch.setattr(
        upgrade,
        "collect_schema_contract",
        lambda *args, **kwargs: _target(),
    )
    monkeypatch.setattr(
        upgrade,
        "parse_control_observation",
        lambda result: SimpleNamespace(
            truth=truth,
            observation_sha256="c" * 64,
        ),
    )

    receipt = execute_atomic_schema_upgrade(
        _plan(),
        target=_target(),
        artifact=_load_source_artifacts_for_tests()["base_migration"],
        session=session,
        writer_config=config,
        writer_managed_hba_receipt=_managed_hba(config, user=config.user),
        admin_managed_hba_receipt=_managed_hba(
            config,
            user=session.username,
        ),
        authorization_sha256="f" * 64,
        started_at_unix=100,
    )

    assert receipt["state"] == "already_exact_target"
    assert receipt["mutation_applied"] is False
    assert session.sql[-1] == "ROLLBACK"
    assert not any(
        sql.startswith("-- Privileged Canonical Writer v1")
        for sql in session.sql
    )


def test_execute_atomic_upgrade_rolls_back_unreviewed_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    config = _config()
    drifted = copy.deepcopy(_source_contract().value)
    drifted["attestation"]["routine_identities"][0][
        "definition_sha256"
    ] = "f" * 64
    observed = SchemaContract.from_mapping(drifted)
    truth = SimpleNamespace(sha256="b" * 64)
    monkeypatch.setattr(
        upgrade,
        "collect_schema_contract",
        lambda *args, **kwargs: observed,
    )
    monkeypatch.setattr(
        upgrade,
        "parse_control_observation",
        lambda result: SimpleNamespace(
            truth=truth,
            observation_sha256="c" * 64,
        ),
    )

    with pytest.raises(
        SchemaReconciliationError,
        match="schema_upgrade_unreviewed_database_drift",
    ):
        execute_atomic_schema_upgrade(
            _plan(),
            target=_target(),
            artifact=_load_source_artifacts_for_tests()["base_migration"],
            session=session,
            writer_config=config,
            writer_managed_hba_receipt=_managed_hba(config, user=config.user),
            admin_managed_hba_receipt=_managed_hba(
                config,
                user=session.username,
            ),
            authorization_sha256="a" * 64,
            started_at_unix=100,
        )
    assert session.sql[-1] == "ROLLBACK"


def test_post_apply_contract_mismatch_reports_only_structural_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    config = _config()
    drifted = copy.deepcopy(_target().value)
    drifted["attestation"]["routine_identities"][0][
        "definition_sha256"
    ] = "f" * 64
    observed_target = SchemaContract.from_mapping(drifted)
    contracts = iter((_source_contract(), observed_target))
    truth = SimpleNamespace(sha256="b" * 64)
    observations = iter(
        (
            SimpleNamespace(truth=truth, observation_sha256="c" * 64),
            SimpleNamespace(truth=truth, observation_sha256="d" * 64),
        )
    )
    monkeypatch.setattr(
        upgrade,
        "collect_schema_contract",
        lambda *args, **kwargs: next(contracts),
    )
    monkeypatch.setattr(
        upgrade,
        "parse_control_observation",
        lambda result: next(observations),
    )

    with pytest.raises(
        SchemaReconciliationError,
        match="schema_upgrade_post_apply_public_routines_invalid",
    ):
        execute_atomic_schema_upgrade(
            _plan(),
            target=_target(),
            artifact=_load_source_artifacts_for_tests()["base_migration"],
            session=session,
            writer_config=config,
            writer_managed_hba_receipt=_managed_hba(config, user=config.user),
            admin_managed_hba_receipt=_managed_hba(
                config,
                user=session.username,
            ),
            authorization_sha256="a" * 64,
            started_at_unix=100,
        )
    assert session.sql[-1] == "ROLLBACK"


def test_post_apply_collection_failure_is_stage_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    config = _config()
    calls = 0

    def collect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _source_contract()
        raise SchemaReconciliationError("schema_reconciliation_contract_invalid")

    truth = SimpleNamespace(sha256="b" * 64)
    monkeypatch.setattr(upgrade, "collect_schema_contract", collect)
    monkeypatch.setattr(
        upgrade,
        "parse_control_observation",
        lambda result: SimpleNamespace(
            truth=truth,
            observation_sha256="c" * 64,
        ),
    )

    with pytest.raises(
        SchemaReconciliationError,
        match="schema_upgrade_post_apply_contract_collection_failed",
    ):
        execute_atomic_schema_upgrade(
            _plan(),
            target=_target(),
            artifact=_load_source_artifacts_for_tests()["base_migration"],
            session=session,
            writer_config=config,
            writer_managed_hba_receipt=_managed_hba(config, user=config.user),
            admin_managed_hba_receipt=_managed_hba(
                config,
                user=session.username,
            ),
            authorization_sha256="a" * 64,
            started_at_unix=100,
        )
    assert session.sql[-1] == "ROLLBACK"


def test_upgrade_admin_authority_requires_every_fixed_database_invariant() -> None:
    session = _Session()
    session.query = lambda sql, maximum_rows: QueryResult(
        columns=upgrade._UPGRADE_ADMIN_AUTHORITY_COLUMNS,
        rows=(tuple("t" for _ in upgrade._UPGRADE_ADMIN_AUTHORITY_COLUMNS),),
        command_tag="SELECT 1",
    )
    receipt = collect_upgrade_admin_authority_receipt(
        session,
        observed_at_unix=100,
    )
    assert receipt["database_roles"] == [
        "canonical_brain_schema_reconciler",
        "cloudsqlsuperuser",
    ]
    assert receipt["temporary_admin_owns_zero_objects"] is True

    values = ["t" for _ in upgrade._UPGRADE_ADMIN_AUTHORITY_COLUMNS]
    values[5] = "f"
    session.query = lambda sql, maximum_rows: QueryResult(
        columns=upgrade._UPGRADE_ADMIN_AUTHORITY_COLUMNS,
        rows=(tuple(values),),
        command_tag="SELECT 1",
    )
    with pytest.raises(
        SchemaReconciliationError,
        match="schema_upgrade_admin_authority_invalid",
    ):
        collect_upgrade_admin_authority_receipt(
            session,
            observed_at_unix=101,
        )
