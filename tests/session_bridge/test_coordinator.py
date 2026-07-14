from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from threading import Event
from typing import Any
import uuid

import pytest

from session_bridge.claude_adapter import (
    AmbiguousPlaceholderCreation,
    ClaudeCursor,
    ClaudeParseResult,
    PlaceholderCreationError,
    PlaceholderResult,
)
from session_bridge.codex_adapter import CodexThreadSummary
from session_bridge.config import BridgeConfig
from session_bridge.context_pack import ContextPackRequest
from session_bridge.coordinator import (
    ContinueRequest,
    JobSummary,
    ReconcileSummary,
    SessionBridgeCoordinator,
)
from session_bridge.mirror import MirrorPolicy
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    MirrorJobState,
    OriginKind,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    UpsertResult,
)


_CLAUDE_PENDING_KEY = "session-bridge:scan:claude:pending"
_CLAUDE_PROGRESS_KEY = "session-bridge:scan:claude:progress"
_CODEX_PENDING_KEY = "session-bridge:scan:codex:pending"
_CODEX_PROGRESS_KEY = "session-bridge:scan:codex:progress"
_ATTEMPT_KEY_PREFIX = "session-bridge:attempt:"


class _FakeAWatchFactory:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.paths: tuple[Path | str, ...] | None = None
        self.stop_event: asyncio.Event | None = None
        self.kwargs: dict[str, object] = {}

    def __call__(
        self,
        *paths: Path | str,
        stop_event: asyncio.Event | None = None,
        **kwargs: object,
    ) -> AsyncIterator[set[tuple[int, str]]]:
        assert stop_event is not None
        self.paths = paths
        self.stop_event = stop_event
        self.kwargs = dict(kwargs)
        return self._iterate(stop_event)

    def emit(self, path: Path) -> None:
        self.queue.put_nowait({(1, str(path))})

    def fail(self, error: Exception) -> None:
        self.queue.put_nowait(error)

    async def _iterate(
        self,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[set[tuple[int, str]]]:
        self.started.set()
        try:
            while not stop_event.is_set():
                item_task = asyncio.create_task(self.queue.get())
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {item_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        return
                    item = item_task.result()
                finally:
                    for task in (item_task, stop_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(item_task, stop_task, return_exceptions=True)
                if isinstance(item, Exception):
                    raise item
                assert isinstance(item, set)
                yield item
        finally:
            self.closed.set()


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async def wait_loop() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_loop(), timeout=timeout)


def _watcher_config(*, catalog_scan_seconds: float) -> BridgeConfig:
    config = BridgeConfig()
    return replace(
        config,
        service=replace(
            config.service,
            catalog_scan_seconds=catalog_scan_seconds,
            reconcile_seconds=10.0,
        ),
    )


class _LifecycleClaudeAdapter:
    def __init__(self) -> None:
        self.discover_calls = 0
        self.scan_started = asyncio.Event()

    def discover(self) -> list[Path]:
        self.discover_calls += 1
        self.scan_started.set()
        return []


class _LifecycleCodexAdapter:
    def __init__(self) -> None:
        self.inventory_calls = 0

    def list_inventory(self, *, archived: bool) -> list[object]:
        del archived
        self.inventory_calls += 1
        return []


class _ExplodingClaudeAdapter:
    def __init__(self, message: str) -> None:
        self.message = message
        self.discover_calls = 0

    def discover(self) -> list[Path]:
        self.discover_calls += 1
        raise RuntimeError(self.message)


class _SuccessfulCodexAdapter:
    def __init__(self, projection: SessionProjection) -> None:
        self.projection = projection
        self.inventory_calls = 0
        self.project_calls = 0

    def list_inventory(self, *, archived: bool) -> list[object]:
        self.inventory_calls += 1
        return (
            []
            if archived
            else [_codex_summary(self.projection.native_id, self.projection.last_active)]
        )

    def project_thread(self, summary: object) -> SessionProjection:
        del summary
        self.project_calls += 1
        return self.projection


class _RecordingStore:
    def __init__(self) -> None:
        self.projections: list[SessionProjection] = []

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        self.projections.append(projection)
        return UpsertResult(
            session_id=f"{projection.provider.value}:{projection.native_id}",
            inserted_messages=len(projection.messages),
            rebuilt=rebuild,
            first_seen=True,
        )


class _ForbiddenAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self) -> list[Path]:
        self.calls += 1
        raise AssertionError("disabled catalog must not call adapters")

    def list_inventory(self, *, archived: bool) -> list[object]:
        del archived
        self.calls += 1
        raise AssertionError("disabled catalog must not call adapters")


class _StateStore:
    def __init__(
        self,
        operations: list[tuple[object, ...]],
        *,
        fail_upsert_number: int | None = None,
    ) -> None:
        self.operations = operations
        self.states: dict[str, dict[str, Any]] = {}
        self.projections: list[SessionProjection] = []
        self.upsert_attempts: list[str] = []
        self.fail_upsert_number = fail_upsert_number

    def get_state(self, key: str) -> dict[str, Any] | None:
        state = self.states.get(key)
        return deepcopy(state) if state is not None else None

    def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        snapshot = deepcopy(dict(value))
        self.operations.append(("set_state", key, snapshot))
        self.states[key] = snapshot

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        self.upsert_attempts.append(projection.native_id)
        self.operations.append(("upsert", projection.native_id))
        if len(self.upsert_attempts) == self.fail_upsert_number:
            raise RuntimeError("synthetic upsert failure")
        self.projections.append(projection)
        return UpsertResult(
            session_id=f"{projection.provider.value}:{projection.native_id}",
            inserted_messages=len(projection.messages),
            rebuilt=rebuild,
            first_seen=True,
        )


class _BacklogClaudeAdapter:
    def __init__(
        self,
        *,
        discover_batches: list[list[Path]],
        paths_by_native_id: Mapping[str, Path],
        operations: list[tuple[object, ...]],
    ) -> None:
        self.discover_batches = [list(batch) for batch in discover_batches]
        self.paths_by_native_id = dict(paths_by_native_id)
        self.operations = operations
        self.parsed_native_ids: list[str] = []
        self.find_calls: list[str] = []

    def discover(self) -> list[Path]:
        batch = self.discover_batches.pop(0)
        self.operations.append(("discover", tuple(path.stem for path in batch)))
        return batch

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        self.operations.append(("find", native_id))
        return self.paths_by_native_id.get(native_id)

    def parse(self, path: Path) -> ClaudeParseResult:
        native_id = path.stem
        self.parsed_native_ids.append(native_id)
        self.operations.append(("parse", native_id))
        return ClaudeParseResult(
            projection=_scan_projection(Provider.CLAUDE, native_id),
            cursor=ClaudeCursor(
                offset=1,
                head_length=1,
                head_hash="a" * 64,
            ),
            rebuild=False,
            malformed_lines=0,
            unknown_records=0,
        )


class _BacklogCodexAdapter:
    def __init__(
        self,
        *,
        inventory_batches: list[list[CodexThreadSummary]],
        summaries_by_native_id: Mapping[str, CodexThreadSummary],
        operations: list[tuple[object, ...]],
    ) -> None:
        self.inventory_batches = [list(batch) for batch in inventory_batches]
        self.summaries_by_native_id = dict(summaries_by_native_id)
        self.operations = operations
        self.inventory_calls = 0
        self.projected_native_ids: list[str] = []
        self.find_calls: list[str] = []

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self.inventory_calls += 1
        if archived:
            self.operations.append(("inventory", ()))
            return []
        batch = self.inventory_batches.pop(0)
        self.operations.append(
            ("inventory", tuple(summary.native_id for summary in batch))
        )
        return batch

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
    ) -> CodexThreadSummary | None:
        del source_kinds
        self.find_calls.append(native_id)
        self.operations.append(("find", native_id))
        return self.summaries_by_native_id.get(native_id)

    def project_thread(self, summary: CodexThreadSummary) -> SessionProjection:
        self.projected_native_ids.append(summary.native_id)
        self.operations.append(("project", summary.native_id))
        return _scan_projection(Provider.CODEX, summary.native_id)


def _scan_projection(provider: Provider, native_id: str) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} {native_id}",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=20.0,
        messages=(),
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
    )


def _codex_summary(native_id: str, last_active: float) -> CodexThreadSummary:
    return CodexThreadSummary(
        native_id=native_id,
        title=native_id,
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=last_active,
        archived=False,
        revision=f"revision-{native_id}",
    )


def _job_config(
    *,
    automatic_creation: bool = False,
    max_attempts: int = 5,
) -> BridgeConfig:
    config = BridgeConfig()
    return replace(
        config,
        mirrors=replace(
            config.mirrors,
            automatic_creation=automatic_creation,
            max_attempts=max_attempts,
        ),
    )


def _running_job(
    *,
    job_id: str = "job:durable-1",
    attempts: int = 1,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "idempotency_key": "job-key-one",
        "source_session_id": "claude:source-native-1",
        "target_provider": Provider.CODEX.value,
        "state": MirrorJobState.RUNNING.value,
        "attempts": attempts,
        "next_attempt_at": 100.0,
        "created_at": 90.0,
        "updated_at": 100.0,
        "target_native_id": None,
        "error_code": None,
        "error_detail": None,
    }


