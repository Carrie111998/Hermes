from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import session_bridge.coordinator as coordinator_module
from session_bridge.claude_adapter import PlaceholderCreationError, PlaceholderResult
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import (
    ContinueRequest,
    RefreshResult,
    ReconcileSummary,
    SessionBridgeCoordinator,
)
from session_bridge.mirror import DiscoveryMode
from session_bridge.models import (
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    UpsertResult,
    canonical_session_id,
)
from session_bridge.store import SessionBridgeStore
from tests.session_bridge.test_end_to_end import _SidebarEndToEndHarness


def _projection(
    provider: Provider,
    native_id: str,
    *,
    last_active: float = 100.0,
    cursor: str | None = None,
    source_hash: str | None = None,
    bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=native_id,
        cwd="C:/workspace",
        started_at=last_active - 1.0,
        last_active=last_active,
        messages=(
            ProjectedMessage(
                native_event_id=f"event:{native_id}",
                ordinal=0,
                role="user",
                content="meaningful request",
                timestamp=last_active,
            ),
        ),
        native_cursor=cursor or f"cursor:{native_id}",
        native_hash=source_hash or f"hash:{native_id}",
        origin_kind=(
            OriginKind.BRIDGE_PLACEHOLDER
            if bridge_id is not None
            else OriginKind.NATIVE
        ),
        origin_bridge_id=bridge_id,
    )


class _StateStore:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.external: dict[str, dict[str, Any]] = {}
        self.upserts: list[str] = []

    def get_state(self, key: str) -> dict[str, Any] | None:
        value = self.states.get(key)
        return deepcopy(value) if value is not None else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        self.states[key] = deepcopy(value)

    def upsert_projection(
        self, projection: SessionProjection, *, rebuild: bool
    ) -> UpsertResult:
        del rebuild
        session_id = canonical_session_id(projection.provider, projection.native_id)
        self.upserts.append(projection.native_id)
        self.external[session_id] = {
            "session_id": session_id,
            "provider": projection.provider.value,
            "native_id": projection.native_id,
            "origin_bridge_id": projection.origin_bridge_id,
            "last_native_cursor": projection.native_cursor,
            "last_native_hash": projection.native_hash,
        }
        return UpsertResult(
            session_id=session_id,
            inserted_messages=len(projection.messages),
            rebuilt=False,
            first_seen=True,
        )

    def get_external_session(self, session_id: str) -> dict[str, Any] | None:
        return deepcopy(self.external.get(session_id))


class _RealTypeStateStore(_StateStore, SessionBridgeStore):
    """A no-database store that still exercises the coordinator's durable path."""

    def __init__(self) -> None:
        _StateStore.__init__(self)

    def list_mirror_jobs(self, states: list[MirrorJobState], *, limit: int):
        del states, limit
        return []

    def list_continuation_snapshots(self, *, limit: int, **kwargs: object):
        del limit, kwargs
        return []


class _CodexInventory:
    def __init__(self, batches: list[list[SessionProjection]]) -> None:
        self.batches = list(batches)
        self.current: dict[str, SessionProjection] = {
            projection.native_id: projection
            for batch in batches
            for projection in batch
        }
        self.projected: list[str] = []

    def list_inventory(self, *, archived: bool) -> list[SessionProjection]:
        if archived:
            return []
        if len(self.batches) > 1:
            return self.batches.pop(0)
        return self.batches[0]

    def find_native_thread(self, native_id: str) -> SessionProjection | None:
        return self.current.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        self.projected.append(summary.native_id)
        return summary


@pytest.mark.asyncio
async def test_initial_backfill_is_durable_before_continuous_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RealTypeStateStore()
    before_watermark = _projection(Provider.CODEX, "recent-before", last_active=90.0)
    after_watermark = _projection(Provider.CODEX, "new-after", last_active=110.0)
    adapter = _CodexInventory([[before_watermark], [after_watermark]])
    observed_modes: list[DiscoveryMode] = []

    def enqueue(*args: object, **kwargs: Any) -> dict[str, str]:
        del args
        observed_modes.append(kwargs["context"].discovery_mode)
        return {"id": "job"}

    watermark_key = "test:continuous-watermark"

    def load_watermark(target: _RealTypeStateStore) -> float | None:
        state = target.get_state(watermark_key)
        return None if state is None else float(state["watermark"])

    def persist_watermark(target: _RealTypeStateStore, watermark: float) -> None:
        target.set_state(watermark_key, {"watermark": watermark})

    monkeypatch.setattr(coordinator_module, "enqueue_mirror_job", enqueue)
    monkeypatch.setattr(coordinator_module, "load_continuous_watermark", load_watermark)
    monkeypatch.setattr(
        coordinator_module,
        "persist_continuous_watermark",
        persist_watermark,
    )
    config = replace(
        BridgeConfig(),
        mirrors=replace(BridgeConfig().mirrors, automatic_creation=True),
    )
    first = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=1,
        clock=lambda: 100.0,
    )

    first_summary = await first.scan_once(Provider.CODEX)
    restarted = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=1,
        clock=lambda: 120.0,
    )
    second_summary = await restarted.scan_once(Provider.CODEX)

    assert first_summary.indexed == 1
    assert second_summary.indexed == 1
    assert observed_modes == [
        DiscoveryMode.INITIAL_BACKFILL,
        DiscoveryMode.CONTINUOUS,
    ]


