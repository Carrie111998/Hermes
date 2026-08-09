from datetime import datetime, timezone

import pytest

from plugins.agentops.control.models import AuthorityMode
from plugins.agentops.control.observer_models import TargetSnapshot
from plugins.agentops.control.registry import TargetRegistrationError, bootstrap_gateway_registry


def test_bootstrap_registry_has_the_five_phase_zero_profiles_in_observe_only_mode():
    registry = bootstrap_gateway_registry()

    targets = registry.list_targets()

    assert [target.target_id for target in targets] == [
        "hermes:profile:default:gateway",
        "hermes:profile:feishu3:gateway",
        "hermes:profile:feishu4:gateway",
        "hermes:profile:feishu5:gateway",
        "hermes:profile:newbot:gateway",
    ]
    assert {target.authority_mode for target in targets} == {AuthorityMode.OBSERVE_ONLY}
    assert registry.coverage_report().coverage_percent == 0


def test_fleet_snapshot_coverage_reaches_one_hundred_percent_only_after_all_targets():
    registry = bootstrap_gateway_registry()
    default = registry.get_target("hermes:profile:default:gateway")
    assert default.spec.labels.get("process_observation") == "enabled"
    assert default.spec.labels.get("process_marker") == "default"
    for target in registry.list_targets():
        if target.spec.profile != "default":
            assert "process_marker_optional" not in target.spec.labels
            assert "process_command_label_optional" not in target.spec.labels
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)

    for target in registry.list_targets():
        registry.record_target_snapshot(
            TargetSnapshot(target_id=target.target_id, observed_at=observed_at, facts={"present": True})
        )

    coverage = registry.coverage_report()
    assert (coverage.registered_targets, coverage.snapshotted_targets, coverage.coverage_percent) == (1, 1, 100)
    assert "processes" not in coverage.unmanaged_collectors
    assert len(coverage.out_of_scope_targets) == 4


def test_registry_refuses_duplicate_targets_and_unknown_snapshots():
    registry = bootstrap_gateway_registry()
    target = registry.list_targets()[0]

    with pytest.raises(TargetRegistrationError):
        registry.register_target(target.spec)
    with pytest.raises(TargetRegistrationError):
        registry.record_target_snapshot(
            TargetSnapshot(
                target_id="hermes:profile:missing:gateway",
                observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
                facts={},
            )
        )
