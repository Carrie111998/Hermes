from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from session_bridge.health import (
    MAX_FAILURES,
    MAX_SAFE_INTEGER,
    MAX_SYMBOLIC_CODE_LENGTH,
    build_session_health_evidence,
)


EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "axis_policy_version",
    "observation_started_at",
    "observation_completed_at",
    "service",
    "protocol_baseline",
    "request_authentication",
    "authorization_governance",
    "catalog",
    "queues",
    "mirrors",
    "failures",
    "service_impact_summary",
    "governance_summary",
}


def healthy_inputs() -> dict[str, Any]:
    return {
        "observation_started_at": 1_000.0,
        "observation_completed_at": 1_001.0,
        "health_observed_at": 1_000.1,
        "catalog_observed_at": 1_000.2,
        "sidebar_observed_at": 1_000.3,
        "hydration_observed_at": 1_000.4,
        "claude_visibility_observed_at": 1_000.5,
        "coordinator_health": {
            "running": True,
            "watcher_state": "running",
            "providers": {
                "claude": {
                    "last_success": 990.0,
                    "lag_seconds": 10.0,
                    "degraded_reason": None,
                },
                "codex": {
                    "last_success": 991.0,
                    "lag_seconds": 9.0,
                    "degraded_reason": None,
                },
            },
            "queue_counts": {
                "queued": 0,
                "running": 0,
                "retry": 0,
                "succeeded": 4,
                "manual_failure": 0,
            },
            "mirror_mode": "manual",
            "backfill_progress": {
                "claude": {"version": 1, "indexed_total": 4, "remaining": 0},
                "codex": {"version": 1, "indexed_total": 5, "remaining": 0},
            },
            "recent_error_codes": [],
        },
        "catalog_status": {
            "providers": {
                "claude": {"sessions": 4, "degraded": 0},
                "codex": {"sessions": 5, "degraded": 0},
                "hermes": {"sessions": 2, "degraded": 0},
            },
            "total_sessions": 11,
        },
        "sidebar_status": {
            "counts": {
                "sidebar_pending": 0,
                "sidebar_leased": 0,
                "sidebar_visible": 3,
                "sidebar_retry": 0,
                "sidebar_failed": 0,
            },
            "blocking_failed_count": 0,
            "terminal_resolution_ledger_valid": True,
            "execution_blockers": [],
            "oldest_pending_age_seconds": None,
            "recent_error_codes": [],
        },
        "hydration_status": {
            "counts": {
                "hydration_pending": 0,
                "hydration_leased": 0,
                "hydration_retry": 0,
                "hydration_visible": 3,
                "hydration_failed": 0,
            },
            "active_lease": False,
            "reserved_reconciliation": 0,
            "oldest_pending_age_seconds": None,
            "recent_error_codes": [],
        },
        "claude_visibility_status": {
            "enabled": True,
            "counts": {
                "claude_pending": 0,
                "claude_leased": 0,
                "claude_retry": 0,
                "claude_visible": 3,
                "claude_failed": 0,
            },
            "retry_codes": {},
            "failed_codes": {},
            "degraded_reasons": [],
            "lineage": {
                "unlinked_visible": 0,
                "repairable": 0,
                "blocked": 0,
                "blocker_codes": {},
            },
        },
        "catalog_scan_seconds": 30,
        "hydration_enabled": True,
        "claude_visibility_enabled": True,
    }


def test_builds_exact_v1_policy_and_base_axes() -> None:
    evidence = build_session_health_evidence(**healthy_inputs())

    assert set(evidence) == EXPECTED_TOP_LEVEL_KEYS
    assert evidence["schema_version"] == 1
    assert evidence["axis_policy_version"] == 1
    assert evidence["service"]["state"] == "healthy"
    assert evidence["service"]["code"] == "functional_status_available"
    assert evidence["service"]["owner"] == "session_bridge_server"
    assert evidence["service"]["required_for_service_impact"] is True
    assert evidence["service"]["required_for_governance"] is True
    assert evidence["protocol_baseline"]["baseline"] == "2025-11-25"
    assert evidence["request_authentication"]["code"] == "authenticated_dispatch"
    assert evidence["authorization_governance"]["state"] == "unknown"
    assert evidence["authorization_governance"]["code"] == (
        "per_harness_identity_not_assessed"
    )
    assert set(evidence["catalog"]["providers"]) == {"claude", "codex", "hermes"}
    assert set(evidence["queues"]) == {
        "mirror_jobs",
        "sidebar_registration",
        "sidebar_hydration",
        "claude_visibility",
    }
    for axis in (
        evidence["service"],
        evidence["protocol_baseline"],
        evidence["request_authentication"],
        evidence["authorization_governance"],
    ):
        assert math.isfinite(axis["observed_at"])
        assert 1_000.0 <= axis["observed_at"] <= 1_001.0