def _attempt_sidecar(bridge_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": "provider_call_started",
        "bridge_id": bridge_id,
        "target_provider": Provider.CODEX.value,
        "policy_generation": 1,
        "attempts": 1,
    }


def _expected_bridge_id(job: Mapping[str, Any]) -> str:
    return "bridge:" + hashlib.sha256(
        f"session-bridge:{job['idempotency_key']}".encode()
    ).hexdigest()


class _JobCodexSourceAdapter:
    def __init__(self, operations: list[tuple[object, ...]]) -> None:
        self.operations = operations
        self.projections: dict[str, SessionProjection] = {}
        self.find_calls: list[str] = []
        self.project_calls: list[str] = []
        self.marker_payloads: dict[str, BridgeMarkerPayload] = {}
        self.marker_calls: list[tuple[str, BridgeMarkerPayload]] = []

    def add_placeholder(
        self,
        native_id: str,
        bridge_id: str,
        *,
        source_session_id: str = "claude:source-native-1",
        policy_generation: int = 1,
    ) -> None:
        self.projections[native_id] = SessionProjection(
            provider=Provider.CODEX,
            native_id=native_id,
            title="Hermes bridge placeholder",
            cwd="C:/workspace/project",
            started_at=100.0,
            last_active=100.0,
            messages=(),
            native_cursor=f"revision-{native_id}",
            native_hash=f"hash-{native_id}",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
        self.marker_payloads[native_id] = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=policy_generation,
        )

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
    ) -> SessionProjection | None:
        del source_kinds
        self.find_calls.append(native_id)
        self.operations.append(("find_exact", native_id))
        return self.projections.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        self.project_calls.append(summary.native_id)
        self.operations.append(("project_exact", summary.native_id))
        return summary

    def projection_has_marker_payload(
        self,
        projection: SessionProjection,
        payload: BridgeMarkerPayload,
    ) -> bool:
        self.marker_calls.append((projection.native_id, payload))
        return self.marker_payloads.get(projection.native_id) == payload


class _JobStore:
    def __init__(
        self,
        *,
        claimed: list[dict[str, Any]] | None = None,
        running: list[dict[str, Any]] | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        self.operations: list[tuple[object, ...]] = []
        self.claimed = [deepcopy(job) for job in (claimed or [])]
        self.running = [deepcopy(job) for job in (running or [])]
        self.states: dict[str, dict[str, Any]] = {}
        self.origin_rows: dict[tuple[str, Provider], dict[str, Any]] = {}
        self.counts = dict(counts or {})
        self.claim_policies: list[MirrorPolicy] = []
        self.retry_calls: list[dict[str, Any]] = []
        self.manual_failure_calls: list[dict[str, Any]] = []
        self.completions: list[dict[str, Any]] = []
        self.automatic_enqueue_calls = 0

    def claim_due_jobs(
        self,
        *,
        now: float,
        limit: int,
        policy: MirrorPolicy,
    ) -> list[dict[str, Any]]:
        self.operations.append(("claim", now, limit))
        self.claim_policies.append(policy)
        claimed, self.claimed = self.claimed, []
        return deepcopy(claimed)

    def get_state(self, key: str) -> dict[str, Any] | None:
        value = self.states.get(key)
        return deepcopy(value) if value is not None else None

    def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        snapshot = deepcopy(dict(value))
        self.operations.append(("set_state", key, snapshot))
        self.states[key] = snapshot

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        assert rebuild is False
        self.operations.append(("upsert", projection.native_id))
        session_id = f"{projection.provider.value}:{projection.native_id}"
        if projection.origin_bridge_id is not None:
            self.origin_rows[(projection.origin_bridge_id, projection.provider)] = {
                "session_id": session_id,
                "provider": projection.provider.value,
                "native_id": projection.native_id,
                "origin_bridge_id": projection.origin_bridge_id,
            }
        return UpsertResult(
            session_id=session_id,
            inserted_messages=0,
            rebuilt=False,
            first_seen=True,
        )

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
        completion = {
            "job_id": job_id,
            "target_native_id": target_native_id,
            "target_session_id": target_session_id,
            "bridge_id": bridge_id,
        }
        self.operations.append(("complete", job_id, target_native_id))
        self.completions.append(completion)

    def retry_job(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
        next_attempt_at: float,
    ) -> None:
        retry = {
            "job_id": job_id,
            "code": code,
            "detail": detail,
            "next_attempt_at": next_attempt_at,
        }
        self.operations.append(("retry", job_id, code))
        self.retry_calls.append(retry)

    def fail_job_manually(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
    ) -> None:
        failure = {"job_id": job_id, "code": code, "detail": detail}
        self.operations.append(("manual_failure", job_id, code))
        self.manual_failure_calls.append(failure)

    def list_mirror_jobs(
        self,
        states: list[MirrorJobState | str],
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("list_jobs", tuple(str(state) for state in states), limit)
        )
        return deepcopy(self.running)

    def mirror_job_counts(self) -> dict[str, int]:
        self.operations.append(("job_counts",))
        return dict(self.counts)

    def find_external_session_by_origin_bridge(
        self,
        bridge_id: str,
        provider: Provider,
    ) -> dict[str, Any] | None:
        self.operations.append(("find_origin", bridge_id, provider.value))
        row = self.origin_rows.get((bridge_id, provider))
        return deepcopy(row) if row is not None else None

    def enqueue_mirror_job(self, *_args: object, **_kwargs: object) -> None:
        self.automatic_enqueue_calls += 1
        raise AssertionError("automatic mirror enqueue is disabled")


class _JobTargetAdapter:
    def __init__(
        self,
        *,
        store: _JobStore,
        source: _JobCodexSourceAdapter,
        job_id: str,
        outcome: str,
        native_id: str = "codex-target-1",
    ) -> None:
        self.store = store
        self.source = source
        self.job_id = job_id
        self.outcome = outcome
        self.native_id = native_id
        self.calls: list[dict[str, Any]] = []

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        sidecar = self.store.get_state(f"{_ATTEMPT_KEY_PREFIX}{self.job_id}")
        assert sidecar is not None
        assert sidecar["phase"] == "provider_call_started"
        self.store.operations.append(("target_create", self.job_id))
        self.calls.append(deepcopy(kwargs))
        if self.outcome in {"success", "ambiguous_with_id"}:
            self.source.add_placeholder(
                self.native_id,
                kwargs["bridge_id"],
                source_session_id=kwargs["source_session_id"],
                policy_generation=kwargs["policy_generation"],
            )
        if self.outcome == "down":
            raise PlaceholderCreationError("codex_unavailable")
        if self.outcome == "ambiguous_with_id":
            raise AmbiguousPlaceholderCreation(
                "codex_creation_ambiguous",
                native_id=self.native_id,
            )
        if self.outcome == "ambiguous_without_id":
            raise AmbiguousPlaceholderCreation("codex_creation_ambiguous")
        return PlaceholderResult(
            native_id=self.native_id,
            canonical_session_id=f"codex:{self.native_id}",
            used_registration_turn=True,
            verified_at=100.0,
        )


class _ActiveJobStore(_JobStore):
    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__(claimed=[job], running=[job])
        self.active = True

    def list_mirror_jobs(
        self,
        states: list[MirrorJobState | str],
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("list_jobs", tuple(str(state) for state in states), limit)
        )
        return deepcopy(self.running if self.active else [])

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
        super().complete_job(
            job_id,
            target_native_id=target_native_id,
            target_session_id=target_session_id,
            bridge_id=bridge_id,
        )
        self.active = False


class _BlockingJobTargetAdapter:
    def __init__(
        self,
        *,
        source: _JobCodexSourceAdapter,
        started: Event,
        release: Event,
    ) -> None:
        self.source = source
        self.started = started
        self.release = release
        self.calls = 0

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise RuntimeError("test target release timed out")
        native_id = "codex-blocking-target"
        self.source.add_placeholder(native_id, kwargs["bridge_id"])
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=f"codex:{native_id}",
            used_registration_turn=True,
            verified_at=100.0,
        )


class _UnexpectedAfterCreationTargetAdapter:
    def __init__(self, source: _JobCodexSourceAdapter) -> None:
        self.source = source
        self.calls = 0

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        self.calls += 1
        self.source.add_placeholder("codex-possibly-created", kwargs["bridge_id"])
        raise RuntimeError("unexpected failure after provider creation")


class _SequenceJobTargetAdapter:
    def __init__(
        self,
        source: _JobCodexSourceAdapter,
        outcomes: list[str],
    ) -> None:
        self.source = source
        self.outcomes = list(outcomes)
        self.calls = 0

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if outcome == "down":
            raise PlaceholderCreationError("codex_unavailable")
        native_id = f"codex-sequence-target-{self.calls}"
        self.source.add_placeholder(native_id, kwargs["bridge_id"])
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=f"codex:{native_id}",
            used_registration_turn=True,
            verified_at=100.0,
        )


