from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import Mock, call

import pytest

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import production_database_recovery_gate as recovery


REVISION = "a" * 40
NOW = 1_800_000_000
CA_PEM = "-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n"


class Clock:
    def __init__(self, current: int = NOW) -> None:
        self.current = current

    def __call__(self) -> float:
        value = self.current
        self.current += 1
        return float(value)


class Probe:
    def __init__(self) -> None:
        self.available_checks = 0
        self.calls = 0

    def require_available(self) -> None:
        self.available_checks += 1

    def probe(
        self,
        *,
        release_revision: str,
        scratch: Mapping[str, Any],
        now_unix: int,
    ) -> Mapping[str, Any]:
        self.calls += 1
        return recovery.build_probe_receipt(
            release_revision=release_revision,
            scratch_instance=str(scratch["instance"]),
            transaction_read_only=True,
            schema_sha256="1" * 64,
            content_sha256="2" * 64,
            canonical_event_row_count=14_073,
            probed_at_unix=now_unix,
            scratch_private_ip=str(scratch["private_ip"]),
            server_ca_sha256=str(scratch["server_ca_sha256"]),
        )


class MalformedProbe(Probe):
    def probe(
        self,
        *,
        release_revision: str,
        scratch: Mapping[str, Any],
        now_unix: int,
    ) -> Mapping[str, Any]:
        value = dict(super().probe(
            release_revision=release_revision,
            scratch=scratch,
            now_unix=now_unix,
        ))
        value["content_sha256"] = "not-a-digest"
        unsigned = {
            name: item for name, item in value.items() if name != "receipt_sha256"
        }
        value["receipt_sha256"] = cutover._sha256_json(unsigned)
        return value


