"""Complete settlement and dispatcher-fence control for incident repair."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from jobflow_dispatch.store import ActivationRow, WakeOutboxRow


def _claim(message_key="message-1", activity_id="cron.jobflow.matcher"):
    return ActivationRow(
        message_key=message_key,
        activity_id=activity_id,
        correlation_id="correlation-1",
        state="claimed",
        claimed_at=1000.0,
        completed_at=None,
        outcome=None,
    )


class _ActivationStore:
    def __init__(self, rows=(), outbox=()):
        self.rows = list(rows)
        self.outbox = list(outbox)

    def claim_census(self):
        return list(self.rows)

    def pending_wake_outbox(self):
        return list(self.outbox)


class _Barrier:
    token = "barrier-1"

    def assert_held(self):
        return {
            "schema_version": 1,
            "complete": True,
            "source": "kernel-byte-lock-dispatch-barrier",
            "barrier_token": self.token,
            "coverage": "due_row_capture_through_submission",
        }


class _ControlStore:
    def __init__(self):
        self.calls = []

    def activate_fence(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "queried_at": "2026-08-21T00:00:00Z",
            "required": kwargs["required"],
            "authorization_request_id": kwargs["authorization_request_id"],
            "pre": {"claims": [], "wakes": []},
            "post": {
                "fenced": True,
                "generation": 7,
                "fence_token": "fence-7",
                "claims": [],
                "wakes": [],
            },
        }

    def fence_state(self):
        return {
            "fenced": True,
            "generation": 7,
            "fence_token": "fence-7",
            "authorization_request_id": "auth-1",
            "changed_at": "2026-08-21T00:00:00Z",
        }

    def pending_wakes(self):
        return ()

    def verify_fence(self, token):
        assert token == "fence-7"
        return {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "verified_at": "2026-08-21T00:00:01Z",
            "fenced": True,
            "generation": 7,
            "fence_token": token,
            "authorization_request_id": "auth-1",
            "claims": [],
            "wakes": [],
            "proof_digest": "proof",
        }


def _settlement(**overrides):
    from jobflow_dispatch.quarantine_settlement import QuarantineSettlementControl

    values = {
        "control_store": _ControlStore(),
        "activation_store": _ActivationStore(),
        "running_jobs": lambda: frozenset({"job-b", "job-a"}),
        "execution_census": lambda: [
            {
                "id": "execution-1",
                "job_id": "target-1",
                "status": "running",
                "owner_liveness": "live",
                "owner_liveness_evidence": {
                    "process_id": "process-1",
                    "pid": 42,
                    "process_started_at": 100.0,
                },
            }
        ],
        "now": lambda: "2026-08-21T00:00:02Z",
    }
    values.update(overrides)
    return QuarantineSettlementControl(**values)


def test_snapshot_composes_every_complete_source_without_filtering():
    claim = _claim()
    outbox = WakeOutboxRow(
        message_key=claim.message_key,
        activity_id=claim.activity_id,
        outbox_token="outbox-1",
        job_id="job-a",
        caller="jobflow-dispatcher",
        reason="mailbox_message",
        requested_at=1000.0,
    )
    control = _settlement(
        activation_store=_ActivationStore([claim], [outbox])
    )

    snapshot = control.snapshot(("target-1", "target-2"))

    assert snapshot == {
        "schema_version": 1,
        "complete": True,
        "source": "cron-execution-and-jobflow-activation-census",
        "queried_at": "2026-08-21T00:00:02Z",
        "running_job_ids": ["job-a", "job-b"],
        "executions": [
            {
                "id": "execution-1",
                "job_id": "target-1",
                "status": "running",
                "owner_liveness": "live",
                "owner_liveness_evidence": {
                    "process_id": "process-1",
                    "pid": 42,
                    "process_started_at": 100.0,
                },
            }
        ],
        "dispatcher_claims": [asdict(claim)],
        "dispatcher_wake_outbox": [asdict(outbox)],
        "target_ids": ["target-1", "target-2"],
    }


def test_snapshot_rejects_malformed_dispatcher_wake_outbox():
    malformed = {
        "message_key": "message-1",
        "activity_id": "cron.jobflow.matcher",
        "job_id": "job-a",
    }

    with pytest.raises(RuntimeError, match="wake outbox"):
        _settlement(
            activation_store=_ActivationStore(outbox=[malformed])
        ).snapshot(("target-1",))


def test_snapshot_preserves_unprovable_execution_liveness():
    execution = {
        "id": "execution-1",
        "job_id": "target-1",
        "status": "claimed",
        "owner_liveness": "unprovable",
        "owner_liveness_evidence": {
            "process_id": "process-1",
            "pid": 42,
            "process_started_at": None,
            "reason": "recorded_process_start_time_missing",
        },
    }
    snapshot = _settlement(execution_census=lambda: [execution]).snapshot(("target-1",))
    assert snapshot["executions"] == [execution]


def test_snapshot_failure_propagates_instead_of_claiming_completeness():
    def fail():
        raise RuntimeError("injected execution census failure")

    with pytest.raises(RuntimeError, match="execution census failure"):
        _settlement(execution_census=fail).snapshot(("target-1",))


def test_snapshot_rejects_malformed_execution_liveness_evidence():
    malformed = {
        "id": "execution-1",
        "job_id": "target-1",
        "status": "running",
        "owner_liveness": "live",
        "owner_liveness_evidence": {"pid": 42},
    }
    with pytest.raises(RuntimeError, match="owner identity evidence"):
        _settlement(execution_census=lambda: [malformed]).snapshot(("target-1",))


@pytest.mark.parametrize("field", ["id", "job_id"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_snapshot_rejects_malformed_execution_identity(field, value):
    malformed = {
        "id": "execution-1",
        "job_id": "target-1",
        "status": "running",
        "owner_liveness": "live",
        "owner_liveness_evidence": {
            "process_id": "process-1",
            "pid": 42,
            "process_started_at": 100.0,
        },
    }
    malformed[field] = value

    with pytest.raises(RuntimeError, match="execution identity"):
        _settlement(execution_census=lambda: [malformed]).snapshot(("target-1",))


def test_fence_transition_requires_the_exact_held_barrier_capability():
    control_store = _ControlStore()
    control = _settlement(control_store=control_store)

    with pytest.raises(RuntimeError, match="held scheduler barrier"):
        control.fence_dispatcher_and_drain(
            required=True, authorization_request_id="auth-1"
        )

    control.bind_dispatch_barrier(_Barrier())
    transition = control.fence_dispatcher_and_drain(
        required=True, authorization_request_id="auth-1"
    )

    assert control_store.calls == [{
        "barrier_token": "barrier-1",
        "authorization_request_id": "auth-1",
        "required": True,
    }]
    assert transition["post"]["fence_token"] == "fence-7"


def test_bind_dispatch_barrier_refuses_a_token_without_held_capability():
    control = _settlement()
    with pytest.raises(RuntimeError, match="held scheduler barrier capability"):
        control.bind_dispatch_barrier("barrier-1")


def test_final_census_refuses_after_bound_barrier_is_released():
    class ReleasedBarrier(_Barrier):
        def __init__(self):
            self.held = True

        def assert_held(self):
            if not self.held:
                raise RuntimeError("released")
            return super().assert_held()

    barrier = ReleasedBarrier()
    control = _settlement()
    control.bind_dispatch_barrier(barrier)
    control.fence_dispatcher_and_drain(
        required=True, authorization_request_id="auth-1"
    )
    barrier.held = False

    with pytest.raises(RuntimeError, match="scheduler barrier is no longer held"):
        control.final_census(("target-1",))


def test_final_census_independently_verifies_exact_live_fence_token():
    control = _settlement()
    control.bind_dispatch_barrier(_Barrier())
    control.fence_dispatcher_and_drain(
        required=True, authorization_request_id="auth-1"
    )

    snapshot = control.final_census(("target-1",))

    assert snapshot["fence_proof"]["fence_token"] == "fence-7"
    assert snapshot["fence_proof"]["generation"] == 7
    assert snapshot["complete"] is True


def test_final_census_refuses_without_an_active_fence_transition():
    with pytest.raises(RuntimeError, match="active dispatcher fence"):
        _settlement().final_census(("target-1",))


def test_inspect_dispatcher_fence_reads_durable_state_without_prior_attachment():
    control = _settlement()
    control.bind_dispatch_barrier(_Barrier())

    proof = control.inspect_dispatcher_fence()

    assert proof == {
        "schema_version": 1,
        "complete": True,
        "source": "durable-jobflow-dispatch-control",
        "verified_at": "2026-08-21T00:00:02Z",
        "fenced": True,
        "generation": 7,
        "fence_token": "fence-7",
        "authorization_request_id": "auth-1",
        "changed_at": "2026-08-21T00:00:00Z",
        "claims": [],
        "wakes": [],
    }


def test_inspect_dispatcher_fence_preserves_inactive_state():
    class InactiveStore(_ControlStore):
        def fence_state(self):
            return {
                "fenced": False,
                "generation": 7,
                "fence_token": None,
                "authorization_request_id": None,
                "changed_at": "2026-08-21T00:00:00Z",
            }

    control = _settlement(control_store=InactiveStore())
    control.bind_dispatch_barrier(_Barrier())

    proof = control.inspect_dispatcher_fence()

    assert proof["fenced"] is False
    assert proof["fence_token"] is None
    assert proof["authorization_request_id"] is None
    assert proof["claims"] == []
    assert proof["wakes"] == []


def test_inspect_dispatcher_fence_refuses_pending_wakes():
    class WakingStore(_ControlStore):
        def pending_wakes(self):
            return ({"job_id": "job-1", "wake_token": "wake-1"},)

    control = _settlement(control_store=WakingStore())
    control.bind_dispatch_barrier(_Barrier())

    with pytest.raises(RuntimeError, match="not drained"):
        control.inspect_dispatcher_fence()


def test_attach_existing_fence_requires_exact_live_token_and_generation():
    control = _settlement()
    control.attach_active_fence(fence_token="fence-7", generation=7)
    control.bind_dispatch_barrier(_Barrier())

    assert control.final_census(("target-1",))["fence_proof"]["generation"] == 7

    with pytest.raises(RuntimeError, match="live state"):
        _settlement().attach_active_fence(fence_token="stale", generation=7)


def test_release_active_fence_requires_retained_barrier_and_exact_token():
    class ReleasingStore(_ControlStore):
        def release_fence(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "schema_version": 1,
                "complete": True,
                "source": "durable-jobflow-dispatch-control",
                "fenced": False,
                "generation": 7,
                "released_fence_token": kwargs["expected_fence_token"],
                "changed_at": "2026-08-21T00:00:03Z",
            }

    store = ReleasingStore()
    control = _settlement(control_store=store)
    control.bind_dispatch_barrier(_Barrier())
    control.attach_active_fence(fence_token="fence-7", generation=7)

    result = control.release_active_fence()

    assert result["fenced"] is False
    assert store.calls[-1] == {
        "barrier_token": "barrier-1",
        "expected_fence_token": "fence-7",
    }
    with pytest.raises(RuntimeError, match="active dispatcher fence"):
        control.final_census(("target-1",))
