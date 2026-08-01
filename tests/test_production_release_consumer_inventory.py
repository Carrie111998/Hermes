from __future__ import annotations

from dataclasses import replace

import pytest

from gateway import production_alias_projection_units as alias_units
from gateway.isolated_worker_units import (
    ISOLATED_WORKER_SERVICE_UNIT,
    ISOLATED_WORKER_SOCKET_UNIT,
)
from ops.muncho.runtime import trusted_cron_collector_rail as cron_rail
from scripts.canary import production_release_consumer_inventory as inventory


PREDECESSOR = "a" * 40
TARGET = "b" * 40


def _release_root(revision: str) -> str:
    return (
        f"/opt/adventico-ai-platform/hermes-agent-releases/hermes-agent-{revision[:12]}"
    )


def _properties(
    spec: inventory.ConsumerSpec,
    revision: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        name: "" for name in inventory.SYSTEMD_RELEASE_REF_PROPERTIES
    }
    result.update({
        "Names": spec.name,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "FragmentPath": spec.fragment_path,
        "DropInPaths": " ".join(spec.drop_in_paths),
        "NeedDaemonReload": "no",
        "MainPID": "0",
        "ControlPID": "0",
        "Triggers": " ".join(spec.triggers),
        "TriggeredBy": " ".join(spec.triggered_by),
        "ExecStart": (
            f"/usr/bin/python3 {_release_root(revision)}/entrypoint.py"
            if spec.executes_release
            else ""
        ),
    })
    return result


def _observation(
    spec: inventory.ConsumerSpec,
    revision: str,
) -> inventory.UnitObservation:
    service_type = (
        "notify"
        if spec.activation_class == inventory.ACTIVATION_CLASS_LONG_RUNNING
        else "oneshot"
    )
    files = {
        spec.fragment_path: (
            (
                f"[Service]\nType={service_type}\n"
                "ExecStart=/usr/bin/python3 "
                f"{_release_root(revision)}/entrypoint.py\n"
            ).encode()
            if spec.executes_release
            else b"[Unit]\nDescription=trigger\n"
        ),
        **{path: b"[Unit]\nAfter=network.target\n" for path in spec.drop_in_paths},
    }
    return inventory.UnitObservation(
        name=spec.name,
        properties=_properties(spec, revision),
        files=files,
    )


def _all_units(revision: str) -> list[inventory.UnitObservation]:
    return [
        _observation(spec, revision)
        for spec in inventory.expected_consumer_catalog().values()
    ]


def _process(
    *,
    pid: int,
    unit: str | None,
    path: str = "",
) -> inventory.ProcessObservation:
    fields: dict[str, object] = {
        name: "" for name in inventory.PROCESS_RELEASE_REF_FIELDS
    }
    fields["cmdline"] = f"/usr/bin/python3 {path}" if path else "/usr/bin/sleep"
    return inventory.ProcessObservation(pid=pid, unit=unit, fields=fields)


def _validate(
    units: list[inventory.UnitObservation],
    *,
    phase: inventory.InventoryPhase = inventory.InventoryPhase.PREDECESSOR_ACTIVE,
    processes: list[inventory.ProcessObservation] | None = None,
) -> inventory.InventoryValidationResult:
    return inventory.validate_release_consumer_inventory(
        unit_observations=units,
        process_observations=processes or [],
        phase=phase,
        predecessor_revision=PREDECESSOR,
        target_revision=TARGET,
    )


