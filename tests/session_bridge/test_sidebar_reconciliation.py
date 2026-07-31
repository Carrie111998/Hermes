from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError
import asyncio
import threading
from typing import Any

import pytest

from hermes_state import SessionDB
from session_bridge.codex_adapter import SidebarVerificationError
from session_bridge.config import BridgeConfig, SidebarConfig
from session_bridge.coordinator import SessionBridgeCoordinator
from session_bridge.models import BridgeMarkerPayload, Provider
from session_bridge.sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    sidebar_bridge_id,
)
from session_bridge.sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationProofInput,
    SidebarReconciliationState,
    sidebar_reconciliation_proof_digest,
)
from session_bridge.store import SessionBridgeStore


SOURCE = "claude:source-1"
BRIDGE = "sidebar:bridge-1"
THREAD = "22222222-2222-4222-8222-222222222222"


def test_sidebar_reconciliation_proof_digest_binds_every_authority_field() -> None:
    base = SidebarReconciliationProofInput(
        job_id="sidebar-job:1",
        source_session_id=SOURCE,
        bridge_id=BRIDGE,
        marker_digest="1" * 64,
        placement_generation=1,
        delivery_generation=1,
        reconciliation_generation="scan:1",
        completed_at=100.0,
        expires_at=130.0,
        inventory_digest="2" * 64,
        state=SidebarReconciliationState.ABSENCE_PROVEN,
        match_count=0,
        recovered_thread_id=None,
        fixed_reason=None,
    )

    digest = sidebar_reconciliation_proof_digest(base)

    assert len(digest) == 64
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, reconciliation_generation="scan:2")
    )
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, placement_generation=2)
    )
    assert digest != sidebar_reconciliation_proof_digest(
        dataclasses.replace(base, source_session_id="claude:other")
    )


@pytest.mark.parametrize(
    ("state", "match_count", "thread_id", "reason"),
    [
        (SidebarReconciliationState.RECOVERED, 1, THREAD, None),
        (SidebarReconciliationState.ABSENCE_PROVEN, 0, None, None),
        (SidebarReconciliationState.BLOCKED, 2, None, "marker_conflict"),
    ],
)
def test_sidebar_reconciliation_evidence_enforces_state_shape(
    state: SidebarReconciliationState,
    match_count: int,
    thread_id: str | None,
    reason: str | None,
) -> None:
    evidence = SidebarReconciliationEvidence.create(
        state=state,
        generation="scan:1",
        completed_at=100.0,
        expires_at=130.0,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=match_count,
        recovered_thread_id=thread_id,
        fixed_reason=reason,
    )

    assert evidence.state is state


def test_sidebar_reconciliation_evidence_rejects_cross_state_shape() -> None:
    with pytest.raises(
        ValueError,
        match="sidebar reconciliation state shape is malformed",
    ):
        SidebarReconciliationEvidence.create(
            state=SidebarReconciliationState.ABSENCE_PROVEN,
            generation="scan:1",
            completed_at=100.0,
            expires_at=130.0,
            inventory_digest="2" * 64,
            marker_digest="1" * 64,
            match_count=1,
            recovered_thread_id=THREAD,
            fixed_reason=None,
        )


def _leased_job(*, expires_at: float = 400.0) -> dict[str, Any]:
    return {
        "id": "sidebar-job:1",
        "source_session_id": SOURCE,
        "bridge_id": BRIDGE,
        "state": "sidebar_leased",
        "attempts": 0,
        "lease_expires_at": expires_at,
        "lease_token": "opaque-lease-token",
    }


