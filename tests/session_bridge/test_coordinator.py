from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
from threading import Barrier, Event, Lock, get_ident
import time
from typing import Any
import uuid

import pytest

from hermes_state import SessionDB
from session_bridge.claude_adapter import (
    AmbiguousPlaceholderCreation,
    ClaudeCursor,
    ClaudeParseResult,
    PlaceholderCreationError,
    PlaceholderResult,
)
from session_bridge.codex_adapter import CodexThreadSummary
from session_bridge.config import BridgeConfig, SidebarConfig
from session_bridge.context_pack import ContextPackRequest
from session_bridge.coordinator import (
    ContinuationBlockedError,
    ContinueRequest,
    JobSummary,
    ReconcileSummary,
    SessionBridgeCoordinator,
    SidebarRegistrationSummary,
)
from session_bridge.mirror import MirrorPolicy
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    HydrationMarkerPayload,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarJobState,
    UpsertResult,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    encode_hydration_marker,
    sidebar_bridge_id,
)
from session_bridge.preview import build_session_preview
from session_bridge.sidebar_executor import SidebarExecutionResult
from session_bridge.store import SessionBridgeStore, SidebarSource, SidebarSourcePage
from session_bridge.worktree import (
    WorktreeSnapshot,
    WorktreeSnapshotError,
    capture_worktree_snapshot,
)


_CLAUDE_PENDING_KEY = "session-bridge:scan:claude:pending"
_CLAUDE_PROGRESS_KEY = "session-bridge:scan:claude:progress"
_CLAUDE_FINGERPRINT_KEY = "session-bridge:scan:claude:fingerprints"
_CLAUDE_STAGED_KEY = "session-bridge:scan:claude:staged-fingerprints"
_CODEX_PENDING_KEY = "session-bridge:scan:codex:pending"
_CODEX_PROGRESS_KEY = "session-bridge:scan:codex:progress"
_CODEX_SEEN_KEY = "session-bridge:scan:codex:seen"
_ATTEMPT_KEY_PREFIX = "session-bridge:attempt:"


class _FakeAWatchFactory:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.paths: tuple[Path | str, ...] | None = None
        self.stop_event: asyncio.Event | None = None
        self.kwargs: dict[str, object] = {}
        self.requests = 0

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
                # Bumped as the consumer asks for the NEXT batch, which it only
                # does after fully handling the previous one.  So requests ==
                # baseline + N proves N batches were processed to completion --
                # unlike queue.empty(), which flips as soon as an item is TAKEN
                # and leaves the consumer's handling of it still in flight.
                self.requests += 1
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


# Deadlock guard for awaits that must not be allowed to hang the suite.  It is
# never an assertion target: no test asserts anything about how much of it was
# consumed, so it is sized for the worst host rather than for expected latency.
_REFRESH_DEADLOCK_GUARD_SECONDS = 30.0


class _GatedDebounceSleep:
    """Replace the watcher's debounce wall-clock wait with a test-driven gate.

    ``_watch_loop`` cancels the pending debounce task on every batch, so a burst
    coalesces into one scan only if the loop drains every queued batch before the
    debounce elapses.  Timing that against ``watch_debounce_seconds`` makes the
    coalescing assertion a race with the scheduler: on a loaded host the debounce
    expires BETWEEN two already-queued batches and the burst splits into two
    scans.  Gating it removes the clock -- no debounce can expire until the test
    opens ``release``, so "all batches consumed" becomes an effect to wait on
    rather than an interval to bet on.

    Only the debounce duration is intercepted; every other sleep the coordinator
    performs (catalog scan, reconcile, mirror poll) still reaches ``asyncio.sleep``.
    """

    def __init__(self, debounce_seconds: float) -> None:
        self._debounce_seconds = float(debounce_seconds)
        self.release = asyncio.Event()
        self.parked = 0

    async def __call__(self, delay: float) -> None:
        if float(delay) != self._debounce_seconds:
            await asyncio.sleep(delay)
            return
        self.parked += 1
        try:
            await self.release.wait()
        finally:
            self.parked -= 1


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
            else [
                _codex_summary(self.projection.native_id, self.projection.last_active)
            ]
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
        existing_native_ids: set[str] | None = None,
    ) -> None:
        self.operations = operations
        self.states: dict[str, dict[str, Any]] = {}
        self.projections: list[SessionProjection] = []
        self.upsert_attempts: list[str] = []
        self.rebuild_attempts: list[tuple[str, bool]] = []
        self.fail_upsert_number = fail_upsert_number
        self.existing_native_ids = set(existing_native_ids or ())

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
        self.rebuild_attempts.append((projection.native_id, rebuild))
        self.operations.append(("upsert", projection.native_id))
        if len(self.upsert_attempts) == self.fail_upsert_number:
            raise RuntimeError("synthetic upsert failure")
        first_seen = projection.native_id not in self.existing_native_ids
        self.existing_native_ids.add(projection.native_id)
        self.projections.append(projection)
        return UpsertResult(
            session_id=f"{projection.provider.value}:{projection.native_id}",
            inserted_messages=len(projection.messages),
            rebuilt=rebuild,
            first_seen=first_seen,
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
        self.find_stem_calls: list[str] = []

    def discover(self) -> list[Path]:
        batch = self.discover_batches.pop(0)
        self.operations.append(("discover", tuple(path.stem for path in batch)))
        return batch

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        self.operations.append(("find", native_id))
        return self.paths_by_native_id.get(native_id)

    def find_native_sessions_by_stem(self, native_id: str) -> list[Path]:
        self.find_stem_calls.append(native_id)
        self.operations.append(("find_stem", native_id))
        path = self.paths_by_native_id.get(native_id)
        return [path] if path is not None else []

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
        self.stderr_lines: list[str] = []
        self.stderr_tail_calls: list[int] = []

    def stderr_tail(self, n: int = 20) -> list[str]:
        self.stderr_tail_calls.append(n)
        return list(self.stderr_lines)

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self.inventory_calls += 1
        if archived:
            self.operations.append(("inventory", ()))
            return []
        batch = self.inventory_batches.pop(0)
        self.operations.append((
            "inventory",
            tuple(summary.native_id for summary in batch),
        ))
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
        projection = _scan_projection(Provider.CODEX, summary.native_id)
        if summary.trusted_origin_bridge_id is None:
            return projection
        return replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=summary.trusted_origin_bridge_id,
        )


class _FullHistoryClaudeAdapter:
    def __init__(
        self,
        paths: list[Path],
        *,
        failing_native_ids: set[str] | None = None,
    ) -> None:
        self.paths = list(paths)
        self.failing_native_ids = set(failing_native_ids or ())
        self.discover_calls = 0
        self.parsed_native_ids: list[str] = []

    def discover(self) -> list[Path]:
        self.discover_calls += 1
        return list(self.paths)

    def parse(self, path: Path) -> ClaudeParseResult:
        native_id = path.stem
        self.parsed_native_ids.append(native_id)
        if native_id in self.failing_native_ids:
            raise RuntimeError("synthetic Claude projection failure")
        return ClaudeParseResult(
            projection=_scan_projection(Provider.CLAUDE, native_id),
            cursor=ClaudeCursor(offset=1, head_length=1, head_hash="a" * 64),
            rebuild=False,
            malformed_lines=0,
            unknown_records=0,
        )


class _FullHistoryCodexAdapter:
    def __init__(
        self,
        *,
        active: list[CodexThreadSummary],
        archived: list[CodexThreadSummary],
        failing_native_ids: set[str] | None = None,
    ) -> None:
        self.active = list(active)
        self.archived = list(archived)
        self.failing_native_ids = set(failing_native_ids or ())
        self.incremental_inventory_calls = 0
        self.full_inventory_calls: list[bool] = []
        self.projected_native_ids: list[str] = []
        self.stderr_lines: list[str] = []
        self.stderr_tail_calls: list[int] = []

    def stderr_tail(self, n: int = 20) -> list[str]:
        self.stderr_tail_calls.append(n)
        return list(self.stderr_lines)

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        del archived
        self.incremental_inventory_calls += 1
        raise AssertionError("full-history scan must not use changed-only inventory")

    def list_full_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self.full_inventory_calls.append(archived)
        return list(self.archived if archived else self.active)

    def project_thread(self, summary: CodexThreadSummary) -> SessionProjection:
        self.projected_native_ids.append(summary.native_id)
        if summary.native_id in self.failing_native_ids:
            raise RuntimeError(
                "synthetic Codex projection failure "
                "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789 "
                "at C:/private/codex/full-history.jsonl"
            )
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
    return (
        "bridge:"
        + hashlib.sha256(
            f"session-bridge:{job['idempotency_key']}".encode()
        ).hexdigest()
    )


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
        self.operations.append((
            "list_jobs",
            tuple(str(state) for state in states),
            limit,
        ))
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


class _TestWorkerLockHandle:
    def __init__(self, lock: Lock) -> None:
        self._lock: Lock | None = lock

    def release(self) -> None:
        if self._lock is None:
            return
        lock = self._lock
        self._lock = None
        lock.release()


