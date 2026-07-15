from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
import uuid

import httpx
import pytest
from mcp.shared.version import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.claude_adapter import (
    ClaudeCursor,
    ClaudeParseResult,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderResult,
)
from session_bridge.codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from session_bridge.config import BridgeConfig, SidebarConfig
from session_bridge.context_pack import ContextPackBuilder
from session_bridge.coordinator import (
    ContinueRequest,
    SessionBridgeCoordinator,
)
from session_bridge.mcp_server import create_app
from session_bridge.mirror import MirrorPolicy, enqueue_mirror_job
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionProjection,
    SidebarJobState,
    canonical_session_id,
    decode_bridge_marker,
)
from session_bridge.sidebar import VerifiedSidebarThread
from session_bridge.store import SessionBridgeStore


_MARKER_SECRET = b"synthetic-end-to-end-marker-secret"
_SIDEBAR_TOKEN = "synthetic-sidebar-mcp-token-at-least-32-bytes"


class _SyntheticCodexClient:
    """In-memory request surface; it never starts or contacts Codex."""

    def __init__(self) -> None:
        self.available = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._threads: dict[str, dict[str, Any]] = {}
        self._next_thread = 1
        self._next_turn = 1
        self._completed_turns: list[str] = []
        self.seed_thread(
            "codex-history",
            content="codexhistoryneedle synthetic prompt",
            title="Synthetic Codex history",
            cwd="C:/synthetic/codex",
        )

    def seed_thread(
        self,
        native_id: str,
        *,
        content: str,
        title: str,
        cwd: str | None,
    ) -> None:
        self._threads[native_id] = {
            "id": native_id,
            "title": title,
            "cwd": cwd,
            "createdAt": 100.0,
            "updatedAt": 101.0,
            "archived": False,
            "revision": "revision-1",
            "turns": [self._user_turn(native_id, content)],
        }

    def append_user_turn(self, native_id: str, content: str) -> None:
        thread = self._threads[native_id]
        thread["turns"].append(self._user_turn(native_id, content))
        self._touch(thread)

    def archive_thread(self, native_id: str) -> None:
        thread = self._threads[native_id]
        thread["archived"] = True
        self._touch(thread)

    def delete_thread(self, native_id: str) -> None:
        del self._threads[native_id]

    def has_thread(self, native_id: str) -> bool:
        return native_id in self._threads

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        self.calls.append((method, deepcopy(params)))
        if not self.available:
            raise RuntimeError("synthetic Codex outage")
        if method == "thread/list":
            archived = bool(params["archived"])
            return {
                "data": [
                    {
                        key: deepcopy(thread[key])
                        for key in (
                            "id",
                            "title",
                            "cwd",
                            "createdAt",
                            "updatedAt",
                            "archived",
                            "revision",
                        )
                    }
                    for thread in self._threads.values()
                    if bool(thread["archived"]) is archived
                ]
            }
        if method == "thread/read":
            thread = self._threads[params["threadId"]]
            return {
                "thread": {
                    "id": thread["id"],
                    "turns": deepcopy(thread["turns"]),
                }
            }
        if method == "thread/start":
            native_id = f"codex-target-{self._next_thread}"
            self._next_thread += 1
            self._threads[native_id] = {
                "id": native_id,
                "title": None,
                "cwd": params.get("cwd"),
                "createdAt": 200.0,
                "updatedAt": 200.0,
                "archived": False,
                "revision": "revision-1",
                "turns": [],
            }
            return {"thread": {"id": native_id}}
        if method == "thread/name/set":
            thread = self._threads[params["threadId"]]
            thread["title"] = params["name"]
            self._touch(thread)
            return {}
        if method == "turn/start":
            native_id = params["threadId"]
            text = params["input"][0]["text"]
            turn = self._user_turn(native_id, text, completed=True)
            self._threads[native_id]["turns"].append(turn)
            self._touch(self._threads[native_id])
            self._completed_turns.append(turn["id"])
            return {"turn": {"id": turn["id"], "status": "completed"}}
        raise AssertionError(f"unexpected synthetic Codex method: {method}")

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        del timeout
        if not self._completed_turns:
            return None
        turn_id = self._completed_turns.pop(0)
        return {
            "method": "turn/completed",
            "params": {"turn": {"id": turn_id, "status": "completed"}},
        }

    def _user_turn(
        self,
        native_id: str,
        content: str,
        *,
        completed: bool = True,
    ) -> dict[str, Any]:
        turn_number = self._next_turn
        self._next_turn += 1
        return {
            "id": f"turn-{native_id}-{turn_number}",
            "status": "completed" if completed else "inProgress",
            "createdAt": 100.0 + turn_number,
            "items": [
                {
                    "type": "userMessage",
                    "id": f"item-{native_id}-{turn_number}",
                    "createdAt": 100.0 + turn_number,
                    "content": [{"type": "text", "text": content}],
                }
            ],
        }

    @staticmethod
    def _touch(thread: dict[str, Any]) -> None:
        revision = int(str(thread["revision"]).rsplit("-", 1)[-1]) + 1
        thread["revision"] = f"revision-{revision}"
        thread["updatedAt"] = float(thread["updatedAt"]) + 1.0