class Provider:
    def __init__(
        self,
        journal_root: Path,
        *,
        fail_restore_once: bool = False,
    ) -> None:
        self.journal_root = journal_root
        self.fail_restore_once = fail_restore_once
        self.restore_failed = False
        self.calls: list[str] = []
        self.backup_exists = False
        self.scratch_exists = False
        self.restored = False

    @staticmethod
    def _scratch_projection(release_revision: str) -> dict[str, Any]:
        scratch = cutover.database_recovery_scratch_instance(release_revision)
        return {
            "project": cutover.PROJECT,
            "instance": scratch,
            "region": cutover.PRODUCTION_SQL_REGION,
            "private_network": cutover.DATABASE_RECOVERY_SCRATCH_NETWORK,
            "database_version": "POSTGRES_18",
            "configuration_sha256": "6" * 64,
            "readback_sha256": "7" * 64,
            "create_operation_id": "create-operation",
            "private_ip": "10.0.0.2",
            "server_ca_pem": CA_PEM,
            "server_ca_sha256": recovery._sha(CA_PEM.encode("ascii")),
            "ssl_mode": "ENCRYPTED_ONLY",
            "server_ca_mode": "GOOGLE_MANAGED_INTERNAL_CA",
            "connection_name": (
                f"{cutover.PROJECT}:{cutover.PRODUCTION_SQL_REGION}:{scratch}"
            ),
        }

    def source_readback(self) -> Mapping[str, Any]:
        self.calls.append("source_readback")
        return {
            "project": cutover.PROJECT,
            "instance": cutover.PRODUCTION_SQL_INSTANCE,
            "region": cutover.PRODUCTION_SQL_REGION,
            "database": cutover.DATABASE,
            "private_network": (
                f"projects/{cutover.PROJECT}/global/networks/production-vpc"
            ),
            "database_version": "POSTGRES_18",
            "configuration_sha256": "3" * 64,
            "readback_sha256": "4" * 64,
        }

    def ensure_backup(
        self, *, release_revision: str, not_before_unix: int
    ) -> Mapping[str, Any]:
        assert release_revision == REVISION
        self.calls.append("ensure_backup")
        self.backup_exists = True
        return {
            "backup_id": "123456789",
            "operation_id": "backup-operation",
            "status": "SUCCESSFUL",
            "type": "ON_DEMAND",
            "source_instance": cutover.PRODUCTION_SQL_INSTANCE,
            "completed_at_unix": not_before_unix + 1,
            "retained": True,
            "readback_sha256": "5" * 64,
        }

    def ensure_scratch(
        self, *, release_revision: str, source: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        assert source["instance"] == cutover.PRODUCTION_SQL_INSTANCE
        self.calls.append("ensure_scratch")
        self.scratch_exists = True
        return self._scratch_projection(release_revision)

    def ensure_restore(
        self,
        *,
        release_revision: str,
        backup: Mapping[str, Any],
        scratch: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert self.scratch_exists
        self.calls.append("ensure_restore")
        self.restored = True
        if self.fail_restore_once and not self.restore_failed:
            self.restore_failed = True
            raise recovery.ProductionDatabaseRecoveryError(
                "injected_restore_response_loss"
            )
        return {
            **dict(scratch),
            "instance": cutover.database_recovery_scratch_instance(
                release_revision
            ),
            "restore_operation_id": "restore-operation",
            "restored_backup_id": backup["backup_id"],
        }

    def scratch_readback(self, *, release_revision: str) -> Mapping[str, Any]:
        assert self.restored
        self.calls.append("scratch_readback")
        result = self._scratch_projection(release_revision)
        result.pop("create_operation_id")
        return result

    def delete_scratch(
        self, *, release_revision: str
    ) -> Mapping[str, Any]:
        assert self.restored
        assert (self.journal_root / "0008-probe_receipt.json").is_file()
        self.calls.append("delete_scratch")
        self.scratch_exists = False
        return {
            "instance": cutover.database_recovery_scratch_instance(
                release_revision
            ),
            "delete_operation_id": "delete-operation",
            "deleted": True,
        }

    def backup_readback(self, *, backup_id: str) -> Mapping[str, Any]:
        assert self.backup_exists
        assert not self.scratch_exists
        self.calls.append("backup_readback")
        return {
            "backup_id": backup_id,
            "status": "SUCCESSFUL",
            "type": "ON_DEMAND",
            "source_instance": cutover.PRODUCTION_SQL_INSTANCE,
            "readback_sha256": "8" * 64,
        }


def test_run_for_owner_release_binds_transport_and_owner_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configuration = object()
    executable = object()
    transport = object()
    probe = Mock()
    secret_accessor = object()
    client = object()
    provider = object()

    class OwnerIdentity:
        gcloud_configuration = configuration

        def __init__(self) -> None:
            self.bind_approved_subject = Mock()

    identity = OwnerIdentity()
    executable_factory = Mock(return_value=executable)
    transport_factory = Mock(return_value=transport)
    probe_factory = Mock(return_value=probe)
    secret_factory = Mock(return_value=secret_accessor)
    client_factory = Mock(return_value=client)
    provider_factory = Mock(return_value=provider)
    execute_gate = Mock(return_value={"ok": True})
    monkeypatch.setattr(
        recovery.owner_transport,
        "TrustedGcloudExecutable",
        executable_factory,
    )
    monkeypatch.setattr(
        recovery,
        "FixedDatabaseRecoveryIapTransport",
        transport_factory,
    )
    monkeypatch.setattr(recovery, "FixedPrivateReadOnlyProbe", probe_factory)
    monkeypatch.setattr(recovery, "FixedSecretManagerAccess", secret_factory)
    monkeypatch.setattr(recovery.owner_transport, "GoogleRestClient", client_factory)
    monkeypatch.setattr(recovery, "CloudSqlRecoveryProvider", provider_factory)
    monkeypatch.setattr(recovery, "_journal_root", lambda _revision: tmp_path)
    monkeypatch.setattr(recovery, "_execute_gate", execute_gate)

    result = recovery.run_for_owner(REVISION, identity, "f" * 64)

    assert result == {"ok": True}
    executable_factory.assert_called_once_with(release_sha=REVISION)
    transport_factory.assert_called_once_with(
        identity,
        gcloud_executable=executable,
        gcloud_configuration=configuration,
    )
    probe_factory.assert_called_once_with(
        REVISION,
        transport=transport,
        secret_accessor=secret_accessor,
    )
    probe.require_available.assert_called_once_with()
    identity.bind_approved_subject.assert_called_once_with("f" * 64)
    execute_gate.assert_called_once_with(
        release_revision=REVISION,
        provider=provider,
        probe=probe,
        journal_root=tmp_path,
    )


def test_fixed_gate_restores_probes_deletes_only_scratch_and_retains_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    probe = Probe()

    receipt = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=probe,
        journal_root=root,
        clock=Clock(),
    )

    assert provider.calls == [
        "source_readback",
        "ensure_backup",
        "ensure_scratch",
        "ensure_restore",
        "scratch_readback",
        "delete_scratch",
        "backup_readback",
    ]
    assert provider.backup_exists is True
    assert provider.scratch_exists is False
    assert receipt["source"] == {
        "project": cutover.PROJECT,
        "instance": cutover.PRODUCTION_SQL_INSTANCE,
        "region": cutover.PRODUCTION_SQL_REGION,
        "database": cutover.DATABASE,
        "private_network": (
            f"projects/{cutover.PROJECT}/global/networks/production-vpc"
        ),
        "database_version": "POSTGRES_18",
        "configuration_sha256": "3" * 64,
        "readback_sha256": "4" * 64,
    }
    assert receipt["backup"]["backup_id"] == "123456789"
    assert receipt["backup"]["operation_id"] == "backup-operation"
    assert receipt["backup"]["retained"] is True
    assert receipt["scratch"]["network"] == (
        "projects/adventico-ai-platform/global/networks/muncho-canary-vpc"
    )
    assert receipt["scratch"]["private_only"] is True
    assert receipt["scratch"]["backup_enabled"] is False
    assert receipt["scratch"]["deletion_protection_enabled"] is False
    assert receipt["scratch"]["deleted"] is True
    assert receipt["probe_receipt"]["transaction_read_only"] is True
    assert [path.name for path in sorted(root.glob("*.json"))] == [
        f"{index:04d}-{stage}.json"
        for index, stage in enumerate(recovery._STAGES)
    ]


def test_resume_reconciles_restore_without_duplicate_backup_or_scratch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root, fail_restore_once=True)
    clock = Clock()
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="injected_restore_response_loss",
    ):
        recovery._execute_gate(
            release_revision=REVISION,
            provider=provider,
            probe=Probe(),
            journal_root=root,
            clock=clock,
        )

    receipt = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=Probe(),
        journal_root=root,
        clock=clock,
    )

    assert provider.calls.count("ensure_backup") == 1
    assert provider.calls.count("ensure_scratch") == 1
    assert provider.calls.count("ensure_restore") == 2
    assert receipt["scratch"]["deleted"] is True


