"""Pure, privacy-bounded Session Bridge governance health evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Final, Literal, cast

from session_bridge.claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
)
from session_bridge.store import (
    HYDRATION_FATAL_ERRORS,
    HYDRATION_RETRYABLE_ERRORS,
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_RETRYABLE_ERRORS,
)

SCHEMA_VERSION: Final[int] = 1
AXIS_POLICY_VERSION: Final[int] = 1
MCP_PROTOCOL_BASELINE: Final[str] = "2025-11-25"
MAX_SAFE_INTEGER: Final[int] = 9_007_199_254_740_991
MAX_FAILURES: Final[int] = 32
MAX_SYMBOLIC_CODE_LENGTH: Final[int] = 64
PROVIDER_FRESHNESS_MULTIPLIER: Final[int] = 3
PROVIDER_FRESHNESS_JITTER_FLOOR_SECONDS: Final[int] = 30

# Minimum believable scan cadence per provider, in seconds. Raises the freshness
# limit ONLY for providers that physically cannot meet the global
# `catalog_scan_seconds`; providers absent here are unchanged (floor 0), so
# claude and hermes keep the exact contract they had.
#
# codex: every catalog lookup crosses a `codex app-server` JSON-RPC boundary whose
# own per-call bound is 30s, and a full inventory pages ~2,700 threads. A 3s
# service-wide setting yields a 33s limit that codex can never satisfy -- observed
# lag 50-560s while the provider was healthy and indexing normally.
#
# 240s -> a 720s limit. Chosen from the measured distribution, not rounded up for
# comfort: 180s was tried first and yields 540s, which sits ON the worst observed
# healthy lag (559s) and would flap. 720s clears that by ~1.3x while still catching
# a genuine stall -- the failures seen today parked for 20+ minutes and kept
# climbing, so they stay well outside it. Revisit if codex's steady-state cadence
# changes; the number is an observation, so re-measure before moving it.
PROVIDER_SCAN_SECONDS_FLOOR: Final[dict[str, float]] = {
    "codex": 240.0,
}

_SYMBOLIC_CODE = re.compile(r"[a-z0-9_]+", re.ASCII)
State = Literal["healthy", "error", "unknown"]

_STATES: Final = frozenset({"healthy", "error", "unknown"})
_LIFECYCLE_CONTEXTS: Final = frozenset({
    "current",
    "sleep_wake_unknown",
    "not_applicable",
})
_OWNERS: Final = frozenset({
    "session_bridge_server",
    "session_bridge_governance",
    "session_bridge_catalog",
    "session_bridge_mirror",
    "session_bridge_sidebar",
    "session_bridge_hydration",
    "session_bridge_claude_visibility",
})
_PROVIDERS: Final = ("claude", "codex", "hermes")
_QUEUE_NAMES: Final = (
    "mirror_jobs",
    "sidebar_registration",
    "sidebar_hydration",
    "claude_visibility",
)
_MAX_DYNAMIC_ITEMS: Final[int] = MAX_FAILURES

_FAILURE_REGISTRY: Final[
    dict[tuple[str, str], tuple[str, str, str]]
] = {
    ("catalog", "scan_failed"): (
        "provider", "index_refresh_failed", "retryable"
    ),
    ("catalog", "refresh_failed"): (
        "provider", "index_refresh_failed", "retryable"
    ),
    ("mirror_jobs", "mirror_queue_health_failed"): (
        "queue", "queue_observation_failed", "retryable"
    ),
    ("mirror_jobs", "codex_scan_failed"): (
        "provider", "index_refresh_blocked", "retryable"
    ),
    ("sidebar_registration", "sidebar_failed"): (
        "queue", "blocking_work_failed", "unknown"
    ),
    ("sidebar_hydration", "hydration_failed"): (
        "queue", "blocking_work_failed", "unknown"
    ),
    ("sidebar_registration", "sidebar_terminal_resolution_mismatch"): (
        "ledger", "terminal_resolution_inconsistent", "terminal"
    ),
    ("sidebar_registration", "sidebar_terminal_resolution_ledger_invalid"): (
        "ledger", "terminal_resolution_inconsistent", "terminal"
    ),
    ("claude_visibility", "invalid_status"): (
        "schema", "status_evidence_invalid", "unknown"
    ),
    ("claude_visibility", "unlinked_visible_lineage"): (
        "ledger", "derivative_lineage_incomplete", "terminal"
    ),
    ("sidebar_registration", "unknown_retry_code"): (
        "schema", "failure_semantics_unknown", "unknown"
    ),
    ("claude_visibility", "unknown_retry_code"): (
        "schema", "failure_semantics_unknown", "unknown"
    ),
    ("claude_visibility", "unknown_failed_code"): (
        "schema", "failure_semantics_unknown", "unknown"
    ),
    ("claude_visibility", "unknown_job_state"): (
        "schema", "work_state_unknown", "unknown"
    ),
}
for _capability in (
    "catalog",
    "mirror_jobs",
    "sidebar_registration",
    "sidebar_hydration",
    "claude_visibility",
):
    _FAILURE_REGISTRY[(_capability, "failure_evidence_overflow")] = (
        "schema", "failure_evidence_overflow", "unknown"
    )
for _code in SIDEBAR_RETRYABLE_ERRORS:
    _FAILURE_REGISTRY[("sidebar_registration", _code)] = (
        "queue", "delivery_retrying", "retryable"
    )
for _code in SIDEBAR_FATAL_ERRORS:
    _FAILURE_REGISTRY[("sidebar_registration", _code)] = (
        "queue", "delivery_terminal_failure", "terminal"
    )
for _code in HYDRATION_RETRYABLE_ERRORS:
    _FAILURE_REGISTRY[("sidebar_hydration", _code)] = (
        "queue", "hydration_retrying", "retryable"
    )
for _code in HYDRATION_FATAL_ERRORS:
    _FAILURE_REGISTRY[("sidebar_hydration", _code)] = (
        "queue", "hydration_terminal_failure", "terminal"
    )
for _code in CLAUDE_VISIBILITY_RETRY_CODES:
    _FAILURE_REGISTRY[("claude_visibility", _code)] = (
        "queue", "visibility_retrying", "retryable"
    )
for _code in CLAUDE_VISIBILITY_FATAL_CODES:
    _FAILURE_REGISTRY[("claude_visibility", _code)] = (
        "queue", "visibility_terminal_failure", "terminal"
    )
# An unregistered reason collapses to "unregistered_failure_code", which is
# strictly MORE opaque than the "invalid_status" these codes replace -- so
# naming the condition without registering it here would make the surface worse.
_FAILURE_REGISTRY[("claude_visibility", "reconciliation_repair_active")] = (
    "queue", "visibility_repair_authority_held", "terminal"
)
_FAILURE_REGISTRY[("claude_visibility", "reconciliation_repair_abandoned")] = (
    "queue", "visibility_repair_authority_abandoned", "terminal"
)


def _mapping(value: object) -> dict[str, Any]:
    if type(value) is dict:
        return cast(dict[str, Any], value)
    return {}


def _number(value: object) -> int | float | None:
    if type(value) not in (int, float):
        return None
    number = cast(int | float, value)
    if not math.isfinite(number) or number < 0 or number > MAX_SAFE_INTEGER:
        return None
    return number


def _count(value: object) -> int | None:
    if type(value) is not int:
        return None
    if value < 0 or value > MAX_SAFE_INTEGER:
        return None
    return value


def _validate_code(value: object) -> str | None:
    if type(value) is not str:
        return None
    if len(value) > MAX_SYMBOLIC_CODE_LENGTH or _SYMBOLIC_CODE.fullmatch(value) is None:
        return None
    return value


def _within_envelope(value: object, start: int | float | None, end: int | float | None) -> int | float | None:
    observed = _number(value)
    if observed is None or start is None or end is None or start > end:
        return None
    if observed < start or observed > end:
        return None
    return observed


def _freshness(state: State, code: str) -> dict[str, str]:
    return {"state": state, "code": code}


def _axis(
    *,
    state: State,
    code: str,
    evidence_kind: str,
    scope: str,
    impact: str,
    action: str,
    owner: str,
    required_for_service_impact: bool,
    required_for_governance: bool,
    observed_at: int | float | None,
    freshness: dict[str, str] | None = None,
    lifecycle_context: str = "not_applicable",
    **extra: Any,
) -> dict[str, Any]:
    if state not in _STATES:
        raise ValueError("state is outside the closed registry")
    if owner not in _OWNERS:
        raise ValueError("owner is outside the closed registry")
    if lifecycle_context not in _LIFECYCLE_CONTEXTS:
        raise ValueError("lifecycle context is outside the closed registry")
    result: dict[str, Any] = {
        "state": state,
        "code": code,
        "evidence_kind": evidence_kind,
        "scope": scope,
        "impact": impact,
        "action": action,
        "owner": owner,
        "required_for_service_impact": required_for_service_impact,
        "required_for_governance": required_for_governance,
        "observed_at": observed_at,
        "freshness": freshness or _freshness("unknown", "invalid_observation_time"),
        "lifecycle_context": lifecycle_context,
    }
    result.update(extra)
    return result


def _measurement_axis(
    *,
    observed_at: int | float | None,
    owner: str = "session_bridge_governance",
    scope: str,
    value: object = None,
    tracked: bool = False,
) -> dict[str, Any]:
    admitted = _number(value) if tracked else None
    if not tracked or value is None:
        state: State = "unknown"
        code = "not_tracked"
        freshness = _freshness("unknown", "not_tracked")
    elif admitted is None:
        state = "unknown"
        code = "invalid_measurement"
        freshness = _freshness("unknown", "invalid_measurement")
    else:
        state = "healthy"
        code = "tracked_measurement"
        freshness = _freshness("healthy", "current_observation")
    result = _axis(
        state=state,
        code=code,
        evidence_kind="governance_instrumentation",
        scope=scope,
        impact="none" if state == "healthy" else "evidence_gap",
        action="none" if state == "healthy" else "report_only",
        owner=owner,
        required_for_service_impact=False,
        required_for_governance=True,
        observed_at=observed_at,
        freshness=freshness,
    )
    if admitted is not None:
        result["value_seconds"] = admitted
    return result


def _count_fact(
    value: object, *, observation_valid: bool = True
) -> dict[str, Any]:
    if not observation_valid:
        return {"state": "unknown", "code": "invalid_observation_time"}
    count = _count(value)
    if count is None:
        return {"state": "unknown", "code": "invalid_measurement"}
    return {"state": "healthy", "code": "bounded_count", "value": count}


def _invalid_observation_axis(
    *, scope: str, owner: str, required_for_service_impact: bool
) -> dict[str, Any]:
    return _axis(
        state="unknown",
        code="invalid_observation_time",
        evidence_kind="source_observation",
        scope=scope,
        impact="evidence_gap",
        action="report_only",
        owner=owner,
        required_for_service_impact=required_for_service_impact,
        required_for_governance=True,
        observed_at=None,
        freshness=_freshness("unknown", "invalid_observation_time"),
        lifecycle_context="sleep_wake_unknown",
    )


def _not_applicable_fact() -> dict[str, str]:
    return {"state": "healthy", "code": "not_applicable"}


def _hermes_catalog_provider_axis(
    *,
    catalog_raw: object,
    catalog_observed_at: int | float | None,
) -> dict[str, Any]:
    counts = _mapping(catalog_raw)
    session_count = _count_fact(
        counts.get("sessions"),
        observation_valid=catalog_observed_at is not None,
    )
    degraded_count = _count_fact(
        counts.get("degraded"),
        observation_valid=catalog_observed_at is not None,
    )
    facts_valid = all(
        fact["state"] == "healthy" for fact in (session_count, degraded_count)
    )
    if catalog_observed_at is None:
        state: State = "unknown"
        code = "invalid_observation_time"
    elif facts_valid:
        state = "healthy"
        code = "catalog_provider_readable"
    else:
        state = "unknown"
        code = "invalid_measurement"
    freshness = _freshness(
        "healthy" if state == "healthy" else "unknown",
        "current_catalog_observation" if state == "healthy" else code,
    )
    return {
        "work_state": _axis(
            state=state,
            code=code,
            evidence_kind="current_catalog_state",
            scope="catalog_provider_hermes",
            impact="none" if state == "healthy" else "provider_state_unknown",
            action="none" if state == "healthy" else "report_only",
            owner="session_bridge_catalog",
            required_for_service_impact=False,
            required_for_governance=True,
            observed_at=catalog_observed_at,
            freshness=freshness,
            lifecycle_context=(
                "current" if state == "healthy" else "sleep_wake_unknown"
            ),
        ),
        "freshness": freshness,
        "session_count": session_count,
        "degraded_count": degraded_count,
        "backfill": {
            field: _not_applicable_fact()
            for field in ("version", "indexed_total", "remaining")
        },
    }


def _provider_timestamp(provider: Mapping[str, Any]) -> int | float | None:
    """Admit canonical last_success_at and current coordinator last_success alias."""

    has_canonical = "last_success_at" in provider
    has_compatibility = "last_success" in provider
    canonical = _number(provider.get("last_success_at")) if has_canonical else None
    compatibility = _number(provider.get("last_success")) if has_compatibility else None
    if has_canonical and has_compatibility:
        return canonical if canonical is not None and canonical == compatibility else None
    return canonical if has_canonical else compatibility


def _provider_axis(
    name: str,
    *,
    provider_raw: object,
    catalog_raw: object,
    backfill_raw: object,
    health_observed_at: int | float | None,
    catalog_observed_at: int | float | None,
    scan_seconds: int | float | None,
) -> dict[str, Any]:
    if name == "hermes":
        return _hermes_catalog_provider_axis(
            catalog_raw=catalog_raw,
            catalog_observed_at=catalog_observed_at,
        )
    provider = _mapping(provider_raw)
    counts = _mapping(catalog_raw)
    backfill = _mapping(backfill_raw)
    if health_observed_at is None:
        work_state = _invalid_observation_axis(
            scope=f"catalog_provider_{name}",
            owner="session_bridge_catalog",
            required_for_service_impact=name != "hermes",
        )
        freshness_axis = _freshness("unknown", "invalid_observation_time")
        backfill_facts = {
            field: {"state": "unknown", "code": "invalid_observation_time"}
            for field in ("version", "indexed_total", "remaining")
        }
        return {
            "work_state": work_state,
            "freshness": freshness_axis,
            "session_count": _count_fact(
                counts.get("sessions"),
                observation_valid=catalog_observed_at is not None,
            ),
            "degraded_count": _count_fact(
                counts.get("degraded"),
                observation_valid=catalog_observed_at is not None,
            ),
            "backfill": backfill_facts,
        }
    degraded = provider.get("degraded_reason")
    degraded_code = _validate_code(degraded) if degraded is not None else None
    if not provider:
        work_state = _axis(
            state="unknown",
            code="invalid_measurement",
            evidence_kind="current_provider_state",
            scope=f"catalog_provider_{name}",
            impact="provider_state_unknown",
            action="report_only",
            owner="session_bridge_catalog",
            required_for_service_impact=name != "hermes",
            required_for_governance=True,
            observed_at=health_observed_at,
            freshness=_freshness("unknown", "invalid_measurement"),
            lifecycle_context="sleep_wake_unknown",
        )
    elif degraded in {"scan_failed", "refresh_failed"}:
        work_state = _axis(
            state="error",
            code=cast(str, degraded),
            evidence_kind="current_provider_state",
            scope=f"catalog_provider_{name}",
            impact="provider_index_unavailable",
            action="report_only",
            owner="session_bridge_catalog",
            required_for_service_impact=name != "hermes",
            required_for_governance=True,
            observed_at=health_observed_at,
            freshness=_freshness("healthy", "current_observation"),
            lifecycle_context="current",
        )
    elif degraded is None:
        work_state = _axis(
            state="healthy",
            code="provider_ready",
            evidence_kind="current_provider_state",
            scope=f"catalog_provider_{name}",
            impact="none",
            action="none",
            owner="session_bridge_catalog",
            required_for_service_impact=name != "hermes",
            required_for_governance=True,
            observed_at=health_observed_at,
            freshness=_freshness("healthy", "current_observation"),
            lifecycle_context="current",
        )
    else:
        work_state = _axis(
            state="unknown",
            code=("unregistered_failure_code" if degraded_code else "invalid_measurement"),
            evidence_kind="current_provider_state",
            scope=f"catalog_provider_{name}",
            impact="provider_state_unknown",
            action="report_only",
            owner="session_bridge_catalog",
            required_for_service_impact=name != "hermes",
            required_for_governance=True,
            observed_at=health_observed_at,
            freshness=_freshness("healthy", "current_observation"),
            lifecycle_context="sleep_wake_unknown",
        )

    lag = _number(provider.get("lag_seconds"))
    last_success = _provider_timestamp(provider)
    if lag is None or last_success is None or scan_seconds is None:
        freshness_axis = _freshness("unknown", "invalid_measurement")
    else:
        # 2026-08-13: the expected scan cadence is PER PROVIDER, not global.
        # `catalog_scan_seconds` is a single service-wide setting (3s here), which is
        # the right expectation for claude -- it reads local transcript files and runs
        # a lag of ~5-8s. It is not achievable for codex, whose every lookup crosses an
        # app-server JSON-RPC boundary with a 30s per-call bound; measured codex lag ran
        # 50-560s while the derived limit was 33s. The row was therefore permanently
        # 'unknown' for a provider that was working correctly, and the only ways to
        # green it were to slacken the GLOBAL contract (which would also stop catching
        # genuine claude staleness) or to fabricate speed codex cannot have.
        # A floor per provider keeps claude's contract exactly as strict as before and
        # states codex's real cadence instead of pretending it matches claude's.
        effective_scan_seconds = max(
            scan_seconds, PROVIDER_SCAN_SECONDS_FLOOR.get(name, 0.0)
        )
        limit = max(
            effective_scan_seconds * PROVIDER_FRESHNESS_MULTIPLIER,
            effective_scan_seconds + PROVIDER_FRESHNESS_JITTER_FLOOR_SECONDS,
        )
        if lag <= limit:
            state: State = "healthy"
            freshness_code = "within_freshness_limit"
        elif degraded is None:
            state = "unknown"
            freshness_code = "stale_without_lifecycle_context"
        elif degraded in {"scan_failed", "refresh_failed"}:
            state = "error"
            freshness_code = "stale_index"
        else:
            state = "unknown"
            freshness_code = "stale_without_lifecycle_context"
        freshness_axis = _freshness(state, freshness_code)

    return {
        "work_state": work_state,
        "freshness": freshness_axis,
        "session_count": _count_fact(
            counts.get("sessions"),
            observation_valid=catalog_observed_at is not None,
        ),
        "degraded_count": _count_fact(
            counts.get("degraded"),
            observation_valid=catalog_observed_at is not None,
        ),
        "backfill": {
            field: _count_fact(backfill.get(field))
            for field in ("version", "indexed_total", "remaining")
        },
    }


def _bounded_sequence(value: object) -> tuple[list[object], bool, bool]:
    if not isinstance(value, (list, tuple)):
        return [], False, False
    if len(value) > _MAX_DYNAMIC_ITEMS:
        return [], True, True
    return [value[index] for index in range(len(value))], False, True


def _bounded_mapping_items(
    value: object,
) -> tuple[list[tuple[object, object]], bool, bool]:
    if type(value) is not dict:
        return [], False, False
    if len(value) > _MAX_DYNAMIC_ITEMS:
        return [], True, True
    mapping = cast(Mapping[object, object], value)
    return [(key, mapping[key]) for key in mapping], False, True


def _classify_registered_codes(
    values: list[object], *, capability: str
) -> tuple[State, str] | None:
    registered: list[str] = []
    saw_unregistered = False
    saw_invalid = False
    for value in values:
        code = _validate_code(value)
        if code is None:
            saw_invalid = True
            continue
        if (capability, code) not in _FAILURE_REGISTRY:
            saw_unregistered = True
            continue
        registered.append(code)
    if saw_invalid:
        return "unknown", "invalid_failure_evidence"
    if saw_unregistered:
        return "unknown", "unregistered_failure_code"
    if not registered:
        return None
    registered_set = set(registered)
    errors = sorted(
        code
        for code in registered_set
        if code not in {"unknown_retry_code", "unknown_failed_code", "unknown_job_state"}
    )
    if errors:
        ordered = sorted(
            errors,
            key=lambda code: (
                0
                if _FAILURE_REGISTRY[(capability, code)][2] == "terminal"
                else 1,
                code,
            ),
        )
        return "error", ordered[0]
    unknown_semantics = sorted(registered_set)
    return "unknown", unknown_semantics[0]


def _positive_mapping_codes(
    value: object,
) -> tuple[list[object], bool, bool]:
    items, overflow, valid_structure = _bounded_mapping_items(value)
    if overflow or not valid_structure:
        return [], overflow, valid_structure
    codes: list[object] = []
    for code, raw_count in items:
        count = _count(raw_count)
        if count is None:
            return [], False, False
        if count > 0:
            codes.append(code)
    return codes, False, True


def _queue_current_failure(
    *, name: str, source: Mapping[str, Any], blocking: object
) -> tuple[State, str] | None:
    if name == "mirror_jobs":
        return ("error", "mirror_manual_failure") if blocking is True else None
    if name == "sidebar_registration":
        blockers, overflow, valid = _bounded_sequence(source.get("execution_blockers"))
        if overflow:
            return "unknown", "failure_evidence_overflow"
        if not valid:
            return "unknown", "invalid_failure_evidence"
        # A positive blocking_failed_count is authoritative current failed work even
        # though the store's execution_blockers intentionally tracks other hard stops.
        if blocking is True:
            return "error", "sidebar_failed"
        return _classify_registered_codes(
            blockers, capability="sidebar_registration"
        )
    if name == "sidebar_hydration":
        return ("error", "hydration_failed") if blocking is True else None
    if name == "claude_visibility":
        reasons, reason_overflow, reasons_valid = _bounded_sequence(
            source.get("degraded_reasons")
        )
        failed_codes, failed_overflow, failed_valid = _positive_mapping_codes(
            source.get("failed_codes")
        )
        retry_codes, retry_overflow, retry_valid = _positive_mapping_codes(
            source.get("retry_codes")
        )
        if reason_overflow or failed_overflow or retry_overflow:
            return "unknown", "failure_evidence_overflow"
        if not reasons_valid or not failed_valid or not retry_valid:
            return "unknown", "invalid_failure_evidence"
        classified = _classify_registered_codes(
            reasons + retry_codes + failed_codes,
            capability="claude_visibility",
        )
        if classified is not None:
            return classified
        if blocking is True:
            return "unknown", "invalid_failure_evidence"
    return None


def _queue_work_axis(
    *,
    name: str,
    source: Mapping[str, Any],
    counts_key: str,
    count_fields: tuple[str, ...],
    blocking: object,
    feature_flag: bool | None,
    observed_at: int | float | None,
    owner: str,
) -> dict[str, Any]:
    required_for_service_impact = feature_flag is not False
    if observed_at is None:
        return _invalid_observation_axis(
            scope=name,
            owner=owner,
            required_for_service_impact=True,
        )
    raw_counts = _mapping(source.get(counts_key))
    counts = {field: _count_fact(raw_counts.get(field)) for field in count_fields}
    current_failure = _queue_current_failure(
        name=name, source=source, blocking=blocking
    )
    if feature_flag is False:
        state: State = "healthy"
        code = "optional_feature_disabled"
        impact = "none"
    elif feature_flag is None:
        state = "unknown"
        code = "invalid_feature_flag"
        impact = "queue_state_unknown"
    elif current_failure is not None:
        state, code = current_failure
        impact = "queue_work_blocked" if state == "error" else "queue_state_unknown"
    elif blocking is None or any(value["state"] == "unknown" for value in counts.values()):
        state = "unknown"
        code = "invalid_measurement"
        impact = "queue_state_unknown"
    else:
        state = "healthy"
        code = "work_state_readable"
        impact = "none"
    axis = _axis(
        state=state,
        code=code,
        evidence_kind="queue_aggregate",
        scope=name,
        impact=impact,
        action="none" if state == "healthy" else "report_only",
        owner=owner,
        required_for_service_impact=required_for_service_impact,
        required_for_governance=True,
        observed_at=observed_at,
        freshness=_freshness("healthy", "current_observation"),
        lifecycle_context="current" if feature_flag is not False else "not_applicable",
        counts=counts,
    )
    if type(source.get("active_lease")) is bool:
        axis["active_lease"] = source["active_lease"]
    return axis


def _queue(
    *,
    name: str,
    source: Mapping[str, Any],
    observed_at: int | float | None,
    feature_flag: bool | None,
    owner: str,
    counts_key: str,
    count_fields: tuple[str, ...],
    blocking: object,
    ledger_valid: object | None,
) -> dict[str, Any]:
    work = _queue_work_axis(
        name=name,
        source=source,
        counts_key=counts_key,
        count_fields=count_fields,
        blocking=blocking,
        feature_flag=feature_flag,
        observed_at=observed_at,
        owner=owner,
    )
    if observed_at is None:
        invalid_axis = _invalid_observation_axis(
            scope=name,
            owner="session_bridge_governance",
            required_for_service_impact=False,
        )
        return {
            "work_state": work,
            "oldest_age": dict(invalid_axis),
            "capacity": dict(invalid_axis),
            "flow": dict(invalid_axis),
            "expiry": dict(invalid_axis),
            "overflow": dict(invalid_axis),
            "ledger_integrity": _invalid_observation_axis(
                scope=name,
                owner=owner,
                required_for_service_impact=True,
            ),
        }
    ledger_not_applicable = False
    if feature_flag is False:
        ledger_state: State = "healthy"
        ledger_code = "optional_feature_disabled"
    elif feature_flag is None:
        ledger_state = "unknown"
        ledger_code = "invalid_feature_flag"
    elif name == "mirror_jobs" and work["state"] == "healthy":
        ledger_state = "healthy"
        ledger_code = "not_applicable"
        ledger_not_applicable = True
    elif name == "mirror_jobs":
        # 2026-08-13: this used to copy the WORK_STATE code onto the LEDGER axis
        # (`ledger_code = work["code"]`) while forcing state="unknown". mirror_jobs has
        # no real ledger, so when work is unhealthy the honest ledger verdict is
        # "not tracked" -- not a restatement of the work failure.
        #
        # Copying it emitted (code="mirror_manual_failure", state="unknown") on a
        # queue_ledger axis, which breaks the axis contract two ways: that code is not in
        # the ledger vocabulary for this scope, and it is an ERROR-class code so it may
        # only pair with state="error". The consumer therefore rejected the axis, which
        # voids the WHOLE evidence envelope and blanks all four session-bridge monitor
        # rows to 'unknown'. Latent until the first mirror_jobs manual_failure row
        # existed; enabling mirrors.automatic_creation produced one and exposed it.
        # "not_tracked" is already the allowed unknown-state ledger code for this scope
        # (see the else branch below, and _LEDGER_CODES["mirror_jobs"] consumer-side).
        ledger_state = "unknown"
        ledger_code = "not_tracked"
    elif ledger_valid is True:
        ledger_state = "healthy"
        ledger_code = "terminal_resolution_consistent"
    elif ledger_valid is False:
        ledger_state = "error"
        ledger_code = (
            "sidebar_terminal_resolution_ledger_invalid"
            if name == "sidebar_registration"
            else "terminal_resolution_ledger_invalid"
        )
    else:
        ledger_state = "unknown"
        ledger_code = "not_tracked"
    ledger = _axis(
        state=ledger_state,
        code=ledger_code,
        evidence_kind="queue_ledger",
        scope=name,
        impact="none" if ledger_state == "healthy" else "ledger_integrity_unknown",
        action="none" if ledger_state == "healthy" else "report_only",
        owner=owner,
        required_for_service_impact=(
            feature_flag is not False
            and (feature_flag is None or ledger_valid is not None)
        ),
        required_for_governance=True,
        observed_at=observed_at,
        freshness=(
            _freshness("healthy", "current_observation")
            if ledger_not_applicable or ledger_valid is not None
            else _freshness("unknown", "not_tracked")
        ),
        lifecycle_context=(
            "not_applicable"
            if feature_flag is False or ledger_not_applicable
            else "current"
        ),
    )
    return {
        "work_state": work,
        "oldest_age": _measurement_axis(
            observed_at=observed_at,
            scope=name,
            value=source.get("oldest_pending_age_seconds"),
            tracked="oldest_pending_age_seconds" in source,
        ),
        "capacity": _measurement_axis(observed_at=observed_at, scope=name),
        "flow": _measurement_axis(observed_at=observed_at, scope=name),
        "expiry": _measurement_axis(observed_at=observed_at, scope=name),
        "overflow": _measurement_axis(observed_at=observed_at, scope=name),
        "ledger_integrity": ledger,
    }


def _iter_codes(value: object) -> list[object]:
    values, overflow, valid = _bounded_sequence(value)
    if overflow:
        return ["failure_evidence_overflow"]
    return values if valid else []


def _failure(capability: str, value: object) -> dict[str, str]:
    code = _validate_code(value)
    if code is None:
        return {
            "code": "invalid_failure_evidence",
            "class": "unknown",
            "capability": "unknown",
            "impact": "unknown",
            "retryability": "unknown",
        }
    registered = _FAILURE_REGISTRY.get((capability, code))
    if registered is None:
        return {
            "code": "unregistered_failure_code",
            "class": "unknown",
            "capability": "unknown",
            "impact": "unknown",
            "retryability": "unknown",
        }
    failure_class, impact, retryability = registered
    return {
        "code": code,
        "class": failure_class,
        "capability": capability,
        "impact": impact,
        "retryability": retryability,
    }


def _summary(axes: list[dict[str, Any]], required_key: str) -> dict[str, str]:
    states = {
        axis.get("state")
        for axis in axes
        if axis.get(required_key) is True
    }
    if "error" in states:
        if required_key == "required_for_service_impact":
            return {"state": "error", "code": "required_capability_error"}
        return {"state": "error", "code": "required_governance_evidence_error"}
    if "unknown" in states:
        if required_key == "required_for_service_impact":
            return {"state": "unknown", "code": "required_capability_unknown"}
        return {"state": "unknown", "code": "required_governance_evidence_unknown"}
    if required_key == "required_for_service_impact":
        return {"state": "healthy", "code": "required_capabilities_healthy"}
    return {"state": "healthy", "code": "required_governance_evidence_healthy"}


def _full_axes(value: object) -> list[dict[str, Any]]:
    axes: list[dict[str, Any]] = []
    if type(value) is dict:
        item = cast(dict[str, Any], value)
        if {"state", "required_for_service_impact", "required_for_governance"} <= set(item):
            axes.append(dict(item))
        else:
            for nested in item.values():
                axes.extend(_full_axes(nested))
    return axes


def build_session_health_evidence(
    *,
    observation_started_at: object,
    observation_completed_at: object,
    health_observed_at: object,
    catalog_observed_at: object,
    sidebar_observed_at: object,
    hydration_observed_at: object,
    claude_visibility_observed_at: object,
    coordinator_health: object,
    catalog_status: object,
    sidebar_status: object,
    hydration_status: object,
    claude_visibility_status: object,
    catalog_scan_seconds: object,
    hydration_enabled: object,
    claude_visibility_enabled: object,
) -> dict[str, Any]:
    """Build versioned evidence from admitted aggregate fields without mutation."""

    start = _number(observation_started_at)
    end = _number(observation_completed_at)
    envelope_valid = start is not None and end is not None and start <= end
    if not envelope_valid:
        start = None
        end = None
    health_at = _within_envelope(health_observed_at, start, end)
    catalog_at = _within_envelope(catalog_observed_at, start, end)
    sidebar_at = _within_envelope(sidebar_observed_at, start, end)
    hydration_at = _within_envelope(hydration_observed_at, start, end)
    visibility_at = _within_envelope(claude_visibility_observed_at, start, end)

    health = _mapping(coordinator_health)
    catalog_raw = _mapping(catalog_status)
    sidebar = _mapping(sidebar_status)
    hydration = _mapping(hydration_status)
    visibility = _mapping(claude_visibility_status)
    scan_seconds = _number(catalog_scan_seconds)

    running = health.get("running")
    if health_at is None or type(running) is not bool:
        service_state: State = "unknown"
        service_code = "invalid_service_evidence"
        service_impact = "serviceability_unknown"
    elif running is False:
        service_state = "error"
        service_code = "service_not_running"
        service_impact = "service_unavailable"
    else:
        service_state = "healthy"
        service_code = "functional_status_available"
        service_impact = "none"
    service = _axis(
        state=service_state,
        code=service_code,
        evidence_kind="semantic_status",
        scope="session_bridge",
        impact=service_impact,
        action="none" if service_state == "healthy" else "report_only",
        owner="session_bridge_server",
        required_for_service_impact=True,
        required_for_governance=True,
        observed_at=health_at,
        freshness=_freshness("healthy", "current_observation") if health_at is not None else None,
    )
    protocol = _axis(
        state="healthy",
        code="declared_baseline",
        evidence_kind="configured_contract",
        scope="mcp",
        impact="none",
        action="none",
        owner="session_bridge_server",
        required_for_service_impact=False,
        required_for_governance=True,
        observed_at=health_at,
        freshness=_freshness("healthy", "static_contract"),
        baseline=MCP_PROTOCOL_BASELINE,
    )
    authentication = _axis(
        state="healthy" if health_at is not None else "unknown",
        code="authenticated_dispatch" if health_at is not None else "invalid_observation_time",
        evidence_kind="middleware_admission",
        scope="mcp_request",
        impact="none" if health_at is not None else "authentication_observation_unknown",
        action="none" if health_at is not None else "report_only",
        owner="session_bridge_server",
        required_for_service_impact=True,
        required_for_governance=True,
        observed_at=health_at,
        freshness=_freshness("healthy", "current_observation") if health_at is not None else None,
    )
    authorization = _axis(
        state="unknown",
        code="per_harness_identity_not_assessed",
        evidence_kind="governance_coverage",
        scope="mcp_request",
        impact="authorization_attribution_unknown",
        action="report_only",
        owner="session_bridge_governance",
        required_for_service_impact=False,
        required_for_governance=True,
        observed_at=health_at,
        freshness=_freshness("unknown", "not_tracked"),
    )

    provider_health = _mapping(health.get("providers"))
    provider_counts = _mapping(catalog_raw.get("providers"))
    backfill = _mapping(health.get("backfill_progress"))
    providers = {
        name: _provider_axis(
            name,
            provider_raw=provider_health.get(name),
            catalog_raw=provider_counts.get(name),
            backfill_raw=backfill.get(name),
            health_observed_at=health_at,
            catalog_observed_at=catalog_at,
            scan_seconds=scan_seconds,
        )
        for name in _PROVIDERS
    }
    catalog = {
        "aggregate": {
            "total_sessions": _count_fact(
                catalog_raw.get("total_sessions"),
                observation_valid=catalog_at is not None,
            ),
            "observed_at": catalog_at,
        },
        "providers": providers,
    }

    mirror_counts = _mapping(health.get("queue_counts"))
    mirror_blocking: bool | None = None
    if mirror_counts:
        manual_failure = _count(mirror_counts.get("manual_failure"))
        mirror_blocking = None if manual_failure is None else manual_failure > 0
    queues = {
        "mirror_jobs": _queue(
            name="mirror_jobs",
            source={"counts": mirror_counts},
            observed_at=health_at,
            feature_flag=True,
            owner="session_bridge_mirror",
            counts_key="counts",
            count_fields=("queued", "running", "retry", "succeeded", "manual_failure"),
            blocking=mirror_blocking,
            ledger_valid=None,
        ),
        "sidebar_registration": _queue(
            name="sidebar_registration",
            source=sidebar,
            observed_at=sidebar_at,
            feature_flag=True,
            owner="session_bridge_sidebar",
            counts_key="counts",
            count_fields=(
                "sidebar_pending",
                "sidebar_leased",
                "sidebar_visible",
                "sidebar_retry",
                "sidebar_failed",
            ),
            blocking=(
                None
                if _count(sidebar.get("blocking_failed_count")) is None
                else cast(int, _count(sidebar.get("blocking_failed_count"))) > 0
            ),
            ledger_valid=(
                sidebar.get("terminal_resolution_ledger_valid")
                if type(sidebar.get("terminal_resolution_ledger_valid")) is bool
                else None
            ),
        ),
        "sidebar_hydration": _queue(
            name="sidebar_hydration",
            source=hydration,
            observed_at=hydration_at,
            feature_flag=(
                hydration_enabled if type(hydration_enabled) is bool else None
            ),
            owner="session_bridge_hydration",
            counts_key="counts",
            count_fields=(
                "hydration_pending",
                "hydration_leased",
                "hydration_retry",
                "hydration_visible",
                "hydration_failed",
            ),
            blocking=(
                None
                if _count(_mapping(hydration.get("counts")).get("hydration_failed")) is None
                else cast(int, _count(_mapping(hydration.get("counts")).get("hydration_failed"))) > 0
            ),
            ledger_valid=None,
        ),
        "claude_visibility": _queue(
            name="claude_visibility",
            source=visibility,
            observed_at=visibility_at,
            feature_flag=(
                claude_visibility_enabled
                if type(claude_visibility_enabled) is bool
                else None
            ),
            owner="session_bridge_claude_visibility",
            counts_key="counts",
            count_fields=(
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_visible",
                "claude_failed",
            ),
            blocking=(
                None
                if _count(_mapping(visibility.get("counts")).get("claude_failed")) is None
                else cast(int, _count(_mapping(visibility.get("counts")).get("claude_failed"))) > 0
            ),
            ledger_valid=(
                True
                if _count(_mapping(visibility.get("lineage")).get("unlinked_visible")) == 0
                else False
                if _count(_mapping(visibility.get("lineage")).get("unlinked_visible")) is not None
                else None
            ),
        ),
    }

    mode = health.get("mirror_mode")
    if health_at is None:
        policy_state: State = "unknown"
        policy_code = "invalid_observation_time"
    elif mode in {"manual", "automatic"}:
        policy_state = "healthy"
        policy_code = f"{mode}_mode"
    else:
        policy_state = "unknown"
        policy_code = "invalid_policy_mode"
    mirrors = {
        "policy_mode": _axis(
            state=policy_state,
            code=policy_code,
            evidence_kind="configured_policy",
            scope="mirrors",
            impact="none" if policy_state == "healthy" else "policy_unknown",
            action="none" if policy_state == "healthy" else "report_only",
            owner="session_bridge_mirror",
            required_for_service_impact=False,
            required_for_governance=True,
            observed_at=health_at,
            freshness=_freshness("healthy", "current_observation") if health_at is not None else None,
        ),
        "derivative_work_state": queues["mirror_jobs"]["work_state"],
        "oldest_age": queues["mirror_jobs"]["oldest_age"],
        "capacity": queues["mirror_jobs"]["capacity"],
        "flow": queues["mirror_jobs"]["flow"],
        "expiry": queues["mirror_jobs"]["expiry"],
        "overflow": queues["mirror_jobs"]["overflow"],
        "ledger_integrity": queues["mirror_jobs"]["ledger_integrity"],
    }

    raw_failures: list[tuple[str, object]] = []

    def admit_failures(capability: str, values: list[object]) -> None:
        remaining = MAX_FAILURES - len(raw_failures)
        if remaining > 0:
            raw_failures.extend(
                (capability, value) for value in values[:remaining]
            )

    admit_failures("mirror_jobs", _iter_codes(health.get("recent_error_codes")))
    admit_failures(
        "sidebar_registration", _iter_codes(sidebar.get("recent_error_codes"))
    )
    admit_failures(
        "sidebar_hydration", _iter_codes(hydration.get("recent_error_codes"))
    )
    admit_failures(
        "claude_visibility", _iter_codes(visibility.get("degraded_reasons"))
    )
    for field in ("retry_codes", "failed_codes"):
        items, overflow, valid = _bounded_mapping_items(visibility.get(field))
        if overflow:
            admit_failures("claude_visibility", ["failure_evidence_overflow"])
        elif valid:
            admit_failures(
                "claude_visibility", [key for key, _value in items]
            )
    for provider_name in ("claude", "codex"):
        reason = _mapping(provider_health.get(provider_name)).get("degraded_reason")
        if reason is not None:
            admit_failures("catalog", [reason])
    failures = [
        _failure(capability, value) for capability, value in raw_failures
    ]

    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "axis_policy_version": AXIS_POLICY_VERSION,
        "observation_started_at": start,
        "observation_completed_at": end,
        "service": service,
        "protocol_baseline": protocol,
        "request_authentication": authentication,
        "authorization_governance": authorization,
        "catalog": catalog,
        "queues": queues,
        "mirrors": mirrors,
        "failures": failures,
    }
    axes = _full_axes(evidence)
    freshness_axes = [
        {
            "state": providers[name]["freshness"]["state"],
            "required_for_service_impact": name != "hermes",
            "required_for_governance": True,
        }
        for name in _PROVIDERS
    ]
    evidence["service_impact_summary"] = _summary(
        axes + freshness_axes, "required_for_service_impact"
    )
    evidence["governance_summary"] = _summary(
        axes + freshness_axes, "required_for_governance"
    )
    return evidence