def test_exact_owner_requiredness_matrix() -> None:
    evidence = build_session_health_evidence(**healthy_inputs())
    expected = {
        "service": ("session_bridge_server", True, True),
        "protocol_baseline": ("session_bridge_server", False, True),
        "request_authentication": ("session_bridge_server", True, True),
        "authorization_governance": (
            "session_bridge_governance",
            False,
            True,
        ),
    }
    for name, policy in expected.items():
        axis_value = evidence[name]
        assert (
            axis_value["owner"],
            axis_value["required_for_service_impact"],
            axis_value["required_for_governance"],
        ) == policy
    for provider in ("claude", "codex"):
        axis_value = evidence["catalog"]["providers"][provider]["work_state"]
        assert axis_value["owner"] == "session_bridge_catalog"
        assert axis_value["required_for_service_impact"] is True
        assert axis_value["required_for_governance"] is True
    hermes = evidence["catalog"]["providers"]["hermes"]
    assert hermes["work_state"]["required_for_service_impact"] is False
    assert hermes["work_state"]["required_for_governance"] is True
    for queue_name, owner in {
        "mirror_jobs": "session_bridge_mirror",
        "sidebar_registration": "session_bridge_sidebar",
        "sidebar_hydration": "session_bridge_hydration",
        "claude_visibility": "session_bridge_claude_visibility",
    }.items():
        queue = evidence["queues"][queue_name]
        assert queue["work_state"]["owner"] == owner
        assert queue["work_state"]["required_for_service_impact"] is True
        assert queue["work_state"]["required_for_governance"] is True
        for axis_name in ("oldest_age", "capacity", "flow", "expiry", "overflow"):
            assert queue[axis_name]["owner"] == "session_bridge_governance"
            assert queue[axis_name]["required_for_service_impact"] is False
            assert queue[axis_name]["required_for_governance"] is True


@pytest.mark.parametrize("lag", [90, 90.0])
def test_provider_exact_freshness_limit_is_healthy(lag: object) -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["providers"]["claude"]["lag_seconds"] = lag
    freshness = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "claude"
    ]["freshness"]
    assert freshness == {"state": "healthy", "code": "within_freshness_limit"}


@pytest.mark.parametrize("reason", ["scan_failed", "refresh_failed"])
def test_stale_provider_with_registered_current_failure_is_error(reason: str) -> None:
    inputs = healthy_inputs()
    provider = inputs["coordinator_health"]["providers"]["claude"]
    provider["lag_seconds"] = 91
    provider["degraded_reason"] = reason
    result = build_session_health_evidence(**inputs)
    claude = result["catalog"]["providers"]["claude"]
    assert claude["freshness"]["state"] == "error"
    assert claude["freshness"]["code"] == "stale_index"
    assert claude["work_state"]["state"] == "error"
    assert claude["work_state"]["code"] == reason
    assert result["service"]["state"] == "healthy"
    assert result["catalog"]["providers"]["codex"]["freshness"]["state"] == (
        "healthy"
    )


def test_stale_provider_without_lifecycle_discriminator_is_unknown() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["providers"]["claude"]["lag_seconds"] = 91
    freshness = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "claude"
    ]["freshness"]
    assert freshness == {
        "state": "unknown",
        "code": "stale_without_lifecycle_context",
    }


@pytest.mark.parametrize(
    "bad", [None, True, "33", -1, float("nan"), float("inf")]
)
def test_invalid_provider_measurement_is_unknown_not_zero(bad: object) -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["providers"]["claude"]["lag_seconds"] = bad
    freshness = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "claude"
    ]["freshness"]
    assert freshness == {"state": "unknown", "code": "invalid_measurement"}


@pytest.mark.parametrize("bad", [None, True, "990", -1, float("nan"), float("inf")])
def test_invalid_last_success_is_unknown(bad: object) -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["providers"]["claude"]["last_success"] = bad
    freshness = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "claude"
    ]["freshness"]
    assert freshness["state"] == "unknown"
    assert freshness["code"] == "invalid_measurement"


def test_missing_provider_state_is_unknown_not_healthy() -> None:
    inputs = healthy_inputs()
    del inputs["coordinator_health"]["providers"]["claude"]
    provider = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "claude"
    ]
    assert provider["work_state"]["state"] == "unknown"
    assert provider["work_state"]["code"] == "invalid_measurement"
    assert provider["freshness"] == {
        "state": "unknown",
        "code": "invalid_measurement",
    }


def test_unregistered_provider_failure_is_unknown_and_not_echoed() -> None:
    secret_code = "new_provider_secret_code"
    inputs = healthy_inputs()
    inputs["coordinator_health"]["providers"]["claude"][
        "degraded_reason"
    ] = secret_code
    result = build_session_health_evidence(**inputs)
    assert result["catalog"]["providers"]["claude"]["work_state"]["state"] == (
        "unknown"
    )
    assert secret_code not in json.dumps(result)


def test_hermes_catalog_only_observation_is_current_without_coordinator_or_backfill() -> None:
    hermes = build_session_health_evidence(**healthy_inputs())["catalog"][
        "providers"
    ]["hermes"]

    assert hermes["work_state"]["state"] == "healthy"
    assert hermes["work_state"]["code"] == "catalog_provider_readable"
    assert hermes["freshness"] == {
        "state": "healthy",
        "code": "current_catalog_observation",
    }
    assert hermes["backfill"] == {
        field: {"state": "healthy", "code": "not_applicable"}
        for field in ("version", "indexed_total", "remaining")
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["catalog_status"]["providers"].pop("hermes"),
        lambda data: data["catalog_status"]["providers"]["hermes"].update(
            sessions=True
        ),
        lambda data: data["catalog_status"]["providers"]["hermes"].update(
            degraded="0"
        ),
    ],
)
def test_hermes_catalog_only_observation_fails_closed(mutation: Any) -> None:
    inputs = healthy_inputs()
    mutation(inputs)
    hermes = build_session_health_evidence(**inputs)["catalog"]["providers"][
        "hermes"
    ]

    assert hermes["work_state"]["state"] == "unknown"
    assert hermes["work_state"]["code"] == "invalid_measurement"
    assert hermes["freshness"] == {
        "state": "unknown",
        "code": "invalid_measurement",
    }


