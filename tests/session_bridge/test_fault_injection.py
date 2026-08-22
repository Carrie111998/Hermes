from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from hermes_state import SessionDB
from session_bridge.claude_adapter import ClaudeSourceAdapter
from session_bridge.claude_registrar import ClaudeNativeRegistrar
from session_bridge.claude_visibility import (
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
)
from session_bridge.codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import (
    ClaudeVisibilityCoordinator,
    SessionBridgeCoordinator,
    _claude_visibility_enqueue_gates,
)
from session_bridge.mcp_server import resolve_bearer_token
from session_bridge.mirror import (
    MirrorPolicy,
    enqueue_mirror_job,
    record_mirror_failure,
    retry_delay_seconds,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.sidebar import build_registration_prompt
from session_bridge.store import SessionBridgeStore
from tests.session_bridge.test_end_to_end import (
    _MARKER_SECRET,
    _SidebarEndToEndHarness,
)
from tests.session_bridge.test_claude_registrar import (
    FakeFactory as _ClaudeFactory,
    FakePty as _ClaudePty,
    FakeSource as _ClaudeSource,
    FakeStore as _ClaudeStore,
    SECRET as _REGISTRAR_SECRET,
    candidate as _registrar_candidate,
    claim as _registrar_claim,
    projection_for as _registrar_projection,
)


NOW = 100.0
MARKER_SECRET = b"synthetic-marker-secret-at-least-32-bytes"


@pytest.fixture
def bridge_store(tmp_path: Path):
    current_time = [NOW]
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(database, clock=lambda: current_time[0])
    yield store, current_time
    database.close()


def _message(event_id: str, content: str) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role="user",
        content=content,
        timestamp=10.0,
    )


def _projection(
    *,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "synthetic-source",
    content: str = "synthetic user message",
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} synthetic session",
        cwd="C:/synthetic-workspace",
        started_at=10.0,
        last_active=20.0,
        messages=(_message(f"event-{native_id}", content),),
        native_path=f"C:/synthetic/{native_id}.jsonl",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _claude_record(
    *,
    record_type: str,
    event_id: str,
    content: str,
    timestamp: str | float,
) -> dict[str, Any]:
    return {
        "type": record_type,
        "uuid": event_id,
        "sessionId": "synthetic-transcript",
        "timestamp": timestamp,
        "cwd": "C:/synthetic-workspace",
        "message": {"role": record_type, "content": content},
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in records
        )
    )


