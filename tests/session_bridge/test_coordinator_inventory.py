from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_state import SessionDB
from session_bridge.codex_adapter import CodexThreadSummary
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import SessionBridgeCoordinator
from session_bridge.mirror import load_continuous_watermark
from session_bridge.models import (
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    UpsertResult,
    canonical_session_id,
)
from session_bridge.store import SessionBridgeStore


def _message(
    event_id: str,
    *,
    timestamp: float,
    content: str = "meaningful user message",
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role="user",
        content=content,
        timestamp=timestamp,
    )


def _projection(
    provider: Provider,
    native_id: str,
    *,
    started_at: float,
    last_active: float,
    cursor: str | None = None,
    source_hash: str | None = None,
    messages: tuple[ProjectedMessage, ...] | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} {native_id}",
        cwd="C:/workspace/project",
        started_at=started_at,
        last_active=last_active,
        messages=(
            messages
            if messages is not None
            else (
                _message(
                    f"event-{native_id}",
                    timestamp=max(started_at, last_active - 1.0),
                ),
            )
        ),
        native_cursor=cursor or f"cursor-{native_id}",
        native_hash=source_hash or f"hash-{native_id}",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


class _StateStore:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.upserted: list[str] = []
        self.external: dict[str, dict[str, Any]] = {}

    def get_state(self, key: str) -> dict[str, Any] | None:
        value = self.states.get(key)
        return deepcopy(value) if value is not None else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        self.states[key] = deepcopy(value)

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        session_id = canonical_session_id(projection.provider, projection.native_id)
        first_seen = session_id not in self.external
        self.upserted.append(projection.native_id)
        self.external[session_id] = {
            "session_id": session_id,
            "provider": projection.provider.value,
            "native_id": projection.native_id,
            "last_native_cursor": projection.native_cursor,
            "last_native_hash": projection.native_hash,
        }
        return UpsertResult(
            session_id=session_id,
            inserted_messages=len(projection.messages),
            rebuilt=rebuild,
            first_seen=first_seen,
        )

    def get_external_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.external.get(session_id)
        return deepcopy(row) if row is not None else None


class _CodexInventory:
    def __init__(
        self,
        *,
        active: list[CodexThreadSummary],
        archived: list[CodexThreadSummary],
    ) -> None:
        self.active = list(active)
        self.archived = list(archived)
        self.calls: list[bool] = []
        self.projected: list[str] = []

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self.calls.append(archived)
        return list(self.archived if archived else self.active)

    def project_thread(self, summary: CodexThreadSummary) -> SessionProjection:
        self.projected.append(summary.native_id)
        return _projection(
            Provider.CODEX,
            summary.native_id,
            started_at=summary.started_at,
            last_active=summary.last_active,
            cursor=summary.revision,
        )

    def find_native_thread(self, native_id: str) -> CodexThreadSummary | None:
        return next(
            (
                summary
                for summary in (*self.active, *self.archived)
                if summary.native_id == native_id
            ),
            None,
        )


def _codex_summary(
    native_id: str,
    last_active: float,
    *,
    archived: bool,
) -> CodexThreadSummary:
    return CodexThreadSummary(
        native_id=native_id,
        title=native_id,
        cwd="C:/workspace/project",
        started_at=last_active - 10.0,
        last_active=last_active,
        archived=archived,
        revision=f"revision-{native_id}-{int(archived)}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_archived", "expected_calls", "expected_ids"),
    (
        (
            True,
            [False, True],
            {"active", "shared", "archived"},
        ),
        (False, [False], {"active", "shared"}),
    ),
)
async def test_codex_scan_always_reads_active_and_optionally_merges_archived_once(
    include_archived: bool,
    expected_calls: list[bool],
    expected_ids: set[str],
) -> None:
    active_shared = _codex_summary("shared", 300.0, archived=False)
    archived_shared = _codex_summary("shared", 100.0, archived=True)
    adapter = _CodexInventory(
        active=[
            _codex_summary("active", 400.0, archived=False),
            active_shared,
        ],
        archived=[
            archived_shared,
            _codex_summary("archived", 200.0, archived=True),
        ],
    )
    store = _StateStore()
    config = replace(
        BridgeConfig(),
        catalog=replace(
            BridgeConfig().catalog,
            include_archived_codex=include_archived,
        ),
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={Provider.CODEX: adapter},
        scan_batch_size=10,
    )

    summary = await coordinator.scan_once(Provider.CODEX)

    assert adapter.calls == expected_calls
    assert set(adapter.projected) == expected_ids
    assert adapter.projected.count("shared") == 1
    assert summary.discovered == len(expected_ids)
    assert summary.indexed == len(expected_ids)


class _ClaudeInventory:
    def __init__(
        self,
        projections: dict[str, SessionProjection],
        paths: dict[str, Path],
        *,
        bad_native_ids: set[str] | None = None,
    ) -> None:
        self.projections = projections
        self.paths = paths
        self.bad_native_ids = set(bad_native_ids or ())
        self.parsed: list[str] = []

    def discover(self) -> list[Path]:
        return list(self.paths.values())

    def find_native_session(self, native_id: str) -> Path | None:
        return self.paths.get(native_id)

    def parse(self, path: Path) -> Any:
        native_id = path.stem
        self.parsed.append(native_id)
        if native_id in self.bad_native_ids:
            raise ValueError("secret provider parser detail")
        return SimpleNamespace(
            projection=self.projections[native_id],
            rebuild=False,
        )


