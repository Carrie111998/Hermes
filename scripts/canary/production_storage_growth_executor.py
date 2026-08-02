#!/usr/bin/env python3
"""Crash-safe executor for the exact production storage growth contract.

The transport is deliberately narrow.  It can observe the one bound resource,
issue the one idempotent provider resize request, and complete the one online
root growth.  It cannot accept a command, path, project, zone, instance, disk,
or arbitrary size from a caller.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_production_storage_growth as passkey
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract


JOURNAL_SCHEMA = "muncho-production-storage-growth-journal.v1"
RESULT_SCHEMA = "muncho-production-storage-growth-result.v1"

_JOURNAL_FIELDS = frozenset({
    "schema",
    "state",
    "plan_sha256",
    "authorization_bundle_sha256",
    "authorization_receipt_sha256",
    "provider_request_id",
    "idempotency_key_sha256",
    "started_at_unix",
    "completed_at_unix",
    "final_observation",
    "prior_journal_head_sha256",
    "journal_sha256",
})


class ExactProductionStorageTransport(Protocol):
    """Fixed-target adapter; implementations own all concrete commands."""

    def observe_exact_target(self) -> Mapping[str, Any]: ...

    def resize_exact_disk_once(
        self, *, provider_request_id: str
    ) -> Mapping[str, Any]: ...

    def grow_exact_root_online(
        self, *, idempotency_key_sha256: str
    ) -> Mapping[str, Any]: ...


class ProductionStorageExecutorError(RuntimeError):
    """Stable, secret-free execution failure."""


def _journal_unsigned(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return {name: item for name, item in value.items() if name != "journal_sha256"}


def _validate_journal(
    value: Any,
    *,
    plan: Mapping[str, Any],
    authorization_bundle: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _JOURNAL_FIELDS:
        raise ProductionStorageExecutorError("production_storage_journal_invalid")
    journal = copy.deepcopy(dict(value))
    bundle_sha = authorization_bundle["bundle_sha256"]
    receipt_sha = authorization_bundle["authorization_receipt"]["receipt_sha256"]
    state = journal.get("state")
    if (
        journal.get("schema") != JOURNAL_SCHEMA
        or state not in {"started", "completed"}
        or journal.get("plan_sha256") != plan["plan_sha256"]
        or journal.get("authorization_bundle_sha256") != bundle_sha
        or journal.get("authorization_receipt_sha256") != receipt_sha
        or journal.get("provider_request_id") != plan["provider_request_id"]
        or journal.get("idempotency_key_sha256") != plan["idempotency_key_sha256"]
        or type(journal.get("started_at_unix")) is not int
        or journal["started_at_unix"] <= 0
        or journal.get("prior_journal_head_sha256")
        != protocol.GENESIS_JOURNAL_HEAD_SHA256
        or journal.get("journal_sha256")
        != protocol.sha256_json(_journal_unsigned(journal))
    ):
        raise ProductionStorageExecutorError("production_storage_journal_invalid")
    if state == "started":
        if (
            journal["completed_at_unix"] is not None
            or journal["final_observation"] is not None
        ):
            raise ProductionStorageExecutorError("production_storage_journal_invalid")
    else:
        if (
            type(journal["completed_at_unix"]) is not int
            or journal["completed_at_unix"] < journal["started_at_unix"]
            or not isinstance(journal["final_observation"], Mapping)
        ):
            raise ProductionStorageExecutorError("production_storage_journal_invalid")
        final = contract.validate_observation(
            journal["final_observation"],
            now_unix=journal["completed_at_unix"],
            require_fresh=False,
        )
        try:
            final_state = contract.classify_observation(
                final,
                now_unix=journal["completed_at_unix"],
                plan=plan,
            )
        except contract.ProductionStorageGrowthError:
            raise ProductionStorageExecutorError(
                "production_storage_journal_invalid"
            ) from None
        if final_state != "target":
            raise ProductionStorageExecutorError("production_storage_journal_invalid")
    return journal


def _write_canonical_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = protocol.canonical_json_bytes(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_journal(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        decoded = protocol.decode_canonical_json(raw.strip())
    except (OSError, protocol.PasskeyV2ProtocolError):
        raise ProductionStorageExecutorError(
            "production_storage_journal_invalid"
        ) from None
    if not isinstance(decoded, Mapping):
        raise ProductionStorageExecutorError("production_storage_journal_invalid")
    return decoded


def _result(
    *, journal: Mapping[str, Any], recovered: bool, mutations: list[str]
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RESULT_SCHEMA,
        "state": journal["state"],
        "plan_sha256": journal["plan_sha256"],
        "journal_sha256": journal["journal_sha256"],
        "recovered_from_started_journal": recovered,
        "mutations_performed_this_attempt": mutations,
        "final_observation_sha256": (
            journal["final_observation"]["observation_sha256"]
            if journal["final_observation"] is not None
            else None
        ),
    }
    return {**unsigned, "result_sha256": protocol.sha256_json(unsigned)}


class ProductionStorageGrowthExecutor:
    """Execute or recover the single exact storage-growth transaction."""

    def __init__(
        self,
        *,
        state_root: Path,
        transport: ExactProductionStorageTransport,
        receipt_public_key: Ed25519PublicKey,
        runtime_binding: Mapping[str, Any],
        read_only_collector_sha256: str,
    ) -> None:
        try:
            checked_runtime = protocol.validate_runtime_binding(runtime_binding)
        except protocol.PasskeyV2ProtocolError:
            raise ProductionStorageExecutorError(
                "production_storage_executor_configuration_invalid"
            ) from None
        if (
            not isinstance(state_root, Path)
            or not isinstance(receipt_public_key, Ed25519PublicKey)
            or not isinstance(read_only_collector_sha256, str)
            or not callable(getattr(transport, "observe_exact_target", None))
            or not callable(getattr(transport, "resize_exact_disk_once", None))
            or not callable(getattr(transport, "grow_exact_root_online", None))
        ):
            raise ProductionStorageExecutorError(
                "production_storage_executor_configuration_invalid"
            )
        self._state_root = state_root
        self._transport = transport
        self._receipt_public_key = receipt_public_key
        self._runtime_binding = checked_runtime
        self._read_only_collector_sha256 = read_only_collector_sha256

    def _journal_path(self, plan: Mapping[str, Any]) -> Path:
        return self._state_root / f"{plan['plan_sha256']}.json"

    @staticmethod
    def _classify(
        observation: Any,
        *,
        now_unix: int,
        plan: Mapping[str, Any],
    ) -> str:
        try:
            return contract.classify_observation(
                observation, now_unix=now_unix, plan=plan
            )
        except contract.ProductionStorageGrowthError:
            raise ProductionStorageExecutorError(
                "production_storage_observation_invalid"
            ) from None

    def execute(
        self,
        *,
        growth_plan: Mapping[str, Any],
        authorization_bundle: Mapping[str, Any],
        now_unix: int,
    ) -> Mapping[str, Any]:
        plan = contract.validate_plan(growth_plan)
        if type(now_unix) is not int or now_unix <= 0:
            raise ProductionStorageExecutorError("production_storage_time_invalid")
        runtime = self._runtime_binding
        if (
            runtime["executor_release_sha"] != plan["release_revision"]
            or runtime["executor_plan_sha256"] != plan["plan_sha256"]
            or runtime["executor_binary_sha256"] != plan["executor_binary_sha256"]
            or runtime["mutation_wrapper_sha256"] != plan["mutation_wrapper_sha256"]
            or runtime["remote_transport_sha256"] != plan["remote_transport_sha256"]
            or self._read_only_collector_sha256 != plan["read_only_collector_sha256"]
        ):
            raise ProductionStorageExecutorError(
                "production_storage_runtime_binding_invalid"
            )
        path = self._journal_path(plan)
        existing = _read_journal(path)
        recovered = existing is not None
        try:
            bundle = passkey.validate_authorization_bundle(
                authorization_bundle,
                growth_plan=plan,
                receipt_public_key=self._receipt_public_key,
                now_unix=now_unix,
                require_current=existing is None,
            )
        except passkey.ProductionStoragePasskeyError:
            raise ProductionStorageExecutorError(
                "production_storage_authorization_invalid"
            ) from None
        if existing is not None:
            journal = _validate_journal(
                existing, plan=plan, authorization_bundle=bundle
            )
            if journal["state"] == "completed":
                return _result(journal=journal, recovered=True, mutations=[])
        else:
            # The first attempt must still be at the exact, owner-rendered
            # source preflight.  A partial/target disk without our durable
            # started receipt is not adopted as our mutation.
            initial_observation = self._transport.observe_exact_target()
            initial_state = self._classify(
                initial_observation, now_unix=now_unix, plan=plan
            )
            if initial_state != "source":
                raise ProductionStorageExecutorError(
                    "production_storage_unowned_partial_state"
                )
            journal_unsigned = {
                "schema": JOURNAL_SCHEMA,
                "state": "started",
                "plan_sha256": plan["plan_sha256"],
                "authorization_bundle_sha256": bundle["bundle_sha256"],
                "authorization_receipt_sha256": bundle["authorization_receipt"][
                    "receipt_sha256"
                ],
                "provider_request_id": plan["provider_request_id"],
                "idempotency_key_sha256": plan["idempotency_key_sha256"],
                "started_at_unix": now_unix,
                "completed_at_unix": None,
                "final_observation": None,
                "prior_journal_head_sha256": protocol.GENESIS_JOURNAL_HEAD_SHA256,
            }
            journal = {
                **journal_unsigned,
                "journal_sha256": protocol.sha256_json(journal_unsigned),
            }
            _write_canonical_atomic(path, journal)

        mutations: list[str] = []
        observation = (
            self._transport.observe_exact_target()
            if existing is not None
            else initial_observation
        )
        state = self._classify(observation, now_unix=now_unix, plan=plan)
        if state == "source":
            self._transport.resize_exact_disk_once(
                provider_request_id=plan["provider_request_id"]
            )
            mutations.append("provider_disk_resize_50_to_100")
            observation = self._transport.observe_exact_target()
            state = self._classify(observation, now_unix=now_unix, plan=plan)
            if state == "source":
                raise ProductionStorageExecutorError(
                    "production_storage_provider_resize_not_visible"
                )
        if state == "partial":
            self._transport.grow_exact_root_online(
                idempotency_key_sha256=plan["idempotency_key_sha256"]
            )
            mutations.append("online_partition_and_ext4_growth")
            observation = self._transport.observe_exact_target()
            state = self._classify(observation, now_unix=now_unix, plan=plan)
        if state != "target":
            raise ProductionStorageExecutorError(
                "production_storage_postflight_threshold_not_met"
            )
        final = contract.validate_observation(observation, now_unix=now_unix)
        completed_unsigned = {
            **_journal_unsigned(journal),
            "state": "completed",
            "completed_at_unix": now_unix,
            "final_observation": final,
        }
        completed = {
            **completed_unsigned,
            "journal_sha256": protocol.sha256_json(completed_unsigned),
        }
        _write_canonical_atomic(path, completed)
        return _result(journal=completed, recovered=recovered, mutations=mutations)


__all__ = [
    "ExactProductionStorageTransport",
    "JOURNAL_SCHEMA",
    "ProductionStorageExecutorError",
    "ProductionStorageGrowthExecutor",
    "RESULT_SCHEMA",
]
