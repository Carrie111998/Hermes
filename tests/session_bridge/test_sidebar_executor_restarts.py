from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_state import SessionDB
from session_bridge.codex_adapter import SidebarThreadVerifier
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    decode_bridge_marker,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    sidebar_bridge_id,
    sidebar_idempotency_key,
)
from session_bridge.sidebar_executor import (
    NativeCreateAmbiguous,
    NativeThreadState,
    NativeThreadStatus,
    SidebarExecutor,
)
from session_bridge.store import SessionBridgeStore


_MARKER_SECRET = b"sidebar-executor-restart-test-secret"
_THREAD_ID = "88888888-8888-4888-8888-888888888888"


@dataclass
class _Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _NativeWorld:
    threads: dict[str, BridgeMarkerPayload] = field(default_factory=dict)
    recovery_keys: dict[str, str] = field(default_factory=dict)
    registered_threads: set[str] = field(default_factory=set)
    create_calls: list[str] = field(default_factory=list)
    register_calls: list[str] = field(default_factory=list)
    rename_calls: list[str] = field(default_factory=list)
    find_by_marker_calls: list[BridgeMarkerPayload] = field(default_factory=list)
    lose_create_response_once: bool = False
    die_after_create_once: bool = False
    die_after_bind_once: bool = False
    die_before_rename_once: bool = False


class _NativeDelivery:
    def __init__(
        self,
        world: _NativeWorld,
        store: SessionBridgeStore,
        clock: _Clock,
    ) -> None:
        self._world = world
        self._store = store
        self._clock = clock

    def preflight(self, *, deadline: float) -> None:
        assert deadline > self._clock()

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        recovery_key: str,
        deadline: float,
    ) -> str:
        assert deadline > self._clock()
        marker = next(
            line.removeprefix("Signed marker: ")
            for line in prompt.splitlines()
            if line.startswith("Signed marker: ")
        )
        payload = decode_bridge_marker(marker, _MARKER_SECRET)
        self._world.create_calls.append(_THREAD_ID)
        self._world.threads[_THREAD_ID] = payload
        self._world.recovery_keys[_THREAD_ID] = recovery_key
        self._store.upsert_projection(
            SessionProjection(
                provider=Provider.CODEX,
                native_id=_THREAD_ID,
                title="Native sidebar placeholder",
                cwd=candidate.cwd,
                started_at=self._clock(),
                last_active=self._clock(),
                messages=(
                    ProjectedMessage(
                        native_event_id=f"registration-{_THREAD_ID}",
                        ordinal=0,
                        role="user",
                        content=prompt,
                        timestamp=self._clock(),
                    ),
                ),
                native_path=f"native://{_THREAD_ID}",
                native_cursor=f"cursor-{_THREAD_ID}",
                native_hash=f"hash-{_THREAD_ID}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=payload.bridge_id,
            )
        )
        if self._world.lose_create_response_once:
            self._world.lose_create_response_once = False
            raise NativeCreateAmbiguous()
        if self._world.die_after_create_once:
            self._world.die_after_create_once = False
            raise SystemExit("synthetic process death after native create")
        return _THREAD_ID

    def register_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
        fresh: bool = False,
    ) -> None:
        del prompt
        assert isinstance(fresh, bool)
        assert deadline > self._clock()
        assert thread_id in self._world.threads
        self._world.register_calls.append(thread_id)
        self._world.registered_threads.add(thread_id)
        if self._world.die_after_bind_once:
            self._world.die_after_bind_once = False
            raise SystemExit("synthetic process death after durable bind")

    def read_thread_state(
        self,
        *,
        thread_id: str,
        deadline: float,
    ) -> NativeThreadState:
        assert deadline > self._clock()
        assert thread_id in self._world.threads
        return NativeThreadState(
            thread_id=thread_id,
            status=NativeThreadStatus.IDLE,
            cwd="C:/workspace/project",
        )

    def rename_thread(
        self,
        *,
        thread_id: str,
        title: str,
        deadline: float,
    ) -> None:
        del title
        assert deadline > self._clock()
        assert thread_id in self._world.threads
        self._world.rename_calls.append(thread_id)


class _Verifier:
    def __init__(self, world: _NativeWorld) -> None:
        self._world = world

    def find_by_marker(
        self,
        expected: BridgeMarkerPayload,
    ) -> VerifiedSidebarThread | None:
        self._world.find_by_marker_calls.append(expected)
        matches = [
            thread_id
            for thread_id, payload in self._world.threads.items()
            if payload == expected and thread_id in self._world.registered_threads
        ]
        assert len(matches) <= 1
        if not matches:
            return None
        return _verified(matches[0], expected)

    def find_by_recovery_key(
        self,
        recovery_key: str,
        *,
        expected_cwd: str,
        deadline: float,
    ) -> str | None:
        assert deadline > 0
        assert expected_cwd == "C:/workspace/project"
        matches = [
            thread_id
            for thread_id, key in self._world.recovery_keys.items()
            if key == recovery_key
        ]
        assert len(matches) <= 1
        return matches[0] if matches else None

    def verify_thread(
        self,
        *,
        thread_id: str,
        expected: BridgeMarkerPayload,
    ) -> VerifiedSidebarThread:
        assert self._world.threads[thread_id] == expected
        if self._world.die_before_rename_once:
            self._world.die_before_rename_once = False
            raise SystemExit("synthetic process death before rename")
        return _verified(thread_id, expected)