def test_invalid_catalog_counts_are_unknown_not_zero() -> None:
    inputs = healthy_inputs()
    inputs["catalog_status"]["providers"]["claude"]["sessions"] = True
    inputs["catalog_status"]["total_sessions"] = "11"
    catalog = build_session_health_evidence(**inputs)["catalog"]
    assert catalog["providers"]["claude"]["session_count"] == {
        "state": "unknown",
        "code": "invalid_measurement",
    }
    assert catalog["aggregate"]["total_sessions"] == {
        "state": "unknown",
        "code": "invalid_measurement",
    }


def test_all_queue_subaxes_are_independent_and_untracked_axes_stay_unknown() -> None:
    evidence = build_session_health_evidence(**healthy_inputs())
    assert set(evidence["queues"]) == {
        "mirror_jobs",
        "sidebar_registration",
        "sidebar_hydration",
        "claude_visibility",
    }
    for queue in evidence["queues"].values():
        assert set(queue) == {
            "work_state",
            "oldest_age",
            "capacity",
            "flow",
            "expiry",
            "overflow",
            "ledger_integrity",
        }
        for name in ("oldest_age", "capacity", "flow", "expiry", "overflow"):
            assert queue[name]["state"] == "unknown"
            assert queue[name]["code"] == "not_tracked"


def test_mirror_ledger_is_not_applicable_only_for_valid_authoritative_work() -> None:
    inputs = healthy_inputs()
    mirror = build_session_health_evidence(**inputs)["queues"]["mirror_jobs"]

    assert mirror["work_state"]["state"] == "healthy"
    assert mirror["ledger_integrity"]["state"] == "healthy"
    assert mirror["ledger_integrity"]["code"] == "not_applicable"
    assert mirror["ledger_integrity"]["lifecycle_context"] == "not_applicable"

    malformed = healthy_inputs()
    malformed["coordinator_health"]["queue_counts"]["queued"] = "0"
    mirror = build_session_health_evidence(**malformed)["queues"]["mirror_jobs"]

    assert mirror["work_state"]["state"] == "unknown"
    assert mirror["ledger_integrity"]["state"] == "unknown"
    # 2026-08-13 (920acee871): the ledger axis must NOT restate the work failure.
    # This used to assert "invalid_measurement" -- work_state's code, copied onto a
    # queue_ledger axis while forcing state="unknown". mirror_jobs has no real
    # ledger, so the honest verdict is "not tracked", and "invalid_measurement" is
    # not in the consumer's mirror_jobs ledger vocabulary
    # ({not_applicable, not_tracked, invalid_observation_time}) -- emitting it made
    # the consumer reject the axis, voiding the whole evidence envelope and blanking
    # all four session-bridge monitor rows to 'unknown'.
    assert mirror["ledger_integrity"]["code"] == "not_tracked"
    # The invariant behind that fix: the ledger code is never the work code.
    assert mirror["ledger_integrity"]["code"] != mirror["work_state"]["code"]


def test_nonzero_pending_and_retry_without_age_is_not_error() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["queue_counts"]["queued"] = 2
    inputs["coordinator_health"]["queue_counts"]["retry"] = 1
    inputs["sidebar_status"]["counts"]["sidebar_pending"] = 2
    inputs["sidebar_status"]["counts"]["sidebar_retry"] = 1
    inputs["hydration_status"]["counts"]["hydration_pending"] = 2
    inputs["hydration_status"]["counts"]["hydration_retry"] = 1
    inputs["claude_visibility_status"]["counts"]["claude_pending"] = 2
    inputs["claude_visibility_status"]["counts"]["claude_retry"] = 1
    evidence = build_session_health_evidence(**inputs)
    for queue in evidence["queues"].values():
        assert queue["work_state"]["state"] == "healthy"
        assert queue["oldest_age"]["state"] == "unknown"


def test_tracked_oldest_age_is_admitted_without_claiming_staleness() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["oldest_pending_age_seconds"] = 12.5
    oldest = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["oldest_age"]
    assert oldest["state"] == "healthy"
    assert oldest["code"] == "tracked_measurement"
    assert oldest["value_seconds"] == 12.5


@pytest.mark.parametrize("bad", [True, "12", -1, float("nan"), float("inf")])
def test_invalid_tracked_oldest_age_is_unknown(bad: object) -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["oldest_pending_age_seconds"] = bad
    oldest = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["oldest_age"]
    assert oldest["state"] == "unknown"
    assert oldest["code"] == "invalid_measurement"


def test_active_hydration_lease_is_a_fact_not_a_defect() -> None:
    inputs = healthy_inputs()
    inputs["hydration_status"]["counts"]["hydration_leased"] = 1
    inputs["hydration_status"]["active_lease"] = True
    work = build_session_health_evidence(**inputs)["queues"]["sidebar_hydration"][
        "work_state"
    ]
    assert work["state"] == "healthy"
    assert work["active_lease"] is True


@pytest.mark.parametrize(
    ("flag", "queue_name"),
    [
        ("hydration_enabled", "sidebar_hydration"),
        ("claude_visibility_enabled", "claude_visibility"),
    ],
)
def test_disabled_optional_feature_is_explicit_not_failure(
    flag: str, queue_name: str
) -> None:
    inputs = healthy_inputs()
    inputs[flag] = False
    queue = build_session_health_evidence(**inputs)["queues"][queue_name]
    assert queue["work_state"]["state"] == "healthy"
    assert queue["work_state"]["code"] == "optional_feature_disabled"
    assert queue["work_state"]["required_for_service_impact"] is False