class _ToggleClaudeAdapter:
    def __init__(self, delegate: ClaudeSourceAdapter) -> None:
        self.delegate = delegate
        self.available = True

    def discover(self) -> list[Path]:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.discover()

    def parse(self, path: Path) -> Any:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.parse(path)

    def find_native_session(self, native_id: str) -> Path | None:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.find_native_session(native_id)


class _SyntheticHarnessAdapter:
    """Provider boundary fake backed only by immutable synthetic projections."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.available = True
        self.sessions: dict[str, SessionProjection] = {}
        self.create_calls: list[str] = []

    def add(self, projection: SessionProjection) -> None:
        assert projection.provider is self.provider
        self.sessions[projection.native_id] = projection

    def create_placeholder(
        self,
        *,
        title: str,
        source_session_id: str,
        bridge_id: str,
        policy_generation: int,
        cwd: str | None = None,
        native_id: str | None = None,
    ) -> PlaceholderResult:
        del source_session_id, policy_generation
        if not self.available:
            raise RuntimeError(f"synthetic {self.provider.value} outage")
        target_native_id = native_id or f"{self.provider.value}-target-1"
        self.create_calls.append(target_native_id)
        self.add(
            _projection(
                self.provider,
                target_native_id,
                content="synthetic bridge placeholder",
                cwd=cwd,
                title=title,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=bridge_id,
            )
        )
        return PlaceholderResult(
            native_id=target_native_id,
            canonical_session_id=canonical_session_id(
                self.provider,
                target_native_id,
            ),
            used_registration_turn=False,
            verified_at=1_000.0,
        )

    def find_native_session(self, native_id: str) -> Path | None:
        self._require_available()
        return Path(f"{native_id}.jsonl") if native_id in self.sessions else None

    def parse(self, path: Path) -> ClaudeParseResult:
        self._require_available()
        projection = self.sessions[path.stem]
        return ClaudeParseResult(
            projection=projection,
            cursor=ClaudeCursor(offset=1, head_length=1, head_hash="a" * 64),
            rebuild=True,
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
        self._require_available()
        return self.sessions.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        self._require_available()
        return summary

    def projection_has_marker_payload(
        self,
        projection: SessionProjection,
        payload: BridgeMarkerPayload,
    ) -> bool:
        return (
            projection.provider is payload.target_provider
            and projection.origin_bridge_id == payload.bridge_id
        )

    def advance(
        self,
        native_id: str,
        content: str,
        *,
        continuation: bool = False,
    ) -> SessionProjection:
        current = self.sessions[native_id]
        ordinal = len(current.messages)
        updated = replace(
            current,
            last_active=current.last_active + 1.0,
            messages=(
                *current.messages,
                ProjectedMessage(
                    native_event_id=f"{native_id}-event-{ordinal}",
                    ordinal=0,
                    role="user",
                    content=content,
                    timestamp=current.last_active + 1.0,
                ),
            ),
            native_cursor=f"cursor-{native_id}-{ordinal + 1}",
            native_hash=hashlib.sha256(
                f"{native_id}:{ordinal + 1}:{content}".encode()
            ).hexdigest(),
            origin_kind=(
                OriginKind.BRIDGE_CONTINUATION
                if continuation
                else current.origin_kind
            ),
        )
        self.sessions[native_id] = updated
        return updated

    def archive(self, native_id: str) -> SessionProjection:
        updated = replace(self.sessions[native_id], native_status="archived")
        self.sessions[native_id] = updated
        return updated

    def delete(self, native_id: str) -> None:
        del self.sessions[native_id]

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError(f"synthetic {self.provider.value} outage")


def _projection(
    provider: Provider,
    native_id: str,
    *,
    content: str,
    cwd: str | None,
    title: str | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=title or f"Synthetic {provider.value} session",
        cwd=cwd,
        started_at=100.0,
        last_active=101.0,
        messages=(
            ProjectedMessage(
                native_event_id=f"{native_id}-event-0",
                ordinal=0,
                role="user",
                content=content,
                timestamp=101.0,
            ),
        ),
        native_status="active",
        native_cursor=f"cursor-{native_id}-1",
        native_hash=hashlib.sha256(f"{native_id}:1:{content}".encode()).hexdigest(),
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _write_claude_transcript(
    path: Path,
    *,
    native_id: str,
    content: str,
    cwd: str | None = "C:/synthetic/claude",
    title: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    if title is not None:
        records.append(
            {
                "type": "custom-title",
                "sessionId": native_id,
                "customTitle": title,
            }
        )
    records.extend([
        {
            "type": "user",
            "sessionId": native_id,
            "uuid": f"event-{native_id}",
            "timestamp": "2026-07-13T10:00:00Z",
            "cwd": cwd,
            "isSidechain": False,
            "message": {"role": "user", "content": content},
        },
        {
            "type": "assistant",
            "sessionId": native_id,
            "uuid": f"response-{native_id}",
            "timestamp": "2026-07-13T10:00:01Z",
            "cwd": cwd,
            "isSidechain": False,
            "message": {"role": "assistant", "content": "synthetic response"},
        },
    ])
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    return path


def _append_claude_user(
    path: Path,
    *,
    native_id: str,
    content: str,
    cwd: str | None = None,
) -> None:
    record = {
        "type": "user",
        "sessionId": native_id,
        "uuid": f"event-{native_id}-{path.stat().st_size}",
        "timestamp": "2026-07-13T10:00:02Z",
        "cwd": cwd,
        "isSidechain": False,
        "message": {"role": "user", "content": content},
    }
    with path.open("a", encoding="utf-8", newline="") as transcript:
        transcript.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )


class _SyntheticClaudeRunner:
    """Injected process boundary that persists exactly what Claude CLI was asked."""

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.paths: dict[str, Path] = {}

    def __call__(
        self,
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        native_id = args[args.index("--session-id") + 1]
        title = args[args.index("--name") + 1]
        path = self.projects_root / "bridge-targets" / f"{native_id}.jsonl"
        self.paths[native_id] = _write_claude_transcript(
            path,
            native_id=native_id,
            title=title,
            cwd=kwargs.get("cwd"),
            content=args[-1],
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")


@pytest.mark.asyncio
async def test_all_history_imports_claude_codex_and_hermes_into_fts(
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    _write_claude_transcript(
        claude_root / "project" / "claude-history.jsonl",
        native_id="claude-history",
        content="claudehistoryneedle synthetic prompt",
    )
    codex_client = _SyntheticCodexClient()
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        db.create_session("hermes-history", "cli", cwd="C:/synthetic/hermes")
        db.append_message(
            "hermes-history",
            "user",
            "hermeshistoryneedle synthetic prompt",
            timestamp=103.0,
        )
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                ),
                Provider.CODEX: CodexSourceAdapter(
                    codex_client,
                    marker_secret=_MARKER_SECRET,
                ),
            },
        )

        summary = await coordinator.scan_all_history()

        assert (summary.discovered, summary.indexed, summary.failed) == (2, 2, 0)
        catalog = UnifiedCatalog(db, store)
        expected = {
            "claudehistoryneedle": "claude:claude-history",
            "codexhistoryneedle": "codex:codex-history",
            "hermeshistoryneedle": "hermes-history",
        }
        for query, session_id in expected.items():
            result = catalog.search(query=query)
            assert [row["session_id"] for row in result["results"]] == [session_id]
        assert [
            params["archived"]
            for method, params in codex_client.calls
            if method == "thread/list"
        ] == [False, True]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_mid_import_resumes_without_duplicate_catalog_rows(
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    for suffix in ("one", "two"):
        _write_claude_transcript(
            claude_root / "project" / f"restart-{suffix}.jsonl",
            native_id=f"restart-{suffix}",
            content=f"restart{suffix}needle synthetic prompt",
        )
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        first = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=SessionBridgeStore(db, clock=lambda: 1_000.0),
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                )
            },
            scan_batch_size=1,
        )
        first_pass = await first.scan_once(Provider.CLAUDE)
        assert (first_pass.discovered, first_pass.indexed, first_pass.failed) == (
            2,
            1,
            0,
        )
    finally:
        db.close()

    restarted_db = SessionDB(db_path=db_path)
    try:
        restarted_store = SessionBridgeStore(restarted_db, clock=lambda: 1_001.0)
        restarted = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=restarted_store,
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                )
            },
            scan_batch_size=1,
        )

        resumed = await restarted.scan_once(Provider.CLAUDE)
        replay = await restarted.scan_once(Provider.CLAUDE)

        assert (resumed.indexed, resumed.failed) == (1, 0)
        assert (replay.discovered, replay.indexed, replay.failed) == (0, 0, 0)
        rows = UnifiedCatalog(restarted_db, restarted_store).search(
            provider="claude",
            limit=10,
        )["results"]
        assert {row["session_id"] for row in rows} == {
            "claude:restart-one",
            "claude:restart-two",
        }
        assert sum(row["message_count"] for row in rows) == 4
    finally:
        restarted_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", (Provider.CLAUDE, Provider.CODEX))
async def test_provider_outage_is_isolated_and_later_scan_recovers(
    provider: Provider,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    _write_claude_transcript(
        claude_root / "project" / "claude-history.jsonl",
        native_id="claude-history",
        content="claude recovery prompt",
    )
    claude = _ToggleClaudeAdapter(
        ClaudeSourceAdapter(claude_root, marker_secret=_MARKER_SECRET)
    )
    codex_client = _SyntheticCodexClient()
    codex = CodexSourceAdapter(codex_client, marker_secret=_MARKER_SECRET)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        )
        if provider is Provider.CLAUDE:
            claude.available = False
        else:
            codex_client.available = False

        outage = await coordinator.scan_once(provider)
        assert (outage.indexed, outage.failed) == (0, 1)
        assert coordinator.health()["providers"][provider.value][
            "degraded_reason"
        ] is not None

        claude.available = True
        codex_client.available = True
        recovered = await coordinator.scan_once(provider)

        assert (recovered.indexed, recovered.failed) == (1, 0)
        assert coordinator.health()["providers"][provider.value][
            "degraded_reason"
        ] is None
        assert UnifiedCatalog(db, store).search(
            provider=provider.value,
            limit=10,
        )["results"]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_provider", "target_provider"),
    (
        (Provider.CLAUDE, Provider.CODEX),
        (Provider.CODEX, Provider.CLAUDE),
    ),
)
async def test_bidirectional_handoff_hydration_continuation_and_local_lifecycle(
    source_provider: Provider,
    target_provider: Provider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    codex_client = _SyntheticCodexClient()
    source_native_id = f"{source_provider.value}-source"
    source_content = (
        f"{source_provider.value}handoffneedle synthetic work; "
        "mempalace://drawer/synthetic and "
        "gbrain://page/synthetic/session-bridge"
    )
    if source_provider is Provider.CLAUDE:
        _write_claude_transcript(
            claude_root / "source" / f"{source_native_id}.jsonl",
            native_id=source_native_id,
            content=source_content,
            cwd=None,
        )
    else:
        codex_client.seed_thread(
            source_native_id,
            content=source_content,
            title="Synthetic Codex source",
            cwd=None,
        )

    claude_source = ClaudeSourceAdapter(
        claude_root,
        marker_secret=_MARKER_SECRET,
    )
    codex_source = CodexSourceAdapter(
        codex_client,
        marker_secret=_MARKER_SECRET,
    )
    claude_runner = _SyntheticClaudeRunner(claude_root)
    claude_target = ClaudeTargetAdapter(
        claude_source,
        marker_secret=_MARKER_SECRET,
        claude_executable=("synthetic-claude",),
        runner=claude_runner,
        clock=lambda: 1_000.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        discovery_timeout=0.0,
    )
    codex_target = CodexTargetAdapter(
        codex_client,
        source_adapter=codex_source,
        marker_secret=_MARKER_SECRET,
        clock=lambda: 1_000.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        request_timeout=1.0,
        require_registration_turn=True,
        verification_timeout=0.0,
    )
    adapters = {
        Provider.CLAUDE: claude_source,
        Provider.CODEX: codex_source,
    }
    target_adapters = {
        Provider.CLAUDE: claude_target,
        Provider.CODEX: codex_target,
    }

    def read_projection(provider: Provider, native_id: str) -> SessionProjection:
        if provider is Provider.CLAUDE:
            path = claude_source.find_native_session(native_id)
            assert path is not None
            return claude_source.parse(path).projection
        summary = codex_source.find_native_thread(native_id)
        assert summary is not None
        return codex_source.project_thread(summary)

    def append_provider_user(
        provider: Provider,
        native_id: str,
        content: str,
    ) -> None:
        if provider is Provider.CLAUDE:
            path = claude_source.find_native_session(native_id)
            assert path is not None
            _append_claude_user(
                path,
                native_id=native_id,
                content=content,
            )
        else:
            codex_client.append_user_turn(native_id, content)

    source = read_projection(source_provider, source_native_id)
    assert source.messages[0].content == source_content
    assert source.cwd is None
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        source_id = store.upsert_projection(source).session_id
        policy = MirrorPolicy(automatic_creation=False)
        job = enqueue_mirror_job(
            store,
            source_id,
            target_provider,
            policy=policy,
            manual_authorized=True,
        )
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters=adapters,
            target_adapters={target_provider: target_adapters[target_provider]},
            context_builder=ContextPackBuilder(db, store),
            clock=lambda: 1_000.0,
        )

        mirrored = await coordinator.process_jobs_once(job_ids=[job["id"]])

        assert (mirrored.claimed, mirrored.succeeded) == (1, 1)
        link = store.get_bridge_summaries([source_id])[source_id]["bridge_links"][0]
        bridge_id = link["bridge_id"]
        assert link["relation"] == Relation.MIRRORS.value
        target_id = link["to_session_id"]
        target_native_id = target_id.split(":", 1)[1]
        if target_provider is Provider.CLAUDE:
            assert len(claude_runner.calls) == 1
        else:
            assert sum(
                method == "thread/start" for method, _params in codex_client.calls
            ) == 1
        marker_payload = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_id,
            target_provider=target_provider,
            policy_generation=policy.generation,
        )
        target_projection = read_projection(target_provider, target_native_id)
        assert target_projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        assert target_projection.origin_bridge_id == bridge_id
        if target_provider is Provider.CLAUDE:
            assert claude_source.projection_has_marker_payload(
                target_projection,
                marker_payload,
            )
        else:
            assert codex_source.projection_has_marker_payload(
                target_projection,
                marker_payload,
            )
        request = ContinueRequest(
            session_id=source_id,
            bridge_id=bridge_id,
            target_provider=target_provider,
            context_budget_chars=8_000,
        )

        def memory_network_forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("continuation must not contact memory backends")

        monkeypatch.setattr(socket, "create_connection", memory_network_forbidden)
        continued = await coordinator.continue_session(request)

        assert continued.link.relation is Relation.CONTINUES
        assert continued.pack.immutable_at == 1_000.0
        assert "[missing cwd]" in continued.pack.payload
        assert "mempalace://drawer/synthetic" in continued.pack.payload
        assert "gbrain://page/synthetic/session-bridge" in continued.pack.payload
        search = UnifiedCatalog(db, store).search(
            query=f"{source_provider.value}handoffneedle"
        )
        assert [row["session_id"] for row in search["results"]] == [source_id]

        append_provider_user(source_provider, source_native_id, "source advanced alone")
        append_provider_user(
            target_provider,
            target_native_id,
            "target advanced alone",
        )
        replay = await coordinator.continue_session(request)

        assert replay.pack.payload == continued.pack.payload
        assert replay.warnings == ("linked_sessions_diverged",)
        source_read = UnifiedCatalog(db, store).get(source_id)
        assert source_read["session"]["diverged"] is True
        assert "source advanced alone" not in replay.pack.payload
        assert "target advanced alone" not in replay.pack.payload
        target_row = store.get_external_session(target_id)
        assert target_row is not None
        assert target_row["origin_kind"] == OriginKind.BRIDGE_CONTINUATION.value

        codex_native_id = (
            source_native_id
            if source_provider is Provider.CODEX
            else target_native_id
        )
        claude_native_id = (
            source_native_id
            if source_provider is Provider.CLAUDE
            else target_native_id
        )
        codex_id = canonical_session_id(Provider.CODEX, codex_native_id)
        claude_id = canonical_session_id(Provider.CLAUDE, claude_native_id)

        codex_client.archive_thread(codex_native_id)
        archived = await coordinator.refresh_session(codex_id, timeout=1.0)
        archived_row = store.get_external_session(codex_id)
        claude_row = store.get_external_session(claude_id)
        assert archived.stale is False
        assert archived_row is not None
        assert claude_row is not None
        assert archived_row["native_status"] == "archived"
        assert claude_row["native_status"] == "active"

        codex_client.delete_thread(codex_native_id)
        codex_stale = await coordinator.refresh_session(codex_id, timeout=1.0)
        claude_path = claude_source.find_native_session(claude_native_id)
        assert codex_stale.stale is True
        assert claude_path is not None

        claude_path.unlink()
        claude_stale = await coordinator.refresh_session(claude_id, timeout=1.0)
        assert claude_stale.stale is True
        assert claude_stale.warning == "source_refresh_failed_using_durable_snapshot"
        assert codex_stale.warning == "source_refresh_failed_using_durable_snapshot"
        assert codex_client.has_thread(codex_native_id) is False
        assert store.get_external_session(codex_id) is not None
        assert store.get_external_session(claude_id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_mid_job_recovers_exact_claude_target_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    source_adapter = _SyntheticHarnessAdapter(Provider.CODEX)
    target_adapter = _SyntheticHarnessAdapter(Provider.CLAUDE)
    source = _projection(
        Provider.CODEX,
        "restart-job-source",
        content="restart job source",
        cwd="C:/synthetic/restart-job",
    )
    source_adapter.add(source)
    db = SessionDB(db_path=db_path)
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        source_id = store.upsert_projection(source).session_id
        policy = MirrorPolicy(automatic_creation=False)
        job = enqueue_mirror_job(
            store,
            source_id,
            Provider.CLAUDE,
            policy=policy,
            manual_authorized=True,
        )
        claimed = store.claim_due_jobs_with_limits(
            now=1_000.0,
            limit=1,
            policy=policy,
            job_ids=[job["id"]],
        )
        assert len(claimed) == 1
        bridge_id = "bridge:" + hashlib.sha256(
            f"session-bridge:{job['idempotency_key']}".encode()
        ).hexdigest()
        target_native_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes-session-bridge:{job['idempotency_key']}",
            )
        )
        target_adapter.add(
            _projection(
                Provider.CLAUDE,
                target_native_id,
                content="created before synthetic crash",
                cwd="C:/synthetic/restart-job",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=bridge_id,
            )
        )
        store.set_state(
            f"session-bridge:attempt:{job['id']}",
            {
                "version": 1,
                "phase": "provider_call_started",
                "bridge_id": bridge_id,
                "target_provider": Provider.CLAUDE.value,
                "policy_generation": policy.generation,
                "attempts": claimed[0]["attempts"],
                "expected_native_id": target_native_id,
            },
        )
    finally:
        db.close()

    restarted_db = SessionDB(db_path=db_path)
    try:
        restarted_store = SessionBridgeStore(restarted_db, clock=lambda: 1_001.0)
        restarted = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=restarted_store,
            adapters={
                Provider.CODEX: source_adapter,
                Provider.CLAUDE: target_adapter,
            },
            target_adapters={Provider.CLAUDE: target_adapter},
            clock=lambda: 1_001.0,
        )

        recovered = await restarted.reconcile_once()
        replay = await restarted.reconcile_once()

        assert (recovered.examined, recovered.recovered, recovered.failed) == (
            1,
            1,
            0,
        )
        assert replay.recovered == 0
        assert target_adapter.create_calls == []
        assert set(target_adapter.sessions) == {target_native_id}
        assert restarted_store.mirror_job_counts()["succeeded"] == 1
        target_id = canonical_session_id(Provider.CLAUDE, target_native_id)
        assert restarted_store.get_external_session(target_id) is not None
        links = restarted_store.get_bridge_summaries([source_id])[source_id][
            "bridge_links"
        ]
        assert len(links) == 1
        assert links[0]["to_session_id"] == target_id
        assert links[0]["relation"] == Relation.MIRRORS.value
    finally:
        restarted_db.close()


class _SidebarMcpCoordinator:
    """Expose the real coordinator's public sidebar methods without background loops."""

    def __init__(self, delegate: SessionBridgeCoordinator) -> None:
        self.delegate = delegate

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def claim_sidebar_jobs_for_delivery(self, *, limit: int):
        return await self.delegate.claim_sidebar_jobs_for_delivery(limit=limit)

    async def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        ensure_lineage: bool = False,
    ):
        return await self.delegate.commit_sidebar_job(
            lease_token=lease_token,
            codex_thread_id=codex_thread_id,
            ensure_lineage=ensure_lineage,
        )

    def health(self) -> dict[str, Any]:
        return self.delegate.health()