class _RateJobStore(_JobStore):
    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.queue = [deepcopy(job) for job in jobs]

    def claim_due_jobs(
        self,
        *,
        now: float,
        limit: int,
        policy: MirrorPolicy,
    ) -> list[dict[str, Any]]:
        self.operations.append(("claim", now, limit))
        self.claim_policies.append(policy)
        if limit <= 0:
            return []
        claimed = self.queue[:limit]
        del self.queue[:limit]
        return deepcopy(claimed)


class _BreakerJobStore(_JobStore):
    def __init__(
        self,
        *,
        automatic_job: dict[str, Any],
        manual_job: dict[str, Any],
    ) -> None:
        super().__init__()
        self.automatic_job = deepcopy(automatic_job)
        self.manual_job = deepcopy(manual_job)
        self.automatic_returned = False
        self.manual_returned = False

    def claim_due_jobs(
        self,
        *,
        now: float,
        limit: int,
        policy: MirrorPolicy,
    ) -> list[dict[str, Any]]:
        self.operations.append(("claim", now, limit))
        self.claim_policies.append(policy)
        if policy.automatic_creation and not self.automatic_returned:
            self.automatic_returned = True
            return [deepcopy(self.automatic_job)]
        if not policy.automatic_creation and not self.manual_returned:
            self.manual_returned = True
            return [deepcopy(self.manual_job)]
        return []


class _RefreshAdapter:
    def __init__(
        self,
        projection: SessionProjection,
        operations: list[tuple[object, ...]],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.projection = projection
        self.operations = operations
        self.failure = failure

    def find_native_session(self, native_id: str) -> Path | None:
        self.operations.append(("refresh_find", Provider.CLAUDE.value, native_id))
        if self.failure is not None:
            raise self.failure
        return Path(f"C:/synthetic/{native_id}.jsonl")

    def parse(self, path: Path) -> ClaudeParseResult:
        self.operations.append(("refresh_parse", path.stem))
        return ClaudeParseResult(
            projection=self.projection,
            cursor=ClaudeCursor(offset=10, head_length=10, head_hash="b" * 64),
            rebuild=False,
            malformed_lines=0,
            unknown_records=0,
        )

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
    ) -> SessionProjection | None:
        del source_kinds
        self.operations.append(("refresh_find", Provider.CODEX.value, native_id))
        if self.failure is not None:
            raise self.failure
        return self.projection

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        self.operations.append(("refresh_project", summary.native_id))
        return summary


class _HungRefreshAdapter(_RefreshAdapter):
    def __init__(
        self,
        projection: SessionProjection,
        operations: list[tuple[object, ...]],
        *,
        started: Event,
        release: Event,
    ) -> None:
        super().__init__(projection, operations)
        self.started = started
        self.release = release
        self.read_calls = 0

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
    ) -> SessionProjection | None:
        del source_kinds
        self.read_calls += 1
        self.operations.append(("hung_refresh_find", native_id, self.read_calls))
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise RuntimeError("test refresh release timed out")
        return self.projection


class _ContinuationStore:
    def __init__(self, operations: list[tuple[object, ...]]) -> None:
        self.operations = operations
        self.external: dict[str, dict[str, Any]] = {}
        self.origin_rows: dict[tuple[str, Provider], dict[str, Any]] = {}
        self.pack: ContextPack | None = None
        self.link_row: dict[str, Any] | None = None
        self.continuation_snapshot: dict[str, Any] | None = None
        self.transition_calls: list[tuple[str, str, str, str]] = []
        self.divergence_calls: list[tuple[str, float]] = []

    def add_external(
        self,
        session_id: str,
        *,
        provider: Provider,
        native_id: str,
        cursor: str | None,
        source_hash: str | None,
        origin_bridge_id: str | None = None,
    ) -> None:
        row = {
            "session_id": session_id,
            "provider": provider.value,
            "native_id": native_id,
            "last_native_cursor": cursor,
            "last_native_hash": source_hash,
            "origin_bridge_id": origin_bridge_id,
        }
        self.external[session_id] = row
        if origin_bridge_id is not None:
            self.origin_rows[(origin_bridge_id, provider)] = row

    def get_external_session(self, session_id: str) -> dict[str, Any] | None:
        self.operations.append(("get_external", session_id))
        row = self.external.get(session_id)
        return deepcopy(row) if row is not None else None

    def find_external_session_by_origin_bridge(
        self,
        bridge_id: str,
        provider: Provider,
    ) -> dict[str, Any] | None:
        self.operations.append(("find_origin", bridge_id, provider.value))
        row = self.origin_rows.get((bridge_id, provider))
        return deepcopy(row) if row is not None else None

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        assert rebuild is True
        session_id = f"{projection.provider.value}:{projection.native_id}"
        self.operations.append(("refresh_upsert", session_id))
        self.add_external(
            session_id,
            provider=projection.provider,
            native_id=projection.native_id,
            cursor=projection.native_cursor,
            source_hash=projection.native_hash,
            origin_bridge_id=projection.origin_bridge_id,
        )
        return UpsertResult(
            session_id=session_id,
            inserted_messages=len(projection.messages),
            rebuilt=False,
            first_seen=False,
        )

    def transition_link_to_continues(
        self,
        bridge_id: str,
        *,
        pack_id: str,
        target_cursor: str,
        target_hash: str,
    ) -> dict[str, Any]:
        self.operations.append(
            ("transition", bridge_id, pack_id, target_cursor, target_hash)
        )
        self.transition_calls.append(
            (bridge_id, pack_id, target_cursor, target_hash)
        )
        assert self.pack is not None
        assert self.pack.id == pack_id
        if self.pack.immutable_at is None:
            self.pack = replace(self.pack, immutable_at=100.0)
        if self.link_row is None:
            assert self.pack.target_session_id is not None
            self.link_row = {
                "id": "link-continued-1",
                "from_session_id": self.pack.source_session_id,
                "to_session_id": self.pack.target_session_id,
                "relation": Relation.CONTINUES.value,
                "bridge_id": bridge_id,
                "source_cursor": self.pack.source_cursor,
                "source_hash": self.pack.source_hash,
                "created_at": 90.0,
                "hydrated_at": 100.0,
            }
        self.continuation_snapshot = {
            "version": 1,
            "pack_id": self.pack.id,
            "source_session_id": self.pack.source_session_id,
            "source_cursor": self.pack.source_cursor,
            "source_hash": self.pack.source_hash,
            "target_session_id": self.pack.target_session_id,
            "target_cursor": target_cursor,
            "target_hash": target_hash,
        }
        return deepcopy(self.link_row)

    def get_continuation_snapshot(self, bridge_id: str) -> dict[str, Any] | None:
        self.operations.append(("get_continuation_snapshot", bridge_id))
        return deepcopy(self.continuation_snapshot)

    def mark_diverged(self, bridge_id: str, *, at: float) -> None:
        self.operations.append(("mark_diverged", bridge_id, at))
        self.divergence_calls.append((bridge_id, at))

    def get_context_pack(
        self,
        bridge_id: str,
        *,
        budget_chars: int,
    ) -> dict[str, Any] | None:
        self.operations.append(("get_pack", bridge_id, budget_chars))
        if (
            self.pack is None
            or self.pack.bridge_id != bridge_id
            or self.pack.budget_chars != budget_chars
        ):
            return None
        return {
            "id": self.pack.id,
            "bridge_id": self.pack.bridge_id,
            "source_session_id": self.pack.source_session_id,
            "target_session_id": self.pack.target_session_id,
            "source_cursor": self.pack.source_cursor,
            "source_hash": self.pack.source_hash,
            "budget_chars": self.pack.budget_chars,
            "payload": self.pack.payload,
            "created_at": self.pack.created_at,
            "immutable_at": self.pack.immutable_at,
        }


class _RecordingContextBuilder:
    def __init__(
        self,
        store: _ContinuationStore,
        operations: list[tuple[object, ...]],
    ) -> None:
        self.store = store
        self.operations = operations
        self.requests: list[ContextPackRequest] = []

    def build(self, request: ContextPackRequest) -> ContextPack:
        self.operations.append(("build", request.source_cursor, request.source_hash))
        self.requests.append(request)
        if self.store.pack is None:
            self.store.pack = ContextPack(
                id="pack-continue-1",
                bridge_id=request.bridge_id,
                source_session_id=request.source_session_id,
                target_session_id="codex:target-existing",
                source_cursor=request.source_cursor,
                source_hash=request.source_hash,
                budget_chars=request.budget_chars,
                payload=f"handoff:{request.source_cursor}:{request.source_hash}",
                created_at=90.0,
            )
        return self.store.pack


def _refresh_projection(provider: Provider) -> SessionProjection:
    native_id = f"{provider.value}-refresh-native"
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title="Fresh source",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=100.0,
        messages=(),
        native_cursor=f"{provider.value}-cursor-fresh",
        native_hash=f"{provider.value}-hash-fresh",
    )


