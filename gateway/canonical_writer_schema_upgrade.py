"""Exact, release-bound Canonical Writer schema generation upgrade.

The production canary database was last installed from the reviewed
``1ef981b4`` generation.  Its complete structural contract is still current,
but nineteen routine definitions predate the current sealed migration and the
Discord guild target helper does not exist yet.  This module describes that
one exact source generation and derives a transactional upgrade body from the
current sealed migration.  It does not infer versions from names or content.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from gateway.canonical_writer_db import (
    CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY,
    ManagedCloudSQLAdminHBAReceipt,
    QueryResult,
    WriterDBConfig,
)
from gateway.canonical_writer_foundation import SealedSQLArtifact
from gateway.canonical_writer_schema_reconciliation import (
    BASE_ARTIFACT_FILENAME,
    BASE_ARTIFACT_NAME,
    MISSING_HELPER_SIGNATURE,
    SchemaContract,
    SchemaReconciliationError,
    _target_policy,
    collect_schema_contract,
)
from gateway.canonical_writer_schema_reconciliation_db import (
    OBSERVER_CALL_SQL,
    parse_control_observation,
)


SOURCE_SCHEMA_REVISION = "1ef981b479a56254fece4a193dd33eed488e1870"
SOURCE_BASE_ARTIFACT_SHA256 = (
    "08096b0e61a61bd0bac3bd76bbf0a6cddd83bb90612e9851f21731c78478b509"
)
UPGRADE_PLAN_SCHEMA = "muncho-canonical-writer-schema-upgrade-plan.v1"
UPGRADE_PREFLIGHT_SCHEMA = "muncho-canonical-writer-schema-upgrade-preflight.v1"
UPGRADE_TERMINAL_SCHEMA = "muncho-canonical-writer-schema-upgrade-terminal.v1"
UPGRADE_ADMIN_AUTHORITY_SCHEMA = (
    "muncho-canonical-writer-schema-upgrade-admin-authority.v1"
)
UPGRADE_ADMIN_DATABASE_ROLES = (
    "canonical_brain_schema_reconciler",
    "cloudsqlsuperuser",
)
UPGRADE_MIGRATION_CHUNK_SHA256 = (
    "5c6eeec44e13a286575a1b374ab27d306242fa3f6abf92b2d62156792f63fa81",
    "268162ae814e156369246d668f6c830c05fe062147db7f1b21d13463740fd399",
    "675aa6153bde4cdf305047cc56c4446cd81eb7d878dd26f100eb5591429148ab",
)
_UPGRADE_MIGRATION_CHUNK_MARKERS = (
    b"\n-- Fixed public routine 2/17.",
    b"\n-- Fixed public routine 12/17.",
)
_UPGRADE_MIGRATION_CHUNK_MAX_BYTES = 120 * 1024

_PLAN_FIELDS = frozenset(
    {
        "schema",
        "release_revision",
        "source_schema_revision",
        "source_base_artifact_sha256",
        "source_contract_sha256",
        "target_contract_sha256",
        "target_base_artifact_sha256",
        "transactional_migration_body_sha256",
        "transaction_isolation",
        "canonical_truth_preservation_required",
        "services_stopped_required",
        "plan_sha256",
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
# The installed control observer admits this exact one-time login namespace.
# Schema upgrade strengthens that existing boundary with the dual-role admin
# authority receipt below; using a second username namespace would make the
# already-installed SECURITY DEFINER observer reject the caller before it can
# take the canonical-truth locks.
_UPGRADE_ADMIN = re.compile(r"^muncho_canary_reconciler_[0-9a-f]{16}$")

# Full-contract comparison proved that these are the only definition changes
# between the deployed source generation and the current target generation.
# All other routine identity fields and every non-routine contract field are
# byte-identical.  The helper named by MISSING_HELPER_SIGNATURE is absent.
SOURCE_ROUTINE_DEFINITION_SHA256: Mapping[str, str] = {
    "canonical_brain._append_event(text, text, text, jsonb, jsonb, jsonb, jsonb, text, text, jsonb)": "7296765fea3bf86c543d62ebf5f13ba49a6bf28b41c93d466252959317bb3659",
    "canonical_brain._case_scope_authorized(text, jsonb, boolean)": "9ba3e7c0ed31621d602b3d6e9be645d37f9f78ee2d0dcd268df017f8442afe86",
    "canonical_brain._keys_valid(jsonb, text[], text[])": "dc90715846229ac2203ba521c5dc6cab3bbdadb4e3c156a0ad116263b49a188e",
    "canonical_brain._plan_head(text)": "1af372bc217c7b4ed8e8465877420d76af3a2ea0ddd727db5d9873efd9be502e",
    "canonical_brain._runtime_valid(jsonb)": "c6baa25cdb6915f2b8e35c5bd2030e143be2c08e7b9f69b37a92040d95611f0d",
    "canonical_brain.writer_capability_consume(jsonb, jsonb)": "1e1f78d96bb66e8699e9bd5e56f1941afbb34cf95854d13ac74df7c12bfbee98",
    "canonical_brain.writer_capability_grant(jsonb, jsonb)": "5bc55dfbf6f0b233a63df89af14ceecb4bcb6580478f7e0993d7f00cdf6aa649",
    "canonical_brain.writer_capability_revoke_session(jsonb, jsonb)": "8f51b481c383c8b535f6e3eedf995102a2f5f3d36c6de85799eca4705edd91c1",
    "canonical_brain.writer_case_query(jsonb, jsonb)": "b43d99de5b57ca9346686655e20878d43f4af4a327e27065fc6a67100b13ced7",
    "canonical_brain.writer_lease_shadow_record(jsonb, jsonb)": "82822243327221a8f3f7ba3f9df1f1278c043671bb8bf2cdabdae0c20cd9d1b2",
    "canonical_brain.writer_ping(jsonb, jsonb)": "351219d9e269d2f0358e1bffe6a74151c12eecde14ae25237df74dcc1e50efd9",
    "canonical_brain.writer_plan_transition(jsonb, jsonb)": "730ae2b9177e072a697f0c793babd7c497ff83d0594e0d078e0e024807a3db35",
    "canonical_brain.writer_projection_read_events(jsonb, jsonb)": "c213a7dd0fa78111208ab5a4d496b2ffbe6c6f4ad069ace59bd014a3b87ff4d5",
    "canonical_brain.writer_routeback_claim(jsonb, jsonb)": "e8cfad9dc55b241576de8468b2961e4e0e826d15e6af1ecd9d6056eeefd04541",
    "canonical_brain.writer_routeback_context(jsonb, jsonb)": "eea0eb75ecc492f7e1c825484f7999f29b59444a652ec7f3348e961c5b15a490",
    "canonical_brain.writer_routeback_finalize_blocked(jsonb, jsonb)": "825d83b84e59a70136b0d06bf4da326264a9b64493de219085ed3303c02e4cb1",
    "canonical_brain.writer_routeback_finalize_sent(jsonb, jsonb)": "6e45da3a80bbb35bb033c9f4d8ef79a1e732a90448389fe82f5e4207e7088b05",
    "canonical_brain.writer_routeback_recover(jsonb, jsonb)": "71ce695de41feea6839e9a0f56c2c1e441d68bf149dd0746d5e52f1de9d02004",
    "canonical_brain.writer_verification_append(jsonb, jsonb)": "2f2f712caf91629707e28971161b14a432bacff2ddaf223011626f1868c9370e",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def source_contract_value(target: SchemaContract) -> Mapping[str, Any]:
    """Project the exact deployed ``1ef981b4`` contract from the target."""

    if not isinstance(target, SchemaContract) or not target.is_target:
        raise SchemaReconciliationError("schema_upgrade_target_contract_invalid")
    value = copy.deepcopy(dict(target.value))
    attestation = value.get("attestation")
    if not isinstance(attestation, dict):
        raise SchemaReconciliationError("schema_upgrade_target_contract_invalid")
    seen: set[str] = set()
    for field in ("routine_identities", "helper_routine_identities"):
        identities = attestation.get(field)
        if not isinstance(identities, list):
            raise SchemaReconciliationError("schema_upgrade_target_contract_invalid")
        projected: list[Mapping[str, Any]] = []
        for identity in identities:
            if not isinstance(identity, dict):
                raise SchemaReconciliationError("schema_upgrade_target_contract_invalid")
            signature = identity.get("signature")
            if signature == MISSING_HELPER_SIGNATURE:
                continue
            digest = SOURCE_ROUTINE_DEFINITION_SHA256.get(str(signature))
            if digest is not None:
                if signature in seen or _SHA256.fullmatch(digest) is None:
                    raise SchemaReconciliationError(
                        "schema_upgrade_source_contract_invalid"
                    )
                identity["definition_sha256"] = digest
                seen.add(str(signature))
            projected.append(identity)
        attestation[field] = projected
    if seen != set(SOURCE_ROUTINE_DEFINITION_SHA256):
        raise SchemaReconciliationError("schema_upgrade_source_contract_invalid")
    value["helper_catalog_identity"] = None
    return value


def transactional_migration_body(artifact: SealedSQLArtifact) -> bytes:
    """Derive exact migration statements for an outer atomic transaction."""

    if (
        not isinstance(artifact, SealedSQLArtifact)
        or artifact.name != BASE_ARTIFACT_NAME
        or artifact.path.name != BASE_ARTIFACT_FILENAME
        or hashlib.sha256(artifact.payload).hexdigest() != artifact.sha256
    ):
        raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
    begin = b"\nBEGIN;\n"
    end = b"\nCOMMIT;\n"
    position = artifact.payload.find(begin)
    if position < 0 or artifact.payload.find(begin, position + 1) >= 0:
        raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
    if not artifact.payload.endswith(end):
        raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
    body = (
        artifact.payload[: position + 1]
        + artifact.payload[position + len(begin) : -len(end)]
        + b"\n"
    )
    if not body or b"\nCOMMIT;\n" in body or b"\nBEGIN;\n" in body:
        raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
    return body


def transactional_migration_chunks(
    artifact: SealedSQLArtifact,
) -> tuple[bytes, ...]:
    """Partition the exact sealed body below the normal query-size bound.

    The two split markers are reviewed top-level statement boundaries in this
    exact artifact. Chunk digests are pinned so a changed artifact, ambiguous
    marker, or accidental textual rewrite cannot acquire a large-query path.
    """

    body = transactional_migration_body(artifact)
    chunks: list[bytes] = []
    start = 0
    for marker in _UPGRADE_MIGRATION_CHUNK_MARKERS:
        if body.count(marker) != 1:
            raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
        boundary = body.index(marker, start) + 1
        if boundary <= start:
            raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
        chunks.append(body[start:boundary])
        start = boundary
    chunks.append(body[start:])
    result = tuple(chunks)
    if (
        b"".join(result) != body
        or any(
            not chunk or len(chunk) > _UPGRADE_MIGRATION_CHUNK_MAX_BYTES
            for chunk in result
        )
        or tuple(hashlib.sha256(chunk).hexdigest() for chunk in result)
        != UPGRADE_MIGRATION_CHUNK_SHA256
    ):
        raise SchemaReconciliationError("schema_upgrade_artifact_invalid")
    return result


@dataclass(frozen=True)
class SchemaUpgradePlan:
    value: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SchemaUpgradePlan":
        code = "schema_upgrade_plan_invalid"
        if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
            raise SchemaReconciliationError(code)
        raw = copy.deepcopy(dict(value))
        unsigned = {key: item for key, item in raw.items() if key != "plan_sha256"}
        digest_fields = (
            "source_base_artifact_sha256",
            "source_contract_sha256",
            "target_contract_sha256",
            "target_base_artifact_sha256",
            "transactional_migration_body_sha256",
            "plan_sha256",
        )
        if (
            raw.get("schema") != UPGRADE_PLAN_SCHEMA
            or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
            or raw.get("source_schema_revision") != SOURCE_SCHEMA_REVISION
            or any(
                not isinstance(raw.get(name), str)
                or _SHA256.fullmatch(str(raw.get(name))) is None
                for name in digest_fields
            )
            or raw.get("source_base_artifact_sha256")
            != SOURCE_BASE_ARTIFACT_SHA256
            or raw.get("source_contract_sha256")
            == raw.get("target_contract_sha256")
            or raw.get("transaction_isolation") != "SERIALIZABLE"
            or raw.get("canonical_truth_preservation_required") is not True
            or raw.get("services_stopped_required") is not True
            or raw.get("plan_sha256") != _sha256_json(unsigned)
        ):
            raise SchemaReconciliationError(code)
        return cls(json.loads(_canonical_bytes(raw).decode("utf-8")))

    @classmethod
    def build(
        cls,
        *,
        release_revision: str,
        target: SchemaContract,
        artifact: SealedSQLArtifact,
    ) -> "SchemaUpgradePlan":
        if _REVISION.fullmatch(release_revision or "") is None:
            raise SchemaReconciliationError("schema_upgrade_revision_invalid")
        source = source_contract_value(target)
        body = transactional_migration_body(artifact)
        unsigned = {
            "schema": UPGRADE_PLAN_SCHEMA,
            "release_revision": release_revision,
            "source_schema_revision": SOURCE_SCHEMA_REVISION,
            "source_base_artifact_sha256": SOURCE_BASE_ARTIFACT_SHA256,
            "source_contract_sha256": _sha256_json(source),
            "target_contract_sha256": target.sha256,
            "target_base_artifact_sha256": artifact.sha256,
            "transactional_migration_body_sha256": hashlib.sha256(body).hexdigest(),
            "transaction_isolation": "SERIALIZABLE",
            "canonical_truth_preservation_required": True,
            "services_stopped_required": True,
        }
        return cls.from_mapping(
            {**unsigned, "plan_sha256": _sha256_json(unsigned)}
        )

    @property
    def sha256(self) -> str:
        return str(self.value["plan_sha256"])

    @property
    def revision(self) -> str:
        return str(self.value["release_revision"])


def preflight_schema_upgrade(
    plan: SchemaUpgradePlan,
    *,
    observed: SchemaContract,
    observed_at_unix: int,
) -> Mapping[str, Any]:
    if (
        not isinstance(plan, SchemaUpgradePlan)
        or not isinstance(observed, SchemaContract)
        or type(observed_at_unix) is not int
        or observed_at_unix < 0
    ):
        raise SchemaReconciliationError("schema_upgrade_preflight_invalid")
    if observed.sha256 == plan.value["source_contract_sha256"]:
        state = "exact_source_1ef981b4"
        mutation_required = True
    elif observed.sha256 == plan.value["target_contract_sha256"]:
        state = "exact_target"
        mutation_required = False
    else:
        raise SchemaReconciliationError("schema_upgrade_unreviewed_database_drift")
    unsigned = {
        "schema": UPGRADE_PREFLIGHT_SCHEMA,
        "ok": True,
        "release_revision": plan.value["release_revision"],
        "plan_sha256": plan.sha256,
        "observed_contract_sha256": observed.sha256,
        "source_contract_sha256": plan.value["source_contract_sha256"],
        "target_contract_sha256": plan.value["target_contract_sha256"],
        "state": state,
        "mutation_required": mutation_required,
        "observed_at_unix": observed_at_unix,
    }
    return {**unsigned, "preflight_sha256": _sha256_json(unsigned)}


class SchemaUpgradeSession(Protocol):
    username: str

    def query(self, sql: str, *, maximum_rows: int) -> Any: ...


_UPGRADE_ADMIN_AUTHORITY_COLUMNS = (
    "database_exact",
    "postgresql_major_exact",
    "session_identity_exact",
    "temporary_admin_inventory_exact",
    "temporary_admin_attributes_exact",
    "direct_provider_memberships_exact",
    "recursive_authority_closure_exact",
    "migration_owner_membership_absent",
    "temporary_admin_owns_zero_objects",
    "foreign_database_sessions_absent",
    "prepared_transactions_disabled_and_empty",
    "event_trigger_inventory_empty",
)

_UPGRADE_ADMIN_AUTHORITY_SQL = r"""
WITH RECURSIVE session_role AS MATERIALIZED (
    SELECT role.* FROM pg_catalog.pg_roles AS role
     WHERE role.rolname = SESSION_USER
), direct_edges AS MATERIALIZED (
    SELECT granted.rolname AS granted_name,
           member.rolname AS member_name,
           grantor.rolname AS grantor_name,
           membership.admin_option,
           membership.inherit_option,
           membership.set_option
      FROM pg_catalog.pg_auth_members AS membership
      JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
      JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
      JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = membership.grantor
     WHERE membership.member = (SELECT oid FROM session_role)
), forward_role_closure(roleid) AS (
    SELECT membership.roleid
      FROM pg_catalog.pg_auth_members AS membership
     WHERE membership.member = (SELECT oid FROM session_role)
    UNION
    SELECT membership.roleid
      FROM pg_catalog.pg_auth_members AS membership
      JOIN forward_role_closure AS reachable
        ON reachable.roleid = membership.member
), provider_forward_role_closure(roleid) AS (
    SELECT role.oid FROM pg_catalog.pg_roles AS role
     WHERE role.rolname = 'cloudsqlsuperuser'
    UNION
    SELECT membership.roleid
      FROM pg_catalog.pg_auth_members AS membership
      JOIN provider_forward_role_closure AS reachable
        ON reachable.roleid = membership.member
), expected_forward_roles(roleid) AS (
    SELECT roleid FROM provider_forward_role_closure
    UNION
    SELECT role.oid FROM pg_catalog.pg_roles AS role
     WHERE role.rolname = 'canonical_brain_schema_reconciler'
)
SELECT pg_catalog.current_database() = 'muncho_canary_brain'
           AS database_exact,
       pg_catalog.current_setting('server_version_num')::integer / 10000 = 18
           AS postgresql_major_exact,
       CURRENT_USER = SESSION_USER
           AND SESSION_USER ~ '^muncho_canary_reconciler_[0-9a-f]{16}$'
           AND (SELECT pg_catalog.count(*) FROM session_role) = 1
           AS session_identity_exact,
       (SELECT pg_catalog.count(*) = 1 FROM pg_catalog.pg_roles
         WHERE rolname ~ '^muncho_canary_reconciler_[0-9a-f]{16}$')
           AS temporary_admin_inventory_exact,
       (SELECT rolcanlogin AND rolinherit AND NOT rolsuper
               AND rolcreatedb AND rolcreaterole
               AND NOT rolreplication AND NOT rolbypassrls
               AND rolconnlimit = -1 AND rolvaliduntil IS NULL
               AND rolconfig IS NULL FROM session_role)
           AS temporary_admin_attributes_exact,
       (SELECT pg_catalog.count(*) = 2 AND COALESCE(pg_catalog.bool_and(
                   member_name = SESSION_USER
                   AND grantor_name = 'cloudsqladmin'
                   AND granted_name IN (
                       'canonical_brain_schema_reconciler',
                       'cloudsqlsuperuser'
                   )
                   AND admin_option IS FALSE
                   AND inherit_option IS TRUE
                   AND set_option IS TRUE
               ), false)
          AND pg_catalog.count(DISTINCT granted_name) = 2
          FROM direct_edges)
           AS direct_provider_memberships_exact,
       NOT EXISTS (
           (SELECT roleid FROM forward_role_closure
            EXCEPT SELECT roleid FROM expected_forward_roles)
           UNION ALL
           (SELECT roleid FROM expected_forward_roles
            EXCEPT SELECT roleid FROM forward_role_closure)
       ) AS recursive_authority_closure_exact,
       NOT pg_catalog.pg_has_role(
           SESSION_USER, 'canonical_brain_migration_owner', 'MEMBER'
       ) AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = membership.roleid
            JOIN session_role ON session_role.oid = membership.member
           WHERE owner_role.rolname = 'canonical_brain_migration_owner'
       ) AS migration_owner_membership_absent,
       NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_shdepend AS dependency
            JOIN session_role ON session_role.oid = dependency.refobjid
           WHERE dependency.refclassid =
                 'pg_catalog.pg_authid'::pg_catalog.regclass
             AND dependency.deptype = 'o'
       ) AS temporary_admin_owns_zero_objects,
       NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_stat_activity AS activity
            WHERE activity.backend_type = 'client backend'
              AND activity.pid <> pg_catalog.pg_backend_pid()
              AND activity.datname = pg_catalog.current_database()
       ) AS foreign_database_sessions_absent,
       pg_catalog.current_setting('max_prepared_transactions')::integer = 0
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_prepared_xacts)
           AS prepared_transactions_disabled_and_empty,
       NOT EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger)
           AS event_trigger_inventory_empty