class FakeSidebarStore:
    def __init__(
        self,
        *,
        claim_after: float = 0.0,
        reserved_thread_id: str | None = None,
        create_reserved: bool = False,
        reservation_error: Exception | None = None,
        bind_error: Exception | None = None,
    ) -> None:
        self.claim_after = claim_after
        self.reserved_thread_id = reserved_thread_id
        self.create_reserved = create_reserved
        self.reservation_error = reservation_error
        self.bind_error = bind_error
        self.failures: list[tuple[str, str, float]] = []
        self.failure_thread_ids: list[str | None] = []
        self.binds: list[tuple[str, str, float]] = []
        self.commits: list[tuple[str, str, float]] = []

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        assert lease_seconds == 300
        if now < self.claim_after:
            return []
        return [
            {
                **_leased_job(expires_at=now + lease_seconds),
                "codex_thread_id": self.reserved_thread_id,
            }
        ][:limit]

    def bind_sidebar_thread(
        self, *, lease_token: str, codex_thread_id: str, now: float
    ) -> dict[str, Any]:
        if self.bind_error is not None:
            raise self.bind_error
        self.binds.append((lease_token, codex_thread_id, now))
        self.reserved_thread_id = codex_thread_id
        return {
            **_leased_job(),
            "codex_thread_id": codex_thread_id,
        }

    def fail_sidebar_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        now: float,
        codex_thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.failures.append((lease_token, error_code, now))
        self.failure_thread_ids.append(codex_thread_id)
        return {**_leased_job(), "state": "sidebar_failed", "error_code": error_code}

    def commit_sidebar_job(
        self, *, lease_token: str, codex_thread_id: str, now: float
    ) -> dict[str, Any]:
        self.commits.append((lease_token, codex_thread_id, now))
        return {
            **_leased_job(),
            "state": "sidebar_visible",
            "codex_thread_id": codex_thread_id,
        }

    def lookup_sidebar_job_by_lease(self, lease_token: str) -> dict[str, Any]:
        assert lease_token == "opaque-lease-token"
        return {
            "source_session_id": SOURCE,
            "bridge_id": BRIDGE,
            "state": "sidebar_leased",
            "codex_thread_id": None,
        }

    def get_sidebar_create_reservation(
        self, source_session_id: str
    ) -> dict[str, Any] | None:
        assert source_session_id == SOURCE
        if self.reservation_error is not None:
            raise self.reservation_error
        if not self.create_reserved:
            return None
        return {
            "version": 1,
            "job_id": "sidebar-job:1",
            "source_session_id": SOURCE,
            "bridge_id": BRIDGE,
            "recovery_key": "hermes-session-bridge-create-v1:" + "a" * 64,
            "reserved_at": 90.0,
        }


class FakeVerifier:
    def __init__(self, result: VerifiedSidebarThread | None | Exception) -> None:
        self.result = result
        self.find_calls: list[BridgeMarkerPayload] = []
        self.verify_calls: list[tuple[str, BridgeMarkerPayload]] = []

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.find_calls.append(expected)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        self.verify_calls.append((thread_id, expected))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None or self.result.thread_id != thread_id:
            raise SidebarVerificationError("source_identity_mismatch")
        return self.result


class BlockingVerifier(FakeVerifier):
    def __init__(self) -> None:
        super().__init__(None)
        self.started = threading.Event()
        self.release = threading.Event()

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.find_calls.append(expected)
        self.started.set()
        assert self.release.wait(timeout=5)
        return None


class ForbiddenTargetAdapter:
    def create_placeholder(self, **_: Any) -> Any:
        raise AssertionError("native sidebar reconciliation must never create")


def _coordinator(
    store: FakeSidebarStore,
    verifier: FakeVerifier,
    *,
    clock=lambda: 100.0,
    recovery_timeout: float | None = None,
) -> SessionBridgeCoordinator:
    recovery_options = (
        {}
        if recovery_timeout is None
        else {"sidebar_cancellation_recovery_timeout": recovery_timeout}
    )
    return SessionBridgeCoordinator(
        config=BridgeConfig(sidebar=SidebarConfig(enabled=True)),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: ForbiddenTargetAdapter()},
        sidebar_verifier=verifier,
        clock=clock,
        **recovery_options,
    )


def _verified() -> VerifiedSidebarThread:
    return VerifiedSidebarThread(THREAD, SOURCE, BRIDGE)


def test_verified_sidebar_thread_is_frozen() -> None:
    verified = _verified()
    with pytest.raises(FrozenInstanceError):
        verified.thread_id = "different"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_reserved_create_with_zero_marker_is_returned_as_no_create_boundary() -> (
    None
):
    coordinator = _coordinator(
        FakeSidebarStore(create_reserved=True),
        FakeVerifier(None),
    )

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert len(claims) == 1
    assert claims[0].create_reserved is True
    assert claims[0].reserved_thread_id is None
    assert claims[0].recovered_thread is None


@pytest.mark.asyncio
async def test_missing_create_reservation_capability_fails_closed_before_marker_search() -> (
    None
):
    store = FakeSidebarStore()
    store.get_sidebar_create_reservation = None  # type: ignore[method-assign]
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()
    assert store.failures == [
        ("opaque-lease-token", "bridge_temporarily_unavailable", 100.0)
    ]
    assert verifier.find_calls == []