def _load(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> BridgeConfig:
    return BridgeConfig.load(path=path, environ={} if environ is None else environ)


@pytest.mark.asyncio
async def test_start_reconciles_before_background_scans_and_stop_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BridgeConfig()
    config = replace(
        config,
        service=replace(
            config.service,
            catalog_scan_seconds=0.01,
            reconcile_seconds=0.01,
        ),
    )
    claude = _LifecycleClaudeAdapter()
    codex = _LifecycleCodexAdapter()
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
    )
    reconcile_started = asyncio.Event()
    allow_reconcile = asyncio.Event()

    async def reconcile_once() -> ReconcileSummary:
        reconcile_started.set()
        await allow_reconcile.wait()
        return ReconcileSummary(examined=0, recovered=0, retried=0, failed=0)

    monkeypatch.setattr(coordinator, "reconcile_once", reconcile_once)
    start_task = asyncio.create_task(coordinator.start())
    await asyncio.wait_for(reconcile_started.wait(), timeout=1)
    await asyncio.sleep(0.03)

    assert claude.discover_calls == 0
    assert codex.inventory_calls == 0

    allow_reconcile.set()
    await asyncio.wait_for(start_task, timeout=1)
    await asyncio.wait_for(claude.scan_started.wait(), timeout=1)
    await coordinator.stop()
    calls_after_stop = (claude.discover_calls, codex.inventory_calls)

    await coordinator.stop()
    await asyncio.sleep(0.03)

    assert (claude.discover_calls, codex.inventory_calls) == calls_after_stop


@pytest.mark.asyncio
async def test_recurring_reconcile_cycle_processes_jobs_and_survives_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _watcher_config(catalog_scan_seconds=10.0)
    config = replace(
        config,
        service=replace(config.service, reconcile_seconds=0.01),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={
            Provider.CLAUDE: _LifecycleClaudeAdapter(),
            Provider.CODEX: _LifecycleCodexAdapter(),
        },
    )
    operations: list[str] = []
    processing_calls = 0
    secret = "sk-live-job-cycle-secret"
    private_path = "C:/Users/diego/private/mirror-job.json"

    async def reconcile_once() -> ReconcileSummary:
        operations.append("reconcile")
        return ReconcileSummary(examined=0, recovered=0, retried=0, failed=0)

    async def process_jobs_once() -> JobSummary:
        nonlocal processing_calls
        processing_calls += 1
        operations.append(f"process:{processing_calls}")
        if processing_calls == 1:
            raise RuntimeError(f"job failed with {secret} at {private_path}")
        return JobSummary(claimed=1, succeeded=1, retried=0, manual_failure=0)

    monkeypatch.setattr(coordinator, "reconcile_once", reconcile_once)
    monkeypatch.setattr(coordinator, "process_jobs_once", process_jobs_once)

    await coordinator.start()
    try:
        await _wait_until(lambda: processing_calls >= 2)

        assert operations[:5] == [
            "reconcile",
            "reconcile",
            "process:1",
            "reconcile",
            "process:2",
        ]
        health = coordinator.health()
        assert "mirror_job_processing_failed" in health["recent_error_codes"]
        serialized = json.dumps(health, sort_keys=True)
        assert secret not in serialized
        assert private_path not in serialized
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_scan_once_isolates_provider_failure_and_sanitizes_health() -> None:
    secret = "sk-live-super-secret"
    native_path = "C:/Users/diego/.claude/projects/private/session.jsonl"
    claude = _ExplodingClaudeAdapter(f"provider exploded {secret} at {native_path}")
    projection = SessionProjection(
        provider=Provider.CODEX,
        native_id="codex-ok",
        title="Indexed despite Claude failure",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=20.0,
        messages=(),
        native_cursor="revision-1",
        native_hash="hash-1",
    )
    codex = _SuccessfulCodexAdapter(projection)
    store = _RecordingStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
    )

    summary = await coordinator.scan_once()

    assert summary.provider is None
    assert summary.discovered == 1
    assert summary.indexed == 1
    assert summary.rebuilt == 0
    assert summary.failed == 1
    assert summary.duration_ms >= 0
    assert store.projections == [projection]
    assert claude.discover_calls == 1
    assert codex.inventory_calls == 2
    assert codex.project_calls == 1
    health = coordinator.health()
    assert health["providers"]["claude"]["degraded_reason"] == "scan_failed"
    assert health["providers"]["codex"]["degraded_reason"] is None
    assert health["recent_error_codes"] == ["claude_scan_failed"]
    serialized_health = json.dumps(health, sort_keys=True)
    assert secret not in serialized_health
    assert native_path not in serialized_health


@pytest.mark.asyncio
async def test_scan_once_is_a_zero_summary_when_catalog_is_disabled() -> None:
    config = replace(
        BridgeConfig(),
        catalog=replace(BridgeConfig().catalog, enabled=False),
    )
    claude = _ForbiddenAdapter()
    codex = _ForbiddenAdapter()
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
    )

    summary = await coordinator.scan_once()

    assert summary.provider is None
    assert summary.discovered == 0
    assert summary.indexed == 0
    assert summary.rebuilt == 0
    assert summary.failed == 0
    assert summary.duration_ms == 0
    assert claude.calls == 0
    assert codex.calls == 0


def test_bridge_config_load_uses_safe_defaults_when_file_is_absent(
    tmp_path: Path,
) -> None:
    config = _load(tmp_path / "missing.toml")

    assert config.service.host == "127.0.0.1"
    assert config.service.port == 7484
    assert config.service.catalog_scan_seconds == 3
    assert config.service.reconcile_seconds == 30
    assert config.service.allow_non_loopback is False
    assert config.catalog.enabled is True
    assert config.catalog.include_archived_codex is True
    assert config.mirrors.automatic_creation is False
    assert config.mirrors.backfill_days == 30
    assert config.mirrors.creates_per_minute == 6
    assert config.mirrors.max_attempts == 5
    assert config.mirrors.stop_after_attempts == 20
    assert config.mirrors.stop_error_rate == 0.25