@pytest.mark.asyncio
async def test_codex_new_inventory_id_is_promoted_once_ahead_of_pending_backlog() -> (
    None
):
    store = _StateStore()
    store.states["session-bridge:scan:codex:pending"] = {
        "version": 1,
        "native_ids": ["backlog-a", "backlog-b"],
    }
    known = _projection(Provider.CODEX, "known", last_active=80.0)
    new = _projection(Provider.CODEX, "genuinely-new", last_active=100.0)
    backlog_a = _projection(Provider.CODEX, "backlog-a", last_active=70.0)
    backlog_b = _projection(Provider.CODEX, "backlog-b", last_active=60.0)
    store.upsert_projection(known, rebuild=False)
    adapter = _CodexInventory([[new, known], [new, known]])
    adapter.current.update({"backlog-a": backlog_a, "backlog-b": backlog_b})
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=1,
        clock=lambda: 100.0,
    )

    await coordinator.scan_once(Provider.CODEX)
    await coordinator.scan_once(Provider.CODEX)

    assert adapter.projected == ["genuinely-new", "backlog-a"]


@pytest.mark.asyncio
async def test_transient_automatic_enqueue_failure_remains_pending_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RealTypeStateStore()
    projection = _projection(Provider.CODEX, "enqueue-retry", last_active=90.0)
    adapter = _CodexInventory([[projection], [projection]])
    attempts = 0

    def enqueue(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal attempts
        del args, kwargs
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient durable enqueue failure")
        return {"id": "job:retry"}

    monkeypatch.setattr(coordinator_module, "enqueue_mirror_job", enqueue)
    monkeypatch.setattr(
        coordinator_module,
        "load_continuous_watermark",
        lambda target: 100.0,
    )
    base = BridgeConfig()
    coordinator = SessionBridgeCoordinator(
        config=replace(
            base,
            mirrors=replace(base.mirrors, automatic_creation=True),
        ),
        store=store,
        adapters={Provider.CODEX: adapter},
        clock=lambda: 100.0,
    )

    first = await coordinator.scan_once(Provider.CODEX)
    second = await coordinator.scan_once(Provider.CODEX)

    assert first.failed == 1
    assert second.indexed == 1
    assert attempts == 2


def _job(job_id: str, *, attempts: int = 1) -> dict[str, Any]:
    return {
        "id": job_id,
        "idempotency_key": f"idempotency:{job_id}",
        "source_session_id": f"claude:source-{job_id}",
        "target_provider": Provider.CODEX.value,
        "state": MirrorJobState.RUNNING.value,
        "attempts": attempts,
    }


class _JobStore(_StateStore):
    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.jobs = list(jobs)
        self.completed: list[str] = []
        self.retried: list[dict[str, Any]] = []
        self.manual: list[dict[str, Any]] = []

    def claim_due_jobs(
        self, *, now: float, limit: int, policy: object
    ) -> list[dict[str, Any]]:
        del now
        if not getattr(policy, "automatic_creation"):
            return []
        claimed = self.jobs[:limit]
        del self.jobs[:limit]
        return deepcopy(claimed)

    def list_mirror_jobs(self, states: list[MirrorJobState], *, limit: int):
        del states, limit
        return []

    def list_continuation_snapshots(self, *, limit: int, **kwargs: object):
        del limit, kwargs
        return []

    def get_session_launch_metadata(self, session_id: str):
        del session_id
        return {"title": "source", "cwd": None}

    def complete_job(self, job_id: str, **kwargs: object) -> None:
        del kwargs
        self.completed.append(job_id)

    def retry_job(self, job_id: str, **kwargs: object) -> None:
        self.retried.append({"job_id": job_id, **kwargs})

    def fail_job_manually(self, job_id: str, **kwargs: object) -> None:
        self.manual.append({"job_id": job_id, **kwargs})

    def find_external_session_by_origin_bridge(
        self, bridge_id: str, provider: Provider
    ) -> dict[str, Any] | None:
        del bridge_id, provider
        return None


class _JobSource:
    def __init__(self) -> None:
        self.projections: dict[str, SessionProjection] = {}

    def find_native_thread(self, native_id: str) -> SessionProjection | None:
        return self.projections.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        return summary


class _SuccessfulTarget:
    def __init__(self, source: _JobSource) -> None:
        self.source = source
        self.calls = 0

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        self.calls += 1
        native_id = f"target-{self.calls}"
        self.source.projections[native_id] = _projection(
            Provider.CODEX,
            native_id,
            bridge_id=kwargs["bridge_id"],
        )
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=f"codex:{native_id}",
            used_registration_turn=False,
            verified_at=100.0,
        )