def _claude_files(
    tmp_path: Path,
    native_ids_and_mtimes: list[tuple[str, float]],
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for native_id, mtime in native_ids_and_mtimes:
        path = tmp_path / f"{native_id}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        paths[native_id] = path
    return paths


@pytest.mark.asyncio
async def test_claude_committed_fingerprint_prevents_unchanged_requeue(
    tmp_path: Path,
) -> None:
    paths = _claude_files(tmp_path, [("unchanged", 100.0)])
    adapter = _ClaudeInventory(
        {
            "unchanged": _projection(
                Provider.CLAUDE,
                "unchanged",
                started_at=80.0,
                last_active=100.0,
            )
        },
        paths,
    )
    store = _StateStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=10,
    )

    first = await coordinator.scan_once(Provider.CLAUDE)
    second = await coordinator.scan_once(Provider.CLAUDE)

    assert first.indexed == 1
    assert second.discovered == 0
    assert second.indexed == 0
    assert adapter.parsed == ["unchanged"]
    assert store.upserted == ["unchanged"]


@pytest.mark.asyncio
async def test_changed_newest_claude_session_jumps_ahead_of_backlog(
    tmp_path: Path,
) -> None:
    paths = _claude_files(
        tmp_path,
        [("first", 300.0), ("backlog", 200.0), ("changed", 100.0)],
    )
    projections = {
        native_id: _projection(
            Provider.CLAUDE,
            native_id,
            started_at=50.0,
            last_active=mtime,
            cursor=f"cursor-{int(mtime)}",
        )
        for native_id, mtime in (
            ("first", 300.0),
            ("backlog", 200.0),
            ("changed", 100.0),
        )
    }
    adapter = _ClaudeInventory(projections, paths)
    store = _StateStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=1,
    )

    await coordinator.scan_once(Provider.CLAUDE)
    projections["changed"] = replace(
        projections["changed"],
        last_active=400.0,
        native_cursor="cursor-400",
        native_hash="hash-400",
    )
    os.utime(paths["changed"], (400.0, 400.0))
    await coordinator.scan_once(Provider.CLAUDE)

    assert adapter.parsed[:2] == ["first", "changed"]
    assert store.upserted[:2] == ["first", "changed"]


@pytest.mark.asyncio
async def test_permanent_bad_claude_item_does_not_poison_later_batch(
    tmp_path: Path,
) -> None:
    paths = _claude_files(tmp_path, [("bad", 300.0), ("good", 200.0)])
    adapter = _ClaudeInventory(
        {
            native_id: _projection(
                Provider.CLAUDE,
                native_id,
                started_at=100.0,
                last_active=mtime,
            )
            for native_id, mtime in (("bad", 300.0), ("good", 200.0))
        },
        paths,
        bad_native_ids={"bad"},
    )
    store = _StateStore()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
        scan_batch_size=1,
    )

    first = await coordinator.scan_once(Provider.CLAUDE)
    second = await coordinator.scan_once(Provider.CLAUDE)

    assert first.failed == 1
    assert second.indexed == 1
    assert store.upserted == ["good"]
    assert adapter.parsed[:2] == ["bad", "good"]


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_continuous_mirroring_uses_startup_watermark_and_only_new_eligible_native(
    tmp_path: Path,
) -> None:
    clock = _Clock(100.0)
    db = SessionDB(tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=clock)
        paths = _claude_files(tmp_path, [("preexisting", 90.0)])
        projections = {
            "preexisting": _projection(
                Provider.CLAUDE,
                "preexisting",
                started_at=80.0,
                last_active=90.0,
                messages=(_message("old", timestamp=85.0),),
            )
        }
        adapter = _ClaudeInventory(projections, paths)
        disabled = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters={Provider.CLAUDE: adapter},
            clock=clock,
            monotonic=clock,
            scan_batch_size=20,
        )

        await disabled.scan_once(Provider.CLAUDE)

        assert load_continuous_watermark(store) == 100.0
        assert store.list_mirror_jobs([MirrorJobState.QUEUED]) == []

        clock.value = 120.0
        additions = {
            "eligible": _projection(
                Provider.CLAUDE,
                "eligible",
                started_at=105.0,
                last_active=115.0,
                messages=(_message("eligible-user", timestamp=106.0),),
            ),
            "bridge-origin": _projection(
                Provider.CLAUDE,
                "bridge-origin",
                started_at=106.0,
                last_active=116.0,
                messages=(_message("bridge-user", timestamp=107.0),),
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                origin_bridge_id="bridge-existing",
            ),
            "empty": _projection(
                Provider.CLAUDE,
                "empty",
                started_at=107.0,
                last_active=117.0,
                messages=(),
            ),
        }
        projections.update(additions)
        for index, native_id in enumerate(additions, start=1):
            paths.update(_claude_files(tmp_path, [(native_id, 120.0 + index)]))
        enabled_config = replace(
            BridgeConfig(),
            mirrors=replace(
                BridgeConfig().mirrors,
                automatic_creation=True,
            ),
        )
        enabled = SessionBridgeCoordinator(
            config=enabled_config,
            store=store,
            adapters={Provider.CLAUDE: adapter},
            clock=clock,
            monotonic=clock,
            scan_batch_size=20,
        )

        await enabled.scan_once(Provider.CLAUDE)

        jobs = store.list_mirror_jobs([MirrorJobState.QUEUED])
        assert load_continuous_watermark(store) == 100.0
        assert [job["source_session_id"] for job in jobs] == [
            "claude:eligible",
            "claude:preexisting",
        ]
        assert jobs[0]["target_provider"] == Provider.CODEX.value
    finally:
        db.close()