def test_bridge_config_load_reads_injected_toml_path(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text(
        """
[service]
host = "localhost"
port = 8123
catalog_scan_seconds = 1.5
reconcile_seconds = 45

[catalog]
enabled = false
include_archived_codex = false

[mirrors]
automatic_creation = true
backfill_days = 14
creates_per_minute = 3
max_attempts = 7
stop_after_attempts = 12
stop_error_rate = 0.10
""".strip(),
        encoding="utf-8",
    )

    config = _load(path)

    assert config.service.host == "localhost"
    assert config.service.port == 8123
    assert config.service.catalog_scan_seconds == 1.5
    assert config.service.reconcile_seconds == 45
    assert config.catalog.enabled is False
    assert config.catalog.include_archived_codex is False
    assert config.mirrors.automatic_creation is True
    assert config.mirrors.backfill_days == 14
    assert config.mirrors.creates_per_minute == 3
    assert config.mirrors.max_attempts == 7
    assert config.mirrors.stop_after_attempts == 12
    assert config.mirrors.stop_error_rate == 0.10


def test_environment_overrides_are_injectable_and_take_precedence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text(
        """
[service]
host = "127.0.0.1"
port = 7000

[catalog]
enabled = true

[mirrors]
automatic_creation = false
stop_error_rate = 0.25
""".strip(),
        encoding="utf-8",
    )

    config = _load(
        path,
        environ={
            "HERMES_SESSION_BRIDGE_HOST": "localhost",
            "HERMES_SESSION_BRIDGE_PORT": "8124",
            "HERMES_SESSION_BRIDGE_CATALOG_ENABLED": "false",
            "HERMES_SESSION_BRIDGE_AUTOMATIC_CREATION": "true",
            "HERMES_SESSION_BRIDGE_STOP_ERROR_RATE": "0.1",
        },
    )

    assert config.service.host == "localhost"
    assert config.service.port == 8124
    assert config.catalog.enabled is False
    assert config.mirrors.automatic_creation is True
    assert config.mirrors.stop_error_rate == 0.1


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(tmp_path: Path, host: str) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text(f'[service]\nhost = "{host}"\n', encoding="utf-8")

    assert _load(path).service.host == host


def test_non_loopback_host_is_rejected_by_default(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text('[service]\nhost = "0.0.0.0"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="non-loopback"):
        _load(path)


def test_toml_can_explicitly_allow_a_non_loopback_host(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text(
        '[service]\nhost = "192.0.2.10"\nallow_non_loopback = true\n',
        encoding="utf-8",
    )

    config = _load(path)

    assert config.service.host == "192.0.2.10"
    assert config.service.allow_non_loopback is True


def test_environment_cannot_grant_non_loopback_permission(tmp_path: Path) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text('[service]\nhost = "0.0.0.0"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="non-loopback"):
        _load(
            path,
            environ={"HERMES_SESSION_BRIDGE_ALLOW_NON_LOOPBACK": "true"},
        )


@pytest.mark.parametrize(
    ("toml_text", "message"),
    [
        ('[service]\nport = "7484"\n', "port"),
        ("[service]\nport = 0\n", "port"),
        ("[service]\nport = 65536\n", "port"),
        ("[service]\ncatalog_scan_seconds = true\n", "catalog_scan_seconds"),
        ("[service]\nreconcile_seconds = 0\n", "reconcile_seconds"),
        ("[catalog]\nenabled = 1\n", "enabled"),
        ("[mirrors]\nbackfill_days = -1\n", "backfill_days"),
        ("[mirrors]\ncreates_per_minute = 0\n", "creates_per_minute"),
        ("[mirrors]\nmax_attempts = 0\n", "max_attempts"),
        ("[mirrors]\nstop_after_attempts = 0\n", "stop_after_attempts"),
        ("[mirrors]\nstop_error_rate = 1.01\n", "stop_error_rate"),
    ],
)
def test_toml_numeric_and_boolean_values_are_strictly_validated(
    tmp_path: Path,
    toml_text: str,
    message: str,
) -> None:
    path = tmp_path / "bridge.toml"
    path.write_text(toml_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load(path)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("HERMES_SESSION_BRIDGE_PORT", "7484.0", "port"),
        ("HERMES_SESSION_BRIDGE_CATALOG_ENABLED", "yes", "enabled"),
        (
            "HERMES_SESSION_BRIDGE_AUTOMATIC_CREATION",
            "1",
            "automatic_creation",
        ),
        ("HERMES_SESSION_BRIDGE_STOP_ERROR_RATE", "nan", "stop_error_rate"),
    ],
)
def test_environment_numeric_and_boolean_values_are_strictly_validated(
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _load(tmp_path / "missing.toml", environ={name: value})


@pytest.mark.asyncio
async def test_claude_bounded_scan_stages_tail_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    paths = {
        native_id: tmp_path / f"{native_id}.jsonl"
        for native_id in ("claude-new", "claude-middle", "claude-old")
    }
    for native_id, mtime in (
        ("claude-new", 300.0),
        ("claude-middle", 200.0),
        ("claude-old", 100.0),
    ):
        paths[native_id].write_text("{}\n", encoding="utf-8")
        os.utime(paths[native_id], (mtime, mtime))
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    adapter = _BacklogClaudeAdapter(
        discover_batches=[
            [paths["claude-old"], paths["claude-new"], paths["claude-middle"]],
            [],
        ],
        paths_by_native_id=paths,
        operations=operations,
    )
    first = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=2,
    )

    first_summary = await first.scan_once(Provider.CLAUDE)

    assert first_summary.discovered == 3
    assert first_summary.indexed == 2
    assert first_summary.failed == 0
    assert adapter.parsed_native_ids == ["claude-new", "claude-middle"]
    staged_all = (
        "set_state",
        _CLAUDE_PENDING_KEY,
        {
            "version": 1,
            "native_ids": ["claude-new", "claude-middle", "claude-old"],
        },
    )
    assert operations.index(staged_all) < operations.index(("parse", "claude-new"))
    assert store.get_state(_CLAUDE_PENDING_KEY) == {
        "version": 1,
        "native_ids": ["claude-old"],
    }
    assert store.get_state(_CLAUDE_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "claude-middle",
        "indexed_total": 2,
        "remaining": 1,
    }

    restarted = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=2,
    )
    second_summary = await restarted.scan_once(Provider.CLAUDE)

    assert second_summary.discovered == 1
    assert second_summary.indexed == 1
    assert second_summary.failed == 0
    assert adapter.find_calls == ["claude-old"]
    assert store.upsert_attempts == ["claude-new", "claude-middle", "claude-old"]
    assert store.get_state(_CLAUDE_PENDING_KEY) == {
        "version": 1,
        "native_ids": [],
    }
    assert store.get_state(_CLAUDE_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "claude-old",
        "indexed_total": 3,
        "remaining": 0,
    }
    serialized_status = json.dumps(
        {"state": store.states, "health": restarted.health()},
        sort_keys=True,
    )
    for path in paths.values():
        assert str(path) not in serialized_status


@pytest.mark.asyncio
async def test_codex_bounded_scan_stages_changed_inventory_before_seen_cache_loss(
) -> None:
    summaries = {
        "codex-new": _codex_summary("codex-new", 300.0),
        "codex-middle": _codex_summary("codex-middle", 200.0),
        "codex-old": _codex_summary("codex-old", 100.0),
    }
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    adapter = _BacklogCodexAdapter(
        inventory_batches=[
            [summaries["codex-new"], summaries["codex-middle"], summaries["codex-old"]],
            [],
        ],
        summaries_by_native_id=summaries,
        operations=operations,
    )
    first = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=2,
    )

    first_summary = await first.scan_once(Provider.CODEX)

    assert first_summary.discovered == 3
    assert first_summary.indexed == 2
    assert first_summary.failed == 0
    assert adapter.projected_native_ids == ["codex-new", "codex-middle"]
    staged_all = (
        "set_state",
        _CODEX_PENDING_KEY,
        {
            "version": 1,
            "native_ids": ["codex-new", "codex-middle", "codex-old"],
        },
    )
    assert operations.index(staged_all) < operations.index(("project", "codex-new"))
    assert store.get_state(_CODEX_PENDING_KEY) == {
        "version": 1,
        "native_ids": ["codex-old"],
    }
    assert store.get_state(_CODEX_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "codex-middle",
        "indexed_total": 2,
        "remaining": 1,
    }

    restarted = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=2,
    )
    second_summary = await restarted.scan_once(Provider.CODEX)

    assert second_summary.discovered == 1
    assert second_summary.indexed == 1
    assert second_summary.failed == 0
    assert adapter.inventory_calls == 4
    assert adapter.find_calls == ["codex-old"]
    assert store.upsert_attempts == ["codex-new", "codex-middle", "codex-old"]
    assert store.get_state(_CODEX_PENDING_KEY) == {
        "version": 1,
        "native_ids": [],
    }
    assert store.get_state(_CODEX_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "codex-old",
        "indexed_total": 3,
        "remaining": 0,
    }


@pytest.mark.asyncio
async def test_failed_batch_keeps_all_identities_and_does_not_advance_progress(
    tmp_path: Path,
) -> None:
    paths = {
        native_id: tmp_path / f"{native_id}.jsonl"
        for native_id in ("failure-new", "failure-middle", "failure-old")
    }
    for native_id, mtime in (
        ("failure-new", 300.0),
        ("failure-middle", 200.0),
        ("failure-old", 100.0),
    ):
        paths[native_id].write_text("{}\n", encoding="utf-8")
        os.utime(paths[native_id], (mtime, mtime))
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations, fail_upsert_number=2)
    adapter = _BacklogClaudeAdapter(
        discover_batches=[
            [paths["failure-old"], paths["failure-new"], paths["failure-middle"]]
        ],
        paths_by_native_id=paths,
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=2,
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.discovered == 3
    assert summary.indexed == 1
    assert summary.failed == 1
    assert adapter.parsed_native_ids == ["failure-new", "failure-middle"]
    assert store.upsert_attempts == ["failure-new", "failure-middle"]
    assert store.get_state(_CLAUDE_PENDING_KEY) == {
        "version": 1,
        "native_ids": ["failure-old", "failure-middle"],
    }
    assert store.get_state(_CLAUDE_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "failure-new",
        "indexed_total": 1,
        "remaining": 2,
    }


@pytest.mark.asyncio
async def test_watcher_burst_debounces_to_one_claude_only_scan(
    tmp_path: Path,
) -> None:
    claude = _LifecycleClaudeAdapter()
    codex = _LifecycleCodexAdapter()
    awatch = _FakeAWatchFactory()
    coordinator = SessionBridgeCoordinator(
        config=_watcher_config(catalog_scan_seconds=10.0),
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        claude_projects_root=tmp_path,
        watch_debounce_seconds=0.03,
        awatch_factory=awatch,
    )

    await coordinator.start()
    try:
        await _wait_until(
            lambda: (
                claude.discover_calls == 1
                and codex.inventory_calls == 2
                and awatch.started.is_set()
            )
        )
        awatch.emit(tmp_path / "one.jsonl")
        awatch.emit(tmp_path / "two.jsonl")
        awatch.emit(tmp_path / "three.jsonl")

        await _wait_until(lambda: claude.discover_calls == 2)
        await asyncio.sleep(0.08)

        assert claude.discover_calls == 2
        assert codex.inventory_calls == 2
        assert awatch.paths == (tmp_path,)
        assert coordinator.health()["watcher_state"] == "running"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_periodic_all_provider_scan_recovers_without_watcher_events(
    tmp_path: Path,
) -> None:
    claude = _LifecycleClaudeAdapter()
    codex = _LifecycleCodexAdapter()
    awatch = _FakeAWatchFactory()
    coordinator = SessionBridgeCoordinator(
        config=_watcher_config(catalog_scan_seconds=0.03),
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        claude_projects_root=tmp_path,
        watch_debounce_seconds=0.01,
        awatch_factory=awatch,
    )

    await coordinator.start()
    try:
        await _wait_until(
            lambda: (
                    claude.discover_calls >= 3
                    and codex.inventory_calls == 2 * claude.discover_calls
            )
        )

        assert awatch.queue.empty()
        assert 2 * claude.discover_calls == codex.inventory_calls
        assert coordinator.health()["watcher_state"] == "running"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_watcher_failure_is_sanitized_and_periodic_scans_continue(
    tmp_path: Path,
) -> None:
    secret = "watcher-secret-token"
    private_path = "C:/Users/diego/.claude/projects/private/session.jsonl"
    claude = _LifecycleClaudeAdapter()
    codex = _LifecycleCodexAdapter()
    awatch = _FakeAWatchFactory()
    coordinator = SessionBridgeCoordinator(
        config=_watcher_config(catalog_scan_seconds=0.03),
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        claude_projects_root=tmp_path,
        watch_debounce_seconds=0.01,
        awatch_factory=awatch,
    )

    await coordinator.start()
    try:
        await _wait_until(
            lambda: (
                claude.discover_calls >= 1
                and codex.inventory_calls >= 1
                and awatch.started.is_set()
            )
        )
        awatch.fail(RuntimeError(f"watch failed {secret} at {private_path}"))
        await _wait_until(
            lambda: coordinator.health()["watcher_state"] == "degraded"
        )
        calls_at_failure = (claude.discover_calls, codex.inventory_calls)
        await _wait_until(
            lambda: (
                claude.discover_calls > calls_at_failure[0]
                and codex.inventory_calls > calls_at_failure[1]
            )
        )

        health = coordinator.health()
        assert health["watcher_state"] == "degraded"
        assert health["watcher_error_code"] == "claude_watcher_failed"
        assert "claude_watcher_failed" in health["recent_error_codes"]
        serialized_health = json.dumps(health, sort_keys=True)
        assert secret not in serialized_health
        assert private_path not in serialized_health
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_signals_and_closes_watcher_without_post_stop_scan(
    tmp_path: Path,
) -> None:
    claude = _LifecycleClaudeAdapter()
    codex = _LifecycleCodexAdapter()
    awatch = _FakeAWatchFactory()
    coordinator = SessionBridgeCoordinator(
        config=_watcher_config(catalog_scan_seconds=10.0),
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        claude_projects_root=tmp_path,
        watch_debounce_seconds=0.02,
        awatch_factory=awatch,
    )

    await coordinator.start()
    try:
        await _wait_until(
            lambda: (
                claude.discover_calls == 1
                and codex.inventory_calls == 2
                and awatch.started.is_set()
            )
        )
        await coordinator.stop()
        await asyncio.wait_for(awatch.closed.wait(), timeout=1)
        calls_after_stop = (claude.discover_calls, codex.inventory_calls)

        assert awatch.stop_event is not None
        assert awatch.stop_event.is_set()
        awatch.emit(tmp_path / "after-stop.jsonl")
        await asyncio.sleep(0.06)

        assert (claude.discover_calls, codex.inventory_calls) == calls_after_stop
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_target_down_records_retry_without_leaking_provider_detail() -> None:
    job = _running_job(attempts=1)
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="down",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(max_attempts=3),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.claimed == 1
    assert summary.succeeded == 0
    assert summary.retried == 1
    assert summary.manual_failure == 0
    assert len(store.retry_calls) == 1
    assert store.retry_calls[0]["job_id"] == job["id"]
    assert store.retry_calls[0]["code"] == "codex_unavailable"
    assert store.retry_calls[0]["next_attempt_at"] > 100.0
    assert "C:/" not in store.retry_calls[0]["detail"]


@pytest.mark.asyncio
async def test_target_failure_at_max_attempts_becomes_manual_failure() -> None:
    job = _running_job(attempts=2)
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="down",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(max_attempts=2),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.claimed == 1
    assert summary.retried == 0
    assert summary.manual_failure == 1
    assert store.retry_calls == []
    assert store.manual_failure_calls == [
        {
            "job_id": job["id"],
            "code": "codex_unavailable",
            "detail": "target placeholder creation failed",
        }
    ]
    assert "codex_target_failed" in coordinator.health()["recent_error_codes"]


@pytest.mark.asyncio
async def test_attempt_sidecar_is_durable_and_sanitized_before_provider_call() -> None:
    job = _running_job()
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="success",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    await coordinator.process_jobs_once()

    attempt_key = f"{_ATTEMPT_KEY_PREFIX}{job['id']}"
    sidecar = store.get_state(attempt_key)
    assert sidecar is not None
    assert sidecar["version"] == 1
    assert sidecar["phase"] == "provider_call_started"
    assert sidecar["target_provider"] == Provider.CODEX.value
    assert sidecar["policy_generation"] == 1
    assert sidecar["attempts"] == job["attempts"]
    assert isinstance(sidecar["bridge_id"], str) and sidecar["bridge_id"]
    assert "expected_native_id" not in sidecar
    set_index = next(
        index
        for index, operation in enumerate(store.operations)
        if operation[:2] == ("set_state", attempt_key)
    )
    create_index = store.operations.index(("target_create", job["id"]))
    assert set_index < create_index
    serialized = json.dumps(sidecar, sort_keys=True)
    assert "sk-live-super-secret" not in serialized
    assert "C:/Users/diego" not in serialized


@pytest.mark.asyncio
async def test_successful_create_indexes_exact_target_before_completing_job() -> None:
    job = _running_job()
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="success",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.claimed == 1
    assert summary.succeeded == 1
    assert summary.retried == 0
    assert summary.manual_failure == 0
    assert source.find_calls == [target.native_id]
    assert source.project_calls == [target.native_id]
    assert store.completions == [
        {
            "job_id": job["id"],
            "target_native_id": target.native_id,
            "target_session_id": f"codex:{target.native_id}",
            "bridge_id": target.calls[0]["bridge_id"],
        }
    ]
    assert store.operations.index(("upsert", target.native_id)) < (
        store.operations.index(("complete", job["id"], target.native_id))
    )


@pytest.mark.asyncio
async def test_explicit_manual_retry_runs_while_automatic_creation_is_disabled(
) -> None:
    job = _running_job(attempts=2)
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="success",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(automatic_creation=False),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.succeeded == 1
    assert len(store.claim_policies) == 1
    assert store.claim_policies[0].automatic_creation is False
    assert target.calls
    assert store.automatic_enqueue_calls == 0


@pytest.mark.asyncio
async def test_ambiguous_creation_with_native_id_reconciles_exact_target() -> None:
    job = _running_job()
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="ambiguous_with_id",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.claimed == 1
    assert summary.succeeded == 1
    assert summary.retried == 0
    assert summary.manual_failure == 0
    assert len(target.calls) == 1
    assert source.find_calls == [target.native_id]
    assert store.completions[0]["target_native_id"] == target.native_id
    assert store.completions[0]["bridge_id"] == target.calls[0]["bridge_id"]


@pytest.mark.asyncio
async def test_ambiguous_creation_without_identity_fails_closed_without_duplicate(
) -> None:
    job = _running_job()
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="ambiguous_without_id",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(max_attempts=5),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    first = await coordinator.process_jobs_once()
    second = await coordinator.process_jobs_once()

    assert first.claimed == 1
    assert first.retried == 0
    assert first.manual_failure == 1
    assert second.claimed == 0
    assert len(target.calls) == 1
    assert store.retry_calls == []
    assert store.manual_failure_calls[0]["code"] == "target_identity_unproven"


@pytest.mark.asyncio
async def test_reconcile_running_job_without_sidecar_retries_without_provider_call(
) -> None:
    job = _running_job()
    store = _JobStore(running=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _JobTargetAdapter(
        store=store,
        source=source,
        job_id=job["id"],
        outcome="success",
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.examined == 1
    assert summary.recovered == 0
    assert summary.retried == 1
    assert summary.failed == 0
    assert len(store.retry_calls) == 1
    assert store.retry_calls[0]["code"] == "provider_call_not_started"
    assert target.calls == []


@pytest.mark.asyncio
async def test_reconcile_running_job_with_sidecar_completes_exact_catalog_target(
) -> None:
    job = _running_job()
    bridge_id = _expected_bridge_id(job)
    target_native_id = "codex-restart-target"
    store = _JobStore(running=[job])
    store.states[f"{_ATTEMPT_KEY_PREFIX}{job['id']}"] = _attempt_sidecar(bridge_id)
    store.origin_rows[(bridge_id, Provider.CODEX)] = {
        "session_id": f"codex:{target_native_id}",
        "provider": Provider.CODEX.value,
        "native_id": target_native_id,
        "origin_bridge_id": bridge_id,
    }
    source = _JobCodexSourceAdapter(store.operations)
    source.add_placeholder(target_native_id, bridge_id)
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.examined == 1
    assert summary.recovered == 1
    assert summary.retried == 0
    assert summary.failed == 0
    assert store.completions == [
        {
            "job_id": job["id"],
            "target_native_id": target_native_id,
            "target_session_id": f"codex:{target_native_id}",
            "bridge_id": bridge_id,
        }
    ]
    assert store.retry_calls == []
    assert store.manual_failure_calls == []
    assert source.marker_calls == [
        (
            target_native_id,
            BridgeMarkerPayload(
                bridge_id=bridge_id,
                source_session_id=job["source_session_id"],
                target_provider=Provider.CODEX,
                policy_generation=1,
            ),
        )
    ]


@pytest.mark.asyncio
async def test_reconcile_running_job_with_unproven_sidecar_fails_closed() -> None:
    job = _running_job()
    bridge_id = _expected_bridge_id(job)
    store = _JobStore(running=[job])
    store.states[f"{_ATTEMPT_KEY_PREFIX}{job['id']}"] = _attempt_sidecar(bridge_id)
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.examined == 1
    assert summary.recovered == 0
    assert summary.retried == 0
    assert summary.failed == 1
    assert store.retry_calls == []
    assert store.manual_failure_calls == [
        {
            "job_id": job["id"],
            "code": "target_identity_unproven",
            "detail": "running mirror target identity could not be proven",
        }
    ]


def test_health_reports_durable_queue_counts_without_sensitive_job_data() -> None:
    counts = {
        MirrorJobState.QUEUED.value: 3,
        MirrorJobState.RUNNING.value: 1,
        MirrorJobState.RETRY.value: 2,
        MirrorJobState.SUCCEEDED.value: 8,
        MirrorJobState.MANUAL_FAILURE.value: 1,
    }
    store = _JobStore(counts=counts)
    coordinator = SessionBridgeCoordinator(
        config=_job_config(automatic_creation=False),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 100.0,
    )

    health = coordinator.health()

    assert health["queue_counts"] == counts
    assert health["mirror_mode"] == "manual"
    serialized = json.dumps(health, sort_keys=True)
    assert "source-native-1" not in serialized
    assert "durable-idempotency-1" not in serialized


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.CODEX])
@pytest.mark.asyncio
async def test_refresh_session_reads_exact_provider_and_persists_fresh_projection(
    provider: Provider,
) -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(provider)
    session_id = f"{provider.value}:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=provider,
        native_id=projection.native_id,
        cursor=f"{provider.value}-cursor-old",
        source_hash=f"{provider.value}-hash-old",
    )
    adapter = _RefreshAdapter(projection, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={provider: adapter},
        clock=lambda: 100.0,
    )

    result = await coordinator.refresh_session(session_id, timeout=0.5)

    assert result.session_id == session_id
    assert result.cursor == projection.native_cursor
    assert result.source_hash == projection.native_hash
    assert result.stale is False
    assert result.warning is None
    assert operations[-1] == ("refresh_upsert", session_id)
    refreshed = store.get_external_session(session_id)
    assert refreshed is not None
    assert refreshed["last_native_cursor"] == projection.native_cursor
    assert refreshed["last_native_hash"] == projection.native_hash