def test_catalog_is_exact_and_derived_from_existing_contracts() -> None:
    catalog = inventory.expected_consumer_catalog()

    assert len(catalog) == inventory.EXPECTED_UNIT_COUNT == 75
    assert (
        sum(spec.executes_release for spec in catalog.values())
        == inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        == 47
    )
    assert (
        sum(
            spec.activation_class == inventory.ACTIVATION_CLASS_LONG_RUNNING
            for spec in catalog.values()
        )
        == inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT
        == 18
    )
    assert (
        sum(
            spec.activation_class == inventory.ACTIVATION_CLASS_STARTUP_ONESHOT
            for spec in catalog.values()
        )
        == inventory.EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        == 1
    )
    assert (
        sum(
            spec.activation_class == inventory.ACTIVATION_CLASS_TRIGGERED_ONESHOT
            for spec in catalog.values()
        )
        == inventory.EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
        == 28
    )
    assert inventory.EXPECTED_ONESHOT_SERVICE_COUNT == 29
    assert (
        sum(spec.kind in {"socket", "timer"} for spec in catalog.values())
        == inventory.EXPECTED_TRIGGER_UNIT_COUNT
        == 28
    )
    assert {spec.source for spec in catalog.values()} == {
        "host",
        "alias_projection",
        "cron",
    }
    assert catalog[ISOLATED_WORKER_SOCKET_UNIT].triggers == (
        ISOLATED_WORKER_SERVICE_UNIT,
    )
    assert catalog[ISOLATED_WORKER_SERVICE_UNIT].triggered_by == (
        ISOLATED_WORKER_SOCKET_UNIT,
    )
    assert catalog[alias_units.PROJECTOR_TIMER].triggers == (
        alias_units.PROJECTOR_UNIT,
    )
    assert catalog[alias_units.PROJECTOR_UNIT].triggered_by == (
        alias_units.PROJECTOR_TIMER,
    )
    assert (
        catalog["muncho-canonical-writer-phase-b-readiness.service"].activation_class
        == inventory.ACTIVATION_CLASS_STARTUP_ONESHOT
    )
    assert all(
        spec.activation_class == inventory.ACTIVATION_CLASS_TRIGGERED_ONESHOT
        for spec in catalog.values()
        if spec.source in {"alias_projection", "cron"} and spec.kind == "service"
    )


def test_rendered_service_type_must_match_the_inventory_activation_class() -> None:
    units = _all_units(PREDECESSOR)
    long_running = next(
        item
        for item in units
        if inventory.expected_consumer_catalog()[item.name].activation_class
        == inventory.ACTIVATION_CLASS_LONG_RUNNING
    )
    files = dict(long_running.files)
    files[inventory.expected_consumer_catalog()[long_running.name].fragment_path] = (
        files[
            inventory.expected_consumer_catalog()[long_running.name].fragment_path
        ].replace(b"Type=notify", b"Type=oneshot")
    )
    units[units.index(long_running)] = replace(long_running, files=files)

    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_service_type_invalid",
    ):
        _validate(units)


def test_trigger_only_classes_match_authoritative_rendered_unit_payloads() -> None:
    cron_payloads = cron_rail.render_package_unit_files(
        inventory._authoritative_cron_manifest()  # noqa: SLF001
    )
    catalog = inventory.expected_consumer_catalog()
    for spec in catalog.values():
        if spec.source == "cron" and spec.kind == "service":
            assert b"\nType=oneshot\n" in cron_payloads[f"systemd/{spec.name}"]

    alias_bundle = alias_units.render_production_alias_projection_units(
        revision=PREDECESSOR,
        database_ip="10.0.0.1",
        writer_user="muncho-canonical-writer",
        writer_group="muncho-canonical-writer",
        writer_uid=2101,
        writer_gid=2101,
        projector_user="muncho-projector",
        projector_group="muncho-projector",
        projector_uid=2102,
        projector_gid=2102,
        gateway_user="ai-platform-brain",
        gateway_group="ai-platform-brain",
        gateway_uid=2103,
        gateway_gid=2103,
        interpreter_sha256="1" * 64,
        writer_module_sha256="2" * 64,
        projector_module_sha256="3" * 64,
        projection_reader_sha256="4" * 64,
        team_registry_sha256="5" * 64,
        cutover_runtime_sha256="6" * 64,
        cutover_entrypoint_sha256="7" * 64,
    )
    for name, payload in alias_bundle.unit_payloads().items():
        spec = catalog[name]
        if spec.kind == "service":
            assert spec.activation_class == (
                inventory.ACTIVATION_CLASS_TRIGGERED_ONESHOT
            )
            assert b"\nType=oneshot\n" in payload