@pytest.mark.parametrize(
    ("flag", "timestamp", "queue_name"),
    [
        ("hydration_enabled", "hydration_observed_at", "sidebar_hydration"),
        (
            "claude_visibility_enabled",
            "claude_visibility_observed_at",
            "claude_visibility",
        ),
    ],
)
def test_invalid_optional_queue_observation_remains_service_required(
    flag: str, timestamp: str, queue_name: str
) -> None:
    inputs = healthy_inputs()
    inputs[flag] = False
    inputs[timestamp] = None
    queue = build_session_health_evidence(**inputs)["queues"][queue_name]
    assert queue["work_state"]["code"] == "invalid_observation_time"
    assert queue["work_state"]["required_for_service_impact"] is True
    assert queue["ledger_integrity"]["code"] == "invalid_observation_time"
    assert queue["ledger_integrity"]["required_for_service_impact"] is True


def test_registered_claude_visibility_failed_code_is_current_error() -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["counts"]["claude_failed"] = 1
    inputs["claude_visibility_status"]["failed_codes"] = {"uuid_conflict": 1}
    work = build_session_health_evidence(**inputs)["queues"]["claude_visibility"][
        "work_state"
    ]
    assert work["state"] == "error"
    assert work["code"] == "uuid_conflict"


def test_unregistered_claude_visibility_failed_code_remains_unknown() -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["counts"]["claude_failed"] = 1
    inputs["claude_visibility_status"]["failed_codes"] = {"novel_private_code": 1}
    work = build_session_health_evidence(**inputs)["queues"]["claude_visibility"][
        "work_state"
    ]
    assert work["state"] == "unknown"
    assert work["code"] == "unregistered_failure_code"
    assert "novel_private_code" not in json.dumps(
        build_session_health_evidence(**inputs)
    )


def test_registered_sidebar_blocker_is_current_error() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["blocking_failed_count"] = 1
    inputs["sidebar_status"]["execution_blockers"] = ["sidebar_failed"]
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["work_state"]
    assert work["state"] == "error"
    assert work["code"] == "sidebar_failed"


def test_invalid_terminal_resolution_ledger_is_error() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["terminal_resolution_ledger_valid"] = False
    ledger = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["ledger_integrity"]
    assert ledger["state"] == "error"
    assert ledger["code"] == "sidebar_terminal_resolution_ledger_invalid"


def test_unknown_queue_state_and_code_remain_unknown() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["counts"]["sidebar_pending"] = "2"
    inputs["sidebar_status"]["blocking_failed_count"] = 1
    inputs["sidebar_status"]["execution_blockers"] = ["unreviewed_blocker"]
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["work_state"]
    assert work["state"] == "error"
    assert work["code"] == "sidebar_failed"


def test_manual_mirror_mode_cannot_mask_derivative_error() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["queue_counts"]["manual_failure"] = 1
    inputs["coordinator_health"]["recent_error_codes"] = [
        "mirror_queue_health_failed"
    ]
    mirrors = build_session_health_evidence(**inputs)["mirrors"]
    assert mirrors["policy_mode"]["state"] == "healthy"
    assert mirrors["policy_mode"]["code"] == "manual_mode"
    assert mirrors["derivative_work_state"]["state"] == "error"


def test_recent_error_history_does_not_turn_current_work_error() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["recent_error_codes"] = ["desktop_offline"]
    evidence = build_session_health_evidence(**inputs)
    assert evidence["queues"]["sidebar_registration"]["work_state"]["state"] == (
        "healthy"
    )
    assert evidence["failures"][0]["code"] == "desktop_offline"


@pytest.mark.parametrize(
    ("capability", "code"),
    [
        ("catalog", "scan_failed"),
        ("mirror_jobs", "codex_scan_failed"),
        ("sidebar_registration", "desktop_offline"),
        ("sidebar_hydration", "hydration_send_ambiguous"),
        ("claude_visibility", "claude_executable_unavailable"),
    ],
)
def test_registered_public_failure_codes_are_admitted(
    capability: str, code: str
) -> None:
    inputs = healthy_inputs()
    if capability == "catalog":
        inputs["coordinator_health"]["providers"]["claude"][
            "degraded_reason"
        ] = code
    elif capability == "mirror_jobs":
        inputs["coordinator_health"]["recent_error_codes"] = [code]
    elif capability == "sidebar_registration":
        inputs["sidebar_status"]["recent_error_codes"] = [code]
    elif capability == "sidebar_hydration":
        inputs["hydration_status"]["recent_error_codes"] = [code]
    else:
        inputs["claude_visibility_status"]["retry_codes"] = {code: 1}
    failures = build_session_health_evidence(**inputs)["failures"]
    failure = next(item for item in failures if item["code"] == code)
    assert failure["capability"] == capability
    assert set(failure) == {
        "code",
        "class",
        "capability",
        "impact",
        "retryability",
    }


def test_unknown_failure_is_constant_and_original_never_appears() -> None:
    secret_code = "token_leak_candidate"
    inputs = healthy_inputs()
    inputs["coordinator_health"]["recent_error_codes"] = [secret_code]
    evidence = build_session_health_evidence(**inputs)
    assert evidence["failures"][0]["code"] == "unregistered_failure_code"
    assert secret_code not in json.dumps(evidence)