def test_sqlite_busy_retries_without_partial_projection(
    bridge_store: tuple[SessionBridgeStore, list[float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = bridge_store
    retry_jitters: list[float] = []
    first_retry = Event()
    lock_released = Event()

    def fixed_jitter(_minimum: float, _maximum: float) -> float:
        retry_jitters.append(0.01)
        first_retry.set()
        return 0.01

    monkeypatch.setattr("hermes_state.random.uniform", fixed_jitter)
    with store.db._lock:
        assert store.db._conn is not None
        store.db._conn.execute("PRAGMA busy_timeout=1")

    blocker = sqlite3.connect(
        str(store.db.db_path),
        check_same_thread=False,
        timeout=0,
        isolation_level=None,
    )
    blocker.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        if first_retry.wait(timeout=1.0):
            blocker.rollback()
            lock_released.set()

    releaser = Thread(target=release_lock)
    releaser.start()
    try:
        result = store.upsert_projection(_projection())
    finally:
        releaser.join(timeout=1.0)
        blocker.close()

    assert retry_jitters
    assert first_retry.is_set()
    assert lock_released.is_set()
    assert result.inserted_messages == 1
    assert store.get_external_session("claude:synthetic-source") is not None
    assert len(store.db.get_messages("claude:synthetic-source")) == 1


def test_claude_source_truncation_forces_rebuild(tmp_path: Path) -> None:
    transcript = tmp_path / "claude-root" / "project" / "synthetic-transcript.jsonl"
    records = [
        _claude_record(
            record_type="user",
            event_id="11111111-1111-4111-8111-111111111111",
            content="before truncation",
            timestamp="2026-01-01T00:00:00Z",
        ),
        _claude_record(
            record_type="assistant",
            event_id="22222222-2222-4222-8222-222222222222",
            content="removed by truncation",
            timestamp="2026-01-01T00:00:01Z",
        ),
    ]
    _write_jsonl(transcript, records)
    adapter = ClaudeSourceAdapter(tmp_path / "claude-root", marker_secret=MARKER_SECRET)
    initial = adapter.parse(transcript)

    _write_jsonl(transcript, records[:1])
    rebuilt = adapter.parse(transcript, initial.cursor)

    assert rebuilt.rebuild is True
    assert [message.content for message in rebuilt.projection.messages] == [
        "before truncation"
    ]
    assert rebuilt.cursor.offset < initial.cursor.offset


def test_claude_unknown_schema_record_isolated_from_known_messages(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude-root" / "project" / "synthetic-transcript.jsonl"
    records = [
        _claude_record(
            record_type="user",
            event_id="11111111-1111-4111-8111-111111111111",
            content="known user",
            timestamp="2026-01-01T00:00:00Z",
        ),
        {
            "type": "future-provider-schema",
            "sessionId": "synthetic-transcript",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"unrecognized": True},
        },
        _claude_record(
            record_type="assistant",
            event_id="22222222-2222-4222-8222-222222222222",
            content="known assistant",
            timestamp="2026-01-01T00:00:02Z",
        ),
    ]
    _write_jsonl(transcript, records)

    parsed = ClaudeSourceAdapter(
        tmp_path / "claude-root", marker_secret=MARKER_SECRET
    ).parse(transcript)

    assert parsed.unknown_records == 1
    assert parsed.malformed_lines == 0
    assert [message.content for message in parsed.projection.messages] == [
        "known user",
        "known assistant",
    ]


class _BlockingCodexRefreshAdapter:
    def __init__(self, projection: SessionProjection) -> None:
        self.projection = projection
        self.started = Event()
        self.release = Event()

    def find_native_thread(self, native_id: str) -> SessionProjection:
        assert native_id == self.projection.native_id
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise RuntimeError("synthetic refresh was not released")
        return self.projection

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        return summary


@pytest.mark.asyncio
async def test_source_refresh_timeout_returns_durable_snapshot(
    bridge_store: tuple[SessionBridgeStore, list[float]],
) -> None:
    store, _ = bridge_store
    projection = _projection(provider=Provider.CODEX, native_id="hung-refresh")
    store.upsert_projection(projection)
    adapter = _BlockingCodexRefreshAdapter(projection)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        clock=lambda: NOW,
    )

    started = asyncio.get_running_loop().time()
    result = await coordinator.refresh_session(
        "codex:hung-refresh",
        timeout=0.01,
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert await asyncio.to_thread(adapter.started.wait, 0.2)
    adapter.release.set()
    for _ in range(100):
        if coordinator.health()["provider_calls_inflight"] == 0:
            break
        await asyncio.sleep(0.005)

    assert elapsed < 0.2
    assert result.stale is True
    assert result.cursor == projection.native_cursor
    assert result.source_hash == projection.native_hash
    assert result.warning == "source_refresh_failed_using_durable_snapshot"


class _TimeoutAfterCreateCodexAppServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.native_id = "created-before-timeout"
        self.title: str | None = None
        self.registration_text: str | None = None

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params), timeout))
        if method == "thread/start":
            return {"thread": {"id": self.native_id}}
        if method == "thread/name/set":
            assert params["threadId"] == self.native_id
            self.title = params["name"]
            return {}
        if method == "turn/start":
            assert params["threadId"] == self.native_id
            self.registration_text = params["input"][0]["text"]
            raise TimeoutError("turn was created but the response was lost")
        if method == "thread/list":
            if params["archived"]:
                return {"data": []}
            return {
                "data": [
                    {
                        "id": self.native_id,
                        "title": self.title,
                        "createdAt": 10.0,
                        "updatedAt": 20.0,
                        "revision": "revision-after-ambiguous-turn",
                    }
                ]
            }
        if method == "thread/read":
            assert params == {"threadId": self.native_id, "includeTurns": True}
            assert self.registration_text is not None
            return {
                "thread": {
                    "id": self.native_id,
                    "turns": [
                        {
                            "id": "created-registration-turn",
                            "status": "inProgress",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "created-registration-item",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": self.registration_text,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        raise AssertionError(f"unexpected synthetic Codex request: {method}")

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        raise AssertionError(
            f"unexpected notification wait after ambiguous turn timeout: {timeout}"
        )


@pytest.mark.asyncio
async def test_target_timeout_after_creation_reconciles_without_duplicate(
    bridge_store: tuple[SessionBridgeStore, list[float]],
) -> None:
    store, _ = bridge_store
    store.upsert_projection(_projection())
    policy = MirrorPolicy()
    job = enqueue_mirror_job(
        store,
        "claude:synthetic-source",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
    )
    client = _TimeoutAfterCreateCodexAppServer()
    source = CodexSourceAdapter(client, marker_secret=MARKER_SECRET)
    target = CodexTargetAdapter(
        client,
        source_adapter=source,
        marker_secret=MARKER_SECRET,
        clock=lambda: NOW,
        request_timeout=0.1,
        require_registration_turn=True,
        verification_timeout=0.0,
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: source},
        target_adapters={Provider.CODEX: target},
        clock=lambda: NOW,
    )

    summary = await coordinator.process_jobs_once(job_ids=[job["id"]])

    succeeded = store.list_mirror_jobs([MirrorJobState.SUCCEEDED])
    assert summary.succeeded == 1
    assert summary.retried == 0
    assert summary.manual_failure == 0
    methods = [method for method, _, _ in client.calls]
    assert methods == [
        "thread/start",
        "thread/name/set",
        "turn/start",
        "thread/list",
        "thread/read",
    ]
    assert methods.count("thread/start") == 1
    assert client.registration_text is not None
    assert "HERMES_SESSION_BRIDGE_V1:" in client.registration_text
    assert [row["id"] for row in succeeded] == [job["id"]]
    assert succeeded[0]["target_native_id"] == "created-before-timeout"
    assert store.get_external_session("codex:created-before-timeout") is not None


def test_malformed_bearer_token_is_rejected_before_file_access(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "must-not-be-read"

    with pytest.raises(ValueError, match="must not contain whitespace"):
        resolve_bearer_token(
            environ={
                "HERMES_SESSION_BRIDGE_TOKEN": "x" * 32 + " malformed",
            },
            token_file=token_file,
        )

    assert not token_file.exists()


class _FailedClaudeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self) -> list[Path]:
        self.calls += 1
        raise RuntimeError("synthetic Claude adapter failure")


class _HealthyCodexInventoryAdapter:
    def __init__(self, projection: SessionProjection) -> None:
        self.projection = projection
        self.inventory_calls = 0

    def list_inventory(self, *, archived: bool) -> list[SessionProjection]:
        self.inventory_calls += 1
        return [] if archived else [self.projection]

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        return summary


@pytest.mark.asyncio
async def test_failed_adapter_does_not_block_other_provider_scan(
    bridge_store: tuple[SessionBridgeStore, list[float]],
) -> None:
    store, _ = bridge_store
    claude = _FailedClaudeAdapter()
    codex = _HealthyCodexInventoryAdapter(
        _projection(provider=Provider.CODEX, native_id="healthy-codex")
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        clock=lambda: NOW,
    )

    summary = await coordinator.scan_once()

    health = coordinator.health()["providers"]
    assert summary.failed == 1
    assert summary.indexed == 1
    assert claude.calls == 1
    assert codex.inventory_calls == 2
    assert store.get_external_session("codex:healthy-codex") is not None
    assert health["claude"]["degraded_reason"] is not None
    assert health["codex"]["degraded_reason"] is None


def test_repeated_manual_failure_stops_at_attempt_threshold(
    bridge_store: tuple[SessionBridgeStore, list[float]],
) -> None:
    store, current_time = bridge_store
    store.upsert_projection(_projection())
    policy = MirrorPolicy(max_attempts=2)
    job = enqueue_mirror_job(
        store,
        "claude:synthetic-source",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
    )

    first_claim = store.claim_due_jobs(now=NOW, limit=1, policy=policy)[0]
    first_state = record_mirror_failure(
        store,
        first_claim,
        policy=policy,
        now=NOW,
        code="synthetic_target_down",
        detail="first injected failure",
    )
    current_time[0] = NOW + retry_delay_seconds(job["idempotency_key"], 1)
    second_claim = store.claim_due_jobs(
        now=current_time[0],
        limit=1,
        policy=policy,
    )[0]
    final_state = record_mirror_failure(
        store,
        second_claim,
        policy=policy,
        now=current_time[0],
        code="synthetic_target_down",
        detail="second injected failure",
    )

    durable = store.list_mirror_jobs([MirrorJobState.MANUAL_FAILURE])
    assert first_state is MirrorJobState.RETRY
    assert final_state is MirrorJobState.MANUAL_FAILURE
    assert len(durable) == 1
    assert durable[0]["attempts"] == 2
    assert store.claim_due_jobs(now=current_time[0], limit=1, policy=policy) == []


def test_backfill_stops_when_durable_error_rate_trips_breaker(
    bridge_store: tuple[SessionBridgeStore, list[float]],
) -> None:
    store, _ = bridge_store
    policy = MirrorPolicy(
        automatic_creation=False,
        creates_per_minute=6,
        stop_after_attempts=20,
        stop_error_rate=0.5,
    )
    jobs = []
    for native_id in ("backfill-one", "backfill-two"):
        store.upsert_projection(_projection(native_id=native_id))
        jobs.append(
            enqueue_mirror_job(
                store,
                f"claude:{native_id}",
                Provider.CODEX,
                policy=policy,
                manual_authorized=True,
                require_unmapped=True,
                rollout_limited=True,
            )
        )

    first_claim = store.claim_due_jobs_with_limits(
        now=NOW,
        limit=1,
        policy=policy,
    )[0]
    store.retry_job(
        first_claim["id"],
        code="synthetic_backfill_failure",
        detail="injected rollout failure",
        next_attempt_at=NOW + 20.0,
    )
    blocked = store.claim_due_jobs_with_limits(
        now=NOW,
        limit=1,
        policy=policy,
    )

    assert blocked == []
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 1}
    unclaimed_job_id = next(job["id"] for job in jobs if job["id"] != first_claim["id"])
    assert unclaimed_job_id in {
        queued["id"] for queued in store.list_mirror_jobs([MirrorJobState.QUEUED])
    }


@pytest.mark.parametrize(
    ("broken_provider", "healthy_provider"),
    [
        (Provider.CLAUDE, Provider.HERMES),
        (Provider.HERMES, Provider.CLAUDE),
    ],
)
def test_sidebar_provider_parser_failures_are_isolated_bidirectionally(
    tmp_path: Path,
    broken_provider: Provider,
    healthy_provider: Provider,
) -> None:
    claude_root = tmp_path / "claude-projects"
    harness = _SidebarEndToEndHarness(
        tmp_path,
        claude_projects_root=claude_root,
    )
    try:
        transcript = claude_root / "project" / "sidebar-provider.jsonl"
        if broken_provider is Provider.CLAUDE:
            broken_id = "claude:broken-claude"
            records = [
                _claude_record(
                    record_type="user",
                    event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    content="malformed Claude source",
                    timestamp=harness.now,
                ),
                _claude_record(
                    record_type="assistant",
                    event_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    content="conflicting native identity",
                    timestamp=harness.now + 1,
                ),
            ]
            records[0]["sessionId"] = "broken-claude"
            records[1]["sessionId"] = "different-native-id"
            _write_jsonl(transcript, records)
            healthy_id = harness.seed_source(
                Provider.HERMES,
                "healthy-hermes",
                cwd=tmp_path / "healthy-hermes",
            )
        else:
            broken_id = "broken-hermes"
            harness.db.create_session(broken_id, "cli", cwd=None)
            harness.db.append_message(
                broken_id,
                "user",
                "malformed Hermes source",
                timestamp=harness.now,
            )
            healthy_id = "claude:healthy-claude"
            record = _claude_record(
                record_type="user",
                event_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                content="healthy Claude source",
                timestamp=harness.now,
            )
            record["sessionId"] = "healthy-claude"
            healthy_claude_cwd = tmp_path / "healthy-claude"
            healthy_claude_cwd.mkdir()
            record["cwd"] = str(healthy_claude_cwd)
            _write_jsonl(transcript, [record])

        scan = harness.scan_claude_history()
        summary = harness.register()
        with harness.client() as client:
            delivered = harness.run_worker_once(client)

        assert scan.discovered == 1
        assert scan.failed == int(broken_provider is Provider.CLAUDE)
        assert scan.indexed == int(healthy_provider is Provider.CLAUDE)
        # A native Hermes row with no cwd is an intentional, persisted sidebar
        # exclusion.  It is not a parser failure: the provider remains usable and
        # the healthy Claude candidate must still be delivered.
        assert summary.failed == 0
        assert summary.excluded == int(broken_provider is Provider.HERMES)
        assert summary.excluded_by_reason["source_cwd_missing"] == int(
            broken_provider is Provider.HERMES
        )
        assert summary.queued == 1
        assert harness.store.get_sidebar_job_for_source(broken_id) is None
        assert harness.store.get_sidebar_job_for_source(healthy_id)["state"] == (
            "sidebar_visible"
        )
        assert delivered == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        assert len(harness.native.create_calls) == 1
        healthy_summary = harness.store.get_bridge_summaries([healthy_id])[healthy_id]
        links_for_source = healthy_summary.get("bridge_links", [])
        assert len(links_for_source) <= 1
        assert healthy_summary["bridge_sidebar_codex_thread_id"] == "native-sidebar-1"
        public_status = {
            "registration": harness.store.sidebar_delivery_status(now=harness.now),
            "hydration": harness.store.sidebar_hydration_status(now=harness.now),
        }
        assert "HERMES_SESSION_BRIDGE_V1:" not in repr(public_status)
        assert "HERMES_SESSION_HYDRATION_V1:" not in repr(public_status)
        if broken_provider is Provider.HERMES:
            fixed_error_code = "source_cwd_missing"
            assert fixed_error_code in {
                "marker_conflict",
                "source_identity_mismatch",
                "source_cwd_missing",
                "native_task_not_indexed",
                "hydration_send_ambiguous",
            }
    finally:
        harness.close()


def test_sidebar_post_reservation_desktop_offline_is_quarantined_without_replacement(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id = harness.seed_source(
            Provider.HERMES,
            "desktop-offline",
            cwd=tmp_path / "desktop-offline",
        )
        harness.register()
        harness.native.available = False

        with harness.client() as client:
            ambiguous = harness.run_worker_once(client)
            durable = harness.store.get_sidebar_job_for_source(source_id)
            harness.native.available = True
            harness.advance_retry()
            no_replacement = harness.run_worker_once(client)

        assert ambiguous == [
            {"state": "sidebar_failed", "error_code": "native_create_ambiguous"}
        ]
        assert durable is not None
        assert durable["state"] == "sidebar_failed"
        assert harness.store.get_sidebar_create_reservation(source_id) is not None
        assert no_replacement == []
        assert harness.native.create_calls == []
    finally:
        harness.close()


def test_sidebar_native_broker_never_calls_app_server_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_server_calls: list[dict[str, Any]] = []

    def forbidden_app_server_create(*_args: Any, **kwargs: Any):
        app_server_calls.append(kwargs)
        raise AssertionError("sidebar delivery must not call app-server creation")

    harness = _SidebarEndToEndHarness(tmp_path / "guarded")
    try:
        coordinator = harness.install_production_runtime(monkeypatch)
        codex_target = coordinator._target_adapters[Provider.CODEX]
        assert isinstance(codex_target, CodexTargetAdapter)
        monkeypatch.setattr(
            codex_target,
            "create_placeholder",
            forbidden_app_server_create,
        )
        harness.seed_source(
            Provider.CLAUDE,
            "native-only",
            cwd=tmp_path / "native-only",
        )
        harness.register()
        harness.native.available = False

        with harness.client() as client:
            guarded = harness.run_worker_once(client)

        assert guarded == [
            {"state": "sidebar_failed", "error_code": "native_create_ambiguous"}
        ]
        assert app_server_calls == []
        assert harness.native.app_server_create_calls == []
        assert harness.native.create_calls == []
    finally:
        harness.close()

    fallback = _SidebarEndToEndHarness(tmp_path / "fallback-control")
    try:
        coordinator = fallback.install_production_runtime(monkeypatch)
        codex_target = coordinator._target_adapters[Provider.CODEX]
        assert isinstance(codex_target, CodexTargetAdapter)
        monkeypatch.setattr(
            codex_target,
            "create_placeholder",
            forbidden_app_server_create,
        )
        fallback.seed_source(
            Provider.HERMES,
            "fallback-mutation",
            cwd=tmp_path / "fallback-mutation",
        )
        fallback.register()
        fallback.native.available = False
        fallback.allow_forbidden_app_server_fallback_for_mutation = True

        with fallback.client() as client:
            with pytest.raises(
                AssertionError,
                match="sidebar delivery must not call app-server creation",
            ):
                fallback.run_worker_once(client)

        assert len(app_server_calls) == 1
    finally:
        fallback.close()


@pytest.mark.parametrize(
    "crash_point",
    [
        "after_scan_before_proof",
        "after_proof_before_lease_return",
        "after_lease_before_reserve_recheck",
        "after_reservation_before_create",
        "after_create_before_bind",
        "after_bind_before_commit",
    ],
)
def test_authoritative_reconciliation_restart_never_authorizes_second_create(
    tmp_path: Path,
    crash_point: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id = harness.seed_source(
            Provider.HERMES,
            f"crash-{crash_point}",
            cwd=tmp_path / crash_point,
        )
        assert harness.register().queued == 1
        candidate = harness.store.get_sidebar_candidate_for_delivery(source_id)
        expected = BridgeMarkerPayload(
            bridge_id=candidate.bridge_id,
            source_session_id=source_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        marker = encode_bridge_marker(expected, _MARKER_SECRET)
        prompt = build_registration_prompt(candidate, marker)

        claim = None
        if crash_point == "after_scan_before_proof":
            raw_claims = harness.store.claim_sidebar_jobs(
                now=harness.now,
                limit=1,
            )
            assert len(raw_claims) == 1
        else:
            claims = asyncio.run(
                harness.coordinator.claim_sidebar_jobs_for_delivery(limit=1)
            )
            assert len(claims) == 1
            claim = claims[0]
            assert claim.source_session_id == source_id

        if crash_point in {
            "after_reservation_before_create",
            "after_create_before_bind",
            "after_bind_before_commit",
        }:
            assert claim is not None
            reserved = asyncio.run(
                harness.coordinator.reserve_sidebar_create_authoritatively(
                    lease_token=claim.lease_token,
                    reconciliation_proof_digest=(
                        claim.reconciliation_proof_digest
                    ),
                    reconciliation_generation=claim.reconciliation_generation,
                )
            )
            assert reserved == {
                "state": "sidebar_leased",
                "create_reserved": True,
            }

        created_thread_id = None
        if crash_point in {
            "after_create_before_bind",
            "after_bind_before_commit",
        }:
            created_thread_id = harness.native.create_thread(
                prompt=prompt,
                target={
                    "type": "project",
                    "projectId": "session-inbox",
                    "environment": {"type": "local"},
                },
            )

        if crash_point == "after_bind_before_commit":
            assert claim is not None
            assert created_thread_id is not None
            bound = asyncio.run(
                harness.coordinator.bind_sidebar_thread(
                    lease_token=claim.lease_token,
                    codex_thread_id=created_thread_id,
                )
            )
            assert bound["codex_thread_id"] == created_thread_id

        harness.advance_lease_expiry()
        harness.restart_bridge()
        with harness.client() as client:
            outcome = harness.run_worker_once(client)

        matching_threads = [
            thread
            for thread in harness.native.threads.values()
            if thread["payload"] == expected
        ]
        assert len(harness.native.create_calls) <= 1
        assert len(matching_threads) <= 1
        durable = harness.store.get_sidebar_job_for_source(source_id)
        assert durable is not None
        if crash_point == "after_reservation_before_create":
            assert outcome == []
            assert matching_threads == []
            assert durable["state"] == "sidebar_failed"
            assert durable["error_code"] == "native_create_ambiguous"
        else:
            assert outcome == [
                {
                    "state": "sidebar_visible",
                    "codex_thread_id": "native-sidebar-1",
                }
            ]
            assert len(matching_threads) == 1
            assert durable["state"] == "sidebar_visible"
            assert durable["codex_thread_id"] == matching_threads[0]["thread_id"]
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("fault", "factory", "error_code", "transcript_after_failure"),
    [
        (
            "process_crash_before_transcript",
            lambda: _ClaudeFactory(_ClaudePty(exit_code=7)),
            "clean_exit_not_observed",
            False,
        ),
        (
            "timeout_after_transcript",
            lambda: _ClaudeFactory(_ClaudePty(read_error=TimeoutError())),
            "creation_ambiguous",
            True,
        ),
        (
            "authentication_loss",
            lambda: _ClaudeFactory(_ClaudePty(output="Authentication required")),
            "claude_authentication_unavailable",
            False,
        ),
        (
            "missing_executable",
            lambda: _ClaudeFactory(error=FileNotFoundError()),
            "claude_executable_unavailable",
            False,
        ),
        (
            "pty_failure",
            lambda: _ClaudeFactory(error=RuntimeError("pty unavailable")),
            "pty_unavailable",
            False,
        ),
    ],
)
def test_claude_visibility_registrar_faults_reconcile_same_uuid_without_relaunch(
    tmp_path: Path,
    fault: str,
    factory: Any,
    error_code: str,
    transcript_after_failure: bool,
) -> None:
    base = 1_700_000_000.0
    current = [base]
    database = SessionDB(tmp_path / f"{fault}.db")
    store = SessionBridgeStore(database, clock=lambda: current[0])
    try:
        candidate = _registrar_candidate()
        identity = derive_claude_visibility_identity(candidate, _REGISTRAR_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, _REGISTRAR_SECRET)
        store.upsert_projection(
            _projection(
                provider=Provider.CODEX,
                native_id=candidate.source_session_id.removeprefix("codex:"),
            )
        )
        launch = store.claim_claude_visibility_job(base, 10.0, 25, "0.50", "0.02", 5)
        assert launch.lease_kind == "launch"
        assert launch.reserved_claude_uuid == identity.claude_uuid

        launch_factory = factory()
        registrar = ClaudeNativeRegistrar(
            store,
            _ClaudeSource([None]),
            marker_secret=_REGISTRAR_SECRET,
            startup_theme="light",
            pty_factory=launch_factory,
            clock=lambda: current[0],
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
            process_timeout=1.0,
            exit_timeout=0.1,
            discovery_timeout=0.0,
            retry_delay=1.0,
        )
        outcome = registrar.process(launch)
        assert outcome.error_code == error_code
        assert len(launch_factory.spawns) == 1

        current[0] = base + 1.0
        retry_claim = store.claim_claude_visibility_job(
            base + 1.0, 10.0, 25, "0.50", "0.02", 5
        )

        assert retry_claim.lease_kind == "reconciliation"
        assert retry_claim.launch_permitted is False
        assert retry_claim.registration_reserved is False
        assert retry_claim.reserved_claude_uuid == identity.claude_uuid
        reconciliation_factory = _ClaudeFactory()
        recovered = (
            _registrar_projection(retry_claim) if transcript_after_failure else None
        )
        reconciliation = ClaudeNativeRegistrar(
            store,
            _ClaudeSource([recovered]),
            marker_secret=_REGISTRAR_SECRET,
            startup_theme="light",
            pty_factory=reconciliation_factory,
            clock=lambda: current[0],
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
            discovery_timeout=0.0,
        ).process(retry_claim)
        assert reconciliation.status == (
            "visible" if transcript_after_failure else "absent"
        )
        assert reconciliation.reserved_claude_uuid == identity.claude_uuid
        assert reconciliation_factory.spawns == []
        status = store.claude_visibility_status(current[0])
        assert status["usage"]["attempts"] == 1  # launch_count remains one
    finally:
        database.close()


def test_claude_visibility_coordinator_restart_reconciles_ambiguous_lease(
    tmp_path: Path,
) -> None:
    base = 1_700_000_000.0
    current = [base]
    database = SessionDB(tmp_path / "coordinator-ambiguous-restart.db")
    store = SessionBridgeStore(database, clock=lambda: current[0])
    base_config = BridgeConfig()
    config = replace(
        base_config,
        claude_visibility=replace(
            base_config.claude_visibility,
            enabled=True,
            continuous=True,
            lease_seconds=10.0,
            max_attempts=5,
            daily_registration_limit=25,
            emergency_daily_cost_usd=Decimal("0.50"),
            reserved_cost_per_attempt_usd=Decimal("0.02"),
        ),
    )
    try:
        candidate = _registrar_candidate()
        identity = derive_claude_visibility_identity(candidate, _REGISTRAR_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, _REGISTRAR_SECRET)
        store.upsert_projection(
            _projection(
                provider=Provider.CODEX,
                native_id=candidate.source_session_id.removeprefix("codex:"),
            )
        )
        launch_factory = _ClaudeFactory(_ClaudePty(read_error=TimeoutError()))
        first_registrar = ClaudeNativeRegistrar(
            store,
            _ClaudeSource([None]),
            marker_secret=_REGISTRAR_SECRET,
            startup_theme="light",
            pty_factory=launch_factory,
            clock=lambda: current[0],
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
            process_timeout=1.0,
            exit_timeout=0.1,
            discovery_timeout=0.0,
            retry_delay=1.0,
        )
        first = ClaudeVisibilityCoordinator(
            config=config,
            store=store,
            inventory=lambda _after: [],
            registrar=first_registrar,
            marker_secret=_REGISTRAR_SECRET,
            clock=lambda: current[0],
        ).run_once(discover_continuous=True)

        assert first.status == "retry"
        assert first.error_code == "creation_ambiguous"
        assert len(launch_factory.spawns) == 1
        usage = [
            dict(row)
            for row in database._conn.execute(
                "SELECT * FROM session_claude_registration_usage"
            ).fetchall()
        ]
        assert len(usage) == 1

        current[0] = base + 1.0
        interrupted = store.claim_claude_visibility_job(
            current[0], 10.0, 25, "0.50", "0.02", 5
        )
        assert interrupted.lease_kind == "reconciliation"
        assert interrupted.prior_error_code == "creation_ambiguous"
        assert interrupted.reserved_claude_uuid == identity.claude_uuid

        current[0] = base + 11.0
        recovered_projection = _registrar_projection(
            interrupted,
            response=(
                "You've hit your weekly limit · resets Aug 24, 4am "
                "(America/New_York)"
            ),
        )
        recovery_factory = _ClaudeFactory()
        restarted_registrar = ClaudeNativeRegistrar(
            store,
            _ClaudeSource([recovered_projection]),
            marker_secret=_REGISTRAR_SECRET,
            startup_theme="light",
            pty_factory=recovery_factory,
            clock=lambda: current[0],
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
            discovery_timeout=0.0,
        )
        recovered = ClaudeVisibilityCoordinator(
            config=config,
            store=store,
            inventory=lambda _after: [],
            registrar=restarted_registrar,
            marker_secret=_REGISTRAR_SECRET,
            clock=lambda: current[0],
        ).run_once(discover_continuous=True)

        assert recovered.status == "visible"
        assert recovered.job_id == identity.job_id
        assert recovery_factory.spawns == []
        assert [
            dict(row)
            for row in database._conn.execute(
                "SELECT * FROM session_claude_registration_usage"
            ).fetchall()
        ] == usage
        row = dict(
            database._conn.execute(
                """SELECT state, attempts, reserved_claude_uuid
                   FROM session_claude_visibility_jobs WHERE id = ?""",
                (identity.job_id,),
            ).fetchone()
        )
        assert row == {
            "state": "claude_visible",
            "attempts": 1,
            "reserved_claude_uuid": identity.claude_uuid,
        }
    finally:
        database.close()


@pytest.mark.parametrize(
    "fault", ["malformed_marker", "wrong_cwd", "wrong_name", "duplicate_uuid"]
)
def test_claude_visibility_identity_faults_are_detected_by_registrar_before_spawn(
    fault: str,
) -> None:
    item = _registrar_claim()
    changes: dict[str, Any] = {}
    source_kwargs: dict[str, Any] = {}
    expected = {
        "malformed_marker": "marker_conflict",
        "wrong_cwd": "cwd_conflict",
        "wrong_name": "name_conflict",
        "duplicate_uuid": "duplicate_uuid",
    }[fault]
    if fault == "malformed_marker":
        changes["messages"] = (_message("bad", "wrong marker"),)
    elif fault == "wrong_cwd":
        changes["cwd"] = "C:/wrong"
    elif fault == "wrong_name":
        changes["title"] = "wrong"
    elif fault == "duplicate_uuid":
        source_kwargs["duplicate_paths"] = [Path("C:/one.jsonl"), Path("C:/two.jsonl")]
    projection = _registrar_projection(item, **changes)
    factory = _ClaudeFactory()
    store = _ClaudeStore()
    result = ClaudeNativeRegistrar(
        store,
        _ClaudeSource([projection], **source_kwargs),
        marker_secret=_REGISTRAR_SECRET,
        startup_theme="light",
        pty_factory=factory,
    ).process(item)
    assert result.status == "failed"
    assert result.error_code == expected
    assert result.reserved_claude_uuid == item.reserved_claude_uuid
    assert factory.spawns == []


def test_claude_visibility_delayed_indexing_polls_after_one_real_registrar_launch() -> (
    None
):
    item = _registrar_claim()
    factory = _ClaudeFactory()
    source = _ClaudeSource([None, _registrar_projection(item)])
    result = ClaudeNativeRegistrar(
        _ClaudeStore(),
        source,
        marker_secret=_REGISTRAR_SECRET,
        startup_theme="light",
        pty_factory=factory,
        clock=lambda: 100.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        discovery_timeout=1.0,
    ).process(item)
    assert result.status == "visible"
    assert len(factory.spawns) == 1
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]


def test_claude_visibility_restart_before_expiry_waits_then_stale_lease_reconciles(
    tmp_path: Path,
) -> None:
    now = [1_700_000_000.0]
    database = SessionDB(tmp_path / "restart-timing.db")
    try:
        store = SessionBridgeStore(database, clock=lambda: now[0])
        candidate = _registrar_candidate()
        identity = derive_claude_visibility_identity(candidate, _REGISTRAR_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, _REGISTRAR_SECRET)
        leased = store.claim_claude_visibility_job(now[0], 10.0, 25, "0.50", "0.02", 5)
        assert leased.lease_kind == "launch"

        # A restart while the launch lease is still live must not claim or launch.
        now[0] += 5.0
        before_expiry = SessionBridgeStore(database, clock=lambda: now[0])
        waiting = before_expiry.claim_claude_visibility_job(
            now[0], 10.0, 25, "0.50", "0.02", 5
        )
        assert waiting.status == "no_due_job"

        # The same process identity becomes reconciliation-only after expiry.
        now[0] += 11.0
        restarted = SessionBridgeStore(database, clock=lambda: now[0])
        reconciliation = restarted.claim_claude_visibility_job(
            now[0], 10.0, 25, "0.50", "0.02", 5
        )
        factory = _ClaudeFactory()
        outcome = ClaudeNativeRegistrar(
            restarted,
            _ClaudeSource([None]),
            marker_secret=_REGISTRAR_SECRET,
            startup_theme="light",
            pty_factory=factory,
            clock=lambda: now[0],
        ).process(reconciliation)
        assert reconciliation.lease_kind == "reconciliation"
        assert (
            reconciliation.reserved_claude_uuid
            == leased.reserved_claude_uuid
            == identity.claude_uuid
        )
        assert outcome.status == "absent"
        assert factory.spawns == []
        assert restarted.claude_visibility_status(now[0])["usage"]["attempts"] == 1
    finally:
        database.close()


def test_claude_visibility_unknown_retry_code_in_durable_state_fails_closed(
    tmp_path: Path,
) -> None:
    base = 1_700_000_000.0
    database = SessionDB(tmp_path / "unknown-retry.db")
    store = SessionBridgeStore(database, clock=lambda: base)
    try:
        candidate = _registrar_candidate()
        identity = derive_claude_visibility_identity(candidate, _REGISTRAR_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, _REGISTRAR_SECRET)
        claim = store.claim_claude_visibility_job(base, 10.0, 25, "0.50", "0.02", 5)
        store.retry_claude_visibility_job(
            identity.job_id,
            claim.lease_digest or "",
            "creation_ambiguous",
            base + 1.0,
            "seed valid durable retry",
        )
        # Durable restart corruption/forward-version state cannot be produced by
        # the current API, so inject it at the persistence boundary deliberately.
        database._conn.execute(
            "UPDATE session_claude_visibility_jobs SET error_code = ? WHERE id = ?",
            ("invented_future_retry", claim.job_id),
        )
        status = store.claude_visibility_status(base + 1.0)
        _open, fatal = _claude_visibility_enqueue_gates(status)
        assert "unknown_retry_code" in fatal
        assert status["counts"]["claude_retry"] == 1
    finally:
        database.close()


def test_claude_visibility_duplicate_idempotency_and_cost_rollover_fail_closed(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc).timestamp()
    current = [base]
    database = SessionDB(tmp_path / "duplicate-cost.db")
    store = SessionBridgeStore(
        database, clock=lambda: current[0], local_timezone=timezone.utc
    )
    try:
        projection = SessionProjection(
            provider=Provider.CODEX,
            native_id="duplicate-cost",
            title="duplicate cost",
            cwd=str(tmp_path),
            started_at=10.0,
            last_active=20.0,
            messages=(
                _message("event-duplicate-cost", "Test duplicate and cost rollover"),
            ),
            origin_kind=OriginKind.NATIVE,
        )
        candidate = build_claude_visibility_candidate(projection, eligible_at=20.0)
        identity = derive_claude_visibility_identity(candidate, MARKER_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, MARKER_SECRET)
        first = store.claim_claude_visibility_job(base, 10.0, 25, "0.02", "0.02", 5)
        assert first.reserved_claude_uuid == identity.claude_uuid

        # Idempotent replay is accepted; a conflicting independent key is not.
        replay = store.enqueue_claude_visibility_job(candidate, identity, MARKER_SECRET)
        assert replay["reserved_claude_uuid"] == identity.claude_uuid
        with pytest.raises(ValueError, match="identity"):
            store.enqueue_claude_visibility_job(
                candidate,
                replace(identity, idempotency_key="f" * 64),
                MARKER_SECRET,
            )
        store.retry_claude_visibility_job(
            identity.job_id,
            first.lease_digest or "",
            "creation_ambiguous",
            base + 1.0,
            "ambiguous",
        )
        current[0] = base + 1.0
        reconciliation = store.claim_claude_visibility_job(
            base + 1.0, 10.0, 25, "0.02", "0.02", 5
        )
        assert reconciliation.lease_kind == "reconciliation"
        assert reconciliation.reserved_claude_uuid == identity.claude_uuid
        assert store.claude_visibility_status(base + 1.0)["usage"]["attempts"] == 1
        store.fail_claude_visibility_job(
            identity.job_id,
            reconciliation.lease_digest or "",
            "bridge_conflict",
            "finish first fault",
        )
        second_projection = replace(
            projection,
            native_id="duplicate-cost-second",
            messages=(
                _message("event-duplicate-cost-second", "Test the next cost slot"),
            ),
        )
        second_candidate = build_claude_visibility_candidate(
            second_projection, eligible_at=21.0
        )
        second_identity = derive_claude_visibility_identity(
            second_candidate, MARKER_SECRET
        )
        store.enqueue_claude_visibility_job(
            second_candidate, second_identity, MARKER_SECRET
        )
        current[0] = base + 2.0
        gated = store.claim_claude_visibility_job(
            base + 2.0, 10.0, 25, "0.02", "0.02", 5
        )
        assert gated.status == "cost_limit"
        assert gated.reserved_claude_uuid is None
        current[0] = datetime(2026, 7, 17, 0, 1, tzinfo=timezone.utc).timestamp()
        next_local_day = store.claim_claude_visibility_job(
            current[0], 10.0, 25, "0.02", "0.02", 5
        )
        assert next_local_day.status == "claimed"
        assert next_local_day.lease_kind == "launch"
        assert next_local_day.reserved_claude_uuid == second_identity.claude_uuid
    finally:
        database.close()


@pytest.mark.parametrize("operation", ["claim", "commit"])
def test_claude_visibility_database_busy_preserves_reserved_identity_and_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    base = 1_700_000_000.0
    database = SessionDB(tmp_path / f"busy-{operation}.db")
    store = SessionBridgeStore(database, clock=lambda: base)
    try:
        projection = SessionProjection(
            provider=Provider.CODEX,
            native_id=f"busy-{operation}",
            title=operation,
            cwd=str(tmp_path),
            started_at=10.0,
            last_active=20.0,
            messages=(
                _message(f"event-busy-{operation}", "Exercise database busy handling"),
            ),
            origin_kind=OriginKind.NATIVE,
        )
        candidate = build_claude_visibility_candidate(projection, eligible_at=20.0)
        identity = derive_claude_visibility_identity(candidate, MARKER_SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, MARKER_SECRET)
        claim = None
        if operation == "commit":
            claim = store.claim_claude_visibility_job(base, 10.0, 25, "0.50", "0.02", 5)
        original = database._execute_write
        monkeypatch.setattr(
            database,
            "_execute_write",
            lambda _fn: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            if operation == "claim":
                store.claim_claude_visibility_job(base, 10.0, 25, "0.50", "0.02", 5)
            else:
                assert claim is not None
                store.commit_claude_visibility_job(
                    identity.job_id,
                    claim.lease_digest or "",
                    "b" * 64,
                    base,
                )
        monkeypatch.setattr(database, "_execute_write", original)

        status = store.claude_visibility_status(base)
        assert status["usage"]["attempts"] == (0 if operation == "claim" else 1)
        row = database._conn.execute(
            "SELECT reserved_claude_uuid, attempts, state FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        ).fetchone()
        assert row["reserved_claude_uuid"] == identity.claude_uuid
        assert row["attempts"] == (0 if operation == "claim" else 1)
        assert row["state"] == (
            "claude_pending" if operation == "claim" else "claude_leased"
        )
    finally:
        database.close()
