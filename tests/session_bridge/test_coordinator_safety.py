from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from session_bridge.claude_adapter import (
    AmbiguousPlaceholderCreation,
    PlaceholderCreationError,
    PlaceholderResult,
)
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import (
    ContinueRequest,
    RefreshResult,
    SessionBridgeCoordinator,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    MirrorJobState,
    OriginKind,
    Provider,
    Relation,
    SessionProjection,
    UpsertResult,
)
from tests.session_bridge.test_end_to_end import (
    _SidebarEndToEndHarness,
    _canonical_sidebar_path,
    _sidebar_call_tool,
    _sidebar_rpc,
)


def _job(provider: Provider, *, state: MirrorJobState = MirrorJobState.RUNNING):
    return {
        "id": f"job:safety-{provider.value}",
        "idempotency_key": f"safety-{provider.value}-idempotency",
        "source_session_id": (
            "codex:source-native"
            if provider is Provider.CLAUDE
            else "claude:source-native"
        ),
        "target_provider": provider.value,
        "state": state.value,
        "attempts": 1,
        "next_attempt_at": 100.0,
        "created_at": 90.0,
        "updated_at": 100.0,
        "target_native_id": None,
        "error_code": None,
        "error_detail": None,
    }


def _bridge_id(job: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        f"session-bridge:{job['idempotency_key']}".encode()
    ).hexdigest()
    return f"bridge:{digest}"


def _claude_native_id(job: Mapping[str, Any]) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes-session-bridge:{job['idempotency_key']}",
        )
    )


def _projection(
    provider: Provider,
    native_id: str,
    *,
    bridge_id: str | None = None,
    cursor: str = "cursor-fresh",
    source_hash: str = "hash-fresh",
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title="Target session",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=100.0,
        messages=(),
        native_cursor=cursor,
        native_hash=source_hash,
        origin_kind=(
            OriginKind.BRIDGE_PLACEHOLDER
            if bridge_id is not None
            else OriginKind.NATIVE
        ),
        origin_bridge_id=bridge_id,
    )


