"""Capacity isolation tests for durable webhook authority scopes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gateway.platforms.webhook_ledger as ledger_module
from gateway.platforms.webhook_auth import WebhookSignatureVerificationReceipt
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    OperationState,
    WebhookOperationLedger,
)


def _envelope(
    identity: str,
    *,
    trace_id: str,
    profile: str = "default",
    route_name: str = "route-a",
    provider: str = "svix",
    body_value: str | None = None,
) -> WebhookEnvelope:
    raw_body = json.dumps(
        {"event": "push", "value": body_value or identity},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if provider == "svix":
        headers = {"svix-id": identity}
    elif provider == "github":
        headers = {
            "X-GitHub-Delivery": identity,
            "X-GitHub-Event": "push",
        }
    else:  # pragma: no cover - this fixture intentionally supports two providers
        raise AssertionError(f"unsupported fixture provider: {provider}")
    route = WebhookRouteConfig.bind(
        route_name,
        {"provider": provider, "profile": profile},
        headers=headers,
        request_profile=profile,
    )
    receipt = WebhookSignatureVerificationReceipt._issue(route, raw_body, headers)
    return WebhookEnvelope.from_receipt(
        receipt,
        raw_body=raw_body,
        media_type="application/json",
        trace_id=trace_id,
    )


@pytest.mark.parametrize(
    ("dimension", "entrant_scope"),
    [
        ("route", {"route_name": "route-b"}),
        ("profile", {"profile": "tenant-b"}),
        ("provider", {"provider": "github"}),
    ],
)
def test_capacity_reserve_is_keyed_by_exact_scope_dimension(
    tmp_path: Path,
    dimension: str,
    entrant_scope: dict[str, str],
):
    ledger = WebhookOperationLedger(tmp_path / "state.db", max_records=4)
    base_scope = {
        "profile": "default",
        "route_name": "route-a",
        "provider": "svix",
    }

    for index in range(3):
        admitted = ledger.admit(
            _envelope(
                f"base-{index}",
                trace_id=f"base-trace-{index}",
                **base_scope,
            )
        )
        assert admitted.disposition is AdmitDisposition.ACCEPTED

    same_scope = ledger.admit(
        _envelope("same-overflow", trace_id="same-overflow-trace", **base_scope)
    )
    assert same_scope.disposition is AdmitDisposition.SATURATED

    distinct_scope = {**base_scope, **entrant_scope}
    reserved = ledger.admit(
        _envelope(
            f"reserved-{dimension}",
            trace_id=f"reserved-{dimension}-trace",
            **distinct_scope,
        )
    )
    assert reserved.disposition is AdmitDisposition.ACCEPTED
    assert reserved.authority is not None
    assert (
        reserved.authority.profile,
        reserved.authority.route,
        reserved.authority.provider,
    ) == (
        distinct_scope["profile"],
        distinct_scope["route_name"],
        distinct_scope["provider"],
    )

    assert ledger.count() == 4
    globally_full = ledger.admit(
        _envelope(
            f"global-overflow-{dimension}",
            trace_id=f"global-overflow-{dimension}-trace",
            profile="other-profile",
            route_name="other-route",
            provider="github",
        )
    )
    assert globally_full.disposition is AdmitDisposition.SATURATED
    assert ledger.count() == 4


def test_indeterminate_evidence_is_retained_but_excluded_from_live_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=1,
        terminal_retention_seconds=1,
    )
    unknown_envelope = _envelope(
        "unknown-effect",
        trace_id="unknown-effect-trace",
        body_value="same-authenticated-body",
    )
    unknown = ledger.admit(unknown_envelope)
    assert unknown.disposition is AdmitDisposition.ACCEPTED
    assert unknown.authority is not None
    assert ledger.mark_indeterminate(unknown.authority, "effect outcome unknown")

    clock["now"] = 10_000.0
    assert ledger.prune() == 0
    retained = ledger.lookup_session(unknown.authority.session_key)
    assert retained is not None
    assert retained.state is OperationState.INDETERMINATE
    assert retained.operation_id == "unknown-effect-trace"
    assert retained.body_sha256 == unknown_envelope.body_sha256

    replay = ledger.admit(
        _envelope(
            "unknown-effect",
            trace_id="unknown-effect-retry-trace",
            body_value="same-authenticated-body",
        )
    )
    assert replay.disposition is AdmitDisposition.INDETERMINATE
    assert replay.authority is not None
    assert replay.authority.operation_id == retained.operation_id

    live = ledger.admit(_envelope("new-live", trace_id="new-live-trace"))
    assert live.disposition is AdmitDisposition.ACCEPTED
    assert live.authority is not None
    assert ledger.count() == 2

    full = ledger.admit(_envelope("another-live", trace_id="another-live-trace"))
    assert full.disposition is AdmitDisposition.SATURATED
    still_retained = ledger.lookup_session(unknown.authority.session_key)
    assert still_retained is not None
    assert still_retained.state is OperationState.INDETERMINATE