@pytest.mark.asyncio
async def test_unreadable_create_reservation_fails_closed_before_marker_search() -> (
    None
):
    store = FakeSidebarStore(reservation_error=RuntimeError("stale store"))
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()
    assert store.failures == [
        ("opaque-lease-token", "bridge_temporarily_unavailable", 100.0)
    ]
    assert verifier.find_calls == []


@pytest.mark.asyncio
async def test_sidebar_delivery_claim_rejects_every_limit_except_one() -> None:
    coordinator = _coordinator(FakeSidebarStore(), FakeVerifier(None))

    with pytest.raises(ValueError, match="exactly one"):
        await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=2)


@pytest.mark.asyncio
async def test_lost_commit_recovers_one_authenticated_thread_without_creation() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(_verified())
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert len(claims) == 1
    assert claims[0].recovered_thread == _verified()
    assert claims[0].reconcile_required is True
    assert claims[0].rename_required is True
    assert verifier.find_calls == [
        BridgeMarkerPayload(BRIDGE, SOURCE, Provider.CODEX, 1)
    ]
    assert store.binds == [("opaque-lease-token", THREAD, 100.0)]
    assert store.failures == []


@pytest.mark.asyncio
async def test_recovered_thread_bind_failure_atomically_retains_exact_id() -> None:
    store = FakeSidebarStore(bind_error=RuntimeError("bind unavailable"))
    verifier = FakeVerifier(_verified())
    coordinator = _coordinator(store, verifier)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()
    assert store.failures == [
        ("opaque-lease-token", "bridge_temporarily_unavailable", 100.0)
    ]
    assert store.failure_thread_ids == [THREAD]


class BlockingBindStore(FakeSidebarStore):
    def __init__(self) -> None:
        super().__init__()
        self.bind_started = threading.Event()
        self.bind_release = threading.Event()

    def bind_sidebar_thread(
        self, *, lease_token: str, codex_thread_id: str, now: float
    ) -> dict[str, Any]:
        self.bind_started.set()
        assert self.bind_release.wait(timeout=5)
        return super().bind_sidebar_thread(
            lease_token=lease_token,
            codex_thread_id=codex_thread_id,
            now=now,
        )


@pytest.mark.asyncio
async def test_cancellation_during_recovered_bind_settles_with_exact_id() -> None:
    store = BlockingBindStore()
    coordinator = _coordinator(store, FakeVerifier(_verified()))
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(store.bind_started.wait, 5)

    claim_task.cancel("cancel-during-recovered-bind")
    try:
        with pytest.raises(asyncio.CancelledError):
            await claim_task
    finally:
        store.bind_release.set()

    assert store.failure_thread_ids == [THREAD]


@pytest.mark.asyncio
async def test_reserved_thread_id_forces_exact_recovery_without_marker_search() -> None:
    store = FakeSidebarStore(reserved_thread_id=THREAD)
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert len(claims) == 1
    assert claims[0].reserved_thread_id == THREAD
    assert claims[0].recovered_thread is None
    assert claims[0].reconcile_required is True
    assert claims[0].rename_required is True
    assert verifier.find_calls == []
    assert store.binds == []
    assert store.failures == []


@pytest.mark.asyncio
async def test_zero_match_permits_retry_only_after_previous_lease_expiry() -> None:
    store = FakeSidebarStore(claim_after=300.0)
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=299.999, limit=1) == ()
    assert verifier.find_calls == []

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=300.0, limit=1)
    assert len(claims) == 1
    assert claims[0].recovered_thread is None
    assert claims[0].reconcile_required is True
    assert claims[0].rename_required is False


@pytest.mark.asyncio
async def test_multiple_authenticated_matches_are_fatal_and_never_delivered() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(SidebarVerificationError("marker_conflict"))
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert claims == ()
    assert store.failures == [("opaque-lease-token", "marker_conflict", 100.0)]
    assert store.commits == []


@pytest.mark.parametrize(
    "code",
    ["source_identity_mismatch", "provider_mismatch"],
)
@pytest.mark.asyncio
async def test_related_near_match_is_persisted_fatal_and_never_exposed(
    code: str,
) -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(SidebarVerificationError(code))
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert claims == ()
    assert store.failures == [("opaque-lease-token", code, 100.0)]
    assert store.commits == []