class _SafetyStore:
    def __init__(
        self,
        *,
        claimed: list[dict[str, Any]] | None = None,
        running: list[dict[str, Any]] | None = None,
        launch_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.claimed = [deepcopy(job) for job in (claimed or [])]
        self.running = [deepcopy(job) for job in (running or [])]
        self.launch_metadata = (
            deepcopy(dict(launch_metadata)) if launch_metadata is not None else None
        )
        self.states: dict[str, dict[str, Any]] = {}
        self.origin_rows: dict[tuple[str, Provider], dict[str, Any]] = {}
        self.external: dict[str, dict[str, Any]] = {}
        self.completions: list[dict[str, Any]] = []
        self.manual_failures: list[dict[str, Any]] = []
        self.retries: list[dict[str, Any]] = []
        self.rebuild_flags: list[bool] = []
        self.metadata_calls: list[str] = []

    def get_state(self, key: str) -> dict[str, Any] | None:
        value = self.states.get(key)
        return deepcopy(value) if value is not None else None

    def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        self.states[key] = deepcopy(dict(value))

    def claim_due_jobs(self, *, now: float, limit: int, policy: object):
        del now, policy
        claimed = self.claimed[:limit]
        del self.claimed[:limit]
        return deepcopy(claimed)

    def list_mirror_jobs(self, states: list[object], *, limit: int = 1000):
        del states, limit
        return deepcopy(self.running)

    def list_continuation_snapshots(
        self,
        *,
        limit: int = 1000,
        after_bridge_id: str | None = None,
    ):
        del limit, after_bridge_id
        return []

    def get_session_launch_metadata(self, session_id: str):
        self.metadata_calls.append(session_id)
        return deepcopy(self.launch_metadata)

    def get_external_session(self, session_id: str):
        row = self.external.get(session_id)
        return deepcopy(row) if row is not None else None

    def find_external_session_by_origin_bridge(
        self,
        bridge_id: str,
        provider: Provider,
    ):
        row = self.origin_rows.get((bridge_id, provider))
        return deepcopy(row) if row is not None else None

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        self.rebuild_flags.append(rebuild)
        session_id = f"{projection.provider.value}:{projection.native_id}"
        row = {
            "session_id": session_id,
            "provider": projection.provider.value,
            "native_id": projection.native_id,
            "origin_bridge_id": projection.origin_bridge_id,
            "last_native_cursor": projection.native_cursor,
            "last_native_hash": projection.native_hash,
        }
        self.external[session_id] = row
        if projection.origin_bridge_id is not None:
            self.origin_rows[(projection.origin_bridge_id, projection.provider)] = row
        return UpsertResult(
            session_id=session_id,
            inserted_messages=0,
            rebuilt=rebuild,
            first_seen=False,
        )

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
        self.completions.append({
            "job_id": job_id,
            "target_native_id": target_native_id,
            "target_session_id": target_session_id,
            "bridge_id": bridge_id,
        })

    def fail_job_manually(self, job_id: str, *, code: str, detail: str) -> None:
        self.manual_failures.append({"job_id": job_id, "code": code, "detail": detail})

    def retry_job(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
        next_attempt_at: float,
    ) -> None:
        self.retries.append({
            "job_id": job_id,
            "code": code,
            "detail": detail,
            "next_attempt_at": next_attempt_at,
        })


class _ClaudeSource:
    def __init__(self, *, marker_ok: bool) -> None:
        self.marker_ok = marker_ok
        self.projection: SessionProjection | None = None
        self.marker_calls: list[tuple[SessionProjection, BridgeMarkerPayload]] = []
        self.find_calls: list[str] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.find_calls.append(native_id)
        if self.projection is None or self.projection.native_id != native_id:
            return None
        return Path(f"C:/synthetic/{native_id}.jsonl")

    def parse(self, path: Path):
        del path
        assert self.projection is not None
        return SimpleNamespace(projection=self.projection, rebuild=False)

    def projection_has_marker_payload(
        self,
        projection: SessionProjection,
        payload: BridgeMarkerPayload,
    ) -> bool:
        self.marker_calls.append((projection, payload))
        return self.marker_ok


class _AmbiguousClaudeTarget:
    def __init__(self, source: _ClaudeSource) -> None:
        self.source = source

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        native_id = kwargs["native_id"]
        self.source.projection = _projection(
            Provider.CLAUDE,
            native_id,
            bridge_id=kwargs["bridge_id"],
        )
        raise AmbiguousPlaceholderCreation(
            "claude_creation_ambiguous",
            native_id=native_id,
        )


class _CodexSource:
    def __init__(self) -> None:
        self.projections: dict[str, SessionProjection] = {}

    def find_native_thread(self, native_id: str):
        return self.projections.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        return summary


class _RecordingCodexTarget:
    def __init__(
        self,
        source: _CodexSource,
        *,
        used_registration_turn: bool = True,
        failure: Exception | None = None,
    ) -> None:
        self.source = source
        self.used_registration_turn = used_registration_turn
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def create_placeholder(self, **kwargs: Any) -> PlaceholderResult:
        self.calls.append(deepcopy(kwargs))
        if self.failure is not None:
            raise self.failure
        native_id = "codex-launched-target"
        self.source.projections[native_id] = _projection(
            Provider.CODEX,
            native_id,
            bridge_id=kwargs["bridge_id"],
        )
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=f"codex:{native_id}",
            used_registration_turn=self.used_registration_turn,
            verified_at=100.0,
        )