def test_unavailable_fixed_probe_fails_before_cloud_or_journal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="injected_probe_transport_unavailable",
    ):
        class UnavailableProbe:
            def require_available(self) -> None:
                raise recovery.ProductionDatabaseRecoveryError(
                    "injected_probe_transport_unavailable"
                )

        recovery._execute_gate(
            release_revision=REVISION,
            provider=provider,
            probe=UnavailableProbe(),  # type: ignore[arg-type]
            journal_root=root,
            clock=Clock(),
        )

    assert provider.calls == []
    assert not root.exists()


def test_source_network_or_configuration_drift_fails_before_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    original = provider.source_readback

    def drifted() -> Mapping[str, Any]:
        value = dict(original())
        value["private_network"] = "caller-controlled-network"
        return value

    provider.source_readback = drifted  # type: ignore[method-assign]
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_source_configuration_invalid",
    ):
        recovery._execute_gate(
            release_revision=REVISION,
            provider=provider,
            probe=Probe(),
            journal_root=root,
            clock=Clock(),
        )
    assert "ensure_backup" not in provider.calls


def test_receipt_tamper_and_stale_backup_recheck_fail_closed(
    tmp_path: Path,
) -> None:
    receipt = recovery._execute_gate(
        release_revision=REVISION,
        provider=Provider(tmp_path / "journal"),
        probe=Probe(),
        journal_root=tmp_path / "journal",
        clock=Clock(),
    )
    changed = copy.deepcopy(receipt)
    changed["scratch"]["network"] = "projects/p/global/networks/public"
    unsigned = {
        name: item for name, item in changed.items() if name != "receipt_sha256"
    }
    changed["receipt_sha256"] = cutover._sha256_json(unsigned)
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_receipt_invalid",
    ):
        recovery.validate_receipt_for_freeze(
            changed,
            release_revision=REVISION,
            now_unix=receipt["backup_rechecked_at_unix"],
        )

    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_receipt_invalid",
    ):
        recovery.validate_receipt_for_freeze(
            receipt,
            release_revision=REVISION,
            now_unix=(
                receipt["backup_rechecked_at_unix"]
                + recovery.MAX_BACKUP_RECHECK_AGE_SECONDS
                + 1
            ),
        )