@pytest.mark.asyncio
async def test_mismatched_verifier_candidate_id_is_never_durably_bound() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(
        VerifiedSidebarThread(
            thread_id=THREAD,
            source_session_id="claude:wrong-source",
            bridge_id=BRIDGE,
        )
    )
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert claims == ()
    assert store.failures == [("opaque-lease-token", "source_identity_mismatch", 100.0)]
    assert store.failure_thread_ids == [None]
    assert store.binds == []


@pytest.mark.asyncio
async def test_inventory_budget_exhaustion_is_retryable_and_exposes_no_claim() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(SidebarVerificationError("bridge_temporarily_unavailable"))
    coordinator = _coordinator(store, verifier)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()
    assert store.failures == [
        ("opaque-lease-token", "bridge_temporarily_unavailable", 100.0)
    ]
    assert store.commits == []


@pytest.mark.asyncio
async def test_native_not_indexed_defers_reconciliation_to_native_broker() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(SidebarVerificationError("native_task_not_indexed"))
    coordinator = _coordinator(store, verifier)

    claims = await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert len(claims) == 1
    assert claims[0].source_session_id == SOURCE
    assert claims[0].bridge_id == BRIDGE
    assert claims[0].reconcile_required is True
    assert claims[0].rename_required is False
    assert claims[0].recovered_thread is None
    assert store.failures == []
    assert store.commits == []


@pytest.mark.asyncio
async def test_recovered_rename_failure_renames_same_thread_before_verified_commit() -> (
    None
):
    events: list[tuple[str, str]] = []
    store = FakeSidebarStore()
    verifier = FakeVerifier(_verified())
    coordinator = _coordinator(store, verifier, clock=lambda: 101.0)

    claim = (await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1))[0]
    assert claim.recovered_thread == _verified()
    assert claim.recovered_thread is not None

    events.append(("rename", claim.recovered_thread.thread_id))
    committed = await coordinator.commit_sidebar_job(
        lease_token=claim.lease_token,
        codex_thread_id=claim.recovered_thread.thread_id,
    )
    events.append(("commit", committed["codex_thread_id"]))

    assert events == [("rename", THREAD), ("commit", THREAD)]
    assert verifier.verify_calls == [
        (THREAD, BridgeMarkerPayload(BRIDGE, SOURCE, Provider.CODEX, 1))
    ]
    assert store.commits == [("opaque-lease-token", THREAD, 101.0)]


@pytest.mark.asyncio
async def test_commit_binds_native_id_before_transient_verification_failure() -> None:
    store = FakeSidebarStore()
    verifier = FakeVerifier(SidebarVerificationError("bridge_temporarily_unavailable"))
    coordinator = _coordinator(store, verifier, clock=lambda: 101.0)

    with pytest.raises(SidebarVerificationError) as failure:
        await coordinator.commit_sidebar_job(
            lease_token="opaque-lease-token",
            codex_thread_id=THREAD,
        )

    assert failure.value.code == "bridge_temporarily_unavailable"
    assert store.binds == [("opaque-lease-token", THREAD, 101.0)]
    assert store.commits == []


@pytest.mark.asyncio
async def test_commit_and_exact_replay_survive_coordinator_and_store_restart(
    tmp_path,
) -> None:
    path = tmp_path / "sidebar-restart.db"
    source = "claude:restart-source"
    bridge = sidebar_bridge_id(source)
    token = "restart-safe-opaque-token"
    first_db = SessionDB(path)
    first_store = SessionBridgeStore(
        first_db,
        sidebar_token_factory=lambda: token,
        sidebar_jitter=lambda _bound: 0.0,
    )
    first_db.ensure_session(source, source="cli")
    first_store.enqueue_sidebar_job(
        SidebarCandidate(
            source_session_id=source,
            provider=Provider.CLAUDE,
            bridge_id=bridge,
            title="[Claude] Restart source",
            cwd="C:/source",
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=10.0,
        )
    )
    verified = VerifiedSidebarThread(THREAD, source, bridge)
    first = SessionBridgeCoordinator(
        config=BridgeConfig(sidebar=SidebarConfig(enabled=True)),
        store=first_store,
        adapters={},
        sidebar_verifier=FakeVerifier(verified),
        clock=lambda: 100.0,
    )
    claim = (await first.claim_sidebar_jobs_for_delivery(now=100.0, limit=1))[0]
    assert claim.lease_token == token
    assert not hasattr(first, "_sidebar_claim_expectations")
    first_db.close()

    second_db = SessionDB(path)
    second = SessionBridgeCoordinator(
        config=BridgeConfig(sidebar=SidebarConfig(enabled=True)),
        store=SessionBridgeStore(second_db),
        adapters={},
        sidebar_verifier=FakeVerifier(verified),
        clock=lambda: 200.0,
    )
    committed = await second.commit_sidebar_job(
        lease_token=token,
        codex_thread_id=THREAD,
    )
    assert committed["state"] == "sidebar_visible"
    second_db.close()

    replay_db = SessionDB(path)
    replay = SessionBridgeCoordinator(
        config=BridgeConfig(sidebar=SidebarConfig(enabled=True)),
        store=SessionBridgeStore(replay_db),
        adapters={},
        sidebar_verifier=FakeVerifier(verified),
        clock=lambda: 201.0,
    )
    assert (
        await replay.commit_sidebar_job(
            lease_token=token,
            codex_thread_id=THREAD,
        )
        == committed
    )
    replay_db.close()