@pytest.mark.asyncio
async def test_refresh_failure_uses_durable_snapshot_with_fixed_sanitized_warning(
) -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(Provider.CODEX)
    session_id = f"codex:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CODEX,
        native_id=projection.native_id,
        cursor="codex-cursor-durable",
        source_hash="codex-hash-durable",
    )
    secret = "sk-live-never-expose-this-provider-error"
    private_path = "C:/Users/diego/private/thread.jsonl"
    adapter = _RefreshAdapter(
        projection,
        operations,
        failure=TimeoutError(f"timeout {secret} at {private_path}"),
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        clock=lambda: 100.0,
    )

    result = await coordinator.refresh_session(session_id, timeout=0.01)

    assert result.session_id == session_id
    assert result.cursor == "codex-cursor-durable"
    assert result.source_hash == "codex-hash-durable"
    assert result.stale is True
    assert result.warning == "source_refresh_failed_using_durable_snapshot"
    serialized = json.dumps(result.__dict__, sort_keys=True)
    assert secret not in serialized
    assert private_path not in serialized


@pytest.mark.asyncio
async def test_refresh_failure_refuses_when_durable_snapshot_is_unavailable() -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(Provider.CLAUDE)
    session_id = f"claude:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CLAUDE,
        native_id=projection.native_id,
        cursor=None,
        source_hash=None,
    )
    adapter = _RefreshAdapter(
        projection,
        operations,
        failure=RuntimeError("Claude source is unavailable"),
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        clock=lambda: 100.0,
    )

    with pytest.raises(RuntimeError, match="durable snapshot unavailable"):
        await coordinator.refresh_session(session_id, timeout=0.01)