class _FakeNativeCodexTasks:
    """Native Codex project/task surface used by the Task 11 broker tests."""

    def __init__(self, marker_secret: bytes, *, on_create=None) -> None:
        self.marker_secret = marker_secret
        self.on_create = on_create
        self.projects: list[dict[str, str]] = []
        self.threads: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.reconciliation_calls: list[BridgeMarkerPayload] = []
        self.app_server_create_calls: list[dict[str, Any]] = []
        self.available = True
        self.rename_failures_remaining = 0

    def add_project(self, project_id: str, path: Path) -> None:
        self.projects.append(
            {"projectId": project_id, "path": _canonical_sidebar_path(path)}
        )

    def create_thread(
        self,
        *,
        prompt: str,
        project_id: str,
        source_cwd: str,
    ) -> str:
        if not self.available:
            raise RuntimeError("synthetic Desktop offline")
        thread_id = f"native-sidebar-{len(self.threads) + 1}"
        marker = _registration_marker(prompt)
        payload = decode_bridge_marker(marker, self.marker_secret)
        call = {
            "thread_id": thread_id,
            "prompt": prompt,
            "project_id": project_id,
            "source_cwd": source_cwd,
        }
        self.create_calls.append(call)
        self.threads[thread_id] = {
            **call,
            "title": None,
            "marker": marker,
            "payload": payload,
        }
        if self.on_create is not None:
            self.on_create(self.threads[thread_id])
        return thread_id

    def set_thread_title(self, thread_id: str, title: str) -> None:
        self.rename_calls.append((thread_id, title))
        if self.rename_failures_remaining:
            self.rename_failures_remaining -= 1
            raise RuntimeError("synthetic rename failure")
        self.threads[thread_id]["title"] = title

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.reconciliation_calls.append(expected)
        matches = [
            thread
            for thread in self.threads.values()
            if thread["payload"] == expected
        ]
        if not matches:
            return None
        assert len(matches) == 1, "fake native inventory must never hide duplicates"
        return _verified_native_thread(matches[0])

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        thread = self.threads[thread_id]
        assert thread["payload"] == expected
        return _verified_native_thread(thread)


