"""Real PostgreSQL 18 proof for the sealed 1ef981b4 schema upgrade."""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import uuid

import pytest

from gateway import canonical_writer_schema_upgrade as schema_upgrade
from gateway.canonical_writer_db import (
    CredentialSource,
    WriterDBConfig,
    _open_postgres_session,
)
from gateway.canonical_writer_foundation import _load_source_artifacts_for_tests
from gateway.canonical_writer_schema_reconciliation import (
    SchemaContractAsset,
    _target_policy,
    collect_schema_contract,
)
from gateway.canonical_writer_schema_upgrade import (
    SOURCE_BASE_ARTIFACT_SHA256,
    SchemaUpgradePlan,
    collect_upgrade_admin_authority_receipt,
    execute_atomic_schema_upgrade,
)
from tests.integration.test_canonical_writer_real_postgres import (
    CONTROL_INSTALL,
    DATABASE,
    IMAGE,
    LOGIN,
    MIGRATION,
    SCHEMA_CONTRACT_ASSET,
    _canonical14_identity,
    _generate_tls,
    _migration_invocation,
    _psql,
    _psql_as,
    _psql_fields,
    _run,
    _test_managed_hba_receipt,
    _wait_ready,
)


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FIXTURE = (
    ROOT / "tests/fixtures/canonical_writer_v1_1ef981b.sql.gz.b64"
)


def _source_migration() -> str:
    payload = gzip.decompress(base64.b64decode(SOURCE_FIXTURE.read_bytes()))
    assert hashlib.sha256(payload).hexdigest() == SOURCE_BASE_ARTIFACT_SHA256
    return payload.decode("utf-8", errors="strict")


def _install_source_generation_control(name: str) -> None:
    control_login = "muncho_canary_control_" + "c" * 16
    # Vanilla PostgreSQL cannot reproduce Cloud SQL's provider-only
    # CREATEROLE authority. The superuser surrogate exists only while the
    # reviewed control artifact is installed in this disposable container.
    _psql_as(
        name,
        DATABASE,
        "cloudsqladmin",
        "ALTER ROLE cloudsqlsuperuser SUPERUSER;\n"
        f"CREATE ROLE {control_login} LOGIN INHERIT NOSUPERUSER CREATEDB "
        "CREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1;\n"
        f"GRANT canonical_brain_migration_owner TO {control_login} "
        "WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;\n"
        f"GRANT cloudsqlsuperuser TO {control_login} "
        "WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;\n",
    )
    _psql_as(
        name,
        DATABASE,
        control_login,
        CONTROL_INSTALL.read_text(encoding="utf-8"),
    )
    _psql(
        name,
        DATABASE,
        f"DROP ROLE {control_login};\n"
        "ALTER ROLE cloudsqlsuperuser LOGIN NOSUPERUSER CREATEDB CREATEROLE "
        "NOREPLICATION NOBYPASSRLS;\n",
    )


def _seed_canonical_truth(name: str) -> None:
    _psql(
        name,
        DATABASE,
        "INSERT INTO public.canonical_event_log ("
        "event_id,schema_version,event_type,occurred_at,case_id,source,actor,"
        "subject,evidence,decision,status,next_action,safety,payload) VALUES ("
        "'00000000-0000-0000-0000-000000000001','v1','schema_upgrade_e2e',"
        "'2026-08-03T00:00:00Z','schema-upgrade-e2e','{}','{}','{}','{}','{}',"
        "'{}','{}','{}','{\"proof\":true}');\n",
    )