@pytest.mark.asyncio
async def test_successful_breaker_batch_resets_and_continues_automatic_work() -> None:
    store = _JobStore([_job("one"), _job("two")])
    source = _JobSource()
    target = _SuccessfulTarget(source)
    base = BridgeConfig()
    config = replace(
        base,
        mirrors=replace(
            base.mirrors,
            automatic_creation=True,
            stop_after_attempts=1,
            stop_error_rate=0.5,
        ),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    first = await coordinator.process_jobs_once()
    second = await coordinator.process_jobs_once()

    assert first.succeeded == 1
    assert second.succeeded == 1
    assert store.completed == ["one", "two"]


@pytest.mark.asyncio
async def test_stale_attempt_sidecar_is_retried_without_target_lookup() -> None:
    job = _job("stale", attempts=2)
    store = _JobStore([])
    store.list_mirror_jobs = lambda states, limit: [deepcopy(job)]  # type: ignore[method-assign]
    store.states[f"session-bridge:attempt:{job['id']}"] = {
        "version": 1,
        "phase": "provider_call_started",
        "bridge_id": coordinator_module._bridge_id(job),
        "target_provider": Provider.CODEX.value,
        "policy_generation": 1,
        "attempts": 1,
    }

    class ForbiddenSource:
        def find_native_thread(self, native_id: str) -> None:
            raise AssertionError(f"stale sidecar must not read {native_id}")

    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: ForbiddenSource()},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.retried == 1
    assert summary.failed == 0
    assert store.manual == []
    assert store.retried[0]["code"] == "provider_call_not_started"


class _PendingPackStore:
    def __init__(self, pack: ContextPack) -> None:
        self.pack = pack
        self.transitioned = False

    def get_continuation_snapshot(self, bridge_id: str) -> None:
        del bridge_id
        return None

    def get_context_pack(self, bridge_id: str, *, budget_chars: int):
        if bridge_id != self.pack.bridge_id or budget_chars != self.pack.budget_chars:
            return None
        return {
            field: getattr(self.pack, field)
            for field in (
                "id",
                "bridge_id",
                "source_session_id",
                "target_session_id",
                "source_cursor",
                "source_hash",
                "budget_chars",
                "payload",
                "created_at",
                "immutable_at",
            )
        }

    def transition_link_to_continues(self, bridge_id: str, **kwargs: str):
        del kwargs
        self.transitioned = True
        self.pack = replace(self.pack, immutable_at=100.0)
        return {
            "id": "link:pending",
            "from_session_id": self.pack.source_session_id,
            "to_session_id": self.pack.target_session_id,
            "relation": "continues",
            "bridge_id": bridge_id,
            "source_cursor": self.pack.source_cursor,
            "source_hash": self.pack.source_hash,
            "created_at": 100.0,
        }


@pytest.mark.asyncio
async def test_pending_mutable_pack_rejects_advanced_refreshed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = ContextPack(
        id="pack:pending",
        bridge_id="bridge:pending",
        source_session_id="claude:source",
        target_session_id="codex:target",
        source_cursor="source-cursor-old",
        source_hash="source-hash-old",
        budget_chars=1000,
        payload="payload",
        created_at=90.0,
        immutable_at=None,
    )
    store = _PendingPackStore(pack)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={},
        clock=lambda: 100.0,
    )

    async def refresh(session_id: str, *, timeout: float) -> RefreshResult:
        del timeout
        if session_id.startswith("claude:"):
            return RefreshResult(
                session_id=session_id,
                cursor="source-cursor-advanced",
                source_hash="source-hash-advanced",
                stale=False,
                warning=None,
            )
        return RefreshResult(
            session_id=session_id,
            cursor="target-cursor",
            source_hash="target-hash",
            stale=False,
            warning=None,
        )

    monkeypatch.setattr(coordinator, "refresh_session", refresh)

    with pytest.raises(ValueError, match="source.*advanced"):
        await coordinator.continue_session(
            ContinueRequest(
                session_id=pack.source_session_id,
                bridge_id=pack.bridge_id,
                target_provider=Provider.CODEX,
                context_budget_chars=pack.budget_chars,
            )
        )
    assert store.transitioned is False