class _ActiveJobStore(_JobStore):
    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__(claimed=[job], running=[job])
        self.active = True
        self.worker_lock = Lock()

    def try_acquire_mirror_worker_lock(self):
        if not self.worker_lock.acquire(blocking=False):
            return None
        return _TestWorkerLockHandle(self.worker_lock)

    def list_mirror_jobs(
        self,
        states: list[MirrorJobState | str],
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.operations.append((
            "list_jobs",
            tuple(str(state) for state in states),
            limit,
        ))
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

    def read_native_thread(self, native_id: str) -> SessionProjection:
        self.operations.append(("refresh_read", Provider.CODEX.value, native_id))
        if self.failure is not None:
            raise self.failure
        return self.projection

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

    def read_native_thread(self, native_id: str) -> SessionProjection:
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
        self.native: dict[str, dict[str, Any]] = {}
        self.origin_rows: dict[tuple[str, Provider], dict[str, Any]] = {}
        self.pack: ContextPack | None = None
        self.link_row: dict[str, Any] | None = None
        self.continuation_snapshot: dict[str, Any] | None = None
        self.transition_calls: list[tuple[str, str, str, str]] = []
        self.divergence_calls: list[tuple[str, float]] = []

    def add_native_snapshot(
        self,
        session_id: str,
        *,
        cursor: str,
        source_hash: str,
    ) -> None:
        self.native[session_id] = {
            "session_id": session_id,
            "provider": Provider.HERMES.value,
            "cursor": cursor,
            "source_hash": source_hash,
        }

    def get_native_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        self.operations.append(("get_native", session_id))
        row = self.native.get(session_id)
        return deepcopy(row) if row is not None else None

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
        self.operations.append((
            "transition",
            bridge_id,
            pack_id,
            target_cursor,
            target_hash,
        ))
        self.transition_calls.append((bridge_id, pack_id, target_cursor, target_hash))
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


def _exact_cwd_repo(path: Path) -> Path:
    path.mkdir()
    git = [
        "git",
        "-c",
        "user.name=Session Bridge Tests",
        "-c",
        "user.email=session-bridge@example.invalid",
        "-C",
        str(path),
    ]
    subprocess.run([*git, "init", "-b", "main"], check=True, capture_output=True)
    (path / "tracked.txt").write_text("initial", encoding="utf-8")
    subprocess.run([*git, "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-m", "initial"], check=True, capture_output=True)
    return path


def _remove_tree(path: Path) -> None:
    def _make_writable(function, value, _error) -> None:
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(path, onerror=_make_writable)


def _directory_alias(alias: Path, target: Path) -> str:
    try:
        alias.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {symlink_error}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            pytest.skip("directory retarget race test requires a symlink or junction")
        return "junction"


@pytest.mark.asyncio
async def test_start_returns_while_initial_reconcile_gates_background_scans(
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
    await asyncio.wait_for(coordinator.start(), timeout=1)
    await asyncio.wait_for(reconcile_started.wait(), timeout=1)
    await asyncio.sleep(0.03)

    assert claude.discover_calls == 0
    assert codex.inventory_calls == 0

    allow_reconcile.set()
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
    full_summary = await coordinator.scan_all_history()

    assert summary.provider is None
    assert summary.discovered == 0
    assert summary.indexed == 0
    assert summary.rebuilt == 0
    assert summary.failed == 0
    assert summary.duration_ms == 0
    assert full_summary.provider is None
    assert full_summary.discovered == 0
    assert full_summary.indexed == 0
    assert full_summary.rebuilt == 0
    assert full_summary.failed == 0
    assert full_summary.duration_ms == 0
    assert claude.calls == 0
    assert codex.calls == 0


@pytest.mark.asyncio
async def test_scan_all_history_is_newest_first_catalog_only_and_aggregates_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    claude = _FullHistoryClaudeAdapter(
        [paths["claude-old"], paths["claude-new"], paths["claude-middle"]],
        failing_native_ids={"claude-middle"},
    )
    codex = _FullHistoryCodexAdapter(
        active=[
            _codex_summary("codex-old", 100.0),
            _codex_summary("codex-new", 300.0),
        ],
        archived=[_codex_summary("codex-middle", 200.0)],
        failing_native_ids={"codex-middle"},
    )
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    config = replace(
        BridgeConfig(),
        mirrors=replace(BridgeConfig().mirrors, automatic_creation=True),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        scan_batch_size=1,
    )

    async def reject_automatic_enqueue(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("full-history catalog scan must not enqueue mirrors")

    monkeypatch.setattr(
        coordinator,
        "_maybe_enqueue_automatic",
        reject_automatic_enqueue,
    )

    summary = await coordinator.scan_all_history()

    assert summary.provider is None
    assert summary.discovered == 6
    assert summary.indexed == 4
    assert summary.rebuilt == 0
    assert summary.failed == 2
    assert summary.duration_ms >= 0
    assert claude.discover_calls == 1
    assert claude.parsed_native_ids == [
        "claude-new",
        "claude-middle",
        "claude-old",
    ]
    assert codex.incremental_inventory_calls == 0
    assert codex.full_inventory_calls == [False, True]
    assert codex.projected_native_ids == [
        "codex-new",
        "codex-middle",
        "codex-old",
    ]
    assert store.upsert_attempts == [
        "claude-new",
        "claude-old",
        "codex-new",
        "codex-old",
    ]
    assert not [operation for operation in operations if operation[0] == "set_state"]
    health = coordinator.health()
    assert health["providers"]["claude"]["degraded_reason"] == "scan_failed"
    assert health["providers"]["codex"]["degraded_reason"] == "scan_failed"
    assert health["recent_error_codes"] == [
        "claude_scan_failed",
        "codex_scan_failed",
        "codex_scan_failed",
    ]


@pytest.mark.asyncio
async def test_full_history_codex_scan_records_redacted_thread_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    native_path = "C:/private/codex/full-history.jsonl"
    failing = _codex_summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 300.0)
    healthy = _codex_summary("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 200.0)
    adapter = _FullHistoryCodexAdapter(
        active=[failing, healthy],
        archived=[],
        failing_native_ids={failing.native_id},
    )
    adapter.stderr_lines = [f"stderr {secret} {native_path}"]
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_StateStore([]),
        adapters={Provider.CODEX: adapter},
    )

    with caplog.at_level(logging.WARNING, logger="session_bridge.coordinator"):
        summary = await coordinator.scan_all_history(Provider.CODEX)

    assert summary.failed == 1
    assert summary.indexed == 1
    assert adapter.projected_native_ids == [failing.native_id, healthy.native_id]
    assert adapter.stderr_tail_calls == [12]
    health = coordinator.health()
    assert "codex_scan_failed" in health["recent_error_codes"]
    serialized = json.dumps(health, sort_keys=True)
    assert secret not in serialized
    assert native_path not in serialized
    assert "task:" not in serialized
    assert "codex_scan_diagnostic" in caplog.text
    assert "full_history_project" in caplog.text
    assert "codex_scan_failed" in caplog.text
    assert "task:" in caplog.text
    assert failing.native_id not in caplog.text
    assert secret not in caplog.text
    assert native_path not in caplog.text


def test_codex_scan_stderr_diagnostics_are_bounded_and_redact_relative_paths() -> None:
    from session_bridge.coordinator import _redacted_codex_stderr_tail

    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    native_path = "logs/codex/private.jsonl"

    class OversizedAdapter:
        def stderr_tail(self, n: int = 20) -> list[str]:
            assert n == 12
            return [f"stderr {secret} {native_path}"] * 20

    result = _redacted_codex_stderr_tail(OversizedAdapter())

    assert len(result) == 8
    assert all(secret not in line for line in result)
    assert all(native_path not in line for line in result)
    assert all("[REDACTED_PATH]" in line for line in result)


@pytest.mark.parametrize(
    "native_path",
    (
        r"C:\\Users\\Private Owner\\Codex Logs\\thread.jsonl",
        r"\\server\\private share\\Codex Logs\\thread.jsonl",
        r"\\?\\C:\\Users\\Private Owner\\Codex Logs\\thread.jsonl",
        r"\\?\\UNC\\server\\private share\\Codex Logs\\thread.jsonl",
        r"C:\\Users\\Private Owner",
        r"\\server\\private share",
        r"\\?\\C:\\Users\\Private Owner",
        r"\\?\\UNC\\server\\private share",
    ),
)
def test_codex_scan_diagnostic_redacts_windows_path_forms(native_path: str) -> None:
    from session_bridge.coordinator import _redacted_codex_diagnostic_text

    result = _redacted_codex_diagnostic_text(f"failed at {native_path}")

    assert native_path not in result
    assert "Private Owner" not in result
    assert "private share" not in result
    assert "[REDACTED_PATH]" in result


@pytest.mark.parametrize(
    "native_path",
    (
        r"C:\\Users\\Private Owner",
        r"\\server\\private share",
        r"\\?\\C:\\Users\\Private Owner",
        r"\\?\\UNC\\server\\private share",
    ),
)
def test_codex_scan_diagnostic_redacts_entire_terminal_windows_path(
    native_path: str,
) -> None:
    from session_bridge.coordinator import _redacted_codex_diagnostic_text

    assert _redacted_codex_diagnostic_text(f"failed at {native_path}") == (
        "failed at [REDACTED_PATH]"
    )


@pytest.mark.parametrize(
    "native_path",
    (
        "/home/Private Owner",
        "/home/Private Owner/Codex Logs/thread.jsonl",
        "/var/lib/Private Workspace/Session Data",
    ),
)
def test_codex_scan_diagnostic_redacts_entire_terminal_posix_path(
    native_path: str,
) -> None:
    from session_bridge.coordinator import _redacted_codex_diagnostic_text

    assert _redacted_codex_diagnostic_text(f"failed at {native_path}") == (
        "failed at [REDACTED_PATH]"
    )


def test_codex_scan_diagnostic_does_not_over_redact_ordinary_prose() -> None:
    from session_bridge.coordinator import _redacted_codex_diagnostic_text

    prose = "Private Owner reviewed the failure after retry"

    assert _redacted_codex_diagnostic_text(prose) == prose


@pytest.mark.asyncio
async def test_codex_scan_diagnostic_failure_never_replaces_projection_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnprintableProjectionError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("synthetic str failure")

    failing = _codex_summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 300.0)
    healthy = _codex_summary("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 200.0)
    adapter = _FullHistoryCodexAdapter(active=[failing, healthy], archived=[])
    original_project = adapter.project_thread

    def project_thread(summary: CodexThreadSummary) -> SessionProjection:
        if summary.native_id == failing.native_id:
            raise UnprintableProjectionError()
        return original_project(summary)

    adapter.project_thread = project_thread  # type: ignore[method-assign]
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_StateStore([]),
        adapters={Provider.CODEX: adapter},
    )

    with caplog.at_level(logging.WARNING, logger="session_bridge.coordinator"):
        summary = await coordinator.scan_all_history(Provider.CODEX)

    assert summary.failed == 1
    assert summary.indexed == 1
    assert adapter.projected_native_ids == [healthy.native_id]
    assert "codex_scan_diagnostic" in caplog.text
    assert "diagnostic_unavailable" in caplog.text
    assert "codex_scan_failed" in coordinator.health()["recent_error_codes"]


@pytest.mark.asyncio
async def test_scan_all_codex_history_includes_archived_when_steady_state_excludes_it() -> (
    None
):
    codex = _FullHistoryCodexAdapter(
        active=[_codex_summary("codex-active", 300.0)],
        archived=[_codex_summary("codex-archived", 200.0)],
    )
    config = replace(
        BridgeConfig(),
        catalog=replace(BridgeConfig().catalog, include_archived_codex=False),
    )
    store = _StateStore([])
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: codex},
    )

    summary = await coordinator.scan_all_history(Provider.CODEX)

    assert codex.full_inventory_calls == [False, True]
    assert codex.projected_native_ids == ["codex-active", "codex-archived"]
    assert summary.discovered == 2
    assert summary.indexed == 2


@pytest.mark.asyncio
async def test_scan_all_history_rebuilds_existing_rows_without_counting_new_rows(
    tmp_path: Path,
) -> None:
    claude_path = tmp_path / "claude-existing.jsonl"
    claude_path.write_text("{}\n", encoding="utf-8")
    claude = _FullHistoryClaudeAdapter([claude_path])
    codex = _FullHistoryCodexAdapter(
        active=[_codex_summary("codex-new", 300.0)],
        archived=[_codex_summary("codex-existing", 200.0)],
    )
    store = _StateStore(
        [],
        existing_native_ids={"claude-existing", "codex-existing"},
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
    )

    summary = await coordinator.scan_all_history()

    assert store.rebuild_attempts == [
        ("claude-existing", True),
        ("codex-new", True),
        ("codex-existing", True),
    ]
    assert summary.discovered == 3
    assert summary.indexed == 3
    assert summary.rebuilt == 2


@pytest.mark.asyncio
async def test_scan_all_claude_history_isolates_a_path_that_disappears_before_stat(
    tmp_path: Path,
) -> None:
    available = tmp_path / "claude-available.jsonl"
    disappeared = tmp_path / "claude-disappeared.jsonl"
    available.write_text("{}\n", encoding="utf-8")
    disappeared.write_text("{}\n", encoding="utf-8")
    disappeared.unlink()
    claude = _FullHistoryClaudeAdapter([disappeared, available])
    store = _StateStore([])
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: claude},
    )

    summary = await coordinator.scan_all_history(Provider.CLAUDE)

    assert summary.discovered == 2
    assert summary.indexed == 1
    assert summary.rebuilt == 0
    assert summary.failed == 1
    assert claude.parsed_native_ids == ["claude-available"]


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
    assert adapter.find_stem_calls == ["claude-old"]
    assert adapter.find_calls == []
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
async def test_claude_bounded_scan_isolates_a_path_that_disappears_before_stat(
    tmp_path: Path,
) -> None:
    available = tmp_path / "claude-available.jsonl"
    disappeared = tmp_path / "claude-disappeared.jsonl"
    available.write_text("{}\n", encoding="utf-8")
    disappeared.write_text("{}\n", encoding="utf-8")
    disappeared.unlink()
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    adapter = _BacklogClaudeAdapter(
        discover_batches=[[disappeared, available]],
        paths_by_native_id={"claude-available": available},
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.discovered == 2
    assert summary.indexed == 1
    assert summary.rebuilt == 0
    assert summary.failed == 1
    assert adapter.parsed_native_ids == ["claude-available"]


@pytest.mark.asyncio
async def test_claude_bounded_scan_isolates_one_parse_failure_and_retries_it(
    tmp_path: Path,
) -> None:
    paths = {
        native_id: tmp_path / f"{native_id}.jsonl"
        for native_id in ("claude-new", "claude-failing", "claude-old")
    }
    for native_id, mtime in (
        ("claude-new", 300.0),
        ("claude-failing", 200.0),
        ("claude-old", 100.0),
    ):
        paths[native_id].write_text("{}\n", encoding="utf-8")
        os.utime(paths[native_id], (mtime, mtime))
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    secret = "private-parse-secret"

    class OneFailureAdapter(_BacklogClaudeAdapter):
        def parse(self, path: Path) -> ClaudeParseResult:
            if path.stem == "claude-failing":
                raise RuntimeError(f"{secret} at {path}")
            return super().parse(path)

    adapter = OneFailureAdapter(
        discover_batches=[list(paths.values())],
        paths_by_native_id=paths,
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=3,
    )
    fingerprint_before_failure = store.get_state(_CLAUDE_FINGERPRINT_KEY)
    provider_cursor_before_failure = (
        fingerprint_before_failure or {"sessions": {}}
    )["sessions"].get("claude-failing")

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.discovered == 3
    assert summary.indexed == 2
    assert summary.failed == 1
    assert store.upsert_attempts == ["claude-new", "claude-old"]
    assert store.get_state(_CLAUDE_PENDING_KEY) == {
        "version": 1,
        "native_ids": ["claude-failing"],
    }
    assert set(store.get_state(_CLAUDE_FINGERPRINT_KEY)["sessions"]) == {
        "claude-new",
        "claude-old",
    }
    assert set(store.get_state(_CLAUDE_STAGED_KEY)["sessions"]) == {
        "claude-failing",
    }
    fingerprint_after_failure = store.get_state(_CLAUDE_FINGERPRINT_KEY)
    provider_cursor_after_failure = fingerprint_after_failure["sessions"].get(
        "claude-failing"
    )
    assert provider_cursor_after_failure == provider_cursor_before_failure is None
    assert {"claude-new", "claude-old"}.issubset(store.upsert_attempts)
    health = coordinator.health()
    assert health["providers"]["claude"]["degraded_reason"] == "scan_failed"
    assert health["recent_error_codes"] == ["claude_scan_failed"]
    serialized = json.dumps(health, sort_keys=True)
    assert secret not in serialized
    assert str(paths["claude-failing"]) not in serialized


@pytest.mark.asyncio
async def test_claude_bounded_scan_retires_exactly_absent_persisted_pending_source() -> (
    None
):
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations)
    store.states[_CLAUDE_PENDING_KEY] = {
        "version": 1,
        "native_ids": ["claude-gone"],
    }
    store.states[_CLAUDE_PROGRESS_KEY] = {
        "version": 1,
        "last_committed_native_id": "claude-last",
        "indexed_total": 3,
        "remaining": 1,
    }
    adapter = _BacklogClaudeAdapter(
        discover_batches=[[]],
        paths_by_native_id={},
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.discovered == 1
    assert summary.indexed == 0
    assert summary.failed == 0
    assert adapter.find_stem_calls == ["claude-gone"]
    assert adapter.find_calls == []
    assert store.get_state(_CLAUDE_PENDING_KEY) == {
        "version": 1,
        "native_ids": [],
    }
    assert store.get_state(_CLAUDE_PROGRESS_KEY) == {
        "version": 1,
        "last_committed_native_id": "claude-last",
        "indexed_total": 3,
        "remaining": 0,
    }


@pytest.mark.asyncio
async def test_immediate_codex_scan_records_redacted_thread_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    native_path = "C:/private/codex/immediate.jsonl"
    failing = _codex_summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 300.0)
    healthy = _codex_summary("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 200.0)

    class FailingAdapter(_BacklogCodexAdapter):
        def project_thread(self, thread_summary: CodexThreadSummary) -> SessionProjection:
            projection = super().project_thread(thread_summary)
            if thread_summary.native_id == failing.native_id:
                raise RuntimeError(f"projection failed {secret} at {native_path}")
            return projection

    adapter = FailingAdapter(
        inventory_batches=[[failing, healthy]],
        summaries_by_native_id={
            failing.native_id: failing,
            healthy.native_id: healthy,
        },
        operations=[],
    )
    adapter.stderr_lines = [f"stderr {secret} {native_path}"]
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_RecordingStore(),
        adapters={Provider.CODEX: adapter},
    )

    with caplog.at_level(logging.WARNING, logger="session_bridge.coordinator"):
        result = await coordinator.scan_once(Provider.CODEX)

    assert result.failed == 1
    assert result.indexed == 1
    assert adapter.projected_native_ids == [failing.native_id, healthy.native_id]
    assert adapter.stderr_tail_calls == [12]
    health = coordinator.health()
    assert "codex_scan_failed" in health["recent_error_codes"]
    serialized = json.dumps(health, sort_keys=True)
    assert secret not in serialized
    assert native_path not in serialized
    assert "task:" not in serialized
    assert "codex_scan_diagnostic" in caplog.text
    assert "immediate_project" in caplog.text
    assert "codex_scan_failed" in caplog.text
    assert "task:" in caplog.text
    assert failing.native_id not in caplog.text
    assert secret not in caplog.text
    assert native_path not in caplog.text