@pytest.mark.asyncio
async def test_ambiguous_target_requires_exact_authenticated_marker_payload() -> None:
    job = _job(Provider.CLAUDE)
    store = _SafetyStore(claimed=[job])
    source = _ClaudeSource(marker_ok=False)
    target = _AmbiguousClaudeTarget(source)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: source},
        target_adapters={Provider.CLAUDE: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    expected_payload = BridgeMarkerPayload(
        bridge_id=_bridge_id(job),
        source_session_id=job["source_session_id"],
        target_provider=Provider.CLAUDE,
        policy_generation=1,
    )
    assert source.marker_calls == [(source.projection, expected_payload)]
    assert summary.manual_failure == 1
    assert summary.succeeded == 0
    assert store.completions == []


@pytest.mark.asyncio
async def test_restart_indexes_deterministic_claude_target_before_completion() -> None:
    job = _job(Provider.CLAUDE)
    bridge_id = _bridge_id(job)
    native_id = _claude_native_id(job)
    store = _SafetyStore(running=[job])
    store.states[f"session-bridge:attempt:{job['id']}"] = {
        "version": 1,
        "phase": "provider_call_started",
        "bridge_id": bridge_id,
        "target_provider": Provider.CLAUDE.value,
        "policy_generation": 1,
        "attempts": job["attempts"],
        "expected_native_id": native_id,
    }
    source = _ClaudeSource(marker_ok=True)
    source.projection = _projection(
        Provider.CLAUDE,
        native_id,
        bridge_id=bridge_id,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: source},
        clock=lambda: 100.0,
    )

    summary = await coordinator.reconcile_once()

    assert summary.recovered == 1
    assert summary.failed == 0
    assert source.find_calls == [native_id]
    assert source.marker_calls == [
        (
            source.projection,
            BridgeMarkerPayload(
                bridge_id=bridge_id,
                source_session_id=job["source_session_id"],
                target_provider=Provider.CLAUDE,
                policy_generation=1,
            ),
        )
    ]
    assert store.completions[0]["target_native_id"] == native_id


@pytest.mark.asyncio
async def test_launch_uses_sanitized_metadata_and_reports_registration_fallback() -> (
    None
):
    job = _job(Provider.CODEX)
    store = _SafetyStore(
        claimed=[job],
        launch_metadata={
            "title": "  Source\nworkspace\t ",
            "cwd": " C:/workspace/project ",
        },
    )
    source = _CodexSource()
    target = _RecordingCodexTarget(source, used_registration_turn=True)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    summary = await coordinator.process_jobs_once()

    assert summary.succeeded == 1
    assert store.metadata_calls == [job["source_session_id"]]
    assert target.calls[0]["title"] == "Source workspace"
    assert target.calls[0]["cwd"] == "C:/workspace/project"
    assert coordinator.health()["registration_turn_fallback"] is True


@pytest.mark.asyncio
async def test_refresh_full_snapshot_rebuilds_to_reflect_removed_events() -> None:
    session_id = "codex:refresh-target"
    store = _SafetyStore()
    store.external[session_id] = {
        "session_id": session_id,
        "provider": Provider.CODEX.value,
        "native_id": "refresh-target",
        "last_native_cursor": "cursor-old",
        "last_native_hash": "hash-old",
    }
    source = _CodexSource()
    source.projections["refresh-target"] = _projection(
        Provider.CODEX,
        "refresh-target",
        cursor="cursor-after-truncation",
        source_hash="hash-after-truncation",
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: source},
        clock=lambda: 100.0,
    )

    result = await coordinator.refresh_session(session_id, timeout=1.0)

    assert result.stale is False
    assert store.rebuild_flags == [True]


@pytest.mark.asyncio
async def test_refresh_failure_is_reported_with_sanitized_provider_health() -> None:
    session_id = "codex:refresh-target"
    store = _SafetyStore()
    store.external[session_id] = {
        "session_id": session_id,
        "provider": Provider.CODEX.value,
        "native_id": "refresh-target",
        "last_native_cursor": "cursor-old",
        "last_native_hash": "hash-old",
    }
    secret = "C:/private/refresh-provider-secret"

    class ExplodingCodexSource(_CodexSource):
        def project_thread(self, summary: SessionProjection) -> SessionProjection:
            del summary
            raise RuntimeError(secret)

    source = ExplodingCodexSource()
    source.projections["refresh-target"] = _projection(
        Provider.CODEX,
        "refresh-target",
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: source},
        clock=lambda: 100.0,
    )

    result = await coordinator.refresh_session(session_id, timeout=1.0)
    health = coordinator.health()

    assert result.stale is True
    assert health["providers"][Provider.CODEX.value]["degraded_reason"] == (
        "refresh_failed"
    )
    assert "codex_refresh_failed" in health["recent_error_codes"]
    assert secret not in repr(health)