class ExpiringFailureStore(FakeSidebarStore):
    def fail_sidebar_job(
        self, *, lease_token: str, error_code: str, now: float
    ) -> dict[str, Any]:
        assert now >= 400.0
        raise ValueError("sidebar lease has expired")


@pytest.mark.asyncio
async def test_reconciliation_failure_uses_fresh_clock_and_hides_expired_lease() -> (
    None
):
    store = ExpiringFailureStore()
    verifier = FakeVerifier(SidebarVerificationError("marker_conflict"))
    coordinator = _coordinator(store, verifier, clock=lambda: 401.0)

    assert await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1) == ()


@pytest.mark.asyncio
async def test_cancelled_reconciliation_releases_every_claimed_lease(tmp_path) -> None:
    db = SessionDB(tmp_path / "cancelled-sidebar.db")
    tokens = iter(("cancelled-lease-one",))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    sources: list[str] = []
    for ordinal in (1,):
        source = f"claude:cancelled-{ordinal}"
        sources.append(source)
        db.ensure_session(source, source="cli")
        store.enqueue_sidebar_job(
            SidebarCandidate(
                source_session_id=source,
                provider=Provider.CLAUDE,
                bridge_id=sidebar_bridge_id(source),
                title=f"[Claude] Cancelled {ordinal}",
                cwd=f"C:/cancelled/{ordinal}",
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
                eligible_at=10.0 + ordinal,
            )
        )
    verifier = BlockingVerifier()
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)

    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(verifier.started.wait, 5)
    claim_task.cancel()
    verifier.release.set()
    with pytest.raises(asyncio.CancelledError):
        await claim_task

    assert all(
        store.get_sidebar_job_for_source(source)["state"] == "sidebar_pending"
        for source in sources
    )
    db.close()


@pytest.mark.asyncio
async def test_cancelled_durable_claim_returns_by_deadline_then_recovers_in_background(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "cancelled-during-claim.db")
    tokens = iter(("claim-boundary-one",))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    sources: list[str] = []
    for ordinal in (1,):
        source = f"claude:claim-boundary-{ordinal}"
        sources.append(source)
        db.ensure_session(source, source="cli")
        store.enqueue_sidebar_job(
            SidebarCandidate(
                source_session_id=source,
                provider=Provider.CLAUDE,
                bridge_id=sidebar_bridge_id(source),
                title=f"[Claude] Claim boundary {ordinal}",
                cwd=f"C:/claim-boundary/{ordinal}",
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
                eligible_at=10.0 + ordinal,
            )
        )
    committed = threading.Event()
    release = threading.Event()
    original_claim = store.claim_sidebar_jobs

    def claim_then_pause(**kwargs: Any) -> list[dict[str, Any]]:
        claimed = original_claim(**kwargs)
        committed.set()
        assert release.wait(timeout=5)
        return claimed

    monkeypatch.setattr(store, "claim_sidebar_jobs", claim_then_pause)
    coordinator = _coordinator(
        store,
        FakeVerifier(None),
        clock=lambda: 100.0,
        recovery_timeout=0.02,
    )
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(committed.wait, 5)

    claim_task.cancel("claim-deadline-cancel")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(claim_task, timeout=0.5)

    leased = [store.get_sidebar_job_for_source(source) for source in sources]
    assert all(job is not None for job in leased)
    assert all(job["state"] == "sidebar_leased" for job in leased if job is not None)
    assert len(coordinator._sidebar_recovery_tasks) == 1
    await asyncio.wait_for(coordinator.stop(), timeout=0.2)
    assert len(coordinator._sidebar_recovery_tasks) == 1

    release.set()
    deadline = asyncio.get_running_loop().time() + 1.0
    while any(
        store.get_sidebar_job_for_source(source)["state"] != "sidebar_pending"
        for source in sources
    ):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)

    jobs = [store.get_sidebar_job_for_source(source) for source in sources]
    assert all(job is not None for job in jobs)
    assert all(job["state"] == "sidebar_pending" for job in jobs if job is not None)
    assert all(job["lease_digest"] is None for job in jobs if job is not None)
    while coordinator._sidebar_recovery_tasks:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    assert coordinator._sidebar_recovery_tasks == set()
    db.close()