@pytest.mark.asyncio
async def test_codex_bounded_scan_stages_changed_inventory_before_seen_cache_loss() -> (
    None
):
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
async def test_persistent_codex_scan_records_redacted_thread_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    native_path = "C:/private/codex/persistent.jsonl"
    summary = _codex_summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 300.0)
    operations: list[tuple[object, ...]] = []

    class FailingAdapter(_BacklogCodexAdapter):
        def project_thread(self, thread_summary: CodexThreadSummary) -> SessionProjection:
            super().project_thread(thread_summary)
            raise RuntimeError(f"projection failed {secret} at {native_path}")

    adapter = FailingAdapter(
        inventory_batches=[[summary]],
        summaries_by_native_id={summary.native_id: summary},
        operations=operations,
    )
    adapter.stderr_lines = [f"stderr {secret} {native_path}"]
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=_StateStore(operations),
        adapters={Provider.CODEX: adapter},
    )

    with caplog.at_level(logging.WARNING, logger="session_bridge.coordinator"):
        result = await coordinator.scan_once(Provider.CODEX)

    assert result.failed == 1
    assert result.indexed == 0
    assert adapter.stderr_tail_calls == [12]
    health = coordinator.health()
    assert "codex_scan_failed" in health["recent_error_codes"]
    serialized = json.dumps(health, sort_keys=True)
    assert secret not in serialized
    assert native_path not in serialized
    assert "task:" not in serialized
    assert "codex_scan_diagnostic" in caplog.text
    assert "persistent_project" in caplog.text
    assert "codex_scan_failed" in caplog.text
    assert "task:" in caplog.text
    assert summary.native_id not in caplog.text
    assert secret not in caplog.text
    assert native_path not in caplog.text


@pytest.mark.asyncio
async def test_codex_continuous_scan_uses_recent_bounded_inventory() -> None:
    summary = _codex_summary("codex-recent", 300.0)
    operations: list[tuple[object, ...]] = []

    class RecentCodexAdapter(_BacklogCodexAdapter):
        def __init__(self) -> None:
            super().__init__(
                inventory_batches=[],
                summaries_by_native_id={summary.native_id: summary},
                operations=operations,
            )
            self.recent_calls: list[tuple[bool, float, frozenset[str]]] = []

        def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
            del archived
            raise AssertionError("continuous scan must not page through full inventory")

        def list_recent_inventory(
            self,
            *,
            archived: bool,
            after: float,
            known_native_ids: frozenset[str],
        ) -> list[CodexThreadSummary]:
            self.recent_calls.append((archived, after, known_native_ids))
            return [] if archived else [summary]

    adapter = RecentCodexAdapter()
    store = _StateStore(operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
    )
    coordinator._continuous_watermark = 250.0
    store.states[_CODEX_SEEN_KEY] = {
        "version": 1,
        "native_ids": ["codex-known"],
    }

    result = await coordinator.scan_once(Provider.CODEX)

    assert result.failed == 0
    assert result.indexed == 1
    assert adapter.recent_calls == [
        (False, 250.0, frozenset({"codex-known"})),
        (True, 250.0, frozenset({"codex-known"})),
    ]
    assert adapter.projected_native_ids == ["codex-recent"]


@pytest.mark.asyncio
async def test_codex_scan_revisits_cataloged_native_when_trusted_origin_appears() -> (
    None
):
    native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    bridge_id = "characterization-11111111-1111-4111-8111-111111111111-codex"
    summary = CodexThreadSummary(
        **{
            **_codex_summary(native_id, 300.0).__dict__,
            "trusted_origin_bridge_id": bridge_id,
        }
    )
    operations: list[tuple[object, ...]] = []

    class CatalogedNativeStore(_StateStore):
        def get_external_session(self, session_id: str) -> dict[str, Any] | None:
            if session_id != f"codex:{native_id}":
                return None
            return {
                "session_id": session_id,
                "provider": Provider.CODEX.value,
                "native_id": native_id,
                "origin_kind": OriginKind.NATIVE.value,
                "origin_bridge_id": None,
            }

    store = CatalogedNativeStore(operations, existing_native_ids={native_id})
    store.states[_CODEX_SEEN_KEY] = {"version": 1, "native_ids": [native_id]}
    adapter = _BacklogCodexAdapter(
        inventory_batches=[[summary], []],
        summaries_by_native_id={native_id: summary},
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
    )

    result = await coordinator.scan_once(Provider.CODEX)

    assert result.discovered == 1
    assert result.indexed == 1
    assert result.failed == 0
    assert store.upsert_attempts == [native_id]
    assert store.projections[0].origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert store.projections[0].origin_bridge_id == bridge_id


@pytest.mark.asyncio
async def test_codex_scan_does_not_trust_seen_cache_without_catalog_provenance() -> (
    None
):
    native_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    bridge_id = "characterization-22222222-2222-4222-8222-222222222222-codex"
    summary = CodexThreadSummary(
        **{
            **_codex_summary(native_id, 300.0).__dict__,
            "trusted_origin_bridge_id": bridge_id,
        }
    )
    operations: list[tuple[object, ...]] = []
    store = _StateStore(operations, existing_native_ids={native_id})
    store.states[_CODEX_SEEN_KEY] = {"version": 1, "native_ids": [native_id]}
    adapter = _BacklogCodexAdapter(
        inventory_batches=[[summary], []],
        summaries_by_native_id={native_id: summary},
        operations=operations,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
    )

    result = await coordinator.scan_once(Provider.CODEX)

    assert result.discovered == 1
    assert result.indexed == 1
    assert store.projections[0].origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert store.projections[0].origin_bridge_id == bridge_id


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
    debounce = _GatedDebounceSleep(0.03)
    coordinator = SessionBridgeCoordinator(
        config=_watcher_config(catalog_scan_seconds=10.0),
        store=object(),
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        claude_projects_root=tmp_path,
        watch_debounce_seconds=0.03,
        awatch_factory=awatch,
        sleep=debounce,
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
        consumed_before = awatch.requests
        awatch.emit(tmp_path / "one.jsonl")
        awatch.emit(tmp_path / "two.jsonl")
        awatch.emit(tmp_path / "three.jsonl")

        # Settled state for the whole burst: all three batches handled to
        # completion AND exactly one surviving debounce parked on the gate.
        # Each batch cancels its predecessor's debounce, so parked dips to 0
        # mid-burst; this predicate is only true once the loop is idle with the
        # last debounce armed.  No debounce can have expired yet -- the gate is
        # still shut -- so the burst provably has not split.
        await _wait_until(
            lambda: awatch.requests == consumed_before + 3 and debounce.parked == 1
        )
        debounce.release.set()
        await _wait_until(lambda: claude.discover_calls == 2)

        assert claude.discover_calls == 2
        # No re-arm: the burst left exactly one debounce, which has now fired.
        assert debounce.parked == 0
        assert awatch.requests == consumed_before + 3
        assert codex.inventory_calls == 2
        assert awatch.paths == (tmp_path,)
        assert coordinator.health()["watcher_state"] == "running"
    finally:
        debounce.release.set()
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
        await _wait_until(lambda: coordinator.health()["watcher_state"] == "degraded")
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
async def test_process_jobs_once_passes_exact_scope_and_limit_to_atomic_store() -> None:
    class ScopedStore(_JobStore):
        def __init__(self) -> None:
            super().__init__()
            self.scoped_claims: list[dict[str, object]] = []

        def claim_due_jobs_with_limits(
            self,
            *,
            now: float,
            limit: int,
            policy: MirrorPolicy,
            job_ids: Sequence[str] | None = None,
        ) -> list[dict[str, Any]]:
            self.scoped_claims.append({
                "now": now,
                "limit": limit,
                "policy": policy,
                "job_ids": job_ids,
            })
            return []

    store = ScopedStore()
    coordinator = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once(
        job_ids=["job:selected-two", "job:selected-one"],
        limit=1,
    )

    assert summary.claimed == 0
    assert store.scoped_claims == [
        {
            "now": 100.0,
            "limit": 1,
            "policy": MirrorPolicy(),
            "job_ids": ("job:selected-two", "job:selected-one"),
        }
    ]
    with pytest.raises(ValueError, match="limit"):
        await coordinator.process_jobs_once(limit=0)


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
async def test_explicit_manual_retry_runs_while_automatic_creation_is_disabled() -> (
    None
):
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
async def test_ambiguous_creation_without_identity_fails_closed_without_duplicate() -> (
    None
):
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
async def test_reconcile_running_job_without_sidecar_retries_without_provider_call() -> (
    None
):
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
async def test_reconcile_running_job_with_sidecar_completes_exact_catalog_target() -> (
    None
):
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


@pytest.fixture
def sidebar_db(tmp_path: Path) -> Iterator[SessionDB]:
    database = SessionDB(tmp_path / "sidebar-state.db")
    yield database
    database.close()


def _sidebar_projection(
    *,
    provider: Provider,
    native_id: str,
    content: str,
    last_active: float,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
    cwd: str | None = "C:/workspace/sidebar",
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=None,
        cwd=cwd,
        started_at=last_active - 10,
        last_active=last_active,
        messages=(
            ProjectedMessage(
                native_event_id=f"event-{provider.value}-{native_id}",
                ordinal=0,
                role="user",
                content=content,
                timestamp=last_active,
            ),
        ),
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
        git_branch="feature/sidebar",
    )


def _sidebar_source_repo(db: SessionDB) -> Path:
    path = db.db_path.parent / "sidebar-source-repo"
    if not path.exists():
        _exact_cwd_repo(path)
    return path


def _add_hermes_sidebar_source(
    db: SessionDB,
    *,
    session_id: str,
    content: str | None,
    last_active: float,
    source: str = "cli",
    model_config: dict[str, str] | None = None,
    cwd: str | None = "C:/workspace/sidebar",
) -> None:
    if cwd == "C:/workspace/sidebar":
        cwd = str(_sidebar_source_repo(db))
    db.ensure_session(
        session_id,
        source=source,
        model_config=model_config,
        cwd=cwd,
    )
    if content is not None:
        db.append_message(
            session_id,
            role="user",
            content=content,
            timestamp=last_active,
        )

    def _write(conn: Any) -> None:
        conn.execute(
            """UPDATE sessions
               SET title = NULL, git_branch = ?, git_repo_root = ?, started_at = ?
               WHERE id = ?""",
            ("feature/sidebar", cwd, last_active - 10, session_id),
        )

    db._execute_write(_write)


def _sidebar_config(
    *,
    enabled: bool = True,
    continuous: bool = False,
    backfill_days: int = 30,
) -> BridgeConfig:
    return replace(
        BridgeConfig(),
        sidebar=replace(
            SidebarConfig(),
            enabled=enabled,
            continuous=continuous,
            backfill_days=backfill_days,
        ),
    )


def _seed_sidebar_sources(
    db: SessionDB,
    *,
    now: float,
) -> SessionBridgeStore:
    store = SessionBridgeStore(db, clock=lambda: now)
    source_cwd = str(_sidebar_source_repo(db))
    cutoff = now - 30 * 86_400
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="meaningful-claude",
            content="Build the native sidebar broker",
            last_active=now,
            cwd=source_cwd,
        )
    )
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CODEX,
            native_id="reverse-loop-codex",
            content="Do not register Codex as a source",
            last_active=now - 1,
            cwd=source_cwd,
        )
    )
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="bridge-placeholder",
            content="This bridge row must stay excluded",
            last_active=now - 2,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge:placeholder",
            cwd=source_cwd,
        )
    )
    _add_hermes_sidebar_source(
        db,
        session_id="meaningful-hermes",
        content="Fix the Hermes catalog",
        last_active=now - 3,
    )
    _add_hermes_sidebar_source(
        db,
        session_id="ack-hermes",
        content="yes",
        last_active=now - 4,
    )
    _add_hermes_sidebar_source(
        db,
        session_id="automation-hermes",
        content="Generate the scheduled digest",
        last_active=now - 5,
        source="cron",
    )
    _add_hermes_sidebar_source(
        db,
        session_id="subagent-hermes",
        content="Inspect the delegated implementation",
        last_active=now - 6,
        source="subagent",
        model_config={"_delegate_from": "parent-session"},
    )
    _add_hermes_sidebar_source(
        db,
        session_id="empty-hermes",
        content=None,
        last_active=now - 7,
    )
    _add_hermes_sidebar_source(
        db,
        session_id="boundary-hermes",
        content="Keep the inclusive boundary",
        last_active=cutoff,
    )
    _add_hermes_sidebar_source(
        db,
        session_id="old-hermes",
        content="This session is too old",
        last_active=cutoff - 0.001,
    )
    return store