""".strip()


def collect_upgrade_admin_authority_receipt(
    session: SchemaUpgradeSession,
    *,
    observed_at_unix: int,
) -> Mapping[str, Any]:
    code = "schema_upgrade_admin_authority_invalid"
    if (
        type(observed_at_unix) is not int
        or observed_at_unix < 0
        or _UPGRADE_ADMIN.fullmatch(str(getattr(session, "username", "")))
        is None
    ):
        raise SchemaReconciliationError(code)
    result = session.query(_UPGRADE_ADMIN_AUTHORITY_SQL, maximum_rows=1)
    if (
        not isinstance(result, QueryResult)
        or result.command_tag.upper() != "SELECT 1"
        or result.columns != _UPGRADE_ADMIN_AUTHORITY_COLUMNS
        or len(result.rows) != 1
        or len(result.rows[0]) != len(_UPGRADE_ADMIN_AUTHORITY_COLUMNS)
        or any(item not in {"t", "true"} for item in result.rows[0])
    ):
        raise SchemaReconciliationError(code)
    unsigned = {
        "schema": UPGRADE_ADMIN_AUTHORITY_SCHEMA,
        "database_roles": list(UPGRADE_ADMIN_DATABASE_ROLES),
        "temporary_admin_username_sha256": hashlib.sha256(
            session.username.encode("ascii")
        ).hexdigest(),
        **dict(zip(_UPGRADE_ADMIN_AUTHORITY_COLUMNS, (True,) * 12, strict=True)),
        "observed_at_unix": observed_at_unix,
        "secret_material_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha256_json(unsigned)}


def _require_command(session: SchemaUpgradeSession, sql: str, tag: str) -> None:
    result = session.query(sql, maximum_rows=0)
    if str(getattr(result, "command_tag", "")).upper() != tag or tuple(
        getattr(result, "rows", ())
    ):
        raise SchemaReconciliationError("schema_upgrade_database_command_invalid")


def _setting_sql(name: str, value: str) -> str:
    if (
        name not in {
            "muncho.canonical_writer_migration_scope",
            "muncho.canonical_writer_migration_database",
            "muncho.canonical_writer_migration_approval_receipt_sha256",
            "muncho.canonical_writer_cloudsqladmin_hba_rejection_sha256",
        }
        or not isinstance(value, str)
        or "'" in value
        or "\\" in value
    ):
        raise SchemaReconciliationError("schema_upgrade_database_binding_invalid")
    return f"SET LOCAL {name} = '{value}'"


def _validate_hba_receipts(
    *,
    writer_config: WriterDBConfig,
    writer_receipt: ManagedCloudSQLAdminHBAReceipt,
    admin_receipt: ManagedCloudSQLAdminHBAReceipt,
    admin_username: str,
    now_unix: int,
) -> None:
    writer_binding = (
        writer_config.host,
        writer_config.tls_server_name,
        writer_config.port,
        writer_config.user,
    )
    if (
        (
            writer_receipt.host,
            writer_receipt.tls_server_name,
            writer_receipt.port,
            writer_receipt.user,
        )
        != writer_binding
        or (
            admin_receipt.host,
            admin_receipt.tls_server_name,
            admin_receipt.port,
            admin_receipt.user,
        )
        != (*writer_binding[:3], admin_username)
        or not writer_receipt.is_fresh(now_unix)
        or not admin_receipt.is_fresh(now_unix)
        or writer_receipt.server_certificate_sha256
        != admin_receipt.server_certificate_sha256
    ):
        raise SchemaReconciliationError("schema_upgrade_hba_binding_invalid")


def _terminal_receipt(unsigned: Mapping[str, Any]) -> Mapping[str, Any]:
    return {**unsigned, "receipt_sha256": _sha256_json(unsigned)}


def _apply_transactional_migration_chunks(
    session: SchemaUpgradeSession,
    artifact: SealedSQLArtifact,
) -> None:
    chunks = transactional_migration_chunks(artifact)
    expected_tags = ("CREATE FUNCTION", "CREATE FUNCTION", "DO")
    for index, (chunk, expected_tag) in enumerate(
        zip(chunks, expected_tags, strict=True)
    ):
        result = session.query(
            chunk.decode("utf-8", errors="strict"),
            maximum_rows=1 if index == 0 else 0,
        )
        if (
            str(getattr(result, "command_tag", "")).upper() != expected_tag
            or (
                index == 0
                and (
                    tuple(getattr(result, "columns", ()))
                    != ("pg_advisory_xact_lock",)
                    or tuple(getattr(result, "rows", ())) != (("",),)
                )
            )
            or (
                index != 0
                and (
                    tuple(getattr(result, "columns", ()))
                    or tuple(getattr(result, "rows", ()))
                )
            )
        ):
            raise SchemaReconciliationError(
                "schema_upgrade_database_apply_failed"
            )


def execute_atomic_schema_upgrade(
    plan: SchemaUpgradePlan,
    *,
    target: SchemaContract,
    artifact: SealedSQLArtifact,
    session: SchemaUpgradeSession,
    writer_config: WriterDBConfig,
    writer_managed_hba_receipt: ManagedCloudSQLAdminHBAReceipt,
    admin_managed_hba_receipt: ManagedCloudSQLAdminHBAReceipt,
    authorization_sha256: str,
    started_at_unix: int,
) -> Mapping[str, Any]:
    """Upgrade one exact source generation inside one SERIALIZABLE xact.

    The already-installed fixed control observer acquires the global
    deployment lock before it reads any canonical row and keeps its table
    locks until this outer transaction commits.  The current base artifact is
    executed without only its own BEGIN/COMMIT wrapper so source observation,
    migration, target observation, and full truth equality are atomic.
    """

    if (
        not isinstance(plan, SchemaUpgradePlan)
        or not isinstance(target, SchemaContract)
        or target.sha256 != plan.value.get("target_contract_sha256")
        or not isinstance(writer_config, WriterDBConfig)
        or writer_config.user != "muncho_canary_writer_login"
        or not isinstance(writer_managed_hba_receipt, ManagedCloudSQLAdminHBAReceipt)
        or not isinstance(admin_managed_hba_receipt, ManagedCloudSQLAdminHBAReceipt)
        or _SHA256.fullmatch(authorization_sha256 or "") is None
        or type(started_at_unix) is not int
        or started_at_unix < 0
        or not isinstance(getattr(session, "username", None), str)
        or re.fullmatch(
            r"muncho_canary_reconciler_[0-9a-f]{16}", session.username
        )
        is None
    ):
        raise SchemaReconciliationError("schema_upgrade_execution_invalid")
    canonical_plan = SchemaUpgradePlan.from_mapping(plan.value)
    if canonical_plan.value != plan.value:
        raise SchemaReconciliationError("schema_upgrade_plan_binding_invalid")
    _validate_hba_receipts(
        writer_config=writer_config,
        writer_receipt=writer_managed_hba_receipt,
        admin_receipt=admin_managed_hba_receipt,
        admin_username=session.username,
        now_unix=started_at_unix,
    )
    body = transactional_migration_body(artifact)
    if (
        artifact.sha256 != plan.value["target_base_artifact_sha256"]
        or hashlib.sha256(body).hexdigest()
        != plan.value["transactional_migration_body_sha256"]
        or admin_managed_hba_receipt.user != session.username
        or writer_managed_hba_receipt.user != writer_config.user
    ):
        raise SchemaReconciliationError("schema_upgrade_plan_binding_invalid")

    settings = (
        (
            "muncho.canonical_writer_migration_scope",
            "isolated_canary_copy",
        ),
        (
            "muncho.canonical_writer_migration_database",
            writer_config.database,
        ),
        (
            "muncho.canonical_writer_migration_approval_receipt_sha256",
            authorization_sha256,
        ),
        (
            "muncho.canonical_writer_cloudsqladmin_hba_rejection_sha256",
            admin_managed_hba_receipt.sha256,
        ),
    )
    transaction_open = False
    initial_contract: SchemaContract | None = None
    final_contract: SchemaContract | None = None
    initial_observation = None
    final_observation = None
    initial_truth = None
    final_truth = None
    try:
        _require_command(session, "BEGIN ISOLATION LEVEL SERIALIZABLE", "BEGIN")
        transaction_open = True
        for name, value in settings:
            _require_command(session, _setting_sql(name, value), "SET")
        initial_observation = parse_control_observation(
            session.query(OBSERVER_CALL_SQL, maximum_rows=1)
        )
        initial_truth = initial_observation.truth
        initial_contract = collect_schema_contract(
            session,
            config=writer_config,
            policy=_target_policy(target.attestation),
            managed_hba_receipt=writer_managed_hba_receipt,
            subject_user=writer_config.user,
            allow_missing_helper=True,
        )
        if initial_contract.sha256 != plan.value["source_contract_sha256"]:
            if initial_contract.sha256 == target.sha256:
                _require_command(session, "ROLLBACK", "ROLLBACK")
                transaction_open = False
                return _terminal_receipt(
                    {
                        "schema": UPGRADE_TERMINAL_SCHEMA,
                        "ok": True,
                        "state": "already_exact_target",
                        "release_revision": plan.value["release_revision"],
                        "plan_sha256": plan.sha256,
                        "authorization_sha256": authorization_sha256,
                        "initial_contract_sha256": target.sha256,
                        "final_contract_sha256": target.sha256,
                        "canonical_truth_receipt_sha256": initial_truth.sha256,
                        "initial_observation_sha256": (
                            initial_observation.observation_sha256
                        ),
                        "final_observation_sha256": (
                            initial_observation.observation_sha256
                        ),
                        "writer_managed_hba_receipt_sha256": (
                            writer_managed_hba_receipt.sha256
                        ),
                        "admin_managed_hba_receipt_sha256": (
                            admin_managed_hba_receipt.sha256
                        ),
                        "mutation_applied": False,
                        "deployment_lock_key": (
                            CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY
                        ),
                        "started_at_unix": started_at_unix,
                        "secret_material_recorded": False,
                    }
                )
            raise SchemaReconciliationError(
                "schema_upgrade_unreviewed_database_drift"
            )
        _apply_transactional_migration_chunks(session, artifact)
        final_contract = collect_schema_contract(
            session,
            config=writer_config,
            policy=_target_policy(target.attestation),
            managed_hba_receipt=writer_managed_hba_receipt,
            subject_user=writer_config.user,
            allow_missing_helper=False,
        )
        final_observation = parse_control_observation(
            session.query(OBSERVER_CALL_SQL, maximum_rows=1)
        )
        final_truth = final_observation.truth
        if final_contract.sha256 != target.sha256:
            raise SchemaReconciliationError(
                "schema_upgrade_post_apply_contract_invalid"
            )
        if final_truth != initial_truth:
            raise SchemaReconciliationError("schema_upgrade_canonical_truth_changed")
        _require_command(session, "COMMIT", "COMMIT")
        transaction_open = False
    except BaseException:
        if transaction_open:
            try:
                _require_command(session, "ROLLBACK", "ROLLBACK")
            except BaseException:
                pass
        raise
    if (
        initial_contract is None
        or final_contract is None
        or initial_observation is None
        or final_observation is None
        or initial_truth is None
        or final_truth is None
    ):
        raise SchemaReconciliationError("schema_upgrade_execution_invalid")
    unsigned = {
        "schema": UPGRADE_TERMINAL_SCHEMA,
        "ok": True,
        "state": "exact_target_committed",
        "release_revision": plan.value["release_revision"],
        "plan_sha256": plan.sha256,
        "authorization_sha256": authorization_sha256,
        "initial_contract_sha256": initial_contract.sha256,
        "final_contract_sha256": final_contract.sha256,
        "canonical_truth_receipt_sha256": initial_truth.sha256,
        "initial_observation_sha256": initial_observation.observation_sha256,
        "final_observation_sha256": final_observation.observation_sha256,
        "writer_managed_hba_receipt_sha256": (
            writer_managed_hba_receipt.sha256
        ),
        "admin_managed_hba_receipt_sha256": (
            admin_managed_hba_receipt.sha256
        ),
        "mutation_applied": True,
        "deployment_lock_key": CANONICAL_WRITER_DEPLOYMENT_LOCK_KEY,
        "started_at_unix": started_at_unix,
        "secret_material_recorded": False,
    }
    return _terminal_receipt(unsigned)


__all__ = [
    "SOURCE_BASE_ARTIFACT_SHA256",
    "SOURCE_ROUTINE_DEFINITION_SHA256",
    "SOURCE_SCHEMA_REVISION",
    "UPGRADE_ADMIN_DATABASE_ROLES",
    "UPGRADE_MIGRATION_CHUNK_SHA256",
    "SchemaUpgradePlan",
    "collect_upgrade_admin_authority_receipt",
    "execute_atomic_schema_upgrade",
    "preflight_schema_upgrade",
    "source_contract_value",
    "transactional_migration_body",
    "transactional_migration_chunks",
]