@pytest.mark.parametrize(
    "bad",
    [None, True, "A", "x" * (MAX_SYMBOLIC_CODE_LENGTH + 1), "has-dash", 42],
)
def test_invalid_failure_evidence_is_constant_and_not_echoed(bad: object) -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["recent_error_codes"] = [bad]
    serialized = json.dumps(build_session_health_evidence(**inputs))
    failure = json.loads(serialized)["failures"][0]
    assert failure["code"] == "invalid_failure_evidence"
    if type(bad) is str:
        assert bad not in serialized


def test_failures_are_bounded_to_32() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["recent_error_codes"] = [
        f"unregistered_{index}" for index in range(MAX_FAILURES)
    ]
    assert len(build_session_health_evidence(**inputs)["failures"]) == MAX_FAILURES


def test_oversized_failure_history_is_constant_overflow() -> None:
    inputs = healthy_inputs()
    values = [f"private_history_{index}" for index in range(MAX_FAILURES + 1)]
    inputs["coordinator_health"]["recent_error_codes"] = values
    failures = build_session_health_evidence(**inputs)["failures"]
    assert failures == [{
        "code": "failure_evidence_overflow",
        "class": "schema",
        "capability": "mirror_jobs",
        "impact": "failure_evidence_overflow",
        "retryability": "unknown",
    }]
    assert all(value not in json.dumps(failures) for value in values)


def test_symbolic_code_and_numeric_boundaries_are_exact() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"]["recent_error_codes"] = [
        "x" * MAX_SYMBOLIC_CODE_LENGTH,
        "x" * (MAX_SYMBOLIC_CODE_LENGTH + 1),
    ]
    inputs["catalog_status"]["total_sessions"] = MAX_SAFE_INTEGER
    inputs["catalog_status"]["providers"]["claude"]["sessions"] = (
        MAX_SAFE_INTEGER + 1
    )
    evidence = build_session_health_evidence(**inputs)
    assert evidence["failures"][0]["code"] == "unregistered_failure_code"
    assert evidence["failures"][1]["code"] == "invalid_failure_evidence"
    assert evidence["catalog"]["aggregate"]["total_sessions"]["value"] == (
        MAX_SAFE_INTEGER
    )
    assert evidence["catalog"]["providers"]["claude"]["session_count"][
        "state"
    ] == "unknown"


def test_canaries_in_every_source_never_leak_and_inputs_are_not_mutated() -> None:
    canary = "super_secret_transcript_path_token_canary"
    inputs = healthy_inputs()
    inputs["coordinator_health"]["private"] = {"transcript": canary}
    inputs["catalog_status"]["session_ids"] = [canary]
    inputs["sidebar_status"]["last_visible_task_id"] = canary
    inputs["hydration_status"]["lease_token"] = canary
    inputs["claude_visibility_status"]["characterizations"] = [{"job_id": canary}]
    before = copy.deepcopy(inputs)
    evidence = build_session_health_evidence(**inputs)
    assert canary not in json.dumps(evidence)
    assert inputs == before


def test_invalid_required_envelope_is_bounded_and_axes_unknown() -> None:
    inputs = healthy_inputs()
    inputs["observation_started_at"] = float("nan")
    inputs["observation_completed_at"] = "1001"
    evidence = build_session_health_evidence(**inputs)
    assert evidence["observation_started_at"] is None
    assert evidence["observation_completed_at"] is None
    assert evidence["service"]["state"] == "unknown"
    assert evidence["service"]["observed_at"] is None


def test_summary_precedence_is_deterministic_and_domain_never_down() -> None:
    healthy = build_session_health_evidence(**healthy_inputs())
    assert healthy["service_impact_summary"] == {
        "state": "healthy",
        "code": "required_capabilities_healthy",
    }
    assert healthy["governance_summary"] == {
        "state": "unknown",
        "code": "required_governance_evidence_unknown",
    }
    unknown_inputs = healthy_inputs()
    unknown_inputs["coordinator_health"]["providers"]["claude"][
        "lag_seconds"
    ] = None
    unknown = build_session_health_evidence(**unknown_inputs)
    assert unknown["service_impact_summary"]["state"] == "unknown"
    error_inputs = healthy_inputs()
    error_inputs["coordinator_health"]["providers"]["claude"][
        "degraded_reason"
    ] = "scan_failed"
    error = build_session_health_evidence(**error_inputs)
    assert error["service_impact_summary"]["state"] == "error"
    for evidence in (healthy, unknown, error):
        assert '"down"' not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("running", "expected_state", "expected_code", "summary_state"),
    [
        (True, "healthy", "functional_status_available", "healthy"),
        (False, "error", "service_not_running", "error"),
        (None, "unknown", "invalid_service_evidence", "unknown"),
        ("true", "unknown", "invalid_service_evidence", "unknown"),
    ],
)
def test_service_running_exact_bool_semantics(
    running: object,
    expected_state: str,
    expected_code: str,
    summary_state: str,
) -> None:
    inputs = healthy_inputs()
    if running is None:
        del inputs["coordinator_health"]["running"]
    else:
        inputs["coordinator_health"]["running"] = running
    evidence = build_session_health_evidence(**inputs)
    assert evidence["service"]["state"] == expected_state
    assert evidence["service"]["code"] == expected_code
    assert evidence["service_impact_summary"]["state"] == summary_state
    if summary_state == "error":
        assert evidence["governance_summary"]["state"] == "error"