@pytest.mark.asyncio
async def test_stop_timeout_blocks_restart_until_provider_tasks_drain() -> None:
    started = Event()
    release = Event()

    class BlockingClaude:
        def discover(self) -> list[Path]:
            started.set()
            if not release.wait(timeout=2.0):
                raise RuntimeError("test release timed out")
            return []

    class EmptyCodex:
        def list_inventory(self, *, archived: bool) -> list[object]:
            del archived
            return []

    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=object(),
        adapters={Provider.CLAUDE: BlockingClaude(), Provider.CODEX: EmptyCodex()},
        refresh_timeout=0.01,
    )
    await coordinator.start()
    assert await asyncio.to_thread(started.wait, 1.0)
    await coordinator.stop()
    # Independent provider loops may leave the blocked Claude call plus a
    # concurrently scheduled Codex call draining at the stop deadline.
    assert coordinator.health()["provider_calls_inflight"] >= 1

    restart = asyncio.create_task(coordinator.start())
    await asyncio.sleep(0.03)
    blocked_before_drain = not restart.done()
    release.set()
    await asyncio.wait_for(restart, timeout=1.0)
    await coordinator.stop()

    assert blocked_before_drain is True


@pytest.mark.asyncio
async def test_continuation_reconcile_cursor_reaches_second_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PagedStore(_StateStore):
        def __init__(self) -> None:
            super().__init__()
            self.after_calls: list[str | None] = []

        def list_mirror_jobs(self, states: list[MirrorJobState], *, limit: int):
            del states, limit
            return []

        def list_continuation_snapshots(
            self,
            *,
            limit: int,
            after_bridge_id: str | None = None,
        ):
            self.after_calls.append(after_bridge_id)
            start = (
                0 if after_bridge_id is None else int(after_bridge_id.split(":")[1]) + 1
            )
            return [
                {
                    "version": 1,
                    "bridge_id": f"bridge:{index:04d}",
                    "pack_id": f"pack:{index:04d}",
                    "source_session_id": f"claude:source-{index:04d}",
                    "source_cursor": "source-cursor",
                    "source_hash": "source-hash",
                    "target_session_id": f"codex:target-{index:04d}",
                    "target_cursor": "target-cursor",
                    "target_hash": "target-hash",
                }
                for index in range(start, min(1001, start + limit))
            ]

    store = PagedStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={},
        clock=lambda: 100.0,
    )

    async def refresh(session_id: str, *, timeout: float) -> RefreshResult:
        del timeout
        source = session_id.startswith("claude:")
        return RefreshResult(
            session_id=session_id,
            cursor="source-cursor" if source else "target-cursor",
            source_hash="source-hash" if source else "target-hash",
            stale=False,
            warning=None,
        )

    monkeypatch.setattr(coordinator, "refresh_session", refresh)

    first = await coordinator.reconcile_once()
    second = await coordinator.reconcile_once()

    assert first.examined == 5
    assert second.examined == 5
    assert store.after_calls == [None, "bridge:0004"]


class _FailingHealthStore:
    def __init__(self) -> None:
        self.state = {"sentinel": "unchanged"}

    def mirror_job_counts(self) -> dict[str, int]:
        raise RuntimeError("fixed mirror queue query failure")


def retained_coordinator_snapshot(
    coordinator: SessionBridgeCoordinator,
) -> dict[str, Any]:
    return {
        "provider_health": deepcopy(coordinator._provider_health),
        "recent_error_codes": list(coordinator._recent_error_codes),
        "backfill_progress": deepcopy(coordinator._backfill_progress),
        "sidebar_registration_counts": deepcopy(
            coordinator._sidebar_registration_counts
        ),
        "watcher_state": (
            coordinator._watcher_state,
            coordinator._watcher_error_code,
        ),
        "store_state": deepcopy(vars(coordinator._store)),
    }