class _CommitDroppingClient:
    """Drop one public commit request before or after MCP server processing."""

    def __init__(self, delegate: TestClient, *, timing: str) -> None:
        if timing not in {"before_processing", "after_processing"}:
            raise ValueError("commit drop timing is invalid")
        self.delegate = delegate
        self.timing = timing
        self.commit_attempts = 0
        self.dropped = False
        self.tool_calls: list[str] = []

    @property
    def headers(self):
        return self.delegate.headers

    def post(self, *args: Any, **kwargs: Any):
        payload = kwargs.get("json")
        tool_name = None
        if isinstance(payload, dict) and payload.get("method") == "tools/call":
            params = payload.get("params")
            if isinstance(params, dict):
                tool_name = params.get("name")
                if isinstance(tool_name, str):
                    self.tool_calls.append(tool_name)
        if tool_name != "session_sidebar_commit" or self.dropped:
            return self.delegate.post(*args, **kwargs)

        self.commit_attempts += 1
        self.dropped = True
        if self.timing == "before_processing":
            raise httpx.ReadError("synthetic commit connection drop")
        self.delegate.post(*args, **kwargs)
        raise httpx.ReadError("synthetic commit response drop")


class _SidebarEndToEndHarness:
    """Drive registration and delivery through the public MCP tools."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        claude_projects_root: Path | None = None,
    ) -> None:
        self.now = time.time()
        self.db = SessionDB(tmp_path / "sidebar-e2e-state.db")
        self.store = SessionBridgeStore(
            self.db,
            clock=lambda: self.now,
            sidebar_jitter=lambda _bound: 0.0,
        )
        self.config = replace(
            BridgeConfig(),
            sidebar=replace(SidebarConfig(), enabled=True, continuous=False),
        )
        self.native = _FakeNativeCodexTasks(
            _MARKER_SECRET,
            on_create=self._index_native_thread,
        )
        adapters = {}
        if claude_projects_root is not None:
            adapters[Provider.CLAUDE] = ClaudeSourceAdapter(
                claude_projects_root,
                marker_secret=_MARKER_SECRET,
            )
        self.coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self.store,
            adapters=adapters,
            target_adapters={},
            sidebar_verifier=self.native,
            clock=lambda: self.now,
        )
        self.catalog = UnifiedCatalog(self.db, self.store)
        self.inbox = tmp_path / ".hermes"
        self.inbox.mkdir()
        self.native.add_project("session-inbox", self.inbox)
        self.app = create_app(
            catalog=self.catalog,
            coordinator=_SidebarMcpCoordinator(self.coordinator),
            store=self.store,
            config=self.config,
            token=_SIDEBAR_TOKEN,
            marker_key=_MARKER_SECRET,
        )

    def close(self) -> None:
        self.db.close()

    def add_project(self, project_id: str, path: Path) -> None:
        self.native.add_project(project_id, path)

    def seed_source(
        self,
        provider: Provider,
        native_id: str,
        *,
        cwd: Path,
        content: str | None = "Build the native sidebar broker",
        git_root: Path | None = None,
    ) -> str:
        cwd.mkdir(parents=True, exist_ok=True)
        if provider is Provider.CLAUDE:
            messages = (
                ()
                if content is None
                else (
                    ProjectedMessage(
                        native_event_id=f"event-{native_id}",
                        ordinal=0,
                        role="user",
                        content=content,
                        timestamp=self.now,
                    ),
                )
            )
            projection = SessionProjection(
                provider=Provider.CLAUDE,
                native_id=native_id,
                title=f"Claude {native_id}",
                cwd=str(cwd),
                started_at=self.now - 10,
                last_active=self.now,
                messages=messages,
                native_path=str(cwd / f"{native_id}.jsonl"),
                native_cursor=f"cursor-{native_id}",
                native_hash=f"hash-{native_id}",
                origin_kind=OriginKind.NATIVE,
            )
            source_id = self.store.upsert_projection(projection).session_id
        elif provider is Provider.HERMES:
            source_id = native_id
            self.db.create_session(
                source_id,
                "cli",
                cwd=str(cwd),
            )
            self.db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET title = ?, started_at = ? WHERE id = ?",
                    (f"Hermes {native_id}", self.now - 10, source_id),
                )
            )
            if content is not None:
                self.db.append_message(
                    source_id,
                    "user",
                    content,
                    timestamp=self.now,
                )
        else:  # pragma: no cover - misuse guard for the shared harness
            raise ValueError("sidebar source must be Claude or Hermes")
        if git_root is not None:
            self.db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET git_repo_root = ? WHERE id = ?",
                    (str(git_root), source_id),
                )
            )
        return source_id

    def _index_native_thread(self, thread: dict[str, Any]) -> None:
        payload = thread["payload"]
        self.store.upsert_projection(
            SessionProjection(
                provider=Provider.CODEX,
                native_id=thread["thread_id"],
                title="Native sidebar placeholder",
                cwd=thread["source_cwd"],
                started_at=self.now,
                last_active=self.now,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"registration-{thread['thread_id']}",
                        ordinal=0,
                        role="user",
                        content=thread["prompt"],
                        timestamp=self.now,
                    ),
                ),
                native_path=f"native://{thread['thread_id']}",
                native_cursor=f"cursor-{thread['thread_id']}",
                native_hash=f"hash-{thread['thread_id']}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=payload.bridge_id,
            )
        )

    def register(self, *, limit: int = 100):
        return asyncio.run(
            self.coordinator.register_sidebar_jobs_once(
                now=self.now,
                limit=limit,
            )
        )

    def scan_claude_history(self):
        return asyncio.run(
            self.coordinator.scan_all_history(Provider.CLAUDE)
        )

    @contextmanager
    def client(self):
        with TestClient(
            self.app,
            base_url="http://127.0.0.1:7484",
            follow_redirects=False,
        ) as client:
            yield client

    def advance_retry(self) -> None:
        self.now += 120.0

    def advance_lease_expiry(self) -> None:
        self.now += 301.0

    def run_worker_once(
        self,
        client: Any,
    ) -> list[dict[str, Any]]:
        jobs = _sidebar_call_tool(
            client,
            "session_sidebar_pending",
            {"limit": 5},
        )["jobs"]
        outcomes: list[dict[str, Any]] = []
        projects = {
            project["path"]: project["projectId"]
            for project in self.native.projects
        }
        for job in jobs:
            project_id = projects.get(_canonical_sidebar_path(job["cwd"]))
            if project_id is None and job["git_root"] is not None:
                project_id = projects.get(
                    _canonical_sidebar_path(job["git_root"])
                )
            if project_id is None:
                project_id = projects[_canonical_sidebar_path(self.inbox)]

            thread_id = job["recovered_thread_id"]
            if thread_id is None:
                try:
                    thread_id = self.native.create_thread(
                        prompt=job["registration_prompt"],
                        project_id=project_id,
                        source_cwd=job["cwd"],
                    )
                except RuntimeError:
                    outcomes.append(
                        _sidebar_call_tool(
                            client,
                            "session_sidebar_fail",
                            {
                                "lease_token": job["lease_token"],
                                "error_code": "desktop_offline",
                            },
                        )
                    )
                    continue

            try:
                self.native.set_thread_title(thread_id, job["title"])
            except RuntimeError:
                outcomes.append(
                    _sidebar_call_tool(
                        client,
                        "session_sidebar_fail",
                        {
                            "lease_token": job["lease_token"],
                            "error_code": "rename_failed",
                        },
                    )
                )
                continue

            try:
                outcomes.append(
                    _sidebar_call_tool(
                        client,
                        "session_sidebar_commit",
                        {
                            "lease_token": job["lease_token"],
                            "codex_thread_id": thread_id,
                        },
                    )
                )
            except httpx.TransportError:
                outcomes.append({"state": "commit_unknown"})
        return outcomes


def _canonical_sidebar_path(value: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _registration_marker(prompt: str) -> str:
    return next(
        line.removeprefix("Signed marker: ")
        for line in prompt.splitlines()
        if line.startswith("Signed marker: ")
    )


def _verified_native_thread(thread: dict[str, Any]) -> VerifiedSidebarThread:
    payload = thread["payload"]
    return VerifiedSidebarThread(
        thread_id=thread["thread_id"],
        source_session_id=payload.source_session_id,
        bridge_id=payload.bridge_id,
    )


def _sidebar_rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
):
    if method != "initialize" and "Mcp-Session-Id" not in client.headers:
        _sidebar_rpc(
            client,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sidebar-e2e", "version": "1"},
            },
            request_id=0,
        )
        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {_SIDEBAR_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": client.headers["Mcp-Session-Id"],
            },
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        assert initialized.status_code == 202, initialized.text
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {_SIDEBAR_TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("Mcp-Session-Id")
    if session_id:
        client.headers["Mcp-Session-Id"] = session_id
    if "text/event-stream" in response.headers.get("content-type", ""):
        lines = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert lines, response.text
        return json.loads(lines[-1])
    return response.json()


def _sidebar_call_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
):
    payload = _sidebar_rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=9,
    )
    assert "error" not in payload, payload
    result = payload["result"]
    if result.get("isError"):
        pytest.fail(result["content"][0]["text"])
    structured = result.get("structuredContent")
    return (
        structured
        if structured is not None
        else json.loads(result["content"][0]["text"])
    )


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
def test_sidebar_meaningful_source_reaches_visible_catalog_through_public_mcp(
    tmp_path: Path,
    provider: Provider,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"{provider.value}-source"
        harness.add_project(f"{provider.value}-project", source_cwd)
        source_id = harness.seed_source(
            provider,
            f"meaningful-{provider.value}",
            cwd=source_cwd,
        )
        summary = harness.register()

        with harness.client() as client:
            outcomes = harness.run_worker_once(client)

        assert summary.by_provider[provider.value] == 1
        assert outcomes == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        catalog_row = harness.store.get_bridge_summaries([source_id])[source_id]
        assert catalog_row["bridge_sidebar_state"] == "visible"
        assert catalog_row["bridge_sidebar_codex_thread_id"] == "native-sidebar-1"
        assert harness.native.threads["native-sidebar-1"]["title"].startswith(
            "[Claude] " if provider is Provider.CLAUDE else "[Hermes] "
        )
    finally:
        harness.close()


def test_sidebar_exact_cwd_saved_project_selection(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "exact-cwd"
        harness.add_project("exact-cwd-project", source_cwd)
        harness.seed_source(Provider.CLAUDE, "exact-cwd", cwd=source_cwd)
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        assert harness.native.create_calls[0]["project_id"] == "exact-cwd-project"
    finally:
        harness.close()


def test_sidebar_exact_git_root_saved_project_selection(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        repo = tmp_path / "saved-git-root"
        source_cwd = repo / "nested" / "worktree"
        source_cwd.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
        )
        harness.add_project("git-root-project", repo)
        harness.seed_source(
            Provider.HERMES,
            "git-root-source",
            cwd=source_cwd,
            git_root=repo,
        )
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        assert harness.native.create_calls[0]["project_id"] == "git-root-project"
    finally:
        harness.close()


def test_sidebar_inbox_fallback_preserves_exact_source_cwd(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "unsaved" / "source-worktree"
        harness.seed_source(Provider.CLAUDE, "inbox-source", cwd=source_cwd)
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        created = harness.native.create_calls[0]
        assert created["project_id"] == "session-inbox"
        assert _canonical_sidebar_path(created["source_cwd"]) == (
            _canonical_sidebar_path(source_cwd)
        )
        assert f"Source cwd: {json.dumps(created['source_cwd'])}" in created["prompt"]
    finally:
        harness.close()


@pytest.mark.parametrize("drop_timing", ["before_processing", "after_processing"])
def test_sidebar_commit_drop_reconciles_exact_marker_without_duplicate(
    tmp_path: Path,
    drop_timing: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "commit-drop"
        harness.add_project("commit-drop-project", source_cwd)
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "commit-drop",
            cwd=source_cwd,
        )
        harness.register()

        with harness.client() as client:
            dropped_client = _CommitDroppingClient(client, timing=drop_timing)
            first = harness.run_worker_once(dropped_client)
            state_after_drop = harness.store.get_sidebar_job_for_source(source_id)
            if drop_timing == "before_processing":
                harness.advance_lease_expiry()
            second = harness.run_worker_once(client)

        assert first == [{"state": "commit_unknown"}]
        assert dropped_client.commit_attempts == 1
        assert dropped_client.dropped is True
        assert "session_sidebar_commit" in dropped_client.tool_calls
        assert "session_sidebar_fail" not in dropped_client.tool_calls
        if drop_timing == "before_processing":
            assert state_after_drop["state"] == SidebarJobState.LEASED.value
            assert second == [
                {
                    "state": "sidebar_visible",
                    "codex_thread_id": "native-sidebar-1",
                }
            ]
            assert (
                harness.native.reconciliation_calls[-1].source_session_id
                == source_id
            )
        else:
            assert state_after_drop["state"] == SidebarJobState.VISIBLE.value
            assert second == []
        assert len(harness.native.create_calls) == 1
        assert harness.store.get_sidebar_job_for_source(source_id)["state"] == (
            SidebarJobState.VISIBLE.value
        )
    finally:
        harness.close()