def test_completed_journal_refreshes_backup_existence_before_freeze(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    first = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=Probe(),
        journal_root=root,
        clock=Clock(),
    )
    provider.calls.clear()
    refreshed = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=Probe(),
        journal_root=root,
        clock=Clock(
            first["backup_rechecked_at_unix"]
            + recovery.MAX_BACKUP_RECHECK_AGE_SECONDS
            + 1
        ),
    )

    assert provider.calls == ["backup_readback"]
    assert refreshed["receipt_sha256"] != first["receipt_sha256"]
    assert refreshed["backup_rechecked_at_unix"] > first[
        "backup_rechecked_at_unix"
    ]
    assert (root / "0013-backup_rechecked_refresh_1.json").is_file()
    assert (root / "0014-terminal_receipt_refresh_1.json").is_file()


def test_partial_terminal_refresh_resumes_without_second_backup_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    first = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=Probe(),
        journal_root=root,
        clock=Clock(),
    )
    provider.calls.clear()
    refreshed_at = (
        first["backup_rechecked_at_unix"]
        + recovery.MAX_BACKUP_RECHECK_AGE_SECONDS
        + 1
    )
    journal = recovery._Journal(root, REVISION, now_unix=refreshed_at)
    journal.record(
        "backup_rechecked_refresh_1",
        {
            "backup_id": first["backup"]["backup_id"],
            "status": "SUCCESSFUL",
            "type": "ON_DEMAND",
            "source_instance": cutover.PRODUCTION_SQL_INSTANCE,
            "readback_sha256": "9" * 64,
            "rechecked_at_unix": refreshed_at,
        },
        now_unix=refreshed_at,
    )

    refreshed = recovery._execute_gate(
        release_revision=REVISION,
        provider=provider,
        probe=Probe(),
        journal_root=root,
        clock=Clock(refreshed_at + 1),
    )

    assert provider.calls == []
    assert refreshed["backup_rechecked_at_unix"] == refreshed_at
    assert (root / "0014-terminal_receipt_refresh_1.json").is_file()


def test_malformed_probe_cannot_authorize_or_poison_scratch_delete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    provider = Provider(root)
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_probe_invalid",
    ):
        recovery._execute_gate(
            release_revision=REVISION,
            provider=provider,
            probe=MalformedProbe(),
            journal_root=root,
            clock=Clock(),
        )

    assert provider.scratch_exists is True
    assert "delete_scratch" not in provider.calls
    assert not (root / "0008-probe_receipt.json").exists()