@pytest.mark.parametrize(
    ("timestamp_key", "owned_paths"),
    [
        (
            "catalog_observed_at",
            [
                ("catalog", "providers", "claude", "session_count"),
                ("catalog", "providers", "claude", "degraded_count"),
                ("catalog", "providers", "codex", "session_count"),
                ("catalog", "providers", "codex", "degraded_count"),
                ("catalog", "providers", "hermes", "session_count"),
                ("catalog", "providers", "hermes", "degraded_count"),
            ],
        ),
        (
            "sidebar_observed_at",
            [("queues", "sidebar_registration", name) for name in (
                "work_state", "oldest_age", "capacity", "flow", "expiry",
                "overflow", "ledger_integrity",
            )],
        ),
        (
            "hydration_observed_at",
            [("queues", "sidebar_hydration", name) for name in (
                "work_state", "oldest_age", "capacity", "flow", "expiry",
                "overflow", "ledger_integrity",
            )],
        ),
        (
            "claude_visibility_observed_at",
            [("queues", "claude_visibility", name) for name in (
                "work_state", "oldest_age", "capacity", "flow", "expiry",
                "overflow", "ledger_integrity",
            )],
        ),
    ],
)
@pytest.mark.parametrize("bad_time", [None, "1000", 999.0, 1_002.0])
def test_invalid_source_observation_poison_owned_axes_and_summaries(
    timestamp_key: str,
    owned_paths: list[tuple[str, ...]],
    bad_time: object,
) -> None:
    inputs = healthy_inputs()
    inputs[timestamp_key] = bad_time
    evidence = build_session_health_evidence(**inputs)
    for path in owned_paths:
        value: object = evidence
        for part in path:
            value = value[part]
        assert value["state"] == "unknown"
        assert value["code"] == "invalid_observation_time"
    if timestamp_key == "catalog_observed_at":
        for provider in ("claude", "codex"):
            item = evidence["catalog"]["providers"][provider]
            assert item["work_state"]["state"] == "healthy"
            assert item["freshness"]["state"] == "healthy"
            for fact in item["backfill"].values():
                assert fact["state"] == "healthy"
        hermes = evidence["catalog"]["providers"]["hermes"]
        assert hermes["work_state"]["state"] == "unknown"
        assert hermes["work_state"]["code"] == "invalid_observation_time"
        assert hermes["freshness"]["code"] == "invalid_observation_time"
        assert evidence["catalog"]["aggregate"]["total_sessions"]["code"] == (
            "invalid_observation_time"
        )
        assert evidence["service_impact_summary"]["state"] == "healthy"
    else:
        assert evidence["service_impact_summary"]["state"] == "unknown"
    assert evidence["governance_summary"]["state"] == "unknown"


@pytest.mark.parametrize(
    ("flag", "queue_name"),
    [
        ("hydration_enabled", "sidebar_hydration"),
        ("claude_visibility_enabled", "claude_visibility"),
    ],
)
@pytest.mark.parametrize("bad_flag", [None, "false", 0, 1])
def test_malformed_optional_feature_flag_remains_required_unknown(
    flag: str, queue_name: str, bad_flag: object
) -> None:
    inputs = healthy_inputs()
    inputs[flag] = bad_flag
    evidence = build_session_health_evidence(**inputs)
    work = evidence["queues"][queue_name]["work_state"]
    assert work["state"] == "unknown"
    assert work["code"] == "invalid_feature_flag"
    assert work["required_for_service_impact"] is True
    assert evidence["service_impact_summary"]["state"] == "unknown"


def test_sidebar_blocking_count_is_current_without_synthetic_blocker() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["blocking_failed_count"] = 2
    inputs["sidebar_status"]["counts"]["sidebar_failed"] = 2
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]["work_state"]
    assert work["state"] == "error"
    assert work["code"] == "sidebar_failed"


def test_sidebar_multiple_registered_blockers_use_fixed_precedence() -> None:
    inputs = healthy_inputs()
    inputs["sidebar_status"]["execution_blockers"] = [
        "unknown_retry_code",
        "sidebar_terminal_resolution_mismatch",
    ]
    inputs["sidebar_status"]["terminal_resolution_ledger_valid"] = False
    queue = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]
    assert queue["work_state"]["state"] == "error"
    assert queue["work_state"]["code"] == "sidebar_terminal_resolution_mismatch"
    assert queue["ledger_integrity"]["state"] == "error"
    assert queue["ledger_integrity"]["code"] == (
        "sidebar_terminal_resolution_ledger_invalid"
    )


def test_hydration_failed_with_registered_history_keeps_fixed_current_code() -> None:
    inputs = healthy_inputs()
    inputs["hydration_status"]["counts"]["hydration_failed"] = 2
    inputs["hydration_status"]["recent_error_codes"] = [
        "hydration_send_ambiguous"
    ]
    evidence = build_session_health_evidence(**inputs)
    work = evidence["queues"]["sidebar_hydration"]["work_state"]
    assert work["state"] == "error"
    assert work["code"] == "hydration_failed"
    assert any(
        failure["code"] == "hydration_send_ambiguous"
        and failure["capability"] == "sidebar_hydration"
        for failure in evidence["failures"]
    )


def test_hydration_history_without_current_failed_count_is_not_error() -> None:
    inputs = healthy_inputs()
    inputs["hydration_status"]["recent_error_codes"] = [
        "hydration_send_ambiguous"
    ]
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_hydration"
    ]["work_state"]
    assert work["state"] == "healthy"


@pytest.mark.parametrize(
    ("reason", "expected_state", "expected_code"),
    [
        ("invalid_status", "error", "invalid_status"),
        ("uuid_conflict", "error", "uuid_conflict"),
        ("unknown_failed_code", "unknown", "unknown_failed_code"),
    ],
)
def test_claude_visibility_current_degraded_reason_is_classified(
    reason: str, expected_state: str, expected_code: str
) -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["degraded_reasons"] = [reason]
    work = build_session_health_evidence(**inputs)["queues"][
        "claude_visibility"
    ]["work_state"]
    assert work["state"] == expected_state
    assert work["code"] == expected_code