@pytest.mark.asyncio
async def test_continue_refreshes_before_build_and_atomically_transitions_exact_pack(
) -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(Provider.CLAUDE)
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-continue-1",
    )
    session_id = f"claude:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CLAUDE,
        native_id=projection.native_id,
        cursor="claude-cursor-old",
        source_hash="claude-hash-old",
    )
    store.add_external(
        "codex:target-existing",
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-target-cursor-old",
        source_hash="codex-target-hash-old",
        origin_bridge_id="bridge-continue-1",
    )
    source_adapter = _RefreshAdapter(projection, operations)
    target_adapter = _RefreshAdapter(target_projection, operations)
    builder = _RecordingContextBuilder(store, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: source_adapter,
            Provider.CODEX: target_adapter,
        },
        context_builder=builder,
        clock=lambda: 100.0,
    )
    request = ContinueRequest(
        session_id=session_id,
        bridge_id="bridge-continue-1",
        target_provider=Provider.CODEX,
        context_budget_chars=4000,
    )

    result = await coordinator.continue_session(request)

    assert builder.requests == [
        ContextPackRequest(
            source_session_id=session_id,
            target_provider=Provider.CODEX,
            bridge_id="bridge-continue-1",
            source_cursor="claude-cursor-fresh",
            source_hash="claude-hash-fresh",
            budget_chars=4000,
            stale=False,
            diverged=False,
        )
    ]
    source_refresh_index = operations.index(("refresh_upsert", session_id))
    target_refresh_index = operations.index(
        ("refresh_upsert", "codex:target-existing")
    )
    build_index = operations.index(
        ("build", "claude-cursor-fresh", "claude-hash-fresh")
    )
    transition_index = operations.index(
        (
            "transition",
            "bridge-continue-1",
            "pack-continue-1",
            "codex-target-cursor-fresh",
            "codex-target-hash-fresh",
        )
    )
    assert source_refresh_index < build_index
    assert target_refresh_index < build_index < transition_index
    assert result.pack.id == "pack-continue-1"
    assert result.pack.source_cursor == "claude-cursor-fresh"
    assert result.pack.source_hash == "claude-hash-fresh"
    assert result.pack.immutable_at == 100.0
    assert result.link == SessionLink(
        id="link-continued-1",
        from_session_id=session_id,
        to_session_id="codex:target-existing",
        relation=Relation.CONTINUES,
        bridge_id="bridge-continue-1",
        source_cursor="claude-cursor-fresh",
        source_hash="claude-hash-fresh",
        created_at=90.0,
    )
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_continue_stale_fallback_is_explicit_and_identical_replay_is_stable(
) -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(Provider.CLAUDE)
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-continue-1",
    )
    session_id = f"claude:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CLAUDE,
        native_id=projection.native_id,
        cursor="claude-cursor-durable",
        source_hash="claude-hash-durable",
    )
    store.add_external(
        "codex:target-existing",
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-target-cursor-old",
        source_hash="codex-target-hash-old",
        origin_bridge_id="bridge-continue-1",
    )
    source_adapter = _RefreshAdapter(
        projection,
        operations,
        failure=TimeoutError("private provider timeout detail"),
    )
    target_adapter = _RefreshAdapter(target_projection, operations)
    builder = _RecordingContextBuilder(store, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: source_adapter,
            Provider.CODEX: target_adapter,
        },
        context_builder=builder,
        clock=lambda: 100.0,
    )
    request = ContinueRequest(
        session_id=session_id,
        bridge_id="bridge-continue-1",
        target_provider=Provider.CODEX,
        context_budget_chars=4000,
    )

    first = await coordinator.continue_session(request)
    second = await coordinator.continue_session(request)

    assert len(builder.requests) == 1
    assert all(
        pack_request.source_cursor == "claude-cursor-durable"
        and pack_request.source_hash == "claude-hash-durable"
        and pack_request.stale is True
        for pack_request in builder.requests
    )
    assert first == second
    assert first.pack.payload == "handoff:claude-cursor-durable:claude-hash-durable"
    assert first.pack.immutable_at == 100.0
    assert first.link.relation is Relation.CONTINUES
    assert first.warnings == ("source_refresh_failed_using_durable_snapshot",)
    assert store.transition_calls == [
        (
            "bridge-continue-1",
            "pack-continue-1",
            "codex-target-cursor-fresh",
            "codex-target-hash-fresh",
        ),
        (
            "bridge-continue-1",
            "pack-continue-1",
            "codex-target-cursor-fresh",
            "codex-target-hash-fresh",
        ),
    ]