@pytest.mark.asyncio
async def test_stop_drains_inflight_provider_call_before_returning() -> None:
    started = Event()
    release = Event()

    class BlockingClaudeSource:
        def discover(self) -> list[Path]:
            started.set()
            if not release.wait(timeout=2.0):
                raise RuntimeError("test provider release timed out")
            return []

    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=object(),
        adapters={Provider.CLAUDE: BlockingClaudeSource()},
        refresh_timeout=1.0,
    )
    await coordinator.start()
    assert await asyncio.to_thread(started.wait, 1.0)

    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0.03)
    assert stop_task.done() is False

    release.set()
    await asyncio.wait_for(stop_task, timeout=1.0)
    assert coordinator.health()["provider_calls_inflight"] == 0


class _ReplayStore:
    def __init__(self, pack: ContextPack) -> None:
        self.pack = pack
        self.snapshot: dict[str, Any] | None = None
        self.transition_calls: list[dict[str, str]] = []

    def get_continuation_snapshot(self, bridge_id: str):
        del bridge_id
        return deepcopy(self.snapshot)

    def get_context_pack(self, bridge_id: str, *, budget_chars: int):
        if self.pack.bridge_id != bridge_id or self.pack.budget_chars != budget_chars:
            return None
        return {
            key: getattr(self.pack, key)
            for key in (
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

    def find_external_session_by_origin_bridge(
        self, bridge_id: str, provider: Provider
    ):
        del bridge_id
        return {
            "session_id": "codex:target-existing",
            "provider": provider.value,
            "native_id": "target-existing",
        }

    def transition_link_to_continues(
        self,
        bridge_id: str,
        *,
        pack_id: str,
        target_cursor: str,
        target_hash: str,
    ):
        self.transition_calls.append({
            "bridge_id": bridge_id,
            "pack_id": pack_id,
            "target_cursor": target_cursor,
            "target_hash": target_hash,
        })
        self.pack = replace(self.pack, immutable_at=100.0)
        return {
            "id": "link-existing",
            "from_session_id": self.pack.source_session_id,
            "to_session_id": self.pack.target_session_id,
            "relation": Relation.CONTINUES.value,
            "bridge_id": bridge_id,
            "source_cursor": self.pack.source_cursor,
            "source_hash": self.pack.source_hash,
            "created_at": 90.0,
        }


def _mutable_pack() -> ContextPack:
    return ContextPack(
        id="pack-existing-mutable",
        bridge_id="bridge-replay",
        source_session_id="claude:source-existing",
        target_session_id="codex:target-existing",
        source_cursor="source-cursor",
        source_hash="source-hash",
        budget_chars=4000,
        payload="already persisted before crash",
        created_at=90.0,
        immutable_at=None,
    )


@pytest.mark.asyncio
async def test_continuation_replay_rejects_target_provider_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = replace(_mutable_pack(), immutable_at=100.0)
    store = _ReplayStore(pack)
    store.snapshot = {
        "version": 1,
        "pack_id": pack.id,
        "source_session_id": pack.source_session_id,
        "source_cursor": pack.source_cursor,
        "source_hash": pack.source_hash,
        "target_session_id": pack.target_session_id,
        "target_cursor": "target-cursor",
        "target_hash": "target-hash",
    }
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={},
        clock=lambda: 100.0,
    )

    async def refresh(session_id: str, *, timeout: float) -> RefreshResult:
        del timeout
        return RefreshResult(
            session_id=session_id,
            cursor=(
                "source-cursor" if session_id.startswith("claude:") else "target-cursor"
            ),
            source_hash=(
                "source-hash" if session_id.startswith("claude:") else "target-hash"
            ),
            stale=False,
            warning=None,
        )

    monkeypatch.setattr(coordinator, "refresh_session", refresh)
    request = ContinueRequest(
        session_id=pack.source_session_id,
        bridge_id=pack.bridge_id,
        target_provider=Provider.CLAUDE,
        context_budget_chars=pack.budget_chars,
    )

    with pytest.raises(ValueError, match="target provider"):
        await coordinator.continue_session(request)

    assert store.transition_calls == []


@pytest.mark.asyncio
async def test_crash_recovery_finalizes_existing_mutable_pack_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = _mutable_pack()
    store = _ReplayStore(pack)

    class ForbiddenBuilder:
        calls = 0

        def build(self, request: object):
            del request
            self.calls += 1
            raise AssertionError("existing crash pack must not be rebuilt")

    builder = ForbiddenBuilder()
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={},
        context_builder=builder,
        clock=lambda: 100.0,
    )

    async def refresh(session_id: str, *, timeout: float) -> RefreshResult:
        del timeout
        return RefreshResult(
            session_id=session_id,
            cursor=(
                pack.source_cursor
                if session_id.startswith("claude:")
                else "target-cursor"
            ),
            source_hash=(
                pack.source_hash if session_id.startswith("claude:") else "target-hash"
            ),
            stale=False,
            warning=None,
        )

    monkeypatch.setattr(coordinator, "refresh_session", refresh)
    result = await coordinator.continue_session(
        ContinueRequest(
            session_id=pack.source_session_id,
            bridge_id=pack.bridge_id,
            target_provider=Provider.CODEX,
            context_budget_chars=pack.budget_chars,
        )
    )

    assert builder.calls == 0
    assert result.pack.payload == pack.payload
    assert result.pack.immutable_at == 100.0
    assert store.transition_calls[0]["pack_id"] == pack.id