def test_operation_lookup_is_source_scoped_and_preserves_pagination() -> None:
    first = {
        "kind": "sql#operation",
        "name": "backup-operation-1",
        "targetProject": cutover.PROJECT,
        "targetId": cutover.PRODUCTION_SQL_INSTANCE,
        "operationType": "BACKUP_VOLUME",
        "insertTime": "2026-07-25T00:00:00Z",
        "backupContext": {"backupId": "123456789"},
    }
    second = {
        **first,
        "name": "backup-operation-2",
        "insertTime": "2026-07-25T00:01:00Z",
    }
    client = Mock()
    client.request_json.side_effect = [
        {
            "items": [first],
            "nextPageToken": "source-page-2",
        },
        {"items": [second]},
    ]
    provider = recovery.CloudSqlRecoveryProvider(client)

    operation = provider._find_operation(
        target=cutover.PRODUCTION_SQL_INSTANCE,
        allowed_types=frozenset({"BACKUP_VOLUME"}),
        backup_id="123456789",
    )

    assert operation == second
    base = (
        "https://sqladmin.googleapis.com/sql/v1beta4/projects/"
        f"{cutover.PROJECT}/operations"
    )
    client.request_json.assert_has_calls([
        call(
            "GET",
            f"{base}?maxResults=500&instance={cutover.PRODUCTION_SQL_INSTANCE}",
            body=None,
        ),
        call(
            "GET",
            (
                f"{base}?maxResults=500"
                f"&instance={cutover.PRODUCTION_SQL_INSTANCE}"
                "&pageToken=source-page-2"
            ),
            body=None,
        ),
    ])
    assert client.request_json.call_count == 2


def test_operation_lookup_is_release_bound_scratch_scoped() -> None:
    scratch = cutover.database_recovery_scratch_instance(REVISION)
    expected = {
        "kind": "sql#operation",
        "name": "scratch-create-operation",
        "targetProject": cutover.PROJECT,
        "targetId": scratch,
        "operationType": "CREATE",
        "insertTime": "2026-07-25T00:00:00Z",
    }
    client = Mock()
    client.request_json.return_value = {"items": [expected]}
    provider = recovery.CloudSqlRecoveryProvider(client)

    operation = provider._find_operation(
        target=scratch,
        allowed_types=frozenset({"CREATE"}),
        release_revision=REVISION,
    )

    assert operation == expected
    client.request_json.assert_called_once_with(
        "GET",
        (
            "https://sqladmin.googleapis.com/sql/v1beta4/projects/"
            f"{cutover.PROJECT}/operations?maxResults=500&instance={scratch}"
        ),
        body=None,
    )


def test_operation_lookup_rejects_project_wide_and_unbound_targets() -> None:
    client = Mock()
    provider = recovery.CloudSqlRecoveryProvider(client)
    scratch = cutover.database_recovery_scratch_instance(REVISION)

    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_provider_contract_invalid",
    ):
        provider._pages("/operations", item_kind="sql#operation")
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_provider_contract_invalid",
    ):
        provider._find_operation(
            target="caller-selected-instance",
            allowed_types=frozenset({"CREATE"}),
            release_revision=REVISION,
        )
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_provider_contract_invalid",
    ):
        provider._find_operation(
            target=scratch,
            allowed_types=frozenset({"CREATE"}),
        )

    client.request_json.assert_not_called()