class _CommitResponseLossStore:
    def __init__(self, delegate: SessionBridgeStore) -> None:
        self.delegate = delegate
        self.commit_arguments: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def commit_sidebar_job_with_lineage(self, **arguments: Any) -> dict[str, Any]:
        self.commit_arguments = dict(arguments)
        self.delegate.commit_sidebar_job_with_lineage(**arguments)
        raise OSError("synthetic commit response loss")


class _BindResponseLossStore:
    def __init__(self, delegate: SessionBridgeStore) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def bind_sidebar_thread(self, **arguments: Any) -> dict[str, Any]:
        self.delegate.bind_sidebar_thread(**arguments)
        raise OSError("synthetic bind response loss")


def test_thread_start_response_loss_never_authorizes_a_second_create(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld(lose_create_response_once=True)
    path = tmp_path / "create-response-loss.db"
    db, store = _seed_store(path, clock)
    try:
        first = _executor(store, world, clock).run_once()
        assert first.status == "visible"
        assert first.thread_id == _THREAD_ID
    finally:
        db.close()

    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        restarted = _executor(restarted_store, world, clock).run_once()

        assert restarted.status == "idle"
        assert world.create_calls == [_THREAD_ID]
        job = restarted_store.get_sidebar_job_for_source("restart-source")
        assert job is not None
        assert job["state"] == "sidebar_visible"
        assert job["codex_thread_id"] == _THREAD_ID
    finally:
        restarted_db.close()


def test_process_death_after_create_recovers_reserved_exact_thread_without_recreation(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld(die_after_create_once=True)
    path = tmp_path / "restart-after-create.db"
    db, store = _seed_store(path, clock)
    try:
        with pytest.raises(SystemExit, match="after native create"):
            _executor(store, world, clock).run_once()
        job = store.get_sidebar_job_for_source("restart-source")
        assert job is not None
        assert job["state"] == "sidebar_leased"
        assert job["codex_thread_id"] is None
        reservation = store.get_sidebar_create_reservation("restart-source")
        assert reservation is not None
        assert reservation["recovery_key"] in world.recovery_keys.values()
    finally:
        db.close()

    clock.advance(301.0)
    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        result = _executor(restarted_store, world, clock).run_once()

        assert result.status == "visible"
        assert result.thread_id == _THREAD_ID
        assert world.create_calls == [_THREAD_ID]
        _assert_unique_visible_lineage(restarted_db)
    finally:
        restarted_db.close()


def test_bind_response_loss_atomically_retains_exact_id_across_restart(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld()
    path = tmp_path / "bind-response-loss.db"
    db, store = _seed_store(path, clock)
    try:
        first = _executor(_BindResponseLossStore(store), world, clock).run_once()
        assert first.status == "retry"
        retained = store.get_sidebar_job_for_source("restart-source")
        assert retained is not None
        assert retained["codex_thread_id"] == _THREAD_ID
    finally:
        db.close()

    clock.advance(61.0)
    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        result = _executor(restarted_store, world, clock).run_once()

        assert result.status == "visible"
        assert result.thread_id == _THREAD_ID
        assert world.create_calls == [_THREAD_ID]
        _assert_unique_visible_lineage(restarted_db)
    finally:
        restarted_db.close()


def test_process_restart_after_durable_bind_resumes_the_exact_thread(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld(die_after_bind_once=True)
    path = tmp_path / "restart-after-bind.db"
    db, store = _seed_store(path, clock)
    try:
        with pytest.raises(SystemExit, match="after durable bind"):
            _executor(store, world, clock).run_once()
        bound = store.get_sidebar_job_for_source("restart-source")
        assert bound is not None
        assert bound["state"] == "sidebar_leased"
        assert bound["codex_thread_id"] == _THREAD_ID
    finally:
        db.close()

    clock.advance(301.0)
    world.find_by_marker_calls.clear()
    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        persisted = restarted_store.get_sidebar_job_for_source("restart-source")
        assert persisted is not None
        assert persisted["codex_thread_id"] == _THREAD_ID
        result = _executor(restarted_store, world, clock).run_once()

        assert result.status == "visible"
        assert result.thread_id == _THREAD_ID
        assert world.create_calls == [_THREAD_ID]
        assert world.find_by_marker_calls == []
        assert world.register_calls == [_THREAD_ID, _THREAD_ID]
        _assert_unique_visible_lineage(restarted_db)
    finally:
        restarted_db.close()


def test_restart_before_rename_resumes_the_exact_thread_without_recreation(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld(die_before_rename_once=True)
    path = tmp_path / "restart-before-rename.db"
    db, store = _seed_store(path, clock)
    try:
        with pytest.raises(SystemExit, match="before rename"):
            _executor(store, world, clock).run_once()
        bound = store.get_sidebar_job_for_source("restart-source")
        assert bound is not None
        assert bound["codex_thread_id"] == _THREAD_ID
        assert world.rename_calls == []
    finally:
        db.close()

    clock.advance(301.0)
    world.find_by_marker_calls.clear()
    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        persisted = restarted_store.get_sidebar_job_for_source("restart-source")
        assert persisted is not None
        assert persisted["codex_thread_id"] == _THREAD_ID
        result = _executor(restarted_store, world, clock).run_once()

        assert result.status == "visible"
        assert result.thread_id == _THREAD_ID
        assert world.create_calls == [_THREAD_ID]
        assert world.find_by_marker_calls == []
        assert world.rename_calls == [_THREAD_ID]
        _assert_unique_visible_lineage(restarted_db)
    finally:
        restarted_db.close()


def test_commit_response_loss_replay_is_unique_and_never_recreates(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    world = _NativeWorld()
    path = tmp_path / "commit-response-loss.db"
    db, store = _seed_store(path, clock)
    lossy_store = _CommitResponseLossStore(store)
    commit_arguments: dict[str, Any]
    try:
        result = _executor(lossy_store, world, clock).run_once()

        assert result.status == "unsettled"
        assert lossy_store.commit_arguments is not None
        commit_arguments = dict(lossy_store.commit_arguments)
        _assert_unique_visible_lineage(db)
    finally:
        db.close()

    restarted_db = SessionDB(path)
    try:
        restarted_store = _store(restarted_db, clock)
        replay = restarted_store.commit_sidebar_job_with_lineage(
            **commit_arguments,
        )
        assert replay["state"] == "sidebar_visible"
        assert replay["codex_thread_id"] == _THREAD_ID
        assert (
            replay["completion_digest"]
            == hashlib.sha256(str(commit_arguments["lease_token"]).encode()).hexdigest()
        )
        _assert_unique_visible_lineage(restarted_db)

        restarted = _executor(restarted_store, world, clock).run_once()

        assert restarted.status == "idle"
        assert world.create_calls == [_THREAD_ID]
        _assert_unique_visible_lineage(restarted_db)
    finally:
        restarted_db.close()


def _seed_store(path: Path, clock: _Clock) -> tuple[SessionDB, SessionBridgeStore]:
    db = SessionDB(path)
    db.ensure_session("restart-source", source="cli")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (clock(), "restart-source"),
        )
    )
    store = _store(db, clock)
    store.enqueue_sidebar_job(
        SidebarCandidate(
            source_session_id="restart-source",
            provider=Provider.HERMES,
            bridge_id=sidebar_bridge_id("restart-source"),
            title="[Hermes] restart source",
            cwd="C:/workspace/project",
            git_root="C:/workspace/project",
            git_branch="feature/restart-safety",
            git_head="a" * 40,
            worktree_id="restart-worktree",
            eligible_at=clock(),
        )
    )
    return db, store


def _store(db: SessionDB, clock: _Clock) -> SessionBridgeStore:
    return SessionBridgeStore(
        db,
        clock=clock,
        sidebar_jitter=lambda _bound: 0.0,
    )


def _executor(
    store: Any,
    world: _NativeWorld,
    clock: _Clock,
) -> SidebarExecutor:
    return SidebarExecutor(
        store=store,
        verifier=cast(SidebarThreadVerifier, _Verifier(world)),
        native=_NativeDelivery(
            world,
            store.delegate if isinstance(store, _CommitResponseLossStore) else store,
            clock,
        ),
        marker_secret=_MARKER_SECRET,
        clock=clock,
        monotonic=clock,
        sleep=lambda _seconds: None,
        poll_interval=0.01,
    )


def _verified(
    thread_id: str,
    payload: BridgeMarkerPayload,
) -> VerifiedSidebarThread:
    return VerifiedSidebarThread(
        thread_id=thread_id,
        source_session_id=payload.source_session_id,
        bridge_id=payload.bridge_id,
    )


def _assert_unique_visible_lineage(db: SessionDB) -> None:
    job_rows = _rows(
        db,
        "SELECT source_session_id, bridge_id, idempotency_key, codex_thread_id, state "
        "FROM session_sidebar_jobs",
    )
    assert job_rows == [
        {
            "source_session_id": "restart-source",
            "bridge_id": sidebar_bridge_id("restart-source"),
            "idempotency_key": sidebar_idempotency_key("restart-source"),
            "codex_thread_id": _THREAD_ID,
            "state": "sidebar_visible",
        }
    ]
    assert _rows(
        db,
        "SELECT from_session_id, to_session_id, bridge_id, relation FROM session_links",
    ) == [
        {
            "from_session_id": "restart-source",
            "to_session_id": f"codex:{_THREAD_ID}",
            "bridge_id": sidebar_bridge_id("restart-source"),
            "relation": "mirrors",
        }
    ]


def _rows(db: SessionDB, query: str) -> list[dict[str, object]]:
    with db._lock:
        assert db._conn is not None
        return [dict(row) for row in db._conn.execute(query).fetchall()]
