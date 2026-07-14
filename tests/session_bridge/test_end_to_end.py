from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from typing import Any
import uuid

import pytest

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
from session_bridge.config import BridgeConfig
from session_bridge.context_pack import ContextPackBuilder
from session_bridge.coordinator import (
    ContinueRequest,
    SessionBridgeCoordinator,
)
from session_bridge.mirror import MirrorPolicy, enqueue_mirror_job
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionProjection,
    canonical_session_id,
)
from session_bridge.store import SessionBridgeStore


_MARKER_SECRET = b"synthetic-end-to-end-marker-secret"


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