def test_cloud_sql_mutation_contract_is_fixed_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = recovery.CloudSqlRecoveryProvider(object())  # type: ignore[arg-type]
    source = Provider(Path("/unused")).source_readback()
    body = provider._scratch_body(source, REVISION)
    scratch_name = cutover.database_recovery_scratch_instance(REVISION)
    assert body["name"] == scratch_name
    assert body["region"] == cutover.PRODUCTION_SQL_REGION
    assert body["settings"]["backupConfiguration"]["enabled"] is False
    assert body["settings"]["deletionProtectionEnabled"] is False
    assert body["settings"]["ipConfiguration"] == {
        "ipv4Enabled": False,
        "privateNetwork": cutover.DATABASE_RECOVERY_SCRATCH_NETWORK,
        "authorizedNetworks": [],
        "sslMode": "ENCRYPTED_ONLY",
        "serverCaMode": "GOOGLE_MANAGED_INTERNAL_CA",
    }

    raw_instance = {
        "kind": "sql#instance",
        "project": cutover.PROJECT,
        "name": scratch_name,
        "region": cutover.PRODUCTION_SQL_REGION,
        "state": "RUNNABLE",
        "databaseVersion": "POSTGRES_18",
        "settings": {
            "settingsVersion": "1",
            "tier": "db-custom-1-3840",
            "backupConfiguration": {
                "enabled": False,
                "pointInTimeRecoveryEnabled": False,
            },
            "deletionProtectionEnabled": False,
            "ipConfiguration": {
                "ipv4Enabled": False,
                "privateNetwork": cutover.DATABASE_RECOVERY_SCRATCH_NETWORK,
                "authorizedNetworks": [],
                "sslMode": "ENCRYPTED_ONLY",
                "serverCaMode": "GOOGLE_MANAGED_INTERNAL_CA",
            },
        },
        "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.0.0.2"}],
        "connectionName": (
            f"{cutover.PROJECT}:{cutover.PRODUCTION_SQL_REGION}:{scratch_name}"
        ),
        "serverCaCert": {
            "kind": "sql#sslCert",
            "instance": scratch_name,
            "cert": CA_PEM,
        },
    }
    requests: list[tuple[str, str, Mapping[str, Any] | None, Mapping[str, Any] | None]] = []

    def request(
        method: str,
        suffix: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        requests.append((method, suffix, body, query))
        return {"name": "restore-operation"}

    def wait(
        operation_id: str,
        *,
        target: str,
        allowed_types: frozenset[str],
    ) -> Mapping[str, Any]:
        assert operation_id == "restore-operation"
        assert target == scratch_name
        assert allowed_types == frozenset({"RESTORE_VOLUME"})
        return {"name": operation_id}

    monkeypatch.setattr(provider, "_find_operation", lambda **_kwargs: None)
    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(provider, "_wait_operation", wait)
    monkeypatch.setattr(provider, "_instances", lambda: [raw_instance])
    monkeypatch.setattr(
        provider,
        "_databases",
        lambda _instance: [{"name": cutover.DATABASE}],
    )
    restored = provider.ensure_restore(
        release_revision=REVISION,
        backup={"backup_id": "123456789"},
        scratch={"instance": scratch_name, "create_operation_id": "create-operation"},
    )
    assert restored["restored_backup_id"] == "123456789"
    assert requests == [(
        "POST",
        f"/instances/{scratch_name}/restoreBackup",
        {
            "restoreBackupContext": {
                "kind": "sql#restoreBackupContext",
                "backupRunId": "123456789",
                "instanceId": cutover.PRODUCTION_SQL_INSTANCE,
                "project": cutover.PROJECT,
            }
        },
        None,
    )]

    delete_provider = recovery.CloudSqlRecoveryProvider(  # type: ignore[arg-type]
        object()
    )
    instance_snapshots = iter(([raw_instance], []))
    delete_requests: list[
        tuple[str, str, Mapping[str, Any] | None, Mapping[str, Any] | None]
    ] = []

    def delete_request(
        method: str,
        suffix: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        delete_requests.append((method, suffix, body, query))
        return {"name": "delete-operation"}

    def wait_delete(
        operation_id: str,
        *,
        target: str,
        allowed_types: frozenset[str],
    ) -> Mapping[str, Any]:
        assert operation_id == "delete-operation"
        assert target == scratch_name
        assert allowed_types == frozenset({"DELETE"})
        return {"name": operation_id}

    monkeypatch.setattr(
        delete_provider,
        "_instances",
        lambda: list(next(instance_snapshots)),
    )
    monkeypatch.setattr(
        delete_provider,
        "_find_operation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(delete_provider, "_request", delete_request)
    monkeypatch.setattr(delete_provider, "_wait_operation", wait_delete)
    deleted = delete_provider.delete_scratch(release_revision=REVISION)
    assert deleted["deleted"] is True
    assert delete_requests == [(
        "DELETE",
        f"/instances/{scratch_name}",
        None,
        {"enableFinalBackup": "false"},
    )]

def test_probe_contract_rejects_write_capable_result() -> None:
    with pytest.raises(
        recovery.ProductionDatabaseRecoveryError,
        match="production_database_recovery_probe_invalid",
    ):
        recovery.build_probe_receipt(
            release_revision=REVISION,
            scratch_instance=cutover.database_recovery_scratch_instance(
                REVISION
            ),
            transaction_read_only=False,
            schema_sha256="1" * 64,
            content_sha256="2" * 64,
            canonical_event_row_count=14_073,
            probed_at_unix=NOW,
        )
