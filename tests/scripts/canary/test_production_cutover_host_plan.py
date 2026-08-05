from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path

import pytest

from gateway.canonical_boot_identity import SYSTEMD_BOOT_ID_CREDENTIAL_DIRECTIVE
from gateway.mac_ops_edge_service import DEFAULT_PROJECT_ID
from ops.muncho.runtime import upstream_sync_job_rail as dual_sync_rail
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_host_plan as producer
from tests.scripts.canary.test_package_production_cutover_artifacts import (
    REVISION,
    _release,
    _unit_inputs,
)


def test_effective_identity_fails_closed_without_posix_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(producer.os, "geteuid")
    monkeypatch.delattr(producer.os, "getegid")

    assert producer._effective_identity() is None


def test_every_fixed_target_has_exactly_one_producer_source() -> None:
    partitions = producer.HOST_ARTIFACT_SOURCE_PARTITIONS

    assert set().union(*partitions) == set(package.HOST_ARTIFACT_TARGETS)
    assert sum(len(group) for group in partitions) == len(
        package.HOST_ARTIFACT_TARGETS
    )
    assert producer.ROOT_VERIFIER_ARTIFACT_NAMES == {
        "api_bearer_verifier",
        "api_approval_verifier",
    }
    assert producer.REVIEWED_RELEASE_ARTIFACT_NAMES == {
        "gateway_connector_drop_in"
    }
    assert {
        "dual_upstream_sync_service_unit",
        "dual_upstream_sync_timer_unit",
        "dual_upstream_sync_report_service_unit",
        "dual_upstream_sync_report_timer_unit",
    }.issubset(producer.OWNER_RUNTIME_ARTIFACT_NAMES)


def test_dual_sync_owner_runtime_producer_returns_all_four_exact_payloads() -> None:
    artifacts = {
        dual_sync_rail.SYNC_SERVICE_UNIT: b"sync-service",
        dual_sync_rail.SYNC_TIMER_UNIT: b"sync-timer",
        dual_sync_rail.REPORT_SERVICE_UNIT: b"report-service",
        dual_sync_rail.REPORT_TIMER_UNIT: b"report-timer",
    }
    rendered = dual_sync_rail.RailPackage(
        revision=REVISION,
        release_root=Path("/release"),
        sender_revision=REVISION,
        sender_release_root=Path("/release"),
        source_digests={},
        host_binary_digests={},
        artifacts=artifacts,
        manifest_bytes=b"manifest\n",
        manifest_sha256="a" * 64,
    )
    calls: list[tuple[str, str]] = []

    result = producer._dual_sync_host_payloads(
        REVISION,
        package_builder=lambda release, sender: (
            calls.append((release, sender)) or rendered
        ),
    )

    assert calls == [(REVISION, REVISION)]
    assert result == {
        "dual_upstream_sync_service_unit": b"sync-service",
        "dual_upstream_sync_timer_unit": b"sync-timer",
        "dual_upstream_sync_report_service_unit": b"report-service",
        "dual_upstream_sync_report_timer_unit": b"report-timer",
    }


def test_dual_sync_owner_runtime_producer_rejects_wrong_sender() -> None:
    wrong = dual_sync_rail.RailPackage(
        revision=REVISION,
        release_root=Path("/release"),
        sender_revision="b" * 40,
        sender_release_root=Path("/sender"),
        source_digests={},
        host_binary_digests={},
        artifacts={
            dual_sync_rail.SYNC_SERVICE_UNIT: b"1",
            dual_sync_rail.SYNC_TIMER_UNIT: b"2",
            dual_sync_rail.REPORT_SERVICE_UNIT: b"3",
            dual_sync_rail.REPORT_TIMER_UNIT: b"4",
        },
        manifest_bytes=b"manifest\n",
        manifest_sha256="a" * 64,
    )

    with pytest.raises(
        producer.HostPlanProducerError,
        match="host_plan_dual_sync_render_failed",
    ):
        producer._dual_sync_host_payloads(
            REVISION,
            package_builder=lambda _release, _sender: wrong,
        )


def test_every_fixed_target_has_an_install_identity() -> None:
    inputs = _unit_inputs()
    pre = {"state": "absent", "uid": None, "gid": None, "mode": None}

    identities = {
        name: producer._target_file_identity(
            name,
            inputs=inputs,
            pre=pre,
        )
        for name in package.HOST_ARTIFACT_TARGETS
    }

    assert set(identities) == set(package.HOST_ARTIFACT_TARGETS)
    assert all(
        type(uid) is int
        and type(gid) is int
        and type(mode) is int
        and uid >= 0
        and gid >= 0
        and 0 < mode <= 0o777
        for uid, gid, mode in identities.values()
    )
    assert {
        identity
        for name, identity in identities.items()
        if name.startswith("operational_edge_unit_")
    } == {(0, 0, 0o644)}