def test_sidebar_candidate_query_is_batched_structural_and_stably_paginated(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    cutoff = now - 30 * 86_400
    store = _seed_sidebar_sources(sidebar_db, now=now)

    first_page = store.list_sidebar_candidates(cutoff, 2)
    second_page = store.list_sidebar_candidates(
        cutoff,
        10,
        cursor=first_page.next_cursor,
    )
    sources = [*first_page, *second_page]
    source_ids = [source.projection.native_id for source in sources]

    assert first_page.has_more is True
    assert first_page.next_cursor == (
        first_page[-1].projection.last_active,
        first_page[-1].source_session_id,
    )
    assert source_ids == [
        "meaningful-claude",
        "meaningful-hermes",
        "ack-hermes",
        "automation-hermes",
        "subagent-hermes",
        "empty-hermes",
        "boundary-hermes",
    ]
    assert "reverse-loop-codex" not in source_ids
    assert "bridge-placeholder" not in source_ids
    assert "old-hermes" not in source_ids
    assert (
        next(
            source for source in sources if source.projection.native_id == "ack-hermes"
        )
        .projection.messages[0]
        .content
        == "yes"
    )
    automation = next(
        source
        for source in sources
        if source.projection.native_id == "automation-hermes"
    )
    subagent = next(
        source for source in sources if source.projection.native_id == "subagent-hermes"
    )
    assert automation.automation_only is True
    assert automation.subagent_only is False
    assert subagent.subagent_only is True


def test_sidebar_candidate_query_excludes_only_incoming_hermes_bridge_lineage(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    for offset, session_id in enumerate((
        "ordinary-hermes",
        "outgoing-continues",
        "incoming-continues",
        "outgoing-mirror",
        "incoming-mirror",
    )):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=session_id,
            content=f"Meaningful request for {session_id}",
            last_active=now - offset,
        )
    store.create_link(
        SessionLink(
            id="link-continues",
            from_session_id="outgoing-continues",
            to_session_id="incoming-continues",
            relation=Relation.CONTINUES,
            bridge_id="bridge:continues",
            source_cursor=None,
            source_hash=None,
            created_at=now,
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="outgoing-mirror",
            to_session_id="incoming-mirror",
            relation=Relation.MIRRORS,
            bridge_id="bridge:mirror",
            source_cursor=None,
            source_hash=None,
            created_at=now,
        )
    )

    sources = store.list_sidebar_candidates(now - 100, 100)

    assert {source.source_session_id for source in sources} == {
        "ordinary-hermes",
        "outgoing-continues",
        "outgoing-mirror",
    }


class _ForbiddenSidebarTarget:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"sidebar registration called target adapter method {name}"
        )


@pytest.mark.asyncio
async def test_sidebar_registration_enqueues_only_native_meaningful_sources_once(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = _seed_sidebar_sources(sidebar_db, now=now)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    first = await coordinator.register_sidebar_jobs_once(now=now, limit=100)
    replay = await coordinator.register_sidebar_jobs_once(now=now, limit=100)

    assert first.examined == 7
    assert first.queued == 3
    assert first.by_provider == {"claude": 1, "hermes": 2}
    assert first.failed == 0
    assert replay.queued == 0
    claude_job = store.get_sidebar_job_for_source("claude:meaningful-claude")
    assert claude_job is not None
    assert claude_job["state"] == SidebarJobState.PENDING.value
    assert store.get_sidebar_job_for_source("meaningful-hermes") is not None
    assert store.get_sidebar_job_for_source("boundary-hermes") is not None
    for excluded in (
        "ack-hermes",
        "automation-hermes",
        "subagent-hermes",
        "empty-hermes",
        "old-hermes",
    ):
        assert store.get_sidebar_job_for_source(excluded) is None

    health = coordinator.health()
    assert health["sidebar_registration_counts"] == {
        "examined": 4,
        "queued": 0,
        "claude": 0,
        "hermes": 0,
        "failed": 0,
        "excluded": 0,
        "excluded_by_reason": {"source_cwd_missing": 0},
    }
    serialized = json.dumps(health, sort_keys=True)
    assert "meaningful-claude" not in serialized
    assert "lease_token" not in serialized
    assert "marker" not in serialized


@pytest.mark.asyncio
async def test_sidebar_backfill_preview_is_side_effect_free_and_apply_is_bounded(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = _seed_sidebar_sources(sidebar_db, now=now)
    config = _sidebar_config(continuous=False)
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=False,
    )

    assert preview.queued == 3
    assert preview.by_provider == {"claude": 1, "hermes": 2}
    assert store.sidebar_job_counts() == {
        SidebarJobState.PENDING.value: 0,
        SidebarJobState.LEASED.value: 0,
        SidebarJobState.RETRY.value: 0,
        SidebarJobState.VISIBLE.value: 0,
        SidebarJobState.FAILED.value: 0,
    }
    assert store.get_state("session-bridge:sidebar:registration-cursor") is None
    assert coordinator.health()["sidebar_registration_counts"] == {
        "examined": 0,
        "queued": 0,
        "claude": 0,
        "hermes": 0,
        "failed": 0,
        "excluded": 0,
        "excluded_by_reason": {"source_cwd_missing": 0},
    }

    applied = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=2,
        apply=True,
    )

    assert applied.queued == 2
    assert store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 2
    assert store.get_state("session-bridge:sidebar:registration-cursor") is None
    assert config.sidebar.continuous is False


@pytest.mark.asyncio
async def test_sidebar_all_history_backfill_includes_oldest_candidate(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = _seed_sidebar_sources(sidebar_db, now=now)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=None,
        limit=10,
        apply=False,
    )

    assert preview.queued == 4
    assert store.get_sidebar_job_for_source("old-hermes") is None


@pytest.mark.asyncio
async def test_sidebar_backfill_preview_matches_apply_exclusions(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    valid = _exact_cwd_repo(tmp_path / "valid")
    deleted = _exact_cwd_repo(tmp_path / "deleted")
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="valid-preview",
            content="Keep this exact worktree",
            last_active=now,
            cwd=str(valid),
        )
    )
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="deleted-preview",
            content="This historical worktree is gone",
            last_active=now - 1,
            cwd=str(deleted),
        )
    )
    _remove_tree(deleted)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=False,
    )

    assert preview.queued == 1
    assert preview.failed == 0
    assert preview.excluded == 1
    assert preview.excluded_by_reason == {"source_cwd_missing": 1}
    fixed_error_code = next(iter(preview.excluded_by_reason))
    assert fixed_error_code in {
        "marker_conflict",
        "source_identity_mismatch",
        "source_cwd_missing",
        "native_task_not_indexed",
        "hydration_send_ambiguous",
    }
    assert store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 0
    assert store.sidebar_exclusion_counts()["total"] == 0

    applied = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=True,
    )

    assert asdict(applied) == asdict(preview)
    assert store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 1
    assert store.sidebar_exclusion_counts() == {
        "total": 1,
        "by_reason": {"source_cwd_missing": 1},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["permission_preflight_failed", "source_identity_mismatch"],
)
async def test_sidebar_backfill_nonmissing_preflight_error_is_not_excluded(
    sidebar_db: SessionDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    now = 3_000_000.0
    source = _exact_cwd_repo(tmp_path / "source")
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id=f"preflight-{error_code}",
            content="Preserve unknown preflight failures",
            last_active=now,
            cwd=str(source),
        )
    )

    def _raise_preflight(_cwd: str) -> None:
        raise WorktreeSnapshotError(error_code)

    monkeypatch.setattr(
        "session_bridge.coordinator.capture_worktree_snapshot",
        _raise_preflight,
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=False,
    )

    assert preview.queued == 0
    assert preview.failed == 1
    assert preview.excluded == 0
    assert preview.excluded_by_reason == {"source_cwd_missing": 0}
    assert store.sidebar_exclusion_counts()["total"] == 0


@pytest.mark.asyncio
async def test_sidebar_backfill_confirms_transient_identity_capture_failure(
    sidebar_db: SessionDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 3_000_000.0
    source = _exact_cwd_repo(tmp_path / "source")
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="transient-identity-capture",
            content="Confirm a transient Git capture timeout",
            last_active=now,
            cwd=str(source),
        )
    )
    real_capture = capture_worktree_snapshot
    calls = 0

    def _capture_with_one_transient_failure(cwd: str) -> WorktreeSnapshot:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise WorktreeSnapshotError("source_identity_mismatch")
        return real_capture(cwd)

    monkeypatch.setattr(
        "session_bridge.coordinator.capture_worktree_snapshot",
        _capture_with_one_transient_failure,
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=False,
    )

    assert calls == 3
    assert preview.queued == 1
    assert preview.failed == 0
    assert preview.excluded == 0


@pytest.mark.asyncio
async def test_sidebar_backfill_existing_job_wins_after_cwd_disappears(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    source = _exact_cwd_repo(tmp_path / "source")
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="existing-job",
            content="Keep the existing delivery job",
            last_active=now,
            cwd=str(source),
        )
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )
    applied = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=1,
        apply=True,
    )
    assert applied.queued == 1
    _remove_tree(source)

    replay = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=1,
        apply=False,
    )

    assert replay.queued == 0
    assert replay.failed == 0
    assert replay.excluded == 0
    assert store.sidebar_exclusion_counts()["total"] == 0


@pytest.mark.asyncio
async def test_persisted_sidebar_exclusions_do_not_starve_older_valid_source(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    for offset in range(41):
        native_id = f"excluded-{offset}"
        store.upsert_projection(
            _sidebar_projection(
                provider=Provider.CLAUDE,
                native_id=native_id,
                content="Historical deleted worktree",
                last_active=now - offset,
                cwd=str(tmp_path / native_id),
            )
        )
        store.record_sidebar_exclusion(
            source_session_id=f"claude:{native_id}",
            provider=Provider.CLAUDE,
            reason_code="source_cwd_missing",
            now=now,
        )
    valid = _exact_cwd_repo(tmp_path / "older-valid")
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="older-valid",
            content="Reach the valid source after exclusions",
            last_active=now - 100,
            cwd=str(valid),
        )
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=1,
        apply=False,
    )

    assert preview.examined == 1
    assert preview.queued == 1
    assert preview.failed == 0
    assert preview.excluded == 0


@pytest.mark.asyncio
async def test_backfill_paginates_past_ineligible_sources(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    for offset in range(40):
        store.upsert_projection(
            _sidebar_projection(
                provider=Provider.CLAUDE,
                native_id=f"acknowledgement-{offset}",
                content="ok",
                last_active=now - offset,
                cwd=str(tmp_path),
            )
        )
    valid = _exact_cwd_repo(tmp_path / "older-meaningful")
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="older-meaningful",
            content="Reach the meaningful source after acknowledgements",
            last_active=now - 100,
            cwd=str(valid),
        )
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=1,
        apply=False,
    )

    assert preview.examined == 41
    assert preview.queued == 1
    assert preview.failed == 0
    assert preview.excluded == 0


@pytest.mark.asyncio
async def test_backfill_preview_never_exceeds_its_queue_limit(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="newest-acknowledgement",
            content="ok",
            last_active=now,
            cwd=str(tmp_path),
        )
    )
    valid = _exact_cwd_repo(tmp_path / "bounded-preview")
    for offset in range(11):
        store.upsert_projection(
            _sidebar_projection(
                provider=Provider.CLAUDE,
                native_id=f"bounded-{offset}",
                content=f"Queue bounded meaningful request {offset}",
                last_active=now - offset - 1,
                cwd=str(valid),
            )
        )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=now,
        days=30,
        limit=10,
        apply=False,
    )

    assert preview.examined == 11
    assert preview.queued == 10
    assert preview.by_provider == {"claude": 10, "hermes": 0}