@pytest.mark.asyncio
async def test_continue_replay_marks_divergence_only_when_both_descendants_advance(
) -> None:
    operations: list[tuple[object, ...]] = []
    source_projection = _refresh_projection(Provider.CLAUDE)
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-continue-1",
    )
    session_id = f"claude:{source_projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CLAUDE,
        native_id=source_projection.native_id,
        cursor="claude-cursor-old",
        source_hash="claude-hash-old",
    )
    store.add_external(
        "codex:target-existing",
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-target-cursor-old",
        source_hash="codex-target-hash-old",
        origin_bridge_id="bridge-continue-1",
    )
    source_adapter = _RefreshAdapter(source_projection, operations)
    target_adapter = _RefreshAdapter(target_projection, operations)
    builder = _RecordingContextBuilder(store, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: source_adapter,
            Provider.CODEX: target_adapter,
        },
        context_builder=builder,
        clock=lambda: 100.0,
    )
    request = ContinueRequest(
        session_id=session_id,
        bridge_id="bridge-continue-1",
        target_provider=Provider.CODEX,
        context_budget_chars=4000,
    )

    first = await coordinator.continue_session(request)
    initial_snapshot = store.get_continuation_snapshot("bridge-continue-1")
    source_adapter.projection = replace(
        source_projection,
        native_cursor="claude-cursor-next",
        native_hash="claude-hash-next",
        last_active=110.0,
    )
    target_adapter.projection = replace(
        target_projection,
        native_cursor="codex-target-cursor-next",
        native_hash="codex-target-hash-next",
        last_active=110.0,
    )

    replay_start = len(operations)
    replay = await coordinator.continue_session(request)
    replay_operations = operations[replay_start:]

    assert len(builder.requests) == 1
    assert replay.pack == first.pack
    assert replay.link == first.link
    assert replay.warnings == ("linked_sessions_diverged",)
    assert store.divergence_calls == [("bridge-continue-1", 100.0)]
    assert store.get_continuation_snapshot("bridge-continue-1") == initial_snapshot
    divergence_index = replay_operations.index(
        ("mark_diverged", "bridge-continue-1", 100.0)
    )
    assert replay_operations.index(("refresh_upsert", session_id)) < divergence_index
    assert replay_operations.index(
        ("refresh_upsert", "codex:target-existing")
    ) < divergence_index


@pytest.mark.asyncio
async def test_reconcile_waits_for_active_job_processing_critical_section() -> None:
    job = _running_job()
    store = _ActiveJobStore(job)
    source = _JobCodexSourceAdapter(store.operations)
    started = Event()
    release = Event()
    target = _BlockingJobTargetAdapter(
        source=source,
        started=started,
        release=release,
    )
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    process_task = asyncio.create_task(coordinator.process_jobs_once())
    assert await asyncio.to_thread(started.wait, 1.0)
    reconcile_task = asyncio.create_task(coordinator.reconcile_once())
    await asyncio.sleep(0.03)
    reconcile_completed_during_creation = reconcile_task.done()
    release.set()
    process_summary, reconcile_summary = await asyncio.gather(
        process_task,
        reconcile_task,
    )

    assert reconcile_completed_during_creation is False
    assert process_summary.succeeded == 1
    assert reconcile_summary.examined == 0
    assert target.calls == 1
    assert store.retry_calls == []
    assert store.manual_failure_calls == []


@pytest.mark.asyncio
async def test_unknown_target_exception_after_possible_creation_fails_closed() -> None:
    job = _running_job()
    store = _JobStore(claimed=[job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _UnexpectedAfterCreationTargetAdapter(source)
    coordinator = SessionBridgeCoordinator(
        config=_job_config(max_attempts=5),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.claimed == 1
    assert summary.succeeded == 0
    assert summary.retried == 0
    assert summary.manual_failure == 1
    assert target.calls == 1
    assert store.retry_calls == []
    assert store.manual_failure_calls == [
        {
            "job_id": job["id"],
            "code": "target_outcome_unknown",
            "detail": "target placeholder outcome could not be proven",
        }
    ]


@pytest.mark.asyncio
async def test_refresh_waiting_behind_hung_read_has_its_own_bounded_timeout() -> None:
    operations: list[tuple[object, ...]] = []
    projection = _refresh_projection(Provider.CODEX)
    session_id = f"codex:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CODEX,
        native_id=projection.native_id,
        cursor="codex-cursor-durable",
        source_hash="codex-hash-durable",
    )
    started = Event()
    release = Event()
    adapter = _HungRefreshAdapter(
        projection,
        operations,
        started=started,
        release=release,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        clock=lambda: 100.0,
    )

    first = await coordinator.refresh_session(session_id, timeout=0.01)
    await _wait_until(started.is_set)
    second = None
    try:
        second = await asyncio.wait_for(
            coordinator.refresh_session(session_id, timeout=0.02),
            timeout=0.1,
        )
    except TimeoutError:
        pass
    finally:
        release.set()
        await asyncio.sleep(0.03)

    assert first.stale is True
    assert second is not None
    assert second.stale is True
    assert second.cursor == "codex-cursor-durable"
    assert adapter.read_calls == 1


@pytest.mark.asyncio
async def test_creates_per_minute_is_durable_across_coordinator_restart() -> None:
    first_job = _running_job(job_id="job:rate-1")
    first_job["idempotency_key"] = "rate-idempotency-1"
    second_job = _running_job(job_id="job:rate-2")
    second_job["idempotency_key"] = "rate-idempotency-2"
    store = _RateJobStore([first_job, second_job])
    source = _JobCodexSourceAdapter(store.operations)
    target = _SequenceJobTargetAdapter(source, ["success", "success"])
    now = [100.0]
    config = _job_config()
    config = replace(
        config,
        mirrors=replace(config.mirrors, creates_per_minute=1),
    )
    first = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: now[0],
    )

    first_summary = await first.process_jobs_once()
    restarted = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: now[0],
    )
    immediate_summary = await restarted.process_jobs_once()
    now[0] += 61.0
    later_summary = await restarted.process_jobs_once()

    assert first_summary.succeeded == 1
    assert immediate_summary.claimed == 0
    assert later_summary.succeeded == 1
    assert target.calls == 2


@pytest.mark.asyncio
async def test_durable_automatic_breaker_still_allows_manual_authority() -> None:
    automatic_job = _running_job(job_id="job:breaker-auto")
    automatic_job["idempotency_key"] = "breaker-automatic"
    manual_job = _running_job(job_id="job:breaker-manual")
    manual_job["idempotency_key"] = "breaker-manual"
    store = _BreakerJobStore(
        automatic_job=automatic_job,
        manual_job=manual_job,
    )
    source = _JobCodexSourceAdapter(store.operations)
    target = _SequenceJobTargetAdapter(source, ["down", "success"])
    config = _job_config(automatic_creation=True)
    config = replace(
        config,
        mirrors=replace(
            config.mirrors,
            stop_after_attempts=1,
            stop_error_rate=0.5,
        ),
    )
    first = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    automatic_summary = await first.process_jobs_once()
    restarted = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )
    manual_summary = await restarted.process_jobs_once()

    assert automatic_summary.retried == 1
    assert manual_summary.succeeded == 1
    assert [policy.automatic_creation for policy in store.claim_policies] == [
        True,
        False,
    ]
    assert target.calls == 2


@pytest.mark.parametrize(
    "corruption",
    ["bridge_id", "expected_native_id", "extra_field"],
)
@pytest.mark.asyncio
async def test_reconcile_rejects_non_deterministic_claude_attempt_sidecar(
    corruption: str,
) -> None:
    job = _running_job(job_id="job:claude-sidecar")
    job["idempotency_key"] = "claude-sidecar-idempotency"
    job["target_provider"] = Provider.CLAUDE.value
    expected_bridge = "bridge:" + hashlib.sha256(
        f"session-bridge:{job['idempotency_key']}".encode()
    ).hexdigest()
    expected_native_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes-session-bridge:{job['idempotency_key']}",
        )
    )
    sidecar: dict[str, Any] = {
        "version": 1,
        "phase": "provider_call_started",
        "bridge_id": expected_bridge,
        "target_provider": Provider.CLAUDE.value,
        "policy_generation": 1,
        "attempts": job["attempts"],
        "expected_native_id": expected_native_id,
    }
    if corruption == "bridge_id":
        sidecar["bridge_id"] = f"bridge:{'f' * 64}"
    elif corruption == "expected_native_id":
        sidecar["expected_native_id"] = "00000000-0000-0000-0000-000000000000"
    else:
        sidecar["unexpected"] = "must be rejected"
    store = _JobStore(running=[job])
    store.states[f"{_ATTEMPT_KEY_PREFIX}{job['id']}"] = deepcopy(sidecar)
    store.origin_rows[(sidecar["bridge_id"], Provider.CLAUDE)] = {
        "session_id": f"claude:{expected_native_id}",
        "provider": Provider.CLAUDE.value,
        "native_id": expected_native_id,
        "origin_bridge_id": sidecar["bridge_id"],
    }
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.failed == 1
    assert summary.recovered == 0
    assert store.completions == []
    assert not any(operation[0] == "find_origin" for operation in store.operations)
    assert store.manual_failure_calls[0]["code"] == "attempt_sidecar_invalid"
