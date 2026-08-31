"""Model contracts: immutability, digest stability, payload hygiene."""

import dataclasses
import json

import pytest

from claude_fleet_control.models import (
    FleetPolicy,
    FleetResult,
    TargetSummary,
    identity_of,
)


def test_records_are_frozen():
    policy = FleetPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.mode = "enforce"


def test_identity_pins_create_time():
    assert identity_of(-42, 1234.9) == "-42:1234"


def test_policy_digest_is_stable_and_mode_independent():
    """The digest names WHAT would be enforced. Mode and the approval field
    stay out of it so the enforce config can pin the digest of the very
    policy it appears in without circularity."""
    a = FleetPolicy(mode="shadow", policy_version="v1")
    b = FleetPolicy(mode="enforce", policy_version="v1")
    c = FleetPolicy(mode="shadow", policy_version="v1", approved_enforce_digest="x")
    assert a.digest() == b.digest() == c.digest()
    assert a.digest() != FleetPolicy(mode="shadow", policy_version="v2").digest()
    assert a.digest() != dataclasses.replace(a, max_tree_processes=25).digest()


def test_target_payload_carries_identities_never_cmdlines():
    target = TargetSummary(
        root_identity="-10:1000", root_pid=-10, root_create_time=1000.0,
        member_identities=("-10:1000", "-11:1001"), member_count=2,
        total_rss=123, transcript_path="x.jsonl", transcript_mtime=1.0,
        idle_minutes=45.0, strike_key="k", strikes=2,
    )
    payload = json.dumps(target.to_payload())
    assert "cmdline" not in payload
    assert target.to_payload()["action"] == "hard_terminate"


def test_result_payload_uses_the_scanned_status_key():
    """events.outcomes scans payload['status']; 'failed' there is what earns
    the FAILED verdict that routing promotes. The key name is a contract."""
    result = FleetResult(run_id="r", plan_id="p", status="failed", executor_called=False)
    assert result.to_payload()["status"] == "failed"