def _seed_quarantine_anchors(name: str) -> None:
    _psql(
        name,
        DATABASE,
        "CREATE SCHEMA canonical_brain_legacy_quarantine "
        "AUTHORIZATION postgres;\n"
        "REVOKE ALL ON SCHEMA canonical_brain_legacy_quarantine FROM PUBLIC;\n"
        "CREATE TABLE canonical_brain_legacy_quarantine."
        "canonical_event_log_legacy_v1 (anchor bigint);\n"
        "CREATE TABLE canonical_brain_legacy_quarantine."
        "reconciliation_receipts (anchor bigint);\n"
        "ALTER TABLE canonical_brain_legacy_quarantine."
        "canonical_event_log_legacy_v1 OWNER TO postgres;\n"
        "ALTER TABLE canonical_brain_legacy_quarantine."
        "reconciliation_receipts OWNER TO postgres;\n"
        "REVOKE ALL ON TABLE canonical_brain_legacy_quarantine."
        "canonical_event_log_legacy_v1 FROM PUBLIC;\n"
        "REVOKE ALL ON TABLE canonical_brain_legacy_quarantine."
        "reconciliation_receipts FROM PUBLIC;\n",
    )


def test_source_generation_upgrades_atomically_and_replays_on_postgresql_18(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None or shutil.which("openssl") is None:
        pytest.skip("Docker and OpenSSL are required")
    if subprocess.run(
        ["docker", "info"], capture_output=True, check=False
    ).returncode:
        pytest.skip("Docker daemon is unavailable")

    ca_cert, server_cert, server_key = _generate_tls(tmp_path)
    admin_password = secrets.token_hex(32)
    writer_password = secrets.token_hex(32)
    admin_credential = tmp_path / "schema-upgrade-admin-password"
    writer_credential = tmp_path / "writer-password"
    admin_credential.write_text(admin_password + "\n", encoding="utf-8")
    writer_credential.write_text(writer_password + "\n", encoding="utf-8")
    admin_credential.chmod(0o600)
    writer_credential.chmod(0o600)
    name = "hermes-schema-upgrade-e2e-" + uuid.uuid4().hex[:12]
    admin_username = "muncho_canary_reconciler_" + "a" * 16
    environment = dict(os.environ)
    environment["POSTGRES_PASSWORD"] = secrets.token_hex(32)
    session = None

    try:
        if subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            capture_output=True,
            check=False,
        ).returncode:
            _run(["docker", "pull", IMAGE], timeout=300)
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_USER=cloudsqladmin",
                "-e",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                "-p",
                "127.0.0.1::5432",
                IMAGE,
            ],
            env=environment,
            timeout=180,
            secret_values=(environment["POSTGRES_PASSWORD"],),
        )
        _wait_ready(name, user="cloudsqladmin")
        _psql_as(
            name,
            "postgres",
            "cloudsqladmin",
            "CREATE ROLE postgres LOGIN SUPERUSER CREATEDB CREATEROLE "
            "REPLICATION BYPASSRLS;\n",
        )
        _run(["docker", "cp", str(server_cert), f"{name}:/tmp/cw-server.crt"])
        _run(["docker", "cp", str(server_key), f"{name}:/tmp/cw-server.key"])
        _run(
            [
                "docker",
                "exec",
                "-u",
                "0",
                name,
                "sh",
                "-ec",
                "chown postgres:postgres /tmp/cw-server.crt /tmp/cw-server.key; "
                "chmod 0644 /tmp/cw-server.crt; chmod 0600 /tmp/cw-server.key",
            ]
        )
        _psql(
            name,
            "postgres",
            "ALTER SYSTEM SET ssl = 'on';\n"
            "ALTER SYSTEM SET ssl_cert_file = '/tmp/cw-server.crt';\n"
            "ALTER SYSTEM SET ssl_key_file = '/tmp/cw-server.key';\n",
        )
        _run(["docker", "restart", name])
        _wait_ready(name)

        escaped_writer = writer_password.replace("'", "''")
        _psql(
            name,
            "postgres",
            "REVOKE ALL ON DATABASE postgres FROM PUBLIC;\n"
            "REVOKE ALL ON DATABASE template1 FROM PUBLIC;\n"
            "CREATE ROLE canonical_brain_migration_owner NOLOGIN NOINHERIT "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE canonical_brain_writer NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {LOGIN} LOGIN INHERIT PASSWORD '{escaped_writer}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
            f"GRANT canonical_brain_writer TO {LOGIN} "
            "WITH ADMIN FALSE, INHERIT TRUE, SET FALSE;\n"
            "CREATE ROLE cloudsqlsuperuser LOGIN NOSUPERUSER CREATEDB CREATEROLE "
            "NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE DATABASE {DATABASE} OWNER cloudsqlsuperuser;\n"
            f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;\n"
            f"GRANT CONNECT ON DATABASE {DATABASE} TO canonical_brain_writer;\n",
            secrets_=(writer_password,),
        )
        _psql(
            name,
            DATABASE,
            "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
            "GRANT USAGE ON SCHEMA public TO canonical_brain_migration_owner;\n"
            "CREATE TABLE public.canonical_event_log (\n"
            " event_id uuid NOT NULL, schema_version text NOT NULL,\n"
            " event_type text NOT NULL, occurred_at timestamptz NOT NULL,\n"
            " case_id text NOT NULL, source jsonb NOT NULL, actor jsonb NOT NULL,\n"
            " subject jsonb NOT NULL, evidence jsonb NOT NULL, decision jsonb NOT NULL,\n"
            " status jsonb NOT NULL, next_action jsonb NOT NULL, safety jsonb NOT NULL,\n"
            " payload jsonb NOT NULL, PRIMARY KEY (event_id)\n"
            ");\n"
            "ALTER TABLE public.canonical_event_log OWNER TO "
            "canonical_brain_migration_owner;\n",
        )
        source_sql = _source_migration()
        _psql(name, DATABASE, _migration_invocation(DATABASE, source_sql))
        _psql(name, DATABASE, _migration_invocation(DATABASE, source_sql))
        assert _psql_fields(
            name,
            DATABASE,
            "SELECT to_regprocedure('canonical_brain."
            "_discord_guild_routeback_target_valid(jsonb)') IS NULL;",
        ) == ["t"]
        _install_source_generation_control(name)
        _seed_quarantine_anchors(name)
        _seed_canonical_truth(name)
        truth_before = _canonical14_identity(name)

        escaped_admin = admin_password.replace("'", "''")
        _psql_as(
            name,
            DATABASE,
            "cloudsqladmin",
            f"CREATE ROLE {admin_username} LOGIN INHERIT PASSWORD "
            f"'{escaped_admin}' NOSUPERUSER CREATEDB CREATEROLE "
            "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1;\n"
            f"GRANT canonical_brain_schema_reconciler TO {admin_username} "
            "WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;\n"
            f"GRANT cloudsqlsuperuser TO {admin_username} "
            "WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;\n",
        )

        mapping = _run(["docker", "port", name, "5432/tcp"]).stdout.strip()
        port = int(mapping.rsplit(":", 1)[1])
        writer_config = WriterDBConfig(
            host="127.0.0.1",
            tls_server_name="localhost",
            port=port,
            database=DATABASE,
            user=LOGIN,
            ca_file=ca_cert,
            credential=CredentialSource(
                expected_uid=os.getuid(), path=writer_credential
            ),
            application_name="muncho-schema-upgrade-e2e-writer",
        )
        admin_config = WriterDBConfig(
            host="127.0.0.1",
            tls_server_name="localhost",
            port=port,
            database=DATABASE,
            user=admin_username,
            ca_file=ca_cert,
            credential=CredentialSource(
                expected_uid=os.getuid(), path=admin_credential
            ),
            application_name="muncho-schema-upgrade-e2e-admin",
        )
        target = SchemaContractAsset.from_bytes(
            SCHEMA_CONTRACT_ASSET.read_bytes()
        ).contract
        artifact = _load_source_artifacts_for_tests()["base_migration"]
        assert artifact.path == MIGRATION
        plan = SchemaUpgradePlan.build(
            release_revision="f" * 40,
            target=target,
            artifact=artifact,
        )
        writer_hba = _test_managed_hba_receipt(writer_config)
        admin_hba = _test_managed_hba_receipt(admin_config)
        started_at = int(time.time())

        session = _open_postgres_session(admin_config)
        raw_authority = session.query(
            schema_upgrade._UPGRADE_ADMIN_AUTHORITY_SQL, maximum_rows=1
        )
        authority_diagnostics = dict(
            zip(raw_authority.columns, raw_authority.rows[0], strict=True)
        )
        assert raw_authority.command_tag.upper() == "SELECT 1", (
            raw_authority.command_tag
        )
        assert raw_authority.columns == (
            schema_upgrade._UPGRADE_ADMIN_AUTHORITY_COLUMNS
        ), raw_authority.columns
        assert set(authority_diagnostics.values()) <= {"t", "true"}, (
            authority_diagnostics
        )
        authority = collect_upgrade_admin_authority_receipt(
            session, observed_at_unix=started_at
        )
        assert authority["direct_provider_memberships_exact"] is True
        session.close()
        session = None
        # Cloud SQL's provider role can grant the temporary offline-owner
        # membership while remaining NOSUPERUSER. Vanilla PostgreSQL has no
        # equivalent provider capability, so this disposable fixture promotes
        # only the exact one-time login for the sealed transaction, then
        # immediately demotes it and re-attests the real authority contract.
        _psql(
            name,
            DATABASE,
            f"ALTER ROLE {admin_username} SUPERUSER;\n",
        )
        session = _open_postgres_session(admin_config)
        try:
            receipt = execute_atomic_schema_upgrade(
                plan,
                target=target,
                artifact=artifact,
                session=session,
                writer_config=writer_config,
                writer_managed_hba_receipt=writer_hba,
                admin_managed_hba_receipt=admin_hba,
                authorization_sha256="f" * 64,
                started_at_unix=started_at,
            )
        except BaseException:
            logs = subprocess.run(
                ["docker", "logs", name],
                capture_output=True,
                text=True,
                check=False,
            ).stderr.splitlines()
            database_errors = "\n".join(
                line for line in logs if "ERROR:" in line or "CONTEXT:" in line
            )[-4000:]
            pytest.fail(f"real PostgreSQL upgrade failed:\n{database_errors}")
        assert receipt["state"] == "exact_target_committed"
        assert receipt["mutation_applied"] is True
        session.close()
        session = None
        _psql(
            name,
            DATABASE,
            f"ALTER ROLE {admin_username} NOSUPERUSER;\n",
        )
        session = _open_postgres_session(admin_config)
        terminal_authority = collect_upgrade_admin_authority_receipt(
            session, observed_at_unix=started_at
        )
        assert terminal_authority["temporary_admin_attributes_exact"] is True
        session.close()
        session = None

        writer_session = _open_postgres_session(writer_config)
        try:
            observed = collect_schema_contract(
                writer_session,
                config=writer_config,
                policy=_target_policy(target.attestation),
                managed_hba_receipt=writer_hba,
                subject_user=writer_config.user,
                allow_missing_helper=False,
            )
        finally:
            writer_session.close()
        assert observed.sha256 == target.sha256

        session = _open_postgres_session(admin_config)
        replay = execute_atomic_schema_upgrade(
            plan,
            target=target,
            artifact=artifact,
            session=session,
            writer_config=writer_config,
            writer_managed_hba_receipt=writer_hba,
            admin_managed_hba_receipt=admin_hba,
            authorization_sha256="f" * 64,
            started_at_unix=started_at,
        )
        assert replay["state"] == "already_exact_target"
        assert replay["mutation_applied"] is False
        session.close()
        session = None

        assert _canonical14_identity(name) == truth_before
    finally:
        if session is not None:
            session.close()
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            check=False,
        )