class _HydrationClaimStore:
    def __init__(
        self,
        *,
        candidate: SidebarCandidate,
        snapshot: dict[str, Any],
        preview_digest: str,
        marker: str,
    ) -> None:
        self.candidate = candidate
        self.snapshot = snapshot
        self.failed: list[tuple[str, str, str]] = []
        self.raw = {
            "lease_token": "hydration-lease",
            "source_session_id": candidate.source_session_id,
            "bridge_id": candidate.bridge_id,
            "codex_thread_id": "codex-thread-1",
            "source_cursor": snapshot["source_cursor"],
            "source_hash": snapshot["source_hash"],
            "preview_version": 1,
            "preview_digest": preview_digest,
            "hydration_marker": marker,
            "send_reserved": False,
        }

    def claim_sidebar_hydration_jobs(
        self,
        *,
        now: float,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert now == 100.0
        assert limit == 1
        return [dict(self.raw)]

    def get_sidebar_candidate_for_delivery(
        self,
        source_session_id: str,
    ) -> SidebarCandidate:
        assert source_session_id == self.candidate.source_session_id
        return self.candidate

    def get_sidebar_preview_source(self, source_session_id: str) -> dict[str, Any]:
        assert source_session_id == self.candidate.source_session_id
        return dict(self.snapshot)

    def fail_sidebar_hydration_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        assert now == 100.0
        self.failed.append((lease_token, error_code, codex_thread_id))
        return {"state": "hydration_failed"}


@pytest.mark.asyncio
async def test_hydration_claim_rebuilds_and_verifies_preview_before_send() -> None:
    candidate = SidebarCandidate(
        source_session_id="claude:source-1",
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id("claude:source-1"),
        title="[Claude] Source",
        cwd="C:/work/source",
        git_root="C:/repo/source",
        git_branch="main",
        git_head="a" * 40,
        worktree_id="worktree-1",
        eligible_at=90.0,
    )
    snapshot = {
        "source_session_id": candidate.source_session_id,
        "provider": "claude",
        "source_cursor": "cursor-1",
        "source_hash": "hash-1",
        "title": "Source",
        "cwd": candidate.cwd,
        "captured_at": 99.0,
        "messages": [
            {
                "role": "user",
                "content": "Fix the remaining hydration work.",
                "timestamp": 99.0,
            }
        ],
    }
    preview = build_session_preview(
        source_session_id=candidate.source_session_id,
        source_cursor=snapshot["source_cursor"],
        source_hash=snapshot["source_hash"],
        title=snapshot["title"],
        provider=candidate.provider.value,
        cwd=candidate.cwd,
        captured_at=snapshot["captured_at"],
        messages=snapshot["messages"],
        git_root=candidate.git_root,
        git_branch=candidate.git_branch,
        git_head=candidate.git_head,
        worktree_id=candidate.worktree_id,
    )
    payload = HydrationMarkerPayload(
        bridge_id=candidate.bridge_id,
        codex_thread_id="codex-thread-1",
        preview_digest=preview.digest,
        preview_version=1,
        source_cursor=snapshot["source_cursor"],
        source_hash=snapshot["source_hash"],
        source_session_id=candidate.source_session_id,
    )
    marker = encode_hydration_marker(payload, b"coordinator-hydration-secret")
    store = _HydrationClaimStore(
        candidate=candidate,
        snapshot=snapshot,
        preview_digest=preview.digest,
        marker=marker,
    )
    coordinator = SessionBridgeCoordinator(
        config=replace(
            _sidebar_config(),
            sidebar=replace(
                _sidebar_config().sidebar,
                legacy_hydration_enabled=True,
            ),
        ),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 100.0,
    )

    claims = await coordinator.claim_sidebar_hydration_for_delivery(limit=1)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.codex_thread_id == "codex-thread-1"
    assert claim.send_reserved is False
    assert claim.hydration_message.startswith("# Imported Claude Code Session")
    assert "This is an authenticated in-place Session Bridge hydration." in (
        claim.hydration_message
    )
    assert f"Hydration marker: {marker}" in claim.hydration_message
    assert store.failed == []


class _HeartbeatClaimStore:
    def __init__(
        self,
        *,
        fail_claim: bool = False,
        fail_heartbeat: bool = False,
        block_heartbeat: bool = False,
    ) -> None:
        self.fail_claim = fail_claim
        self.fail_heartbeat = fail_heartbeat
        self.block_heartbeat = block_heartbeat
        self.heartbeats: list[float] = []
        self.events: list[str] = []
        self.registration_claims = 0
        self.hydration_claims = 0
        self.heartbeat_started = Event()
        self.heartbeat_release = Event()

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        self.events.append("registration_claim")
        self.registration_claims += 1
        if self.fail_claim:
            raise RuntimeError("claim failed")
        return []

    def record_sidebar_broker_heartbeat(self, *, now: float) -> None:
        self.events.append("heartbeat")
        self.heartbeats.append(now)
        self.heartbeat_started.set()
        if self.fail_heartbeat:
            raise RuntimeError("heartbeat failed")
        if self.block_heartbeat:
            assert self.heartbeat_release.wait(timeout=5)

    def claim_sidebar_hydration_jobs(
        self, *, now: float, limit: int
    ) -> list[dict[str, Any]]:
        assert limit == 1
        self.events.append("hydration_claim")
        self.hydration_claims += 1
        return []


class _EmptySidebarVerifier:
    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        return None

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        raise AssertionError("empty delivery must not verify a thread")


@pytest.mark.asyncio
async def test_sidebar_delivery_records_heartbeat_before_registration_claim() -> (
    None
):
    successful = _HeartbeatClaimStore()
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=successful,
        adapters={},
        target_adapters={},
        sidebar_verifier=_EmptySidebarVerifier(),
        clock=lambda: 100.0,
    )

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()
    assert successful.heartbeats == [100.0]
    assert successful.events == ["heartbeat", "registration_claim"]

    failing = _HeartbeatClaimStore(fail_claim=True)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=failing,
        adapters={},
        target_adapters={},
        sidebar_verifier=_EmptySidebarVerifier(),
        clock=lambda: 100.0,
    )
    with pytest.raises(RuntimeError, match="claim failed"):
        await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    assert failing.heartbeats == [100.0]
    assert failing.events == ["heartbeat", "registration_claim"]


@pytest.mark.asyncio
async def test_sidebar_hydration_empty_claim_records_broker_heartbeat() -> None:
    store = _HeartbeatClaimStore()
    coordinator = SessionBridgeCoordinator(
        config=replace(
            _sidebar_config(),
            sidebar=replace(
                _sidebar_config().sidebar,
                legacy_hydration_enabled=True,
            ),
        ),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 500.0,
    )

    assert await coordinator.claim_sidebar_hydration_for_delivery(limit=1) == ()
    assert store.heartbeats == [500.0]
    assert store.events == ["heartbeat", "hydration_claim"]


@pytest.mark.asyncio
async def test_sidebar_heartbeat_failure_prevents_both_claim_acquisitions() -> None:
    store = _HeartbeatClaimStore(fail_heartbeat=True)
    enabled_config = replace(
        _sidebar_config(),
        sidebar=replace(_sidebar_config().sidebar, legacy_hydration_enabled=True),
    )
    coordinator = SessionBridgeCoordinator(
        config=enabled_config,
        store=store,
        adapters={},
        target_adapters={},
        sidebar_verifier=_EmptySidebarVerifier(),
        clock=lambda: 502.0,
    )

    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await coordinator.claim_sidebar_jobs_for_delivery(now=502.0, limit=1)
    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await coordinator.claim_sidebar_hydration_for_delivery(limit=1)

    assert store.registration_claims == 0
    assert store.hydration_claims == 0


@pytest.mark.asyncio
async def test_cancelled_heartbeat_cannot_acquire_a_hydration_lease() -> None:
    store = _HeartbeatClaimStore(block_heartbeat=True)
    coordinator = SessionBridgeCoordinator(
        config=replace(
            _sidebar_config(),
            sidebar=replace(_sidebar_config().sidebar, legacy_hydration_enabled=True),
        ),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 503.0,
    )

    task = asyncio.create_task(coordinator.claim_sidebar_hydration_for_delivery())
    assert await asyncio.to_thread(store.heartbeat_started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.heartbeat_release.set()

    assert store.hydration_claims == 0


@pytest.mark.asyncio
async def test_disabled_sidebar_claim_paths_record_broker_heartbeat_before_return() -> None:
    store = _HeartbeatClaimStore()
    coordinator = SessionBridgeCoordinator(
        config=replace(
            _sidebar_config(),
            sidebar=replace(
                _sidebar_config().sidebar,
                enabled=False,
                legacy_hydration_enabled=False,
            ),
        ),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 501.0,
    )

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=501.0, limit=1) == ()
    assert await coordinator.claim_sidebar_hydration_for_delivery(limit=1) == ()
    assert store.heartbeats == [501.0, 501.0]


@pytest.mark.asyncio
async def test_sidebar_registration_isolates_malformed_claude_from_hermes(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="bad-cwd",
            content="Queue this source",
            last_active=now,
            cwd=None,
        )
    )
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="healthy-hermes",
        content="Queue the healthy provider",
        last_active=now - 1,
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    summary = await coordinator.register_sidebar_jobs_once(now=now, limit=100)

    assert summary.queued == 1
    assert summary.by_provider == {"claude": 0, "hermes": 1}
    assert summary.failed == 0
    assert summary.excluded == 1
    assert summary.excluded_by_reason == {"source_cwd_missing": 1}
    assert store.get_sidebar_job_for_source("healthy-hermes") is not None
    assert store.get_sidebar_job_for_source("claude:bad-cwd") is None


@pytest.mark.asyncio
async def test_sidebar_registration_rejects_filesystem_snapshot_for_indexed_git_source(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    source = tmp_path / "indexed-git-source"
    source.mkdir()
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="indexed-git-hermes",
        content="Continue this indexed Git source",
        last_active=now,
        cwd=str(source),
    )
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    summary = await coordinator.register_sidebar_jobs_once(now=now, limit=1)

    assert summary.queued == 0
    assert summary.failed == 1
    assert store.get_sidebar_job_for_source("indexed-git-hermes") is None
    assert store.get_worktree_snapshot("indexed-git-hermes") is None


@pytest.mark.asyncio
async def test_sidebar_registration_accepts_plain_source_without_indexed_git_metadata(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    source = tmp_path / "plain-source"
    source.mkdir()
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="plain-hermes",
        content="Continue this plain directory session",
        last_active=now,
        cwd=str(source),
    )

    def clear_git_metadata(conn: Any) -> None:
        conn.execute(
            "UPDATE sessions SET git_branch = NULL, git_repo_root = NULL WHERE id = ?",
            ("plain-hermes",),
        )

    sidebar_db._execute_write(clear_git_metadata)
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    summary = await coordinator.register_sidebar_jobs_once(now=now, limit=1)

    assert summary.queued == 1
    snapshot = store.get_worktree_snapshot("plain-hermes")
    assert snapshot is not None
    assert snapshot.git_root is None


@pytest.mark.asyncio
async def test_sidebar_registration_preserves_claude_catalog_indexed_at(
    sidebar_db: SessionDB,
) -> None:
    source_active_at = 3_000_000.0
    indexed_at = source_active_at + 6.0
    registration_time = indexed_at + 8.0
    clock = [indexed_at]
    store = SessionBridgeStore(sidebar_db, clock=lambda: clock[0])
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="indexed-timing-claude",
            content="Measure this Claude delivery pipeline",
            last_active=source_active_at,
            cwd=str(_sidebar_source_repo(sidebar_db)),
        )
    )
    clock[0] = registration_time
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: registration_time,
    )

    summary = await coordinator.register_sidebar_jobs_once(
        now=registration_time,
        limit=1,
    )

    assert summary.queued == 1
    job = store.get_sidebar_job_for_source("claude:indexed-timing-claude")
    assert job is not None
    assert job["eligible_at"] == source_active_at
    assert job["indexed_at"] == indexed_at
    assert job["created_at"] == registration_time


@pytest.mark.asyncio
async def test_sidebar_registration_uses_cycle_time_for_hermes_indexed_at(
    sidebar_db: SessionDB,
) -> None:
    source_active_at = 3_000_000.0
    registration_time = source_active_at + 14.0
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="indexed-timing-hermes",
        content="Measure this Hermes delivery pipeline",
        last_active=source_active_at,
    )
    store = SessionBridgeStore(sidebar_db, clock=lambda: registration_time)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: registration_time,
    )

    summary = await coordinator.register_sidebar_jobs_once(
        now=registration_time,
        limit=1,
    )

    assert summary.queued == 1
    job = store.get_sidebar_job_for_source("indexed-timing-hermes")
    assert job is not None
    assert job["eligible_at"] == source_active_at
    assert job["indexed_at"] == registration_time
    assert job["created_at"] == registration_time


@pytest.mark.asyncio
async def test_sidebar_registration_accepts_head_sentinel_without_git_identity(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    now = 3_000_000.0
    source = tmp_path / "plain-head-source"
    source.mkdir()
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="plain-head-hermes",
        content="Continue this non Git directory session",
        last_active=now,
        cwd=str(source),
    )

    def set_head_sentinel(conn: Any) -> None:
        conn.execute(
            "UPDATE sessions SET git_branch = 'HEAD', git_repo_root = NULL "
            "WHERE id = ?",
            ("plain-head-hermes",),
        )

    sidebar_db._execute_write(set_head_sentinel)
    store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    summary = await coordinator.register_sidebar_jobs_once(now=now, limit=1)

    assert summary.queued == 1
    assert summary.failed == 0
    snapshot = store.get_worktree_snapshot("plain-head-hermes")
    assert snapshot is not None
    assert snapshot.git_root is None


class _SidebarScanStore(_RecordingStore):
    def __init__(self) -> None:
        super().__init__()
        self.state: dict[str, dict[str, object]] = {}
        self.sidebar_list_calls: list[tuple[float, int, object]] = []

    def get_state(self, key: str) -> dict[str, object] | None:
        return self.state.get(key)

    def set_state(self, key: str, value: Mapping[str, object]) -> None:
        self.state[key] = dict(value)

    def list_sidebar_candidates(
        self,
        after: float,
        limit: int,
        cursor: object = None,
    ) -> SidebarSourcePage:
        self.sidebar_list_calls.append((after, limit, cursor))
        return SidebarSourcePage()