class BlockingClaimFailureStore(FakeSidebarStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        self.started.set()
        assert self.release.wait(timeout=5)
        raise RuntimeError("claim worker failed with lease=must-not-leak")


@pytest.mark.asyncio
async def test_cancelled_claim_worker_failure_does_not_mask_cancellation() -> None:
    store = BlockingClaimFailureStore()
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(store.started.wait, 5)

    claim_task.cancel()
    await asyncio.sleep(0)
    assert not claim_task.done()
    claim_task.cancel()
    await asyncio.sleep(0)
    assert not claim_task.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await claim_task

    assert verifier.find_calls == []
    assert store.failures == []


@pytest.mark.asyncio
async def test_non_cancelled_claim_worker_exception_propagates_unchanged() -> None:
    store = BlockingClaimFailureStore()
    coordinator = _coordinator(store, FakeVerifier(None), clock=lambda: 100.0)
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(store.started.wait, 5)

    store.release.set()
    with pytest.raises(
        RuntimeError, match="claim worker failed with lease=must-not-leak"
    ):
        await claim_task

    assert coordinator._sidebar_recovery_tasks == set()


@pytest.mark.asyncio
async def test_cancelled_claim_worker_exception_chain_is_fully_detached() -> None:
    store = BlockingClaimFailureStore()
    coordinator = _coordinator(store, FakeVerifier(None), clock=lambda: 100.0)
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(store.started.wait, 5)

    claim_task.cancel("original-claim-cancel")
    store.release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await claim_task

    assert caught.value.args == ("original-claim-cancel",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    chain: list[BaseException] = []
    cursor: BaseException | None = caught.value
    while cursor is not None:
        chain.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    assert chain == [caught.value]
    assert "must-not-leak" not in " ".join(str(node) for node in chain)


class MalformedClaimBatchStore(FakeSidebarStore):
    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any] | object]:
        return [
            {**_leased_job(), "lease_token": "malformed-batch-one"},
            object(),
            {
                **_leased_job(),
                "id": "sidebar-job:2",
                "source_session_id": "claude:source-2",
                "bridge_id": "sidebar:bridge-2",
                "lease_token": "malformed-batch-two",
            },
        ]


@pytest.mark.asyncio
async def test_malformed_claim_batch_releases_every_recoverable_token() -> None:
    store = MalformedClaimBatchStore()
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="claims are malformed"):
        await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert store.failures == [
        ("malformed-batch-one", "broker_time_budget", 100.0),
        ("malformed-batch-two", "broker_time_budget", 100.0),
    ]
    assert verifier.find_calls == []


class TupleClaimBatchStore(MalformedClaimBatchStore):
    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> tuple[dict[str, Any] | object, ...]:
        return tuple(
            super().claim_sidebar_jobs(
                now=now,
                limit=limit,
                lease_seconds=lease_seconds,
            )
        )


@pytest.mark.asyncio
async def test_wrong_claim_container_releases_every_recoverable_token() -> None:
    store = TupleClaimBatchStore()
    verifier = FakeVerifier(None)
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="claims are malformed"):
        await coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)

    assert store.failures == [
        ("malformed-batch-one", "broker_time_budget", 100.0),
        ("malformed-batch-two", "broker_time_budget", 100.0),
    ]
    assert verifier.find_calls == []