def test_release_reference_extraction_is_unique_and_deterministic() -> None:
    predecessor = _release_root(PREDECESSOR)
    target = _release_root(TARGET)
    value = {
        "second": [f"{target}/b.py", predecessor],
        "first": (f"{predecessor}/a.py", target.encode()),
    }

    assert inventory.extract_release_references(value) == (
        inventory.ReleaseReference(PREDECESSOR[:12], predecessor),
        inventory.ReleaseReference(TARGET[:12], target),
    )
    assert not inventory.contains_compatibility_release_reference(value)
    assert inventory.contains_compatibility_release_reference(
        "/opt/adventico-ai-platform/hermes-agent/gateway/run.py"
    )


def test_predecessor_and_target_phases_accept_only_their_exact_release() -> None:
    predecessor_result = _validate(_all_units(PREDECESSOR))
    assert predecessor_result.unit_release_revision_prefixes == (PREDECESSOR[:12],)

    fenced_result = _validate(
        _all_units(PREDECESSOR),
        phase=inventory.InventoryPhase.PREDECESSOR_FENCED,
    )
    assert fenced_result.unit_release_revision_prefixes == (PREDECESSOR[:12],)
    assert fenced_result.process_release_revision_prefixes == ()

    stopped_result = _validate(
        _all_units(TARGET),
        phase=inventory.InventoryPhase.TARGET_INSTALLED_STOPPED,
    )
    assert stopped_result.unit_release_revision_prefixes == (TARGET[:12],)
    assert stopped_result.process_release_revision_prefixes == ()

    gateway = "hermes-cloud-gateway.service"
    active_result = _validate(
        _all_units(TARGET),
        phase=inventory.InventoryPhase.TARGET_ACTIVE,
        processes=[
            _process(
                pid=101,
                unit=gateway,
                path=f"{_release_root(TARGET)}/gateway/run.py",
            )
        ],
    )
    assert active_result.process_release_revision_prefixes == (TARGET[:12],)


def test_wrong_or_mixed_release_reference_fails_closed() -> None:
    units = _all_units(PREDECESSOR)
    first = next(
        index
        for index, observation in enumerate(units)
        if inventory.expected_consumer_catalog()[observation.name].executes_release
    )
    observation = units[first]
    properties = dict(observation.properties)
    properties["ReadOnlyPaths"] = f"{_release_root(TARGET)}/gateway"
    units[first] = replace(observation, properties=properties)

    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_release_ref_invalid",
    ):
        _validate(units)


def test_unknown_release_consumer_and_alias_fail_closed() -> None:
    units = _all_units(PREDECESSOR)
    unknown_spec = inventory.ConsumerSpec(
        name="unknown-release.service",
        source="unknown",
        kind="service",
        fragment_path="/etc/systemd/system/unknown-release.service",
        executes_release=True,
    )
    units.append(_observation(unknown_spec, PREDECESSOR))
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_unknown",
    ):
        _validate(units)

    units = _all_units(PREDECESSOR)
    gateway = next(
        item for item in units if item.name == "hermes-cloud-gateway.service"
    )
    alias_properties = dict(gateway.properties)
    alias_properties["Names"] = "gateway-alias.service"
    units.append(
        inventory.UnitObservation(
            name="gateway-alias.service",
            properties=alias_properties,
            files=gateway.files,
        )
    )
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_unknown",
    ):
        _validate(units)