def test_release_sealed_payloads_reproduce_the_manifest(tmp_path: Path) -> None:
    release = _release(tmp_path)
    inputs = _unit_inputs()
    manifest = package.build_release_artifacts(
        release,
        REVISION,
        unit_inputs=inputs,
    )

    payloads, descriptor, observed_manifest = (
        package.render_release_sealed_host_payloads(
            release_root=release,
            revision=REVISION,
            unit_inputs=inputs,
        )
    )

    assert set(payloads) == producer.RELEASE_SEALED_ARTIFACT_NAMES
    assert descriptor == manifest["sealed_runtime_artifact_request"]
    assert observed_manifest == manifest


def test_stage_uses_runtime_pinned_mac_ops_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MacOpsConfigObserved(RuntimeError):
        pass

    inputs = _unit_inputs()
    assert inputs["target"]["project"] != DEFAULT_PROJECT_ID
    monkeypatch.setattr(
        producer,
        "_validate_reconciliation_intent",
        lambda *_args, **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        producer.package,
        "render_release_sealed_host_payloads",
        lambda **_kwargs: (
            {"mac_ops_unit": b"mac-ops-unit"},
            {},
            {"host_artifact_contract": {}},
        ),
    )
    monkeypatch.setattr(
        producer,
        "_read_regular",
        lambda *_args, **_kwargs: (b"source", {}),
    )
    monkeypatch.setattr(
        producer,
        "_render_connector_unit",
        lambda *_args, **_kwargs: b"connector-unit",
    )
    monkeypatch.setattr(
        producer,
        "_render_connector_config",
        lambda *_args, **_kwargs: b"connector-config",
    )
    monkeypatch.setattr(
        producer,
        "render_production_routeback_config",
        lambda **_kwargs: b"routeback-config",
    )

    def observe_mac_ops_config(**kwargs: object) -> bytes:
        assert kwargs["project_id"] == DEFAULT_PROJECT_ID
        raise MacOpsConfigObserved

    monkeypatch.setattr(
        producer,
        "render_production_mac_ops_config",
        observe_mac_ops_config,
    )

    entered: list[str] = []

    @contextlib.contextmanager
    def lock():
        entered.append("entered")
        try:
            yield
        finally:
            entered.append("exited")

    with pytest.raises(MacOpsConfigObserved):
        producer.stage_fixed_host_artifacts(
            REVISION,
            release_root=tmp_path,
            filesystem_root=tmp_path,
            unit_inputs=inputs,
            require_root=False,
            lock_factory=lock,
        )
    assert entered == ["entered", "exited"]