@pytest.mark.asyncio
async def test_repeated_cancellation_during_cleanup_still_releases_single_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "repeated-cancel-cleanup.db")
    tokens = iter(("repeated-cancel-one",))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    sources: list[str] = []
    for ordinal in (1,):
        source = f"claude:repeated-cancel-{ordinal}"
        sources.append(source)
        db.ensure_session(source, source="cli")
        store.enqueue_sidebar_job(
            SidebarCandidate(
                source_session_id=source,
                provider=Provider.CLAUDE,
                bridge_id=sidebar_bridge_id(source),
                title=f"[Claude] Repeated cancel {ordinal}",
                cwd=f"C:/repeated-cancel/{ordinal}",
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
                eligible_at=10.0 + ordinal,
            )
        )
    verifier = BlockingVerifier()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    original_fail = store.fail_sidebar_job
    cleanup_calls = 0

    def blocking_first_cleanup(**kwargs: Any) -> dict[str, Any]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_started.set()
            assert cleanup_release.wait(timeout=5)
        return original_fail(**kwargs)

    monkeypatch.setattr(store, "fail_sidebar_job", blocking_first_cleanup)
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(verifier.started.wait, 5)
    claim_task.cancel()
    verifier.release.set()
    assert await asyncio.to_thread(cleanup_started.wait, 5)

    claim_task.cancel()
    await asyncio.sleep(0)
    claim_task.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await claim_task

    assert cleanup_calls == 1
    assert all(
        store.get_sidebar_job_for_source(source)["state"] == "sidebar_pending"
        for source in sources
    )
    db.close()


@pytest.mark.asyncio
async def test_hung_cleanup_does_not_block_cancelled_caller_or_shutdown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "hung-cancel-cleanup.db")
    token = "hung-cleanup-token"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=lambda: token,
        sidebar_jitter=lambda _bound: 0.0,
    )
    source = "claude:hung-cleanup"
    db.ensure_session(source, source="cli")
    store.enqueue_sidebar_job(
        SidebarCandidate(
            source_session_id=source,
            provider=Provider.CLAUDE,
            bridge_id=sidebar_bridge_id(source),
            title="[Claude] Hung cleanup",
            cwd="C:/hung-cleanup",
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=10.0,
        )
    )
    verifier = BlockingVerifier()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()

    def hung_cleanup(**_kwargs: Any) -> dict[str, Any]:
        cleanup_started.set()
        assert cleanup_release.wait(timeout=5)
        raise RuntimeError("hung cleanup token=must-not-leak")

    monkeypatch.setattr(store, "fail_sidebar_job", hung_cleanup)
    coordinator = _coordinator(
        store,
        verifier,
        clock=lambda: 100.0,
        recovery_timeout=0.02,
    )
    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(verifier.started.wait, 5)
    claim_task.cancel("hung-cleanup-cancel")
    verifier.release.set()
    assert await asyncio.to_thread(cleanup_started.wait, 5)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(claim_task, timeout=0.5)
    assert coordinator._sidebar_recovery_tasks
    await asyncio.wait_for(coordinator.stop(), timeout=0.2)

    cleanup_release.set()
    deadline = asyncio.get_running_loop().time() + 1.0
    while coordinator._sidebar_recovery_tasks:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    assert coordinator._sidebar_recovery_tasks == set()
    db.close()


class CleanupFailureStore(FakeSidebarStore):
    def __init__(self) -> None:
        super().__init__()
        self.claims = [
            {**_leased_job(), "lease_token": "cleanup-one"},
        ]

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        return self.claims[:limit]

    def fail_sidebar_job(
        self, *, lease_token: str, error_code: str, now: float
    ) -> dict[str, Any]:
        self.failures.append((lease_token, error_code, now))
        if lease_token == "cleanup-one":
            raise RuntimeError("first cleanup failed")
        return {**self.claims[0], "state": "sidebar_pending"}


@pytest.mark.asyncio
async def test_cancelled_reconciliation_cleanup_failure_does_not_mask_cancel() -> None:
    store = CleanupFailureStore()
    verifier = BlockingVerifier()
    coordinator = _coordinator(store, verifier, clock=lambda: 100.0)

    claim_task = asyncio.create_task(
        coordinator.claim_sidebar_jobs_for_delivery(now=100.0, limit=1)
    )
    assert await asyncio.to_thread(verifier.started.wait, 5)
    claim_task.cancel()
    verifier.release.set()
    with pytest.raises(asyncio.CancelledError):
        await claim_task

    assert store.failures == [
        ("cleanup-one", "broker_time_budget", 100.0),
    ]