@pytest.mark.asyncio
async def test_target_failure_uses_fixed_sanitized_health_code() -> None:
    job = _job(Provider.CODEX)
    store = _SafetyStore(claimed=[job])
    source = _CodexSource()
    provider_secret = "C:/private/provider-scan-must-not-leak"
    target_secret = "sk-live-target-error-must-not-leak"

    class ExplodingClaudeAdapter:
        def discover(self) -> list[object]:
            raise RuntimeError(provider_secret)

    target = _RecordingCodexTarget(
        source,
        failure=PlaceholderCreationError(target_secret),
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={
            Provider.CLAUDE: ExplodingClaudeAdapter(),
            Provider.CODEX: source,
        },
        target_adapters={Provider.CODEX: target},
        clock=lambda: 100.0,
    )

    scan = await coordinator.scan_once(Provider.CLAUDE)
    await coordinator.process_jobs_once()
    health = coordinator.health()

    assert scan.failed == 1
    assert "claude_scan_failed" in health["recent_error_codes"]
    assert "codex_target_failed" in health["recent_error_codes"]
    assert provider_secret not in repr(health)
    assert target_secret not in repr(health)
    assert target_secret not in repr(store.retries)


def test_sidebar_expired_lease_stale_worker_cannot_commit(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "expired-before-create"
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "expired-before-create",
            cwd=source_cwd,
        )
        harness.register()

        with harness.client() as client:
            job = _sidebar_call_tool(
                client,
                "session_sidebar_pending",
                {"limit": 1},
            )["jobs"][0]
            harness.now += 301.0
            thread_id = harness.native.create_thread(
                prompt=job["registration_prompt"],
                project_id="session-inbox",
                source_cwd=_canonical_sidebar_path(source_cwd),
            )
            response = _sidebar_rpc(
                client,
                "tools/call",
                {
                    "name": "session_sidebar_commit",
                    "arguments": {
                        "lease_token": job["lease_token"],
                        "codex_thread_id": thread_id,
                    },
                },
                request_id=77,
            )

        durable = harness.store.get_sidebar_job_for_source(source_id)
        assert response["result"]["isError"] is True
        assert durable is not None
        assert durable["state"] == "sidebar_retry"
        assert durable["codex_thread_id"] is None
        assert harness.store.sidebar_job_counts()["sidebar_visible"] == 0
    finally:
        harness.close()


def test_sidebar_empty_control_and_ack_sessions_create_no_jobs(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        harness.seed_source(
            Provider.CLAUDE,
            "empty-source",
            cwd=tmp_path / "empty-source",
            content=None,
        )
        harness.seed_source(
            Provider.HERMES,
            "control-source",
            cwd=tmp_path / "control-source",
            content="/help",
        )
        harness.seed_source(
            Provider.CLAUDE,
            "ack-source",
            cwd=tmp_path / "ack-source",
            content="ok",
        )

        summary = harness.register()
        with harness.client() as client:
            pending = _sidebar_call_tool(
                client,
                "session_sidebar_pending",
                {"limit": 5},
            )

        assert summary.queued == 0
        assert summary.failed == 0
        assert pending == {"jobs": []}
        assert sum(harness.store.sidebar_job_counts().values()) == 0
    finally:
        harness.close()