def _paged_sidebar_source(
    *,
    provider: Provider,
    native_id: str,
    content: str,
    last_active: float,
) -> SidebarSource:
    projection = _sidebar_projection(
        provider=provider,
        native_id=native_id,
        content=content,
        last_active=last_active,
    )
    source_session_id = (
        f"claude:{native_id}" if provider is Provider.CLAUDE else native_id
    )
    return SidebarSource(
        source_session_id=source_session_id,
        projection=projection,
        git_root="C:/workspace/sidebar",
        git_head=None,
        worktree_id=None,
        automation_only=False,
        subagent_only=False,
    )


class _PagedSidebarStore:
    def __init__(
        self,
        pages: Mapping[object, SidebarSourcePage],
        *,
        existing: set[str] | None = None,
    ) -> None:
        self.pages = dict(pages)
        self.existing = set(existing or ())
        self.state: dict[str, dict[str, object]] = {}
        self.list_calls: list[tuple[float, int, object]] = []
        self.enqueued: list[SidebarCandidate] = []

    def get_state(self, key: str) -> dict[str, object] | None:
        return self.state.get(key)

    def set_state(self, key: str, value: Mapping[str, object]) -> None:
        self.state[key] = dict(value)

    def list_sidebar_candidates(
        self,
        after: float,
        limit: int,
        *,
        cursor: tuple[float, str] | None = None,
    ) -> SidebarSourcePage:
        self.list_calls.append((after, limit, cursor))
        return self.pages[cursor]

    def get_sidebar_job_for_source(self, source_session_id: str) -> object | None:
        return {} if source_session_id in self.existing else None

    def enqueue_sidebar_job(self, candidate: SidebarCandidate) -> dict[str, object]:
        self.existing.add(candidate.source_session_id)
        self.enqueued.append(candidate)
        return {"source_session_id": candidate.source_session_id, "created": True}


@pytest.mark.asyncio
async def test_sidebar_registration_drains_pages_past_ineligible_and_existing_rows() -> (
    None
):
    now = 3_000_000.0
    first_cursor = (now - 1, "ack-first-page")
    page_one = SidebarSourcePage(
        (
            _paged_sidebar_source(
                provider=Provider.HERMES,
                native_id="ack-first-page",
                content="yes",
                last_active=now,
            ),
            _paged_sidebar_source(
                provider=Provider.CLAUDE,
                native_id="already-enqueued",
                content="Keep this existing job",
                last_active=now - 1,
            ),
        ),
        has_more=True,
        next_cursor=first_cursor,
    )
    page_two = SidebarSourcePage(
        (
            _paged_sidebar_source(
                provider=Provider.CLAUDE,
                native_id="eligible-second-page",
                content="Queue the older Claude request",
                last_active=now - 2,
            ),
            _paged_sidebar_source(
                provider=Provider.HERMES,
                native_id="eligible-hermes-second-page",
                content="Queue the older Hermes request",
                last_active=now - 3,
            ),
        ),
    )
    store = _PagedSidebarStore(
        {None: page_one, first_cursor: page_two},
        existing={"claude:already-enqueued"},
    )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    summary = await coordinator.register_sidebar_jobs_once(now=now, limit=2)

    assert summary.examined == 4
    assert summary.queued == 2
    assert summary.failed == 0
    assert summary.by_provider == {"claude": 1, "hermes": 1}
    assert [candidate.source_session_id for candidate in store.enqueued] == [
        "claude:eligible-second-page",
        "eligible-hermes-second-page",
    ]
    assert store.list_calls == [
        (now - 30 * 86_400, 30, None),
        (now - 30 * 86_400, 2, first_cursor),
    ]


@pytest.mark.asyncio
async def test_sidebar_registration_rejects_a_repeated_pagination_cursor() -> None:
    now = 3_000_000.0
    repeated_cursor = (now, "ack-loop")
    page = SidebarSourcePage(
        (
            _paged_sidebar_source(
                provider=Provider.HERMES,
                native_id="ack-loop",
                content="yes",
                last_active=now,
            ),
        ),
        has_more=True,
        next_cursor=repeated_cursor,
    )
    store = _PagedSidebarStore({None: page, repeated_cursor: page})
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="sidebar candidate cursor"):
        await coordinator.register_sidebar_jobs_once(now=now, limit=1)


class _BudgetRecordingSidebarStore(SessionBridgeStore):
    def __init__(self, db: SessionDB, *, clock: Callable[[], float]) -> None:
        super().__init__(db, clock=clock)
        self.list_calls: list[tuple[float, int, object]] = []

    def list_sidebar_candidates(
        self,
        after: float,
        limit: int,
        *,
        cursor: tuple[float, str] | None = None,
    ) -> SidebarSourcePage:
        self.list_calls.append((after, limit, cursor))
        return super().list_sidebar_candidates(after, limit, cursor=cursor)


@pytest.mark.asyncio
async def test_sidebar_registration_is_bounded_durable_and_probes_newest_first(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = _BudgetRecordingSidebarStore(sidebar_db, clock=lambda: now)
    for offset in range(50):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=f"ack-{offset:02d}",
            content="yes",
            last_active=now - offset,
        )
    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="older-eligible",
        content="Queue this older eligible request",
        last_active=now - 100,
    )
    first = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    first_summary = await first.register_sidebar_jobs_once(now=now, limit=1)
    first_call_count = len(store.list_calls)
    durable_after_first = store.get_state("session-bridge:sidebar:registration-cursor")

    assert first_summary.queued == 0
    assert first_summary.examined <= 40
    assert 1 <= first_call_count <= 4
    assert durable_after_first is not None
    assert store.get_sidebar_job_for_source("older-eligible") is None

    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="newest-eligible",
        content="Queue this newly arrived request",
        last_active=now + 1,
    )
    for offset in range(15):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=f"newer-automation-{offset}",
            content="Automated maintenance result",
            last_active=now + 2 + offset,
            source="cron",
        )
    restarted = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now + 1,
    )
    before_newest_probe = len(store.list_calls)

    newest_summary = await restarted.register_sidebar_jobs_once(now=now + 1, limit=1)

    assert newest_summary.queued == 1
    assert len(store.list_calls) - before_newest_probe == 1
    assert store.get_sidebar_job_for_source("newest-eligible") is not None
    assert (
        store.get_state("session-bridge:sidebar:registration-cursor")
        == durable_after_first
    )

    older_summary = None
    for _ in range(8):
        continued = SessionBridgeCoordinator(
            config=_sidebar_config(),
            store=store,
            adapters={},
            target_adapters={},
            clock=lambda: now + 1,
        )
        before = len(store.list_calls)
        older_summary = await continued.register_sidebar_jobs_once(
            now=now + 1,
            limit=1,
        )
        assert 1 <= len(store.list_calls) - before <= 4
        assert older_summary.examined <= 40
        if store.get_sidebar_job_for_source("older-eligible") is not None:
            break

    assert older_summary is not None
    assert store.get_sidebar_job_for_source("older-eligible") is not None


@pytest.mark.asyncio
async def test_sidebar_registration_probes_new_profile_session_before_catchup(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    profile_path = sidebar_db.db_path.parent / "profiles" / "main" / "state.db"
    store = _BudgetRecordingSidebarStore(sidebar_db, clock=lambda: now)
    store._hermes_profile_db_paths = lambda: (("main", profile_path),)
    for offset in range(50):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=f"profile-catchup-ack-{offset:02d}",
            content="yes",
            last_active=now - offset,
        )
    first = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    await first.register_sidebar_jobs_once(now=now, limit=1)
    durable_cursor = store.get_state("session-bridge:sidebar:registration-cursor")
    assert durable_cursor is not None

    profile_path.parent.mkdir(parents=True)
    profile_db = SessionDB(profile_path)
    try:
        profile_db.create_session(
            "fresh-profile-session",
            "tui",
            cwd=str(_sidebar_source_repo(sidebar_db)),
        )
        profile_db.append_message(
            "fresh-profile-session",
            "user",
            "Queue this newly persisted profile session",
            timestamp=now + 1,
        )
    finally:
        profile_db.close()
    restarted = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now + 1,
    )

    summary = await restarted.register_sidebar_jobs_once(now=now + 1, limit=1)

    assert summary.queued == 1
    assert summary.by_provider == {"claude": 0, "hermes": 1}
    assert store.get_sidebar_job_for_source("fresh-profile-session") is not None
    assert (
        store.get_state("session-bridge:sidebar:registration-cursor")
        == durable_cursor
    )


@pytest.mark.asyncio
async def test_sidebar_registration_catches_up_new_gap_beyond_newest_probe(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    store = _BudgetRecordingSidebarStore(sidebar_db, clock=lambda: now)
    for offset in range(50):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=f"historical-ack-{offset:02d}",
            content="yes",
            last_active=now - 1_000 - offset,
        )
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now,
    )

    await coordinator.register_sidebar_jobs_once(now=now, limit=1)
    historical_cursor = store.get_state(
        "session-bridge:sidebar:registration-cursor"
    )
    assert historical_cursor is not None

    _add_hermes_sidebar_source(
        sidebar_db,
        session_id="new-gap-eligible",
        content="Register this recovered Claude-style session",
        last_active=now + 1,
    )
    for offset in range(30):
        _add_hermes_sidebar_source(
            sidebar_db,
            session_id=f"new-gap-automation-{offset:02d}",
            content="Automated maintenance result",
            last_active=now + 2 + offset,
            source="cron",
        )

    restarted = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: now + 32,
    )
    summary = await restarted.register_sidebar_jobs_once(now=now + 32, limit=1)

    assert summary.queued == 1
    assert store.get_sidebar_job_for_source("new-gap-eligible") is not None
    assert (
        store.get_state("session-bridge:sidebar:registration-cursor")
        == historical_cursor
    )


class _SlowEmptySidebarStore:
    def __init__(self) -> None:
        self.state: dict[str, dict[str, object]] = {}
        self.guard = Lock()
        self.active = 0
        self.max_active = 0

    def get_state(self, key: str) -> dict[str, object] | None:
        return self.state.get(key)

    def set_state(self, key: str, value: Mapping[str, object]) -> None:
        self.state[key] = dict(value)

    def list_sidebar_candidates(
        self,
        after: float,
        limit: int,
        *,
        cursor: object = None,
    ) -> SidebarSourcePage:
        del after, limit, cursor
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self.guard:
            self.active -= 1
        return SidebarSourcePage()


@pytest.mark.asyncio
async def test_sidebar_registration_serializes_calls_within_one_coordinator() -> None:
    store = _SlowEmptySidebarStore()
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 3_000_000.0,
    )

    await asyncio.gather(
        coordinator.register_sidebar_jobs_once(limit=1),
        coordinator.register_sidebar_jobs_once(limit=1),
    )

    assert store.max_active == 1


@pytest.mark.asyncio
async def test_sidebar_registration_caps_candidate_page_size() -> None:
    store = _SidebarScanStore()
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(),
        store=store,
        adapters={},
        target_adapters={},
        clock=lambda: 3_000_000.0,
    )

    await coordinator.register_sidebar_jobs_once(limit=100)

    assert store.sidebar_list_calls == [(3_000_000.0 - 30 * 86_400, 30, None)]


class _BarrierEnqueueSidebarStore(SessionBridgeStore):
    def __init__(
        self,
        db: SessionDB,
        barrier: Barrier,
        *,
        clock: Callable[[], float],
    ) -> None:
        super().__init__(db, clock=clock)
        self.barrier = barrier

    def enqueue_sidebar_job(self, candidate: SidebarCandidate) -> dict[str, Any]:
        self.barrier.wait(timeout=5)
        return super().enqueue_sidebar_job(candidate)


@pytest.mark.asyncio
async def test_concurrent_sidebar_registration_counts_one_transactional_enqueue(
    sidebar_db: SessionDB,
) -> None:
    now = 3_000_000.0
    seed_store = SessionBridgeStore(sidebar_db, clock=lambda: now)
    seed_store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="concurrent-source",
            content="Queue this source exactly once",
            last_active=now,
        )
    )
    barrier = Barrier(2)
    coordinators = [
        SessionBridgeCoordinator(
            config=_sidebar_config(),
            store=_BarrierEnqueueSidebarStore(
                sidebar_db,
                barrier,
                clock=lambda: now,
            ),
            adapters={},
            target_adapters={},
            clock=lambda: now,
        )
        for _ in range(2)
    ]

    summaries = await asyncio.gather(
        *(
            coordinator.register_sidebar_jobs_once(now=now, limit=1)
            for coordinator in coordinators
        )
    )

    assert sum(summary.queued for summary in summaries) == 1
    assert seed_store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 1


@pytest.mark.asyncio
async def test_sidebar_registration_validates_clock_before_disabled_gate() -> None:
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(enabled=False),
        store=_SidebarScanStore(),
        adapters={},
        target_adapters={},
        clock=lambda: float("nan"),
    )

    with pytest.raises(RuntimeError, match="now is invalid"):
        await coordinator.register_sidebar_jobs_once()