def test_mutable_release_symlink_is_never_an_execution_reference() -> None:
    units = _all_units(PREDECESSOR)
    observation = next(
        item
        for item in units
        if inventory.expected_consumer_catalog()[item.name].executes_release
    )
    properties = dict(observation.properties)
    properties["ExecStart"] = (
        "/usr/bin/python3 /opt/adventico-ai-platform/hermes-agent/gateway/run.py"
    )
    units[units.index(observation)] = replace(observation, properties=properties)

    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_release_symlink_forbidden",
    ):
        _validate(units)


def test_unexpected_drop_in_trigger_and_daemon_reload_fail_closed() -> None:
    units = _all_units(PREDECESSOR)
    timer = next(item for item in units if item.name == alias_units.PROJECTOR_TIMER)
    properties = dict(timer.properties)
    properties["Triggers"] = "unexpected.service"
    units[units.index(timer)] = replace(timer, properties=properties)
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_trigger_invalid",
    ):
        _validate(units)

    units = _all_units(PREDECESSOR)
    gateway = next(
        item for item in units if item.name == "hermes-cloud-gateway.service"
    )
    unexpected = "/etc/systemd/system/hermes-cloud-gateway.service.d/99.conf"
    properties = dict(gateway.properties)
    properties["DropInPaths"] = f"{properties['DropInPaths']} {unexpected}".strip()
    files = {**gateway.files, unexpected: b"[Service]\nNice=5\n"}
    units[units.index(gateway)] = replace(gateway, properties=properties, files=files)
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_drop_ins_invalid",
    ):
        _validate(units)

    units = _all_units(PREDECESSOR)
    gateway = next(
        item for item in units if item.name == "hermes-cloud-gateway.service"
    )
    properties = dict(gateway.properties)
    properties["NeedDaemonReload"] = "yes"
    units[units.index(gateway)] = replace(gateway, properties=properties)
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_daemon_reload_required",
    ):
        _validate(units)


def test_missing_or_duplicate_expected_unit_fails_closed() -> None:
    units = _all_units(PREDECESSOR)
    missing = units.pop()
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match=f"production_release_consumer_missing:{missing.name}",
    ):
        _validate(units)

    units = _all_units(PREDECESSOR)
    units.append(units[0])
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_duplicate",
    ):
        _validate(units)


def test_process_observations_enforce_attribution_phase_and_release() -> None:
    unknown = _process(
        pid=201,
        unit=None,
        path=f"{_release_root(PREDECESSOR)}/gateway/run.py",
    )
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_process_unknown",
    ):
        _validate(_all_units(PREDECESSOR), processes=[unknown])

    gateway = "hermes-cloud-gateway.service"
    predecessor_process = _process(
        pid=202,
        unit=gateway,
        path=f"{_release_root(PREDECESSOR)}/gateway/run.py",
    )
    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_process_unexpected",
    ):
        _validate(
            _all_units(PREDECESSOR),
            phase=inventory.InventoryPhase.PREDECESSOR_FENCED,
            processes=[predecessor_process],
        )

    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_process_unexpected",
    ):
        _validate(
            _all_units(TARGET),
            phase=inventory.InventoryPhase.TARGET_INSTALLED_STOPPED,
            processes=[predecessor_process],
        )

    with pytest.raises(
        inventory.ProductionReleaseConsumerInventoryError,
        match="production_release_consumer_process_release_ref_invalid",
    ):
        _validate(
            _all_units(TARGET),
            phase=inventory.InventoryPhase.TARGET_ACTIVE,
            processes=[predecessor_process],
        )


def test_unrelated_system_unit_without_release_edges_is_ignored() -> None:
    units = _all_units(PREDECESSOR)
    unrelated = inventory.ConsumerSpec(
        name="system-health.service",
        source="operating_system",
        kind="service",
        fragment_path="/usr/lib/systemd/system/system-health.service",
    )
    units.append(_observation(unrelated, PREDECESSOR))

    result = _validate(units)

    assert result.ignored_unrelated_unit_count == 1