def test_claude_visibility_registered_retry_code_is_current_error() -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["counts"]["claude_retry"] = 1
    inputs["claude_visibility_status"]["retry_codes"] = {
        "creation_ambiguous": 1
    }

    work = build_session_health_evidence(**inputs)["queues"][
        "claude_visibility"
    ]["work_state"]

    assert work["state"] == "error"
    assert work["code"] == "creation_ambiguous"


def test_repaired_provider_and_mirror_semantics_close_required_summaries() -> None:
    evidence = build_session_health_evidence(**healthy_inputs())

    assert evidence["catalog"]["providers"]["hermes"]["freshness"]["state"] == (
        "healthy"
    )
    assert evidence["queues"]["mirror_jobs"]["ledger_integrity"]["state"] == (
        "healthy"
    )
    assert evidence["service_impact_summary"] == {
        "state": "healthy",
        "code": "required_capabilities_healthy",
    }
    assert evidence["governance_summary"] == {
        "state": "unknown",
        "code": "required_governance_evidence_unknown",
    }


def test_provider_canonical_timestamp_boundary_and_conflict() -> None:
    canonical = healthy_inputs()
    provider = canonical["coordinator_health"]["providers"]["claude"]
    provider["last_success_at"] = provider.pop("last_success")
    assert build_session_health_evidence(**canonical)["catalog"]["providers"][
        "claude"
    ]["freshness"]["state"] == "healthy"

    compatible = healthy_inputs()
    provider = compatible["coordinator_health"]["providers"]["claude"]
    provider["last_success_at"] = provider["last_success"]
    assert build_session_health_evidence(**compatible)["catalog"]["providers"][
        "claude"
    ]["freshness"]["state"] == "healthy"

    conflict = healthy_inputs()
    provider = conflict["coordinator_health"]["providers"]["claude"]
    provider["last_success_at"] = provider["last_success"] + 1
    assert build_session_health_evidence(**conflict)["catalog"]["providers"][
        "claude"
    ]["freshness"] == {"state": "unknown", "code": "invalid_measurement"}


def test_dynamic_failure_sources_are_bounded_before_iteration() -> None:
    inputs = healthy_inputs()
    oversized = {f"private_code_{index}": 1 for index in range(MAX_FAILURES + 1)}
    inputs["claude_visibility_status"]["failed_codes"] = oversized
    inputs["claude_visibility_status"]["counts"]["claude_failed"] = 1
    evidence = build_session_health_evidence(**inputs)
    work = evidence["queues"]["claude_visibility"]["work_state"]
    assert work["state"] == "unknown"
    assert work["code"] == "failure_evidence_overflow"
    assert len(evidence["failures"]) <= MAX_FAILURES
    assert all(code not in json.dumps(evidence) for code in oversized)


def test_oversized_execution_blocker_sequence_is_constant_unknown() -> None:
    inputs = healthy_inputs()
    blockers = [f"private_blocker_{index}" for index in range(MAX_FAILURES + 1)]
    inputs["sidebar_status"]["execution_blockers"] = blockers
    queue = build_session_health_evidence(**inputs)["queues"][
        "sidebar_registration"
    ]
    assert queue["work_state"]["state"] == "unknown"
    assert queue["work_state"]["code"] == "failure_evidence_overflow"
    assert all(code not in json.dumps(queue) for code in blockers)


@pytest.mark.parametrize(
    ("code", "expected_capabilities"),
    [
        (
            "codex_tool_unavailable",
            {"sidebar_registration", "sidebar_hydration"},
        ),
        (
            "bridge_temporarily_unavailable",
            {"sidebar_registration", "sidebar_hydration"},
        ),
        (
            "native_task_not_indexed",
            {"sidebar_registration", "sidebar_hydration"},
        ),
        (
            "marker_conflict",
            {
                "sidebar_registration",
                "sidebar_hydration",
                "claude_visibility",
            },
        ),
        (
            "source_identity_mismatch",
            {"sidebar_registration", "sidebar_hydration"},
        ),
        (
            "codex_thread_conflict",
            {"sidebar_registration", "sidebar_hydration"},
        ),
        (
            "broker_time_budget",
            {"sidebar_registration", "sidebar_hydration"},
        ),
    ],
)
def test_failure_registry_overlaps_keep_source_capability(
    code: str, expected_capabilities: set[str]
) -> None:
    for capability in expected_capabilities:
        inputs = healthy_inputs()
        if capability == "sidebar_registration":
            inputs["sidebar_status"]["recent_error_codes"] = [code]
        elif capability == "sidebar_hydration":
            inputs["hydration_status"]["recent_error_codes"] = [code]
        else:
            inputs["claude_visibility_status"]["retry_codes"] = {code: 1}
        failures = build_session_health_evidence(**inputs)["failures"]
        matching = [failure for failure in failures if failure["code"] == code]
        assert matching
        assert matching[0]["capability"] == capability