@pytest.mark.parametrize("continuous", (False, True))
@pytest.mark.asyncio
async def test_successful_provider_scan_only_registers_sidebar_in_continuous_mode(
    continuous: bool,
) -> None:
    now = 3_000_000.0
    store = _SidebarScanStore()
    event_loop_thread = get_ident()
    executor_threads: list[int] = []

    class RecordingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            executor_threads.append(get_ident())
            return SidebarExecutionResult(status="idle")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=continuous),
        store=store,
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        sidebar_executor=RecordingExecutor(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert len(store.sidebar_list_calls) == int(continuous)
    assert len(executor_threads) == int(continuous)
    if continuous:
        assert executor_threads[0] != event_loop_thread
    if continuous:
        assert store.sidebar_list_calls == [(now - 30 * 86_400, 30, None)]


@pytest.mark.asyncio
async def test_successful_claude_scan_registers_sidebar_jobs_without_executor() -> None:
    now = 3_000_000.0
    source = _paged_sidebar_source(
        provider=Provider.CLAUDE,
        native_id="desktop-broker-candidate",
        content="Queue this source for the Desktop broker",
        last_active=now,
    )
    store = _PagedSidebarStore({None: SidebarSourcePage((source,))})
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert coordinator._sidebar_executor is None
    assert [candidate.source_session_id for candidate in store.enqueued] == [
        "claude:desktop-broker-candidate"
    ]


@pytest.mark.asyncio
async def test_sidebar_executor_does_not_run_when_any_provider_scan_degrades() -> None:
    now = 3_000_000.0
    store = _SidebarScanStore()
    executor_calls: list[str] = []

    class RecordingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            executor_calls.append("run_once")
            return SidebarExecutionResult(status="idle")

    class DegradedCodexAdapter:
        def list_inventory(self, *, archived: bool) -> list[object]:
            del archived
            raise RuntimeError("private provider detail")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={
            Provider.CLAUDE: _LifecycleClaudeAdapter(),
            Provider.CODEX: DegradedCodexAdapter(),
        },
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        sidebar_executor=RecordingExecutor(),
    )

    summary = await coordinator.scan_once()

    assert summary.failed == 1
    assert executor_calls == []


@pytest.mark.asyncio
async def test_sidebar_executor_does_not_run_while_another_provider_is_degraded() -> (
    None
):
    now = 3_000_000.0
    store = _SidebarScanStore()
    executor_calls: list[str] = []

    class RecordingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            executor_calls.append("run_once")
            return SidebarExecutionResult(status="idle")

    class DegradedCodexAdapter:
        def list_inventory(self, *, archived: bool) -> list[object]:
            del archived
            raise RuntimeError("private provider detail")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={
            Provider.CLAUDE: _LifecycleClaudeAdapter(),
            Provider.CODEX: DegradedCodexAdapter(),
        },
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        sidebar_executor=RecordingExecutor(),
    )

    degraded = await coordinator.scan_once(Provider.CODEX)
    healthy = await coordinator.scan_once(Provider.CLAUDE)

    assert degraded.failed == 1
    assert healthy.failed == 0
    assert executor_calls == []


@pytest.mark.asyncio
async def test_sidebar_executor_waits_for_every_configured_provider_preflight() -> None:
    executor_calls: list[str] = []

    class RecordingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            executor_calls.append("run_once")
            return SidebarExecutionResult(status="idle")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=_SidebarScanStore(),
        adapters={
            Provider.CLAUDE: _LifecycleClaudeAdapter(),
            Provider.CODEX: _LifecycleCodexAdapter(),
        },
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: 3_000_000.0,
        sidebar_executor=RecordingExecutor(),
    )

    claude = await coordinator.scan_once(Provider.CLAUDE)
    assert claude.failed == 0
    assert executor_calls == []

    codex = await coordinator.scan_once(Provider.CODEX)
    assert codex.failed == 0
    assert executor_calls == ["run_once"]


@pytest.mark.asyncio
async def test_sidebar_executor_cancellation_drains_worker_before_propagating() -> None:
    started = Event()
    release = Event()
    completed = Event()

    class BlockingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            started.set()
            if not release.wait(timeout=5.0):
                raise RuntimeError("test executor release timed out")
            completed.set()
            return SidebarExecutionResult(status="idle")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=_SidebarScanStore(),
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: 3_000_000.0,
        sidebar_executor=BlockingExecutor(),
    )
    scan = asyncio.create_task(coordinator.scan_once(Provider.CLAUDE))
    assert await asyncio.to_thread(started.wait, 2.0) is True

    scan.cancel()
    await asyncio.sleep(0)
    cancellation_propagated_before_release = scan.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await scan
    assert completed.wait(timeout=2.0) is True
    assert cancellation_propagated_before_release is False


@pytest.mark.asyncio
async def test_sidebar_executor_does_not_run_after_registration_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor_calls: list[str] = []

    class RecordingExecutor:
        def run_once(self) -> SidebarExecutionResult:
            executor_calls.append("run_once")
            return SidebarExecutionResult(status="idle")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=_SidebarScanStore(),
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: 3_000_000.0,
        sidebar_executor=RecordingExecutor(),
    )

    async def failed_registration(**_kwargs: Any) -> SidebarRegistrationSummary:
        return SidebarRegistrationSummary(
            examined=1,
            queued=0,
            by_provider={Provider.CLAUDE.value: 0, Provider.HERMES.value: 0},
            failed=1,
        )

    monkeypatch.setattr(
        coordinator,
        "register_sidebar_jobs_once",
        failed_registration,
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert executor_calls == []


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
    if provider is Provider.CODEX:
        assert ("refresh_read", Provider.CODEX.value, projection.native_id) in operations
        assert not any(operation[0] == "refresh_find" for operation in operations)


@pytest.mark.asyncio
async def test_refresh_failure_uses_durable_snapshot_with_fixed_sanitized_warning() -> (
    None
):
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
async def test_continue_refreshes_before_build_and_atomically_transitions_exact_pack() -> (
    None
):
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
    target_refresh_index = operations.index(("refresh_upsert", "codex:target-existing"))
    build_index = operations.index((
        "build",
        "claude-cursor-fresh",
        "claude-hash-fresh",
    ))
    transition_index = operations.index((
        "transition",
        "bridge-continue-1",
        "pack-continue-1",
        "codex-target-cursor-fresh",
        "codex-target-hash-fresh",
    ))
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
async def test_continue_hydrates_native_hermes_source_without_external_adapter() -> (
    None
):
    operations: list[tuple[object, ...]] = []
    source_id = "hermes-native-source"
    bridge_id = "sidebar:hermes-native-source"
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=bridge_id,
    )
    store = _ContinuationStore(operations)
    store.add_native_snapshot(
        source_id,
        cursor="hermes:4:293335",
        source_hash="h" * 64,
    )
    store.add_external(
        "codex:target-existing",
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-target-cursor-old",
        source_hash="codex-target-hash-old",
        origin_bridge_id=bridge_id,
    )
    builder = _RecordingContextBuilder(store, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CODEX: _RefreshAdapter(target_projection, operations),
        },
        context_builder=builder,
        clock=lambda: 100.0,
    )

    result = await coordinator.continue_session(
        ContinueRequest(
            session_id=source_id,
            bridge_id=bridge_id,
            target_provider=Provider.CODEX,
            context_budget_chars=4_000,
        )
    )

    assert builder.requests[0].source_session_id == source_id
    assert builder.requests[0].source_cursor == "hermes:4:293335"
    assert builder.requests[0].source_hash == "h" * 64
    assert result.pack.source_session_id == source_id
    assert ("get_native", source_id) in operations
    assert ("get_external", source_id) not in operations


@pytest.mark.asyncio
async def test_periodic_reconcile_refreshes_native_hermes_source_without_adapter() -> (
    None
):
    operations: list[tuple[object, ...]] = []
    source_id = "hermes-native-reconcile"
    bridge_id = "bridge-native-reconcile"
    target_id = "codex:target-existing"
    store = _ContinuationStore(operations)
    store.add_native_snapshot(
        source_id,
        cursor="hermes:1:10:abcdef",
        source_hash="h" * 64,
    )
    store.add_external(
        target_id,
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-target-cursor-fresh",
        source_hash="codex-target-hash-fresh",
        origin_bridge_id=bridge_id,
    )
    store.list_continuation_snapshots = lambda **_kwargs: [
        {
            "bridge_id": bridge_id,
            "version": 1,
            "pack_id": "pack-native-reconcile",
            "source_session_id": source_id,
            "source_cursor": "hermes:1:10:abcdef",
            "source_hash": "h" * 64,
            "target_session_id": target_id,
            "target_cursor": "codex-target-cursor-fresh",
            "target_hash": "codex-target-hash-fresh",
        }
    ]
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_bridge_id=bridge_id,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CODEX: _RefreshAdapter(target_projection, operations),
        },
        context_builder=_RecordingContextBuilder(store, operations),
        clock=lambda: 100.0,
    )

    result = await coordinator._reconcile_continuations()

    assert result.examined == 1
    assert result.failed == 0
    assert ("get_native", source_id) in operations
    assert ("get_external", source_id) not in operations


def test_worktree_snapshot_is_transactional_immutable_and_restart_safe(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    repo = _exact_cwd_repo(tmp_path / "exact-source")
    snapshot = capture_worktree_snapshot(str(repo))
    store = SessionBridgeStore(sidebar_db, clock=lambda: 100.0)
    store.upsert_projection(
        _sidebar_projection(
            provider=Provider.CLAUDE,
            native_id="exact-source",
            content="Continue in the exact source worktree",
            last_active=90.0,
            cwd=snapshot.cwd,
        )
    )
    candidate = SidebarCandidate(
        source_session_id="claude:exact-source",
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id("claude:exact-source"),
        title="[Claude] exact source",
        cwd=snapshot.cwd,
        git_root=snapshot.git_root,
        git_branch=snapshot.branch,
        git_head=snapshot.head,
        worktree_id=snapshot.worktree_id,
        eligible_at=90.0,
    )

    first = store.enqueue_sidebar_job(candidate, worktree_snapshot=snapshot)
    replay = store.enqueue_sidebar_job(candidate, worktree_snapshot=snapshot)

    assert first["created"] is True
    assert replay["created"] is False
    assert store.get_worktree_snapshot(candidate.source_session_id) == snapshot
    restarted = SessionBridgeStore(sidebar_db, clock=lambda: 101.0)
    assert restarted.get_worktree_snapshot(candidate.source_session_id) == snapshot
    with sidebar_db._lock:
        assert sidebar_db._conn is not None
        before_state = [
            tuple(row)
            for row in sidebar_db._conn.execute(
                """SELECT key, value_json, updated_at FROM session_bridge_state
                   WHERE key LIKE 'session-bridge:worktree:%'"""
            ).fetchall()
        ]
        before_jobs = sidebar_db._conn.execute(
            "SELECT COUNT(*) FROM session_sidebar_jobs"
        ).fetchone()[0]
    tampered_id = f"{snapshot.worktree_id}-tampered"
    with pytest.raises(ValueError, match="conflicting worktree snapshot identity"):
        store.enqueue_sidebar_job(
            replace(candidate, worktree_id=tampered_id),
            worktree_snapshot=replace(
                snapshot,
                worktree_id=tampered_id,
            ),
        )
    with sidebar_db._lock:
        assert sidebar_db._conn is not None
        after_state = [
            tuple(row)
            for row in sidebar_db._conn.execute(
                """SELECT key, value_json, updated_at FROM session_bridge_state
                   WHERE key LIKE 'session-bridge:worktree:%'"""
            ).fetchall()
        ]
        after_jobs = sidebar_db._conn.execute(
            "SELECT COUNT(*) FROM session_sidebar_jobs"
        ).fetchone()[0]
    assert after_state == before_state
    assert after_jobs == before_jobs


def _exact_cwd_continuation(
    tmp_path: Path,
    *,
    source_cwd: Path | None = None,
) -> tuple[
    SessionBridgeCoordinator,
    _ContinuationStore,
    _RecordingContextBuilder,
    ContinueRequest,
    Path,
]:
    operations: list[tuple[object, ...]] = []
    repo = source_cwd or _exact_cwd_repo(tmp_path / "source")
    source_projection = replace(_refresh_projection(Provider.CLAUDE), cwd=str(repo))
    source_id = f"claude:{source_projection.native_id}"
    bridge_id = sidebar_bridge_id(source_id)
    target_projection = replace(
        _refresh_projection(Provider.CODEX),
        native_id="target-existing",
        native_cursor="codex-target-cursor-fresh",
        native_hash="codex-target-hash-fresh",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=bridge_id,
    )
    store = _ContinuationStore(operations)
    store.add_external(
        source_id,
        provider=Provider.CLAUDE,
        native_id=source_projection.native_id,
        cursor="claude-old",
        source_hash="claude-old-hash",
    )
    store.add_external(
        "codex:target-existing",
        provider=Provider.CODEX,
        native_id="target-existing",
        cursor="codex-old",
        source_hash="codex-old-hash",
        origin_bridge_id=bridge_id,
    )
    snapshot = capture_worktree_snapshot(str(repo))
    store.get_worktree_snapshot = lambda session_id: (
        snapshot if session_id == source_id else None
    )
    builder = _RecordingContextBuilder(store, operations)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: _RefreshAdapter(source_projection, operations),
            Provider.CODEX: _RefreshAdapter(target_projection, operations),
        },
        context_builder=builder,
        permission_preflight=lambda cwd: cwd == snapshot.cwd,
        clock=lambda: 100.0,
    )
    return (
        coordinator,
        store,
        builder,
        ContinueRequest(
            session_id=source_id,
            bridge_id=bridge_id,
            target_provider=Provider.CODEX,
            context_budget_chars=4000,
        ),
        repo,
    )