def test_staging_receipt_validates_projected_secret_foundation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _unit_inputs()
    foundation_sha256 = inputs["operational_edge_key_foundation_sha256"]
    secret_foundation = {
        "schema": producer.production_secret_stager.STAGING_SCHEMA,
        "bearer_verifier_path": str(
            producer.production_secret_stager.STAGED_API_BEARER_VERIFIER_PATH
        ),
        "bearer_verifier_sha256": "a" * 64,
        "approval_verifier_path": str(
            producer.production_secret_stager.STAGED_API_APPROVAL_VERIFIER_PATH
        ),
        "approval_verifier_sha256": "b" * 64,
        "writer_private_path": str(
            producer.production_secret_stager.STAGED_WRITER_PRIVATE_KEY_PATH
        ),
        "writer_public_key_id": inputs["writer_capability_public_key_id"],
        "edge_private_path": str(
            producer.production_secret_stager.STAGED_EDGE_PRIVATE_KEY_PATH
        ),
        "edge_public_key_id": inputs["discord_edge_receipt_public_key_id"],
        "operational_edge_key_foundation": {
            "receipt_sha256": foundation_sha256,
        },
        "operational_edge_key_foundation_sha256": foundation_sha256,
        "operational_edge_receipt_public_key_ids": inputs[
            "operational_edge_receipt_public_key_ids"
        ],
        "private_content_or_digest_recorded": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    staged = {
        name: {"artifact": name}
        for name in package.HOST_ARTIFACT_TARGETS
    }
    unsigned = {
        "schema": producer.STAGING_SCHEMA,
        "release_revision": REVISION,
        "release_manifest_sha256": "c" * 64,
        "host_artifact_contract_sha256": "d" * 64,
        "unit_inputs_authority_plan_sha256": inputs[
            "authority_plan_sha256"
        ],
        "unit_inputs_authority_approval_sha256": inputs[
            "authority_approval_sha256"
        ],
        "source_gateway_config_sha256": "e" * 64,
        "source_writer_config_sha256": "f" * 64,
        "secret_foundation": secret_foundation,
        "capability_topology": {},
        "staged_file_count": len(staged),
        "staged_files": staged,
        "staged_set_sha256": producer._sha(
            producer._canonical({"files": staged})
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": producer._sha(producer._canonical(unsigned)),
    }
    monkeypatch.setattr(
        producer,
        "validate_operational_edge_key_foundation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        producer,
        "validate_production_capability_topology",
        lambda value: value,
    )

    assert producer._validate_staging_receipt(
        receipt,
        revision=REVISION,
        inputs=inputs,
    ) == receipt

    tampered = copy.deepcopy(receipt)
    tampered["secret_foundation"]["secret_material_recorded"] = True
    tampered_unsigned = {
        name: item
        for name, item in tampered.items()
        if name != "receipt_sha256"
    }
    tampered["receipt_sha256"] = producer._sha(
        producer._canonical(tampered_unsigned)
    )
    with pytest.raises(
        producer.HostPlanProducerError,
        match="host_plan_secret_foundation_invalid",
    ):
        producer._validate_staging_receipt(
            tampered,
            revision=REVISION,
            inputs=inputs,
        )


def test_create_only_staging_resumes_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    logical = Path("/staged/exact.json")
    staged = tmp_path / "staged"
    staged.mkdir(mode=0o700)
    payload = b'{"safe":true}'
    uid = os.geteuid()
    gid = os.getegid()

    producer._create_or_validate(
        logical,
        payload,
        filesystem_root=tmp_path,
        uid=uid,
        gid=gid,
    )
    producer._create_or_validate(
        logical,
        payload,
        filesystem_root=tmp_path,
        uid=uid,
        gid=gid,
    )

    assert (staged / "exact.json").read_bytes() == payload
    assert (staged / "exact.json").stat().st_mode & 0o777 == 0o400
    with pytest.raises(
        producer.HostPlanProducerError,
        match="host_plan_staging_conflict",
    ):
        producer._create_or_validate(
            logical,
            b'{"safe":false}',
            filesystem_root=tmp_path,
            uid=uid,
            gid=gid,
        )


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_create_only_staging_rejects_link_substitution(
    tmp_path: Path,
    kind: str,
) -> None:
    staged = tmp_path / "staged"
    staged.mkdir(mode=0o700)
    source = staged / "source"
    source.write_bytes(b"fixed")
    source.chmod(0o400)
    target = staged / "target"
    if kind == "symlink":
        target.symlink_to(source)
    else:
        os.link(source, target)

    with pytest.raises(
        producer.HostPlanProducerError,
        match="host_plan_file_identity_invalid",
    ):
        producer._create_or_validate(
            Path("/staged/target"),
            b"fixed",
            filesystem_root=tmp_path,
            uid=os.geteuid(),
            gid=os.getegid(),
        )


def test_signed_reconciliation_intent_binds_exact_reviewed_policies() -> None:
    inputs = copy.deepcopy(_unit_inputs())
    legacy = producer._legacy_discord_policy()
    target = producer._target_discord_policy()
    inputs["discord_reconciliation_intent"].update(
        {
            "legacy_public_policy_sha256": producer._sha(
                producer._canonical(legacy)
            ),
            "target_public_policy_sha256": producer._sha(
                producer._canonical(target)
            ),
        }
    )

    assert producer._validate_reconciliation_intent(
        inputs, revision=REVISION
    ) == (legacy, target)

    inputs["discord_reconciliation_intent"][
        "target_public_policy_sha256"
    ] = "f" * 64
    with pytest.raises(
        producer.HostPlanProducerError,
        match="host_plan_reconciliation_intent_mismatch",
    ):
        producer._validate_reconciliation_intent(inputs, revision=REVISION)


def test_connector_renderer_projects_the_complete_target_policy() -> None:
    inputs = _unit_inputs()
    target = producer._target_discord_policy()
    template = (
        Path("ops/muncho/systemd/discord-public-connector.json.in")
        .resolve()
        .read_bytes()
    )

    rendered = producer._render_connector_config(
        template,
        inputs=inputs,
        target_policy=target,
    )
    value = json.loads(rendered)

    assert all(value["discord"][name] == item for name, item in target.items())
    assert "opaque" not in rendered.decode("utf-8")


def test_writer_unit_invokes_release_bound_production_readiness() -> None:
    rendered = producer._render_writer_unit(
        revision=REVISION,
        inputs=_unit_inputs(),
    ).decode("utf-8")
    receipt = (
        "/var/lib/muncho/canonical-writer-phase-b/runtime-receipt.json"
    )

    assert (
        f"--production-release-revision {REVISION} "
        f"--production-phase-b-receipt {receipt}"
    ) in rendered
    assert f"AssertPathExists={receipt}\n" in rendered
    assert f"ReadOnlyPaths={receipt}\n" in rendered
    assert rendered.count(f"{SYSTEMD_BOOT_ID_CREDENTIAL_DIRECTIVE}\n") == 1
    assert rendered.count("LoadCredential=") == 1
    assert (
        "Requires=muncho-canonical-writer-phase-b-readiness.service\n"
        in rendered
    )


def test_derived_validation_sees_revision_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.canary import (
        production_cutover_owner_launcher as owner_launcher,
    )

    inputs = _unit_inputs()
    rows = {
        name: {
            "staged_path": f"/staged/{name}",
            "target_path": target,
            "sha256": f"{index:064x}",
        }
        for index, (name, (target, _binding)) in enumerate(
            package.HOST_ARTIFACT_TARGETS.items(),
            start=1,
        )
    }
    initial = {"cron_continuity_plan": {"cutover_executable": True}}
    manifest = {
        "manifest_sha256": "a" * 64,
        "host_artifact_contract": {"contract_sha256": "b" * 64},
    }
    source = b"source"
    staging = {
        "release_manifest_sha256": manifest["manifest_sha256"],
        "host_artifact_contract_sha256": manifest[
            "host_artifact_contract"
        ]["contract_sha256"],
        "source_gateway_config_sha256": producer._sha(source),
        "source_writer_config_sha256": producer._sha(source),
        "staged_files": rows,
        "capability_topology": {"topology": "fixed"},
        "secret_foundation": {
            "operational_edge_key_foundation": {"foundation": "fixed"},
            "operational_edge_key_foundation_sha256": "c" * 64,
            "operational_edge_receipt_public_key_ids": {"edge": "d" * 64},
        },
    }
    monkeypatch.setattr(
        owner_launcher,
        "validate_initial_collector_receipt",
        lambda *_args, **_kwargs: initial,
    )
    monkeypatch.setattr(
        producer.package,
        "verify_release_artifacts",
        lambda *_args, **_kwargs: manifest,
    )
    monkeypatch.setattr(
        producer,
        "_read_regular",
        lambda *_args, **_kwargs: (source, {}),
    )
    monkeypatch.setattr(producer, "_decode", lambda _raw: {})
    monkeypatch.setattr(
        producer,
        "_validate_staging_receipt",
        lambda *_args, **_kwargs: staging,
    )
    monkeypatch.setattr(
        producer,
        "_staged_rows",
        lambda **_kwargs: rows,
    )
    monkeypatch.setattr(
        producer,
        "_identity_foundation",
        lambda _inputs: {"identity": "fixed"},
    )
    monkeypatch.setattr(
        producer,
        "_discord_key_foundation",
        lambda **_kwargs: {"keys": "fixed"},
    )
    monkeypatch.setattr(
        producer.host_authority,
        "_target_pre_state",
        lambda *_args, **_kwargs: {
            "state": "absent",
            "uid": None,
            "gid": None,
            "mode": None,
        },
    )
    monkeypatch.setattr(
        producer,
        "_target_file_identity",
        lambda *_args, **_kwargs: (1, 2, 0o400),
    )
    monkeypatch.setattr(
        producer,
        "_metadata_only",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        producer,
        "_validate_reconciliation_intent",
        lambda *_args, **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        producer.cutover,
        "build_discord_policy_continuity",
        lambda **_kwargs: {"policy": "fixed"},
    )
    monkeypatch.setattr(
        producer,
        "_directory_prestate",
        lambda *_args, **_kwargs: {
            "state": "absent",
            "uid": None,
            "gid": None,
            "mode": None,
        },
    )
    captured: dict[str, object] = {}

    def validate(
        request: dict[str, object],
        *,
        initial: dict[str, object],
    ) -> None:
        captured["request"] = request
        captured["initial"] = initial
        assert request["release_revision"] == REVISION

    monkeypatch.setattr(
        producer.host_authority,
        "_validate_transition_and_plan",
        validate,
    )

    result = producer.collect_fixed_host_plan(
        REVISION,
        {},
        release_root=tmp_path,
        filesystem_root=tmp_path,
        unit_inputs=inputs,
        require_root=False,
    )

    assert captured["initial"] is initial
    assert set(captured["request"]) == {*result, "release_revision"}
    assert "release_revision" not in result
    assert set(result) == {
        "release_manifest_sha256",
        "gateway_target_identity",
        "writer_target_identity",
        "connector_target_identity",
        "host_transition",
        "capability_topology",
        "cron_continuity_plan",
    }