def test_provider_coordinator_and_catalog_timestamps_have_separate_ownership() -> None:
    invalid_health = healthy_inputs()
    invalid_health["health_observed_at"] = 999.0
    evidence = build_session_health_evidence(**invalid_health)
    for provider in ("claude", "codex"):
        value = evidence["catalog"]["providers"][provider]
        assert value["work_state"]["code"] == "invalid_observation_time"
        assert value["freshness"]["code"] == "invalid_observation_time"
        for fact_name in ("session_count", "degraded_count"):
            assert value[fact_name]["state"] == "healthy"
        for fact in value["backfill"].values():
            assert fact["code"] == "invalid_observation_time"
    hermes = evidence["catalog"]["providers"]["hermes"]
    assert hermes["work_state"]["state"] == "healthy"
    assert hermes["freshness"]["state"] == "healthy"
    assert all(
        fact["code"] == "not_applicable"
        for fact in hermes["backfill"].values()
    )
    assert evidence["catalog"]["aggregate"]["total_sessions"]["state"] == (
        "healthy"
    )
    assert evidence["mirrors"]["policy_mode"]["code"] == (
        "invalid_observation_time"
    )

    invalid_catalog = healthy_inputs()
    invalid_catalog["catalog_observed_at"] = 999.0
    evidence = build_session_health_evidence(**invalid_catalog)
    for provider in ("claude", "codex"):
        value = evidence["catalog"]["providers"][provider]
        assert value["work_state"]["state"] == "healthy"
        assert value["freshness"]["state"] == "healthy"
        assert value["session_count"]["code"] == "invalid_observation_time"
        assert value["degraded_count"]["code"] == "invalid_observation_time"
        for fact in value["backfill"].values():
            assert fact["state"] == "healthy"
    hermes = evidence["catalog"]["providers"]["hermes"]
    assert hermes["work_state"]["code"] == "invalid_observation_time"
    assert hermes["freshness"]["code"] == "invalid_observation_time"


def test_hydration_failed_count_uses_fixed_aggregate_not_recent_cause() -> None:
    inputs = healthy_inputs()
    inputs["hydration_status"]["counts"]["hydration_failed"] = 2
    inputs["hydration_status"]["recent_error_codes"] = ["marker_conflict"]
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_hydration"
    ]["work_state"]
    assert work["state"] == "error"
    assert work["code"] == "hydration_failed"


def test_hydration_recent_history_alone_never_selects_current_cause() -> None:
    inputs = healthy_inputs()
    inputs["hydration_status"]["recent_error_codes"] = ["marker_conflict"]
    work = build_session_health_evidence(**inputs)["queues"][
        "sidebar_hydration"
    ]["work_state"]
    assert work["state"] == "healthy"
    assert work["code"] == "work_state_readable"


def test_claude_unregistered_retry_code_is_unknown() -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["counts"]["claude_retry"] = 2
    inputs["claude_visibility_status"]["retry_codes"] = {
        "native_task_not_indexed": 2
    }
    work = build_session_health_evidence(**inputs)["queues"][
        "claude_visibility"
    ]["work_state"]
    assert work["state"] == "unknown"
    assert work["code"] == "unregistered_failure_code"


class HostileMapping(Mapping[str, object]):
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_non_exact_dict_mappings_are_rejected_at_pure_boundary() -> None:
    inputs = healthy_inputs()
    inputs["coordinator_health"] = HostileMapping(inputs["coordinator_health"])
    inputs["catalog_status"] = HostileMapping(inputs["catalog_status"])
    inputs["sidebar_status"] = HostileMapping(inputs["sidebar_status"])
    inputs["hydration_status"] = HostileMapping(inputs["hydration_status"])
    inputs["claude_visibility_status"] = HostileMapping(
        inputs["claude_visibility_status"]
    )
    evidence = build_session_health_evidence(**inputs)
    assert evidence["service"]["state"] == "unknown"
    assert evidence["catalog"]["aggregate"]["total_sessions"]["state"] == (
        "unknown"
    )
    for queue in evidence["queues"].values():
        assert queue["work_state"]["state"] == "unknown"


def test_hostile_dynamic_mapping_is_not_iterated() -> None:
    inputs = healthy_inputs()
    inputs["claude_visibility_status"]["counts"]["claude_failed"] = 1
    inputs["claude_visibility_status"]["failed_codes"] = HostileMapping(
        {"private_code": 1}
    )
    evidence = build_session_health_evidence(**inputs)
    work = evidence["queues"]["claude_visibility"]["work_state"]
    assert work["state"] == "unknown"
    assert work["code"] == "invalid_failure_evidence"
    assert "private_code" not in json.dumps(evidence)


def test_repair_lease_codes_are_registered_failures() -> None:
    """An unregistered reason degrades to 'unregistered_failure_code'.

    That is strictly more opaque than what it replaces, so naming the condition
    without registering it here would make the surface worse, not better.
    """

    from session_bridge.health import _classify_registered_codes

    assert _classify_registered_codes(
        ["reconciliation_repair_abandoned"], capability="claude_visibility"
    ) == ("error", "reconciliation_repair_abandoned")
    assert _classify_registered_codes(
        ["reconciliation_repair_active"], capability="claude_visibility"
    ) == ("error", "reconciliation_repair_active")


def test_every_status_fatal_code_is_registered_for_health() -> None:
    """The drift that caused this defect, pinned.

    Readers now derive the known-code set from one constant, but health keeps its
    own registry. A code added to the constant and missed here would degrade to
    'unregistered_failure_code' -- opaque in a new way rather than the old one.
    """

    from session_bridge.claude_visibility_codes import (
        CLAUDE_VISIBILITY_STATUS_FATAL_CODES,
    )
    from session_bridge.health import _FAILURE_REGISTRY

    unregistered = sorted(
        code
        for code in CLAUDE_VISIBILITY_STATUS_FATAL_CODES
        if ("claude_visibility", code) not in _FAILURE_REGISTRY
    )

    assert unregistered == []