class _DivergenceStore(_StateStore):
    def __init__(
        self,
        *,
        source_projection: SessionProjection,
        target_projection: SessionProjection,
        source_baseline: tuple[str, str],
        target_baseline: tuple[str, str],
    ) -> None:
        super().__init__()
        self.source_projection = source_projection
        self.target_projection = target_projection
        self.snapshot = {
            "version": 1,
            "bridge_id": "bridge-divergence",
            "pack_id": "pack-divergence",
            "source_session_id": canonical_session_id(
                source_projection.provider,
                source_projection.native_id,
            ),
            "source_cursor": source_baseline[0],
            "source_hash": source_baseline[1],
            "target_session_id": canonical_session_id(
                target_projection.provider,
                target_projection.native_id,
            ),
            "target_cursor": target_baseline[0],
            "target_hash": target_baseline[1],
        }
        for projection, baseline in (
            (source_projection, source_baseline),
            (target_projection, target_baseline),
        ):
            session_id = canonical_session_id(projection.provider, projection.native_id)
            self.external[session_id] = {
                "session_id": session_id,
                "provider": projection.provider.value,
                "native_id": projection.native_id,
                "last_native_cursor": baseline[0],
                "last_native_hash": baseline[1],
            }
        self.listed_continuations = 0
        self.diverged: list[str] = []

    def list_mirror_jobs(
        self,
        states: list[MirrorJobState],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        del states, limit
        return []

    def list_continuation_snapshots(
        self,
        *,
        limit: int = 1000,
        after_bridge_id: str | None = None,
    ) -> list[dict[str, Any]]:
        assert limit > 0
        assert after_bridge_id is None
        self.listed_continuations += 1
        return [deepcopy(self.snapshot)]

    def mark_diverged(self, bridge_id: str, *, at: float) -> None:
        assert at >= 0
        self.diverged.append(bridge_id)


class _OneClaudeSession:
    def __init__(self, path: Path, projection: SessionProjection) -> None:
        self.path = path
        self.projection = projection

    def find_native_session(self, native_id: str) -> Path | None:
        return self.path if native_id == self.projection.native_id else None

    def parse(self, path: Path) -> Any:
        assert path == self.path
        return SimpleNamespace(projection=self.projection, rebuild=False)


class _OneCodexSession:
    def __init__(self, projection: SessionProjection) -> None:
        self.projection = projection
        self.summary = _codex_summary(
            projection.native_id,
            projection.last_active,
            archived=False,
        )

    def find_native_thread(self, native_id: str) -> CodexThreadSummary | None:
        return self.summary if native_id == self.projection.native_id else None

    def project_thread(self, summary: CodexThreadSummary) -> SessionProjection:
        assert summary == self.summary
        return self.projection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_advanced", "target_advanced", "expected_diverged"),
    (
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ),
)
async def test_periodic_reconcile_marks_only_two_sided_divergence_without_continue(
    tmp_path: Path,
    source_advanced: bool,
    target_advanced: bool,
    expected_diverged: bool,
) -> None:
    source_baseline = ("source-cursor-1", "source-hash-1")
    target_baseline = ("target-cursor-1", "target-hash-1")
    source = _projection(
        Provider.CLAUDE,
        "source",
        started_at=10.0,
        last_active=20.0,
        cursor="source-cursor-2" if source_advanced else source_baseline[0],
        source_hash="source-hash-2" if source_advanced else source_baseline[1],
    )
    target = _projection(
        Provider.CODEX,
        "target",
        started_at=10.0,
        last_active=20.0,
        cursor="target-cursor-2" if target_advanced else target_baseline[0],
        source_hash="target-hash-2" if target_advanced else target_baseline[1],
        origin_kind=OriginKind.BRIDGE_CONTINUATION,
        origin_bridge_id="bridge-divergence",
    )
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    store = _DivergenceStore(
        source_projection=source,
        target_projection=target,
        source_baseline=source_baseline,
        target_baseline=target_baseline,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: _OneClaudeSession(source_path, source),
            Provider.CODEX: _OneCodexSession(target),
        },
        clock=lambda: 100.0,
    )

    await coordinator.reconcile_once()

    assert store.listed_continuations == 1
    assert store.diverged == (["bridge-divergence"] if expected_diverged else [])