class _CountingPlaceholderTarget:
    def __init__(self) -> None:
        self.create_calls = 0

    def create_placeholder(self, **_kwargs: object) -> None:
        self.create_calls += 1
        raise AssertionError("continuation attempted a second placeholder")


@pytest.mark.asyncio
async def test_exact_cwd_is_authoritative_for_every_continuation_operation(
    tmp_path: Path,
) -> None:
    coordinator, store, builder, request, repo = _exact_cwd_continuation(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "feature/drift"],
        check=True,
        capture_output=True,
    )

    result = await coordinator.continue_session(request)

    assert result.exact_cwd == os.path.abspath(str(repo))
    assert builder.requests[0].exact_cwd == result.exact_cwd
    assert any(
        warning == "worktree_branch_drift: recorded=main current=feature/drift"
        for warning in result.warnings
    )
    assert store.transition_calls == [
        (
            request.bridge_id,
            "pack-continue-1",
            "codex-target-cursor-fresh",
            "codex-target-hash-fresh",
        )
    ]


@pytest.mark.asyncio
async def test_worktree_registered_native_target_is_reused_without_second_placeholder(
    tmp_path: Path,
) -> None:
    coordinator, store, builder, request, repo = _exact_cwd_continuation(tmp_path)
    target = _CountingPlaceholderTarget()
    coordinator._target_adapters[Provider.CODEX] = target

    result = await coordinator.continue_session(request)

    assert target.create_calls == 0
    assert result.link.to_session_id == "codex:target-existing"
    assert result.pack.target_session_id == "codex:target-existing"
    assert result.exact_cwd == os.path.abspath(str(repo))
    assert builder.requests[0].exact_cwd == result.exact_cwd
    assert ("find_origin", request.bridge_id, "codex") in store.operations


@pytest.mark.asyncio
async def test_exact_cwd_is_revalidated_after_awaits_before_continuation_handoff(
    tmp_path: Path,
) -> None:
    first = _exact_cwd_repo(tmp_path / "first")
    second = _exact_cwd_repo(tmp_path / "second")
    alias = tmp_path / "source-alias"
    alias_kind = _directory_alias(alias, first)
    coordinator, store, builder, request, _repo = _exact_cwd_continuation(
        tmp_path,
        source_cwd=alias,
    )
    original_build = builder.build

    def retarget_during_build(request_value: ContextPackRequest) -> ContextPack:
        if alias_kind == "junction":
            os.rmdir(alias)
        else:
            alias.unlink()
        _directory_alias(alias, second)
        return original_build(request_value)

    builder.build = retarget_during_build

    with pytest.raises(ContinuationBlockedError) as raised:
        await coordinator.continue_session(request)

    assert raised.value.code == "source_identity_mismatch"
    assert raised.value.warnings == (
        "source_identity_mismatch: exact source worktree identity validation failed",
    )
    assert store.transition_calls


@pytest.mark.asyncio
async def test_exact_cwd_revalidation_follows_final_permission_await(
    tmp_path: Path,
) -> None:
    first = _exact_cwd_repo(tmp_path / "first-permission")
    second = _exact_cwd_repo(tmp_path / "second-permission")
    alias = tmp_path / "permission-alias"
    alias_kind = _directory_alias(alias, first)
    coordinator, _store, _builder, request, _repo = _exact_cwd_continuation(
        tmp_path,
        source_cwd=alias,
    )
    calls = 0

    def permission_preflight(_cwd: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            if alias_kind == "junction":
                os.rmdir(alias)
            else:
                alias.unlink()
            _directory_alias(alias, second)
        return True

    coordinator._permission_preflight = permission_preflight

    with pytest.raises(ContinuationBlockedError) as raised:
        await coordinator.continue_session(request)

    assert calls == 2
    assert raised.value.code == "source_identity_mismatch"


@pytest.mark.asyncio
async def test_worktree_permission_preflight_failure_blocks_before_target_lookup(
    tmp_path: Path,
) -> None:
    coordinator, store, builder, request, _repo = _exact_cwd_continuation(tmp_path)
    coordinator._permission_preflight = lambda _cwd: False

    with pytest.raises(ContinuationBlockedError) as raised:
        await coordinator.continue_session(request)

    assert raised.value.code == "permission_preflight_failed"
    assert raised.value.warnings == (
        "permission_preflight_failed: exact source cwd is not authorized",
    )
    assert builder.requests == []
    assert not any(operation[0] == "find_origin" for operation in store.operations)
    assert store.transition_calls == []


@pytest.mark.parametrize("outcome", ["false", "truthy_object", "exception"])
@pytest.mark.asyncio
async def test_worktree_permission_preflight_is_off_loop_strict_and_fixed(
    tmp_path: Path,
    outcome: str,
) -> None:
    coordinator, store, builder, request, _repo = _exact_cwd_continuation(tmp_path)
    loop_thread = get_ident()
    callback_threads: list[int] = []
    secret = "private-preflight-detail"

    def preflight(_cwd: str) -> object:
        callback_threads.append(get_ident())
        if outcome == "exception":
            raise RuntimeError(secret)
        if outcome == "truthy_object":
            return object()
        return False

    coordinator._permission_preflight = preflight

    with pytest.raises(ContinuationBlockedError) as raised:
        await coordinator.continue_session(request)

    assert callback_threads and callback_threads[0] != loop_thread
    assert raised.value.code == "permission_preflight_failed"
    assert str(raised.value) == "permission_preflight_failed"
    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert builder.requests == []
    assert not any(operation[0] == "find_origin" for operation in store.operations)
    assert store.transition_calls == []


@pytest.mark.asyncio
async def test_worktree_missing_legacy_snapshot_blocks_sidebar_continuation(
    tmp_path: Path,
) -> None:
    coordinator, store, builder, request, _repo = _exact_cwd_continuation(tmp_path)
    store.get_worktree_snapshot = lambda _session_id: None

    with pytest.raises(ContinuationBlockedError) as raised:
        await coordinator.continue_session(request)

    assert raised.value.code == "source_identity_mismatch"
    assert raised.value.warnings == (
        "source_identity_mismatch: exact source worktree snapshot is unavailable",
    )
    assert builder.requests == []
    assert store.transition_calls == []


@pytest.mark.asyncio
async def test_continue_stale_fallback_is_explicit_and_identical_replay_is_stable() -> (
    None
):
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
async def test_continue_replay_marks_divergence_only_when_both_descendants_advance() -> (
    None
):
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
    divergence_index = replay_operations.index((
        "mark_diverged",
        "bridge-continue-1",
        100.0,
    ))
    assert replay_operations.index(("refresh_upsert", session_id)) < divergence_index
    assert (
        replay_operations.index(("refresh_upsert", "codex:target-existing"))
        < divergence_index
    )


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
async def test_reconcile_waits_for_other_coordinator_processing_same_store() -> None:
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
    processor = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )
    reconciler = SessionBridgeCoordinator(
        config=_job_config(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={},
        clock=lambda: 100.0,
    )

    process_task = asyncio.create_task(processor.process_jobs_once())
    assert await asyncio.to_thread(started.wait, 1.0)
    reconcile_task = asyncio.create_task(reconciler.reconcile_once())
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
    # The first read is hung in its worker thread and still owns the CODEX scan
    # lock, so the second refresh can only return by hitting its OWN timeout.
    hung_reads = tuple(coordinator._provider_tasks)
    assert len(hung_reads) == 1
    hung_read = hung_reads[0]
    assert hung_read.done() is False

    try:
        # _REFRESH_DEADLOCK_GUARD_SECONDS is a deadlock GUARD, not an assertion:
        # nothing here measures it, and it is sized for the worst host so that a
        # regression which unbounds the lock wait fails the run instead of
        # hanging it.  What proves the bound is the EFFECT captured below -- that
        # this returned while the read was still in flight -- never a stopwatch.
        second = await asyncio.wait_for(
            coordinator.refresh_session(session_id, timeout=0.02),
            timeout=_REFRESH_DEADLOCK_GUARD_SECONDS,
        )
        returned_while_read_still_hung = not hung_read.done()
    finally:
        release.set()
        await _wait_until(hung_read.done)

    assert first.stale is True
    assert second.stale is True
    assert second.cursor == "codex-cursor-durable"
    assert returned_while_read_still_hung is True, (
        "refresh_session only returned once the hung read completed: its wait "
        "for the provider scan lock is not bounded by its own timeout"
    )
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
    expected_bridge = (
        "bridge:"
        + hashlib.sha256(
            f"session-bridge:{job['idempotency_key']}".encode()
        ).hexdigest()
    )
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


@pytest.mark.parametrize("continuous", (False, True))
@pytest.mark.asyncio
async def test_successful_scan_runs_mirror_float_independent_of_sidebar_continuous(
    continuous: bool,
) -> None:
    """Claude-side visibility must not be gated by the Codex sidebar lane.

    Per the 2026-07-17 claude-native-session-visibility design, Claude delivery
    "must not reuse or couple transitions to session_sidebar_jobs, which remains
    specific to Codex". Pausing the Codex sidebar (sidebar.continuous=false) is a
    deliberate, supported state and must not silently stop desktop registry
    records for Codex/Hermes mirrors.
    """
    now = 3_000_000.0
    store = _SidebarScanStore()
    event_loop_thread = get_ident()
    float_threads: list[int] = []

    class RecordingFloatWorker:
        def run_once(self) -> dict[str, int]:
            float_threads.append(get_ident())
            return {"examined": 0, "floated": 0, "skipped": 0}

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=continuous),
        store=store,
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        mirror_float=RecordingFloatWorker(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert len(float_threads) == 1
    assert float_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_successful_scan_floats_mirrors_when_another_provider_is_degraded() -> (
    None
):
    """A degraded Codex provider must not stop Claude-side desktop registration.

    Floating/registering mirrors only reads the local state database and writes
    local registry files, so it cannot depend on Codex reachability. Codex scans
    hang on this host (codex_scan_failed), which previously starved the Claude
    visibility lane indefinitely.
    """
    now = 3_000_000.0
    store = _SidebarScanStore()
    float_calls: list[int] = []

    class RecordingFloatWorker:
        def run_once(self) -> dict[str, int]:
            float_calls.append(1)
            return {"examined": 0, "floated": 0, "skipped": 0}

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={
            Provider.CLAUDE: _LifecycleClaudeAdapter(),
            # Configured but never scanned -> last_success stays None, which is
            # exactly how a hung Codex provider presents.
            Provider.CODEX: _LifecycleClaudeAdapter(),
        },
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        mirror_float=RecordingFloatWorker(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert coordinator._any_configured_provider_unhealthy() is True
    assert float_calls == [1]


def test_mirror_float_must_provide_run_once() -> None:
    with pytest.raises(TypeError, match="mirror_float must provide run_once"):
        SessionBridgeCoordinator(
            config=_sidebar_config(),
            store=_SidebarScanStore(),
            adapters={},
            target_adapters={},
            clock=lambda: 0.0,
            mirror_float=object(),
        )


@pytest.mark.asyncio
async def test_successful_scan_runs_registry_sync_worker() -> None:
    """Desktop registry reconciliation runs post-scan, local-only.

    Like the mirror float and the idle-chip archiver, it reads only the local
    state database and registry files, so it runs outside the provider-health
    gate and off the event loop.
    """
    now = 3_000_000.0
    store = _SidebarScanStore()
    event_loop_thread = get_ident()
    sync_threads: list[int] = []

    class RecordingRegistrySync:
        def run_once(self) -> dict[str, int]:
            sync_threads.append(get_ident())
            return {"examined": 0, "patched": 0}

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        registry_sync=RecordingRegistrySync(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert len(sync_threads) == 1
    assert sync_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_registry_sync_failure_is_recorded_not_raised() -> None:
    now = 3_000_000.0
    store = _SidebarScanStore()

    class ExplodingRegistrySync:
        def run_once(self) -> dict[str, int]:
            raise RuntimeError("boom")

    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=True),
        store=store,
        adapters={Provider.CLAUDE: _LifecycleClaudeAdapter()},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: now,
        registry_sync=ExplodingRegistrySync(),
    )

    summary = await coordinator.scan_once(Provider.CLAUDE)

    assert summary.failed == 0
    assert "desktop_registry_sync_failed" in coordinator._recent_error_codes


def test_registry_sync_must_provide_run_once() -> None:
    with pytest.raises(TypeError, match="registry_sync must provide run_once"):
        SessionBridgeCoordinator(
            config=_sidebar_config(),
            store=_SidebarScanStore(),
            adapters={},
            target_adapters={},
            clock=lambda: 0.0,
            registry_sync=object(),
        )


def test_cached_index_opt_in_is_capability_detected() -> None:
    """Scan resolution opts into a TTL'd inventory index only where one exists.

    The scan tolerates a bounded-stale summary; authoritative callers (refresh,
    characterization) must keep the default active-then-archived lookup. Adapter
    dispatch here is duck-typed, so a double implementing the narrower signature
    must not be handed a keyword it cannot accept.
    """

    from session_bridge.coordinator import _cached_index_kwargs

    class _Supports:
        def find_native_thread(
            self, native_id: str, *, allow_cached_index: bool = False
        ) -> None:
            return None

    class _DoesNot:
        def find_native_thread(self, native_id: str) -> None:
            return None

    assert _cached_index_kwargs(_Supports()) == {"allow_cached_index": True}
    assert _cached_index_kwargs(_DoesNot()) == {}
    assert _cached_index_kwargs(object()) == {}