def test_health_queue_query_failure_is_response_local_and_read_only() -> None:
    store = _FailingHealthStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={},
        clock=lambda: 100.0,
    )
    coordinator._provider_health[Provider.CLAUDE]["last_success"] = 90.0
    coordinator._recent_error_codes.append("catalog_scan_loop_failed")
    coordinator._backfill_progress[Provider.CODEX] = {
        "state": "continuous",
        "indexed": 3,
    }
    coordinator._sidebar_registration_counts["examined"] = 2
    coordinator._watcher_state = "degraded"
    coordinator._watcher_error_code = "claude_watcher_failed"
    before = retained_coordinator_snapshot(coordinator)

    first = coordinator.health()
    second = coordinator.health()

    assert first["recent_error_codes"] == [
        *before["recent_error_codes"],
        "mirror_queue_health_failed",
    ]
    assert second["recent_error_codes"] == first["recent_error_codes"]
    assert retained_coordinator_snapshot(coordinator) == before


@pytest.mark.asyncio
async def test_background_scan_failure_is_reported_and_next_cycle_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = BridgeConfig()
    config = replace(
        base,
        service=replace(base.service, catalog_scan_seconds=0.01),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={},
    )
    recovered = asyncio.Event()
    calls = 0

    async def scan_once(provider: Provider | None = None):
        nonlocal calls
        del provider
        calls += 1
        if calls == 1:
            raise RuntimeError("private transient scan failure")
        recovered.set()
        return None

    monkeypatch.setattr(coordinator, "scan_once", scan_once)

    await coordinator.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1.0)
        assert "catalog_scan_loop_failed" in coordinator.health()["recent_error_codes"]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_background_provider_scans_do_not_block_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = BridgeConfig()
    config = replace(
        base,
        service=replace(base.service, catalog_scan_seconds=0.01),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={},
    )
    claude_scanned_twice = asyncio.Event()
    codex_release = asyncio.Event()
    claude_calls = 0

    async def scan_once(provider: Provider | None = None):
        nonlocal claude_calls
        if provider is Provider.CLAUDE:
            claude_calls += 1
            if claude_calls >= 2:
                claude_scanned_twice.set()
            return None
        await codex_release.wait()
        return None

    monkeypatch.setattr(coordinator, "scan_once", scan_once)

    await coordinator.start()
    try:
        await asyncio.wait_for(claude_scanned_twice.wait(), timeout=1.0)
        assert claude_calls >= 2
    finally:
        codex_release.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_initial_reconcile_timeout_releases_background_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = BridgeConfig()
    config = replace(
        base,
        service=replace(base.service, catalog_scan_seconds=0.01),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=object(),
        adapters={},
        refresh_timeout=0.01,
    )
    reconcile_started = asyncio.Event()
    never_reconcile = asyncio.Event()
    claude_scanned = asyncio.Event()

    async def reconcile_once() -> ReconcileSummary:
        reconcile_started.set()
        await never_reconcile.wait()
        return ReconcileSummary(examined=0, recovered=0, retried=0, failed=0)

    async def scan_once(provider: Provider | None = None):
        if provider is Provider.CLAUDE:
            claude_scanned.set()
        return None

    monkeypatch.setattr(coordinator, "reconcile_once", reconcile_once)
    monkeypatch.setattr(coordinator, "scan_once", scan_once)

    await coordinator.start()
    try:
        await asyncio.wait_for(reconcile_started.wait(), timeout=1.0)
        await asyncio.wait_for(claude_scanned.wait(), timeout=1.0)
        assert "mirror_reconcile_failed" in coordinator.health()["recent_error_codes"]
    finally:
        await coordinator.stop()


def test_sidebar_rename_failure_reconciles_without_duplicate_create(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "rename-reconcile",
            cwd=tmp_path / "rename-reconcile",
        )
        harness.register()
        harness.native.rename_failures_remaining = 1

        with harness.client() as client:
            failed = harness.run_worker_once(client)
            retry_job = harness.store.get_sidebar_job_for_source(source_id)
            harness.advance_retry()
            recovered = harness.run_worker_once(client)

        assert failed == [
            {
                "state": "sidebar_retry",
                "error_code": "rename_failed",
                "codex_thread_id": "native-sidebar-1",
            }
        ]
        assert retry_job is not None
        assert retry_job["state"] == "sidebar_retry"
        assert retry_job["codex_thread_id"] == "native-sidebar-1"
        assert recovered == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        assert len(harness.native.create_calls) == 1
        assert [thread_id for thread_id, _title in harness.native.rename_calls] == [
            "native-sidebar-1",
            "native-sidebar-1",
        ]
    finally:
        harness.close()
