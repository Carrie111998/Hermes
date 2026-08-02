#!/usr/bin/env python3
"""Crash-safe executor for the exact production storage growth contract.

The transport is deliberately narrow.  It can observe the one bound resource,
issue the one idempotent provider resize request, and complete the one online
root growth.  It cannot accept a command, path, project, zone, instance, disk,
or arbitrary size from a caller.
"""

from __future__ import annotations

import copy
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_production_storage_growth as passkey
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract


JOURNAL_SCHEMA = "muncho-production-storage-growth-journal.v1"
RESULT_SCHEMA = "muncho-production-storage-growth-result.v1"
EVENT_SCHEMA = "muncho-production-storage-growth-event.v1"
PRODUCTION_STATE_ROOT = Path("/var/lib/muncho-production-storage-growth")

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
_EVENT_FIELDS = frozenset({
    "schema",
    "sequence",
    "event_kind",
    "plan_sha256",
    "authorization_receipt_sha256",
    "journal_state",
    "observation_sha256",
    "failure_code",
    "occurred_at_unix",
    "prior_event_head_sha256",
    "event_head_sha256",
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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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
        != authorization_bundle["authorization_receipt"]["prior_journal_head_sha256"]
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


def _write_event_log_atomic(
    path: Path,
    events: list[Mapping[str, Any]],
) -> None:
    """Publish a complete verified event prefix without mutating its inode.

    A process or power loss while the temporary file is being written leaves
    the last published event prefix intact.  The new prefix becomes visible in
    one rename only after its bytes are durable; the containing directory is
    then fsynced so the publication itself is durable as well.
    """

    if not events:
        raise ProductionStorageExecutorError(
            "production_storage_event_log_write_failed"
        )
    payload = b"".join(
        protocol.canonical_json_bytes(event) + b"\n" for event in events
    )
    fd: int | None = None
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.publication.",
            dir=path.parent,
        )
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short event publication write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
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
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise protocol.PasskeyV2ProtocolError(
                "production storage journal framing invalid"
            )
        decoded = protocol.decode_canonical_json(raw[:-1])
        if protocol.canonical_json_bytes(decoded) + b"\n" != raw:
            raise protocol.PasskeyV2ProtocolError(
                "production storage journal framing invalid"
            )
    except (OSError, protocol.PasskeyV2ProtocolError):
        raise ProductionStorageExecutorError(
            "production_storage_journal_invalid"
        ) from None
    if not isinstance(decoded, Mapping):
        raise ProductionStorageExecutorError("production_storage_journal_invalid")
    return decoded


def _stable_failure_code(error: BaseException) -> str:
    if (
        isinstance(error, ProductionStorageExecutorError)
        and len(error.args) == 1
        and isinstance(error.args[0], str)
        and error.args[0].startswith("production_storage_")
    ):
        return error.args[0]
    return "production_storage_transport_failure"


def _result(
    *,
    journal: Mapping[str, Any],
    recovered: bool,
    mutations: list[str],
    event_head_sha256: str,
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
        "event_head_sha256": event_head_sha256,
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
        runtime_artifact_attestor: Callable[[], Mapping[str, Any]],
        wall_clock: Callable[[], int],
        expected_state_uid: int = 0,
        expected_state_gid: int = 0,
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
            or not callable(runtime_artifact_attestor)
            or not callable(wall_clock)
            or type(expected_state_uid) is not int
            or expected_state_uid < 0
            or type(expected_state_gid) is not int
            or expected_state_gid < 0
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
        self._runtime_artifact_attestor = runtime_artifact_attestor
        self._wall_clock = wall_clock
        self._last_wall_time = 0
        self._expected_state_uid = expected_state_uid
        self._expected_state_gid = expected_state_gid
        if expected_state_uid == 0 and state_root != PRODUCTION_STATE_ROOT:
            raise ProductionStorageExecutorError(
                "production_storage_state_root_invalid"
            )

    def _journal_path(self, plan: Mapping[str, Any]) -> Path:
        return self._state_root / f"{plan['plan_sha256']}.json"

    def _event_path(self, plan: Mapping[str, Any]) -> Path:
        return self._state_root / f"{plan['plan_sha256']}.events.jsonl"

    def _validate_state_node(
        self,
        path: Path,
        *,
        directory: bool,
        mode: int,
        allow_missing: bool = False,
    ) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise ProductionStorageExecutorError(
                "production_storage_state_storage_invalid"
            ) from None
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_kind(info.st_mode)
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_uid != self._expected_state_uid
            or info.st_gid != self._expected_state_gid
            or not directory
            and info.st_nlink != 1
        ):
            raise ProductionStorageExecutorError(
                "production_storage_state_storage_invalid"
            )
        return info

    def _prepare_state_root(self) -> None:
        if self._expected_state_uid == 0:
            self._validate_state_node(self._state_root, directory=True, mode=0o700)
            return
        existing = self._validate_state_node(
            self._state_root,
            directory=True,
            mode=0o700,
            allow_missing=True,
        )
        if existing is None:
            self._state_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self._validate_state_node(self._state_root, directory=True, mode=0o700)

    def _lock(self) -> int:
        self._prepare_state_root()
        lock_path = self._state_root / ".execution.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != self._expected_state_uid
                or info.st_gid != self._expected_state_gid
                or info.st_nlink != 1
            ):
                raise OSError("unsafe lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except OSError:
            try:
                os.close(fd)
            except (OSError, UnboundLocalError):
                pass
            raise ProductionStorageExecutorError(
                "production_storage_state_storage_invalid"
            ) from None

    def _read_journal_secure(self, path: Path) -> Mapping[str, Any] | None:
        self._validate_state_node(path, directory=False, mode=0o600, allow_missing=True)
        return _read_journal(path)

    def _write_journal_secure(self, path: Path, value: Mapping[str, Any]) -> None:
        self._validate_state_node(path, directory=False, mode=0o600, allow_missing=True)
        _write_canonical_atomic(path, value)
        self._validate_state_node(path, directory=False, mode=0o600)

    def _read_events(
        self,
        path: Path,
        *,
        plan: Mapping[str, Any],
        authorization_bundle: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        self._validate_state_node(path, directory=False, mode=0o600, allow_missing=True)
        if not path.exists():
            return []
        try:
            payload = path.read_bytes()
        except OSError:
            raise ProductionStorageExecutorError(
                "production_storage_event_log_invalid"
            ) from None
        if not payload or not payload.endswith(b"\n") or b"\n\n" in payload:
            raise ProductionStorageExecutorError(
                "production_storage_event_log_invalid"
            )
        lines = payload[:-1].split(b"\n")
        events: list[Mapping[str, Any]] = []
        prior = authorization_bundle["authorization_receipt"][
            "prior_journal_head_sha256"
        ]
        for index, raw in enumerate(lines, start=1):
            try:
                decoded = protocol.decode_canonical_json(raw)
            except protocol.PasskeyV2ProtocolError:
                raise ProductionStorageExecutorError(
                    "production_storage_event_log_invalid"
                ) from None
            unsigned = (
                {
                    name: item
                    for name, item in decoded.items()
                    if name != "event_head_sha256"
                }
                if isinstance(decoded, Mapping)
                else {}
            )
            kind = decoded.get("event_kind") if isinstance(decoded, Mapping) else None
            journal_state = (
                decoded.get("journal_state") if isinstance(decoded, Mapping) else None
            )
            observation_sha = (
                decoded.get("observation_sha256")
                if isinstance(decoded, Mapping)
                else None
            )
            failure_code = (
                decoded.get("failure_code") if isinstance(decoded, Mapping) else None
            )
            semantic_values_valid = (
                kind == "execution_started"
                and journal_state == "started"
                and failure_code is None
                and (observation_sha is None or _is_sha256(observation_sha))
                or kind == "execution_failed"
                and journal_state == "started"
                and isinstance(failure_code, str)
                and failure_code.startswith("production_storage_")
                and (observation_sha is None or _is_sha256(observation_sha))
                or kind == "execution_completed"
                and journal_state == "completed"
                and failure_code is None
                and _is_sha256(observation_sha)
            )
            if (
                not isinstance(decoded, Mapping)
                or set(decoded) != _EVENT_FIELDS
                or decoded.get("schema") != EVENT_SCHEMA
                or decoded.get("sequence") != index
                or decoded.get("event_kind")
                not in {"execution_started", "execution_failed", "execution_completed"}
                or decoded.get("plan_sha256") != plan["plan_sha256"]
                or not semantic_values_valid
                or type(decoded.get("occurred_at_unix")) is not int
                or decoded["occurred_at_unix"] <= 0
                or any(
                    item["event_kind"] == "execution_completed"
                    for item in events
                )
                or decoded.get("authorization_receipt_sha256")
                != authorization_bundle["authorization_receipt"]["receipt_sha256"]
                or decoded.get("prior_event_head_sha256") != prior
                or decoded.get("event_head_sha256") != protocol.sha256_json(unsigned)
            ):
                raise ProductionStorageExecutorError(
                    "production_storage_event_log_invalid"
                )
            prior = decoded["event_head_sha256"]
            events.append(copy.deepcopy(dict(decoded)))
        return events

    def _append_event(
        self,
        path: Path,
        *,
        plan: Mapping[str, Any],
        authorization_bundle: Mapping[str, Any],
        events: list[Mapping[str, Any]],
        event_kind: str,
        journal_state: str,
        observation_sha256: str | None,
        failure_code: str | None,
    ) -> Mapping[str, Any]:
        prior = (
            events[-1]["event_head_sha256"]
            if events
            else authorization_bundle["authorization_receipt"][
                "prior_journal_head_sha256"
            ]
        )
        unsigned = {
            "schema": EVENT_SCHEMA,
            "sequence": len(events) + 1,
            "event_kind": event_kind,
            "plan_sha256": plan["plan_sha256"],
            "authorization_receipt_sha256": authorization_bundle[
                "authorization_receipt"
            ]["receipt_sha256"],
            "journal_state": journal_state,
            "observation_sha256": observation_sha256,
            "failure_code": failure_code,
            "occurred_at_unix": self._now(),
            "prior_event_head_sha256": prior,
        }
        event = {**unsigned, "event_head_sha256": protocol.sha256_json(unsigned)}
        try:
            self._validate_state_node(
                path,
                directory=False,
                mode=0o600,
                allow_missing=True,
            )
            _write_event_log_atomic(path, [*events, event])
        except (OSError, ProductionStorageExecutorError):
            raise ProductionStorageExecutorError(
                "production_storage_event_log_write_failed"
            ) from None
        self._validate_state_node(path, directory=False, mode=0o600)
        events.append(event)
        return event

    def _now(self) -> int:
        value = self._wall_clock()
        if type(value) is not int or value <= 0 or value < self._last_wall_time:
            raise ProductionStorageExecutorError(
                "production_storage_wall_clock_invalid"
            )
        self._last_wall_time = value
        return value

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
    ) -> Mapping[str, Any]:
        plan = contract.validate_plan(growth_plan)
        try:
            observed_artifacts = contract.validate_runtime_artifact_attestation(
                self._runtime_artifact_attestor()
            )
        except contract.ProductionStorageGrowthError:
            raise ProductionStorageExecutorError(
                "production_storage_runtime_artifact_observation_invalid"
            ) from None
        except Exception:
            raise ProductionStorageExecutorError(
                "production_storage_runtime_artifact_observation_invalid"
            ) from None
        runtime = self._runtime_binding
        if (
            observed_artifacts != plan["runtime_artifact_attestation"]
            or observed_artifacts["release_revision"]
            != plan["release_revision"]
            or runtime["executor_release_sha"] != plan["release_revision"]
            or runtime["executor_plan_sha256"] != plan["plan_sha256"]
            or runtime["executor_binary_sha256"] != plan["executor_binary_sha256"]
            or runtime["mutation_wrapper_sha256"] != plan["mutation_wrapper_sha256"]
            or runtime["remote_transport_sha256"] != plan["remote_transport_sha256"]
            or self._read_only_collector_sha256 != plan["read_only_collector_sha256"]
        ):
            raise ProductionStorageExecutorError(
                "production_storage_runtime_binding_invalid"
            )
        lock_fd = self._lock()
        try:
            return self._execute_locked(
                plan=plan,
                authorization_bundle=authorization_bundle,
            )
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _execute_locked(
        self,
        *,
        plan: Mapping[str, Any],
        authorization_bundle: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        attempt_now = self._now()
        path = self._journal_path(plan)
        event_path = self._event_path(plan)
        existing = self._read_journal_secure(path)
        recovered = existing is not None
        try:
            bundle = passkey.validate_authorization_bundle(
                authorization_bundle,
                growth_plan=plan,
                receipt_public_key=self._receipt_public_key,
                now_unix=attempt_now,
                require_current=existing is None,
            )
        except passkey.ProductionStoragePasskeyError:
            raise ProductionStorageExecutorError(
                "production_storage_authorization_invalid"
            ) from None
        events = self._read_events(event_path, plan=plan, authorization_bundle=bundle)
        if existing is not None:
            journal = _validate_journal(
                existing, plan=plan, authorization_bundle=bundle
            )
            if journal["state"] == "completed":
                if not events or events[-1]["event_kind"] != "execution_completed":
                    self._append_event(
                        event_path,
                        plan=plan,
                        authorization_bundle=bundle,
                        events=events,
                        event_kind="execution_completed",
                        journal_state="completed",
                        observation_sha256=journal["final_observation"][
                            "observation_sha256"
                        ],
                        failure_code=None,
                    )
                return _result(
                    journal=journal,
                    recovered=True,
                    mutations=[],
                    event_head_sha256=events[-1]["event_head_sha256"],
                )
        else:
            # The first attempt must still be at the exact, owner-rendered
            # source preflight.  A partial/target disk without our durable
            # started receipt is not adopted as our mutation.
            initial_observation = self._transport.observe_exact_target()
            observation_now = self._now()
            initial_state = self._classify(
                initial_observation, now_unix=observation_now, plan=plan
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
                "started_at_unix": self._now(),
                "completed_at_unix": None,
                "final_observation": None,
                "prior_journal_head_sha256": bundle["authorization_receipt"][
                    "prior_journal_head_sha256"
                ],
            }
            journal = {
                **journal_unsigned,
                "journal_sha256": protocol.sha256_json(journal_unsigned),
            }
            self._write_journal_secure(path, journal)
            self._append_event(
                event_path,
                plan=plan,
                authorization_bundle=bundle,
                events=events,
                event_kind="execution_started",
                journal_state="started",
                observation_sha256=initial_observation["observation_sha256"],
                failure_code=None,
            )

        if not events:
            # Recovery may begin after the durable journal rename but before
            # the first event append. Reconstruct only this mechanically
            # derivable event while holding the process lock.
            self._append_event(
                event_path,
                plan=plan,
                authorization_bundle=bundle,
                events=events,
                event_kind="execution_started",
                journal_state="started",
                observation_sha256=None,
                failure_code=None,
            )
        elif events[0]["event_kind"] != "execution_started":
            raise ProductionStorageExecutorError("production_storage_event_log_invalid")

        mutations: list[str] = []
        observation: Mapping[str, Any] | None = None
        try:
            observation = (
                self._transport.observe_exact_target()
                if existing is not None
                else initial_observation
            )
            observation_now = self._now() if existing is not None else observation_now
            state = self._classify(observation, now_unix=observation_now, plan=plan)
            if state == "source":
                self._transport.resize_exact_disk_once(
                    provider_request_id=plan["provider_request_id"]
                )
                mutations.append("provider_disk_resize_50_to_100")
                observation = self._transport.observe_exact_target()
                state = self._classify(observation, now_unix=self._now(), plan=plan)
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
                state = self._classify(observation, now_unix=self._now(), plan=plan)
            if state != "target":
                raise ProductionStorageExecutorError(
                    "production_storage_postflight_threshold_not_met"
                )
            completed_at_unix = self._now()
            final = contract.validate_observation(
                observation, now_unix=completed_at_unix
            )
        except BaseException as error:
            observation_sha = (
                observation.get("observation_sha256")
                if isinstance(observation, Mapping)
                and isinstance(observation.get("observation_sha256"), str)
                else None
            )
            self._append_event(
                event_path,
                plan=plan,
                authorization_bundle=bundle,
                events=events,
                event_kind="execution_failed",
                journal_state="started",
                observation_sha256=observation_sha,
                failure_code=_stable_failure_code(error),
            )
            raise
        completed_unsigned = {
            **_journal_unsigned(journal),
            "state": "completed",
            "completed_at_unix": completed_at_unix,
            "final_observation": final,
        }
        completed = {
            **completed_unsigned,
            "journal_sha256": protocol.sha256_json(completed_unsigned),
        }
        self._write_journal_secure(path, completed)
        final_event = self._append_event(
            event_path,
            plan=plan,
            authorization_bundle=bundle,
            events=events,
            event_kind="execution_completed",
            journal_state="completed",
            observation_sha256=final["observation_sha256"],
            failure_code=None,
        )
        return _result(
            journal=completed,
            recovered=recovered,
            mutations=mutations,
            event_head_sha256=final_event["event_head_sha256"],
        )


__all__ = [
    "ExactProductionStorageTransport",
    "EVENT_SCHEMA",
    "JOURNAL_SCHEMA",
    "ProductionStorageExecutorError",
    "ProductionStorageGrowthExecutor",
    "PRODUCTION_STATE_ROOT",
    "RESULT_SCHEMA",
]
