"""Complete incident settlement census and dispatcher-fence capability."""

from __future__ import annotations

import copy
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Iterable, Sequence

from jobflow_dispatch.quarantine_control import (
    QuarantineControlStore,
    default_control_store,
)
from jobflow_dispatch.store import ActivationStore, default_ledger_path

_LIVENESS = frozenset({"live", "dead", "unprovable"})
_OWNER_IDENTITY_FIELDS = frozenset({"process_id", "pid", "process_started_at"})


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _target_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("target_ids must be a sequence of identities")
    result = tuple(_identity(value, "target_id") for value in values)
    if len(set(result)) != len(result):
        raise ValueError("target_ids must be unique")
    return result


def _claim_row(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        row = asdict(value)
    elif isinstance(value, dict):
        row = copy.deepcopy(value)
    else:
        raise RuntimeError("dispatcher claim census contains a malformed row")
    required = {"message_key", "activity_id", "state", "claimed_at"}
    if required - set(row) or row["state"] != "claimed":
        raise RuntimeError("dispatcher claim census contains an incomplete row")
    return row


def _execution_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("execution census contains a malformed row")
    row = copy.deepcopy(value)
    if not {"id", "job_id", "status", "owner_liveness", "owner_liveness_evidence"} <= set(row):
        raise RuntimeError("execution census contains an incomplete row")
    if row["status"] not in {"claimed", "running"}:
        raise RuntimeError("execution census contains a terminal row")
    if row["owner_liveness"] not in _LIVENESS:
        raise RuntimeError("execution census contains an invalid liveness verdict")
    evidence = row["owner_liveness_evidence"]
    if not isinstance(evidence, dict) or _OWNER_IDENTITY_FIELDS - set(evidence):
        raise RuntimeError("execution owner identity evidence is incomplete")
    return row


class QuarantineSettlementControl:
    """Compose complete live sources and retain the exact dispatcher fence token."""

    def __init__(
        self,
        *,
        control_store: QuarantineControlStore | None = None,
        activation_store: ActivationStore | None = None,
        running_jobs: Callable[[], Iterable[str]] | None = None,
        execution_census: Callable[[], list[dict[str, Any]]] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        if running_jobs is None:
            from cron.scheduler import get_running_job_ids

            running_jobs = get_running_job_ids
        if execution_census is None:
            from cron.executions import cross_profile_nonterminal_execution_census

            execution_census = cross_profile_nonterminal_execution_census
        if now is None:
            from jobflow_dispatch.quarantine_control import _now

            now = _now
        self.control_store = control_store or default_control_store()
        self.activation_store = activation_store or ActivationStore(default_ledger_path())
        self._running_jobs = running_jobs
        self._execution_census = execution_census
        self._now = now
        self._barrier: Any = None
        self._barrier_token: str | None = None
        self._fence_token: str | None = None
        self._fence_generation: int | None = None

    def bind_dispatch_barrier(self, barrier: Any) -> None:
        """Bind this one-shot control to the exact retained scheduler barrier."""
        assert_held = getattr(barrier, "assert_held", None)
        if not callable(assert_held):
            raise RuntimeError("a held scheduler barrier capability is required")
        try:
            proof = assert_held()
        except Exception as exc:
            raise RuntimeError("scheduler barrier is no longer held") from exc
        if (
            not isinstance(proof, dict)
            or proof.get("schema_version") != 1
            or proof.get("complete") is not True
            or proof.get("coverage") != "due_row_capture_through_submission"
            or proof.get("barrier_token") != getattr(barrier, "token", None)
        ):
            raise RuntimeError("held scheduler barrier proof is incomplete")
        token = _identity(proof["barrier_token"], "barrier_token")
        if self._barrier is not None and self._barrier is not barrier:
            raise RuntimeError("settlement control is already bound to another barrier")
        self._barrier = barrier
        self._barrier_token = token

    def _assert_barrier_held(self) -> None:
        if self._barrier is None:
            raise RuntimeError("exact held scheduler barrier must be bound first")
        try:
            proof = self._barrier.assert_held()
        except Exception as exc:
            raise RuntimeError("scheduler barrier is no longer held") from exc
        if (
            not isinstance(proof, dict)
            or proof.get("barrier_token") != self._barrier_token
            or proof.get("complete") is not True
            or proof.get("coverage") != "due_row_capture_through_submission"
        ):
            raise RuntimeError("scheduler barrier is no longer held")

    def snapshot(self, target_ids: tuple[str, ...]) -> dict[str, Any]:
        targets = _target_ids(target_ids)
        running = sorted({_identity(value, "running job id") for value in self._running_jobs()})
        executions = [_execution_row(row) for row in self._execution_census()]
        claims = [_claim_row(row) for row in self.activation_store.claim_census()]
        return {
            "schema_version": 1,
            "complete": True,
            "source": "cron-execution-and-jobflow-activation-census",
            "queried_at": _identity(self._now(), "queried_at"),
            "running_job_ids": running,
            "executions": executions,
            "dispatcher_claims": claims,
            "target_ids": list(targets),
        }

    def fence_dispatcher_and_drain(
        self, *, required: bool, authorization_request_id: str
    ) -> dict[str, Any]:
        self._assert_barrier_held()
        transition = self.control_store.activate_fence(
            barrier_token=self._barrier_token,
            authorization_request_id=_identity(
                authorization_request_id, "authorization_request_id"
            ),
            required=required,
        )
        post = transition.get("post") if isinstance(transition, dict) else None
        if (
            not isinstance(post, dict)
            or post.get("fenced") is not True
            or not isinstance(post.get("generation"), int)
            or not isinstance(post.get("fence_token"), str)
            or not post["fence_token"]
        ):
            raise RuntimeError("dispatcher fence transition did not return exact live state")
        self._fence_token = post["fence_token"]
        self._fence_generation = post["generation"]
        return copy.deepcopy(transition)

    def inspect_dispatcher_fence(self) -> dict[str, Any]:
        """Read complete durable fence state without adopting it as authority."""
        self._assert_barrier_held()
        state = self.control_store.fence_state()
        wakes = list(self.control_store.pending_wakes())
        if not isinstance(state, dict):
            raise RuntimeError("durable dispatcher fence state is unavailable")
        if wakes:
            raise RuntimeError("durable dispatcher fence is not drained")
        proof = {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "verified_at": _identity(self._now(), "verified_at"),
            **copy.deepcopy(state),
            "claims": [],
            "wakes": wakes,
        }
        if (
            not isinstance(proof.get("fenced"), bool)
            or not isinstance(proof.get("generation"), int)
            or isinstance(proof["generation"], bool)
            or proof["generation"] < 0
            or not isinstance(proof.get("changed_at"), str)
            or not proof["changed_at"]
            or (
                proof["fenced"]
                and (
                    proof["generation"] < 1
                    or not isinstance(proof.get("fence_token"), str)
                    or not proof["fence_token"]
                    or not isinstance(
                        proof.get("authorization_request_id"), str
                    )
                    or not proof["authorization_request_id"]
                )
            )
            or (
                not proof["fenced"]
                and (
                    proof.get("fence_token") is not None
                    or proof.get("authorization_request_id") is not None
                )
            )
        ):
            raise RuntimeError("durable dispatcher fence state is incomplete")
        return proof

    def attach_active_fence(self, *, fence_token: str, generation: int) -> None:
        """Attach to exact independently verified durable fence state."""
        token = _identity(fence_token, "fence_token")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("fence generation must be a positive integer")
        try:
            proof = self.control_store.verify_fence(token)
        except Exception as exc:
            raise RuntimeError("existing dispatcher fence does not match live state") from exc
        if (
            not isinstance(proof, dict)
            or proof.get("schema_version") != 1
            or proof.get("complete") is not True
            or proof.get("fenced") is not True
            or proof.get("fence_token") != token
            or proof.get("generation") != generation
            or proof.get("claims") != []
            or proof.get("wakes") != []
        ):
            raise RuntimeError("existing dispatcher fence does not match live state")
        self._fence_token = token
        self._fence_generation = generation

    def release_active_fence(self) -> dict[str, Any]:
        """Release the exact live fence only under the retained scheduler barrier."""
        if self._fence_token is None:
            raise RuntimeError("an active dispatcher fence transition is required")
        self._assert_barrier_held()
        token = self._fence_token
        result = self.control_store.release_fence(
            barrier_token=self._barrier_token,
            expected_fence_token=token,
        )
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != 1
            or result.get("complete") is not True
            or result.get("fenced") is not False
            or result.get("generation") != self._fence_generation
            or result.get("released_fence_token") != token
        ):
            raise RuntimeError("dispatcher fence release did not return exact durable state")
        self._fence_token = None
        self._fence_generation = None
        return copy.deepcopy(result)

    def verify_active_fence(self) -> dict[str, Any]:
        if self._fence_token is None:
            raise RuntimeError("an active dispatcher fence transition is required")
        self._assert_barrier_held()
        proof = self.control_store.verify_fence(self._fence_token)
        if (
            not isinstance(proof, dict)
            or proof.get("schema_version") != 1
            or proof.get("complete") is not True
            or proof.get("fenced") is not True
            or proof.get("fence_token") != self._fence_token
            or proof.get("generation") != self._fence_generation
            or proof.get("claims") != []
            or proof.get("wakes") != []
        ):
            raise RuntimeError("independent dispatcher fence proof does not match live state")
        return copy.deepcopy(proof)

    def final_census(self, target_ids: tuple[str, ...]) -> dict[str, Any]:
        proof = self.verify_active_fence()
        snapshot = self.snapshot(target_ids)
        snapshot["fence_proof"] = proof
        return snapshot
