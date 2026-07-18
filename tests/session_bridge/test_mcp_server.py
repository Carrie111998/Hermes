from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest
from mcp.shared.version import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.config import BridgeConfig, ClaudeVisibilityConfig, SidebarConfig
from session_bridge.coordinator import (
    ContinuationBlockedError,
    ContinueResult,
    SessionBridgeCoordinator,
    SidebarDeliveryClaim,
)
from session_bridge.mcp_server import (
    EXPECTED_TOOLS,
    _validate_windows_token_acl,
    create_app,
    resolve_bearer_token,
    resolve_marker_key,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarJobState,
    decode_bridge_marker,
)
from session_bridge.sidebar import SidebarCandidate, VerifiedSidebarThread, sidebar_bridge_id
from session_bridge.store import SessionBridgeStore


TOKEN = "bridge-test-token-with-at-least-32-bytes"
MARKER_KEY = b"marker-key-material-with-at-least-32-bytes"


@pytest.fixture
def db(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _restrict_secret_file(path: Path) -> None:
    path.chmod(0o600)
    if os.name != "nt":
        return
    current_sid = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{current_sid}:(F)",
            "*S-1-5-18:(F)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "icacls",
            str(path),
            "/remove:g",
            "*S-1-3-4",
            "*S-1-5-32-544",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _projection(
    provider: Provider,
    native_id: str,
    *,
    title: str,
    cwd: str,
    timestamp: float,
    content: str = "shared bridge keyword",
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=title,
        cwd=cwd,
        started_at=timestamp - 10,
        last_active=timestamp,
        messages=(
            ProjectedMessage(
                native_event_id=f"{native_id}-user",
                ordinal=0,
                role="user",
                content=content,
                timestamp=timestamp,
            ),
        ),
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"sha256:{native_id}",
        parser_version=1,
        git_branch="main",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _seed_external(
    db: SessionDB,
    provider: Provider,
    native_id: str,
    *,
    title: str | None = None,
    cwd: str | None = None,
    repo: str | None = None,
    timestamp: float = 100.0,
    content: str = "shared bridge keyword",
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionBridgeStore:
    store = SessionBridgeStore(db, clock=lambda: 1_000.0)
    store.upsert_projection(
        _projection(
            provider,
            native_id,
            title=title or f"{provider.value} {native_id}",
            cwd=cwd or f"C:/work/{native_id}",
            timestamp=timestamp,
            content=content,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
        )
    )
    resolved_repo = repo or f"C:/repo/{native_id}"
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET git_repo_root = ? WHERE id = ?",
            (resolved_repo, f"{provider.value}:{native_id}"),
        )
    )
    return store


def _seed_linked_pair(db: SessionDB) -> tuple[SessionBridgeStore, str, str, str]:
    bridge_id = "bridge-one"
    store = _seed_external(
        db,
        Provider.CLAUDE,
        "source-one",
        title="Claude source",
        cwd="C:/work/hermes",
        repo="C:/repo/hermes",
        timestamp=200.0,
    )
    _seed_external(
        db,
        Provider.CODEX,
        "target-one",
        title="Codex mirror",
        cwd="C:/work/hermes",
        repo="C:/repo/hermes",
        timestamp=210.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=bridge_id,
    )
    source_id = "claude:source-one"
    target_id = "codex:target-one"
    store.create_link(
        SessionLink(
            id="link-one",
            from_session_id=source_id,
            to_session_id=target_id,
            relation=Relation.MIRRORS,
            bridge_id=bridge_id,
            source_cursor=None,
            source_hash=None,
            created_at=220.0,
        )
    )
    return store, bridge_id, source_id, target_id


def _seed_sidebar_source(db: SessionDB) -> tuple[SessionBridgeStore, SidebarCandidate]:
    store = _seed_external(
        db,
        Provider.CLAUDE,
        "sidebar-source",
        title="Fix private token sk-secret-value",
        cwd="C:/work/sidebar-tree",
        repo="C:/repo/sidebar",
        timestamp=900.0,
        content="Fix the sidebar registration broker",
    )
    candidate = SidebarCandidate(
        source_session_id="claude:sidebar-source",
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id("claude:sidebar-source"),
        title="[Claude] Fix private token [REDACTED]",
        cwd="C:/work/sidebar-tree",
        git_root="C:/repo/sidebar",
        git_branch="main",
        git_head=None,
        worktree_id=None,
        eligible_at=900.0,
    )
    store.enqueue_sidebar_job(candidate)
    return store, candidate


def _seed_claimed_sidebar_pair(
    db: SessionDB,
) -> tuple[SessionBridgeStore, tuple[SidebarDeliveryClaim, ...]]:
    tokens = iter(("pair-lease-one", "pair-lease-two"))
    now = time.time()
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: next(tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidates: list[SidebarCandidate] = []
    for ordinal in (1, 2):
        projection = _projection(
            Provider.CLAUDE,
            f"pair-{ordinal}",
            title=f"Pair {ordinal}",
            cwd=f"C:/work/pair-{ordinal}",
            timestamp=now - 100.0 + ordinal,
            content=f"Fix pair request {ordinal}",
        )
        store.upsert_projection(projection)
        source_id = f"claude:pair-{ordinal}"
        candidate = SidebarCandidate(
            source_session_id=source_id,
            provider=Provider.CLAUDE,
            bridge_id=sidebar_bridge_id(source_id),
            title=f"[Claude] Pair {ordinal}",
            cwd=f"C:/work/pair-{ordinal}",
            git_root=None,
            git_branch="main",
            git_head=None,
            worktree_id=None,
            eligible_at=now - 100.0 + ordinal,
        )
        store.enqueue_sidebar_job(candidate)
        candidates.append(candidate)
    raw_claims = store.claim_sidebar_jobs(now=now, limit=2)
    claims = tuple(
        SidebarDeliveryClaim(
            lease_token=raw["lease_token"],
            source_session_id=raw["source_session_id"],
            bridge_id=raw["bridge_id"],
            reconcile_required=True,
            rename_required=False,
            recovered_thread=None,
        )
        for raw in raw_claims
    )
    return store, claims


class _FakeCoordinator:
    def __init__(
        self,
        *,
        bridge_id: str,
        source_id: str,
        target_id: str,
        exact_cwd: str | None = None,
    ) -> None:
        self.bridge_id = bridge_id
        self.source_id = source_id
        self.target_id = target_id
        self.exact_cwd = exact_cwd
        self.started = 0
        self.stopped = 0
        self.continue_requests: list[Any] = []
        self.sidebar_claims: tuple[SidebarDeliveryClaim, ...] = ()
        self.sidebar_claim_limits: list[int] = []
        self.sidebar_binds: list[tuple[str, str]] = []
        self.sidebar_commits: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def continue_session(self, request: Any) -> ContinueResult:
        self.continue_requests.append(request)
        pack = ContextPack(
            id="pack-stable",
            bridge_id=self.bridge_id,
            source_session_id=self.source_id,
            target_session_id=self.target_id,
            source_cursor="cursor-source-one",
            source_hash="sha256:source-one",
            budget_chars=request.context_budget_chars,
            payload="immutable context payload",
            created_at=230.0,
            immutable_at=231.0,
        )
        link = SessionLink(
            id="link-one",
            from_session_id=self.source_id,
            to_session_id=self.target_id,
            relation=Relation.CONTINUES,
            bridge_id=self.bridge_id,
            source_cursor="cursor-source-one",
            source_hash="sha256:source-one",
            created_at=220.0,
        )
        return ContinueResult(
            pack=pack,
            link=link,
            warnings=("source_refresh_timeout_using_catalog_snapshot",),
            exact_cwd=self.exact_cwd,
        )

    async def claim_sidebar_jobs_for_delivery(
        self, *, limit: int
    ) -> tuple[SidebarDeliveryClaim, ...]:
        self.sidebar_claim_limits.append(limit)
        return self.sidebar_claims[:limit]

    async def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        ensure_lineage: bool = False,
    ) -> dict[str, Any]:
        assert ensure_lineage is True
        self.sidebar_commits.append((lease_token, codex_thread_id))
        return {
            "state": "sidebar_visible",
            "codex_thread_id": codex_thread_id,
        }

    async def bind_sidebar_thread(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
    ) -> dict[str, Any]:
        self.sidebar_binds.append((lease_token, codex_thread_id))
        return {
            "state": "sidebar_leased",
            "codex_thread_id": codex_thread_id,
        }

    def health(self) -> dict[str, Any]:
        return {
            "running": True,
            "watcher_state": "running",
            "recent_error_codes": ["provider_refresh_failed"],
            "token": "must-not-leak",
            "native_path": "C:/private/session.jsonl",
        }


def _create_test_app(
    db: SessionDB,
    store: SessionBridgeStore,
    coordinator: _FakeCoordinator,
):
    return create_app(
        catalog=UnifiedCatalog(db, store),
        coordinator=coordinator,
        store=store,
        config=BridgeConfig(),
        token=TOKEN,
        marker_key=MARKER_KEY,
    )


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
    token: str = TOKEN,
):
    if method != "initialize" and "Mcp-Session-Id" not in client.headers:
        _rpc(
            client,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            request_id=0,
            token=token,
        )
        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
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
            "Authorization": f"Bearer {token}",
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
        data_lines = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert data_lines, response.text
        return json.loads(data_lines[-1])
    return response.json()


def _test_client(app: Any) -> TestClient:
    return TestClient(
        app,
        base_url="http://127.0.0.1:7484",
        follow_redirects=False,
    )


def _call_tool(client: TestClient, name: str, arguments: dict[str, Any]):
    payload = _rpc(
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
    if structured is not None:
        return structured
    return json.loads(result["content"][0]["text"])


def test_catalog_browse_enriches_native_and_external_sessions(db: SessionDB) -> None:
    store = _seed_external(
        db,
        Provider.CLAUDE,
        "claude-one",
        cwd="C:/work/hermes",
        repo="C:/repo/hermes",
        timestamp=200.0,
    )
    db.create_session(
        "hermes-one",
        "tui",
        cwd="C:/work/local",
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET git_repo_root = ?, started_at = ? WHERE id = ?",
            ("C:/repo/local", 100.0, "hermes-one"),
        )
    )
    db.append_message(
        "hermes-one", "user", "local session", timestamp=110.0
    )

    result = UnifiedCatalog(db, store).search(limit=10)

    assert result["mode"] == "browse"
    by_id = {item["session_id"]: item for item in result["results"]}
    assert by_id["claude:claude-one"]["canonical_id"] == "claude:claude-one"
    assert by_id["claude:claude-one"]["native_id"] == "claude-one"
    assert by_id["claude:claude-one"]["provider"] == "claude"
    assert by_id["claude:claude-one"]["origin_kind"] == "native"
    assert by_id["claude:claude-one"]["mirror_state"] == "catalog_only"
    assert by_id["claude:claude-one"]["cwd"] == "C:/work/hermes"
    assert by_id["claude:claude-one"]["repo"] == "C:/repo/hermes"
    assert by_id["claude:claude-one"]["sync_health"] == "healthy"
    assert by_id["hermes-one"]["provider"] == "hermes"
    assert by_id["hermes-one"]["native_id"] == "hermes-one"


def test_catalog_applies_all_filters_in_sql_before_limit(db: SessionDB) -> None:
    store = _seed_external(
        db,
        Provider.CODEX,
        "wanted",
        title="Wanted Codex session",
        cwd="C:/work/wanted",
        repo="C:/repo/wanted",
        timestamp=150.0,
        content="needle common",
    )
    for index in range(12):
        _seed_external(
            db,
            Provider.CLAUDE,
            f"newer-{index}",
            cwd="C:/work/noise",
            repo="C:/repo/noise",
            timestamp=300.0 + index,
            content="needle common",
        )

    result = UnifiedCatalog(db, store).search(
        query="needle",
        provider="codex",
        cwd="C:/work/wanted",
        repo="C:/repo/wanted",
        after=149.0,
        before=151.0,
        limit=1,
    )

    assert result["mode"] == "discover"
    assert [item["session_id"] for item in result["results"]] == ["codex:wanted"]


def test_catalog_relation_and_mirror_state_filters(db: SessionDB) -> None:
    store, _, source_id, target_id = _seed_linked_pair(db)
    catalog = UnifiedCatalog(db, store)

    by_relation = catalog.search(relation="mirrors", limit=10)
    by_state = catalog.search(mirror_state="mirrored", limit=10)

    assert {item["session_id"] for item in by_relation["results"]} == {
        source_id,
        target_id,
    }
    assert {item["session_id"] for item in by_state["results"]} == {
        source_id,
        target_id,
    }
    for item in by_relation["results"]:
        assert item["links"][0]["bridge_id"] == "bridge-one"
        assert item["diverged"] is False


def test_catalog_preserves_read_and_scroll_shapes_and_clamps_bounds(
    db: SessionDB,
) -> None:
    store = _seed_external(
        db,
        Provider.CLAUDE,
        "readable",
        timestamp=100.0,
    )
    anchor = db.append_message(
        "claude:readable", "assistant", "second message", timestamp=101.0
    )
    catalog = UnifiedCatalog(db, store)

    read = catalog.search(session_id="claude:readable", window=999)
    scroll = catalog.search(
        session_id="claude:readable",
        around_message_id=anchor,
        window=999,
    )
    browse = catalog.search(limit=999)

    assert read["mode"] == "read"
    assert read["window"] == 200
    assert read["session"]["provider"] == "claude"
    assert scroll["mode"] == "scroll"
    assert scroll["window"] == 200
    assert any(message.get("anchor") for message in scroll["messages"])
    assert browse["limit"] == 100


def test_catalog_never_exposes_rewound_messages_in_any_shape(db: SessionDB) -> None:
    store = _seed_external(
        db,
        Provider.CLAUDE,
        "rewound",
        timestamp=100.0,
        content="active discovery anchor",
    )
    for index in range(3):
        db.append_message(
            "claude:rewound",
            "assistant",
            f"active filler {index}",
            timestamp=101.0 + index,
        )
    secret_id = db.append_message(
        "claude:rewound",
        "assistant",
        "REWOUND_SECRET_MUST_NOT_LEAK",
        timestamp=110.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?",
            (secret_id,),
        )
    )
    anchor_id = db.get_messages("claude:rewound")[0]["id"]
    catalog = UnifiedCatalog(db, store)

    browse = catalog.search()
    discover = catalog.search(query="active discovery", window=1)
    read = catalog.get("claude:rewound", window=200)
    scroll = catalog.search(
        session_id="claude:rewound",
        around_message_id=anchor_id,
        window=200,
    )

    rendered = json.dumps([browse, discover, read, scroll])
    assert "REWOUND_SECRET_MUST_NOT_LEAK" not in rendered


def test_catalog_window_bounds_decoded_rows_not_only_response_size(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _seed_external(db, Provider.CLAUDE, "large", timestamp=100.0)
    for index in range(500):
        db.append_message(
            "claude:large",
            "assistant",
            f"large transcript row {index}",
            timestamp=101.0 + index,
        )
    decoded = 0
    original_decode = db._decode_content

    def counted_decode(value: Any) -> Any:
        nonlocal decoded
        decoded += 1
        return original_decode(value)

    monkeypatch.setattr(db, "_decode_content", counted_decode)
    catalog = UnifiedCatalog(db, store)

    result = catalog.get("claude:large", window=10)

    assert result["truncated"] is True
    assert len(result["messages"]) == 10
    assert decoded <= 10


def test_health_is_minimal_and_mcp_auth_is_constant_surface(db: SessionDB) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )
    app = _create_test_app(db, store, coordinator)

    with _test_client(app) as client:
        health = client.get("/health")
        missing = client.post("/mcp", json={})
        wrong = client.post(
            "/mcp",
            headers={"Authorization": "Bearer " + ("x" * len(TOKEN))},
            json={},
        )
        double_mounted = client.get(
            "/mcp/mcp",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        initialized = _rpc(
            client,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert double_mounted.status_code == 404
    assert missing.json() == wrong.json() == {"error": "unauthorized"}
    assert initialized["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert coordinator.started == 1
    assert coordinator.stopped == 1


def test_tools_list_exposes_exactly_the_ten_approved_tools(db: SessionDB) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _rpc(client, "tools/list")

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == EXPECTED_TOOLS == {
        "session_search",
        "session_get",
        "session_continue",
        "session_mirror",
        "session_status",
        "session_claude_visibility_status",
        "session_sidebar_pending",
        "session_sidebar_bind",
        "session_sidebar_commit",
        "session_sidebar_fail",
    }


def test_all_eight_tools_are_callable_and_search_filters_are_forwarded(
    db: SessionDB,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        searched = _call_tool(
            client,
            "session_search",
            {
                "query": "shared",
                "provider": "claude",
                "cwd": "C:/work/hermes",
                "repo": "C:/repo/hermes",
                "limit": 500,
            },
        )
        fetched = _call_tool(
            client, "session_get", {"session_id": source_id, "window": 500}
        )
        continued = _call_tool(
            client,
            "session_continue",
            {"session_id": source_id, "context_budget_chars": 500_000},
        )
        mirrored = _call_tool(
            client,
            "session_mirror",
            {
                "session_id": source_id,
                "target_provider": "codex",
                "dry_run": True,
            },
        )
        status = _call_tool(client, "session_status", {})

    assert searched["limit"] == 100
    assert [item["session_id"] for item in searched["results"]] == [source_id]
    assert fetched["mode"] == "read"
    assert fetched["window"] == 200
    assert continued["pack_id"] == "pack-stable"
    assert continued["payload"] == "immutable context payload"
    assert continued["warnings"] == [
        "source_refresh_timeout_using_catalog_snapshot"
    ]
    assert coordinator.continue_requests[0].context_budget_chars == 100_000
    assert mirrored["dry_run"] is True
    assert mirrored["would_enqueue"] is False
    assert mirrored["reason"] == "already_mapped"
    assert status["health"]["recent_error_codes"] == ["provider_refresh_failed"]
    assert "must-not-leak" not in json.dumps(status)
    assert "C:/private/session.jsonl" not in json.dumps(status)


@pytest.mark.parametrize("supplied, expected", [(0, 1), (1, 1), (5, 5), (99, 5)])
def test_session_sidebar_pending_clamps_limit_and_returns_only_broker_fields(
    db: SessionDB, supplied: int, expected: int
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        SidebarDeliveryClaim(
            lease_token="plaintext-opaque-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            reconcile_required=True,
            rename_required=False,
            recovered_thread=None,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(
            client,
            "session_sidebar_pending",
            {"limit": supplied},
        )

    assert coordinator.sidebar_claim_limits == [expected]
    assert set(response) == {"jobs"}
    assert len(response["jobs"]) == 1
    job = response["jobs"][0]
    assert set(job) == {
        "lease_token",
        "registration_prompt",
        "title",
        "provider",
        "cwd",
        "git_root",
        "git_branch",
        "git_head",
        "worktree_id",
        "reconcile_required",
        "rename_required",
        "recovered_thread_id",
    }
    assert job["lease_token"] == "plaintext-opaque-lease"
    assert job["title"].startswith("[Claude] ")
    assert job["provider"] == "claude"
    assert job["cwd"] == "C:/work/sidebar-tree"
    assert job["git_root"] == "C:/repo/sidebar"
    assert job["git_branch"] == "main"
    assert job["git_head"] is None
    assert job["worktree_id"] is None
    assert job["reconcile_required"] is True
    assert job["rename_required"] is False
    assert job["recovered_thread_id"] is None
    prompt = job["registration_prompt"]
    assert "Fix the sidebar registration broker" not in prompt
    assert "C:/claude/sidebar-source.jsonl" not in prompt
    assert "sk-secret-value" not in prompt
    marker = next(
        line.removeprefix("Signed marker: ")
        for line in prompt.splitlines()
        if line.startswith("Signed marker: ")
    )
    assert decode_bridge_marker(marker, MARKER_KEY) == BridgeMarkerPayload(
        candidate.bridge_id,
        candidate.source_session_id,
        Provider.CODEX,
        1,
    )


def test_session_sidebar_pending_returns_durable_reserved_thread_id(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        SidebarDeliveryClaim(
            lease_token="reserved-opaque-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            reconcile_required=True,
            rename_required=True,
            recovered_thread=None,
            reserved_thread_id="44444444-4444-4444-8444-444444444444",
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(
            client,
            "session_sidebar_pending",
            {"limit": 1},
        )

    assert response["jobs"][0]["recovered_thread_id"] == (
        "44444444-4444-4444-8444-444444444444"
    )


def test_session_sidebar_pending_never_reads_transcript_after_enqueue(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    db._execute_write(
        lambda conn: conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?, 'user', ?, ?, 1)",
            [
                (candidate.source_session_id, f"private transcript {index}", 901.0 + index)
                for index in range(600)
            ],
        )
    )
    monkeypatch.setattr(
        db,
        "_decode_content",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("sidebar pending decoded transcript content")
        ),
    )
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        SidebarDeliveryClaim(
            lease_token="bounded-candidate-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            reconcile_required=True,
            rename_required=False,
            recovered_thread=None,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    assert [job["lease_token"] for job in response["jobs"]] == [
        "bounded-candidate-lease"
    ]


def test_session_sidebar_pending_settles_legacy_job_missing_delivery_candidate(
    db: SessionDB,
) -> None:
    token = "legacy-missing-candidate-lease"
    now = time.time()
    store, candidate = _seed_sidebar_source(db)
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM session_bridge_state "
            "WHERE key LIKE 'session-bridge:sidebar-delivery:%'"
        )
    )
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: token,
        sidebar_jitter=lambda _bound: 0.0,
    )
    claims = store.claim_sidebar_jobs(now=now, limit=1)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        SidebarDeliveryClaim(
            lease_token=claims[0]["lease_token"],
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            reconcile_required=True,
            rename_required=False,
            recovered_thread=None,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    assert response == {"jobs": []}
    job = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert job is not None
    assert job["state"] == SidebarJobState.FAILED.value
    assert job["error_code"] == "source_identity_mismatch"


@pytest.mark.parametrize("malformed", [None, True, 1.5, "5", [], {}])
def test_session_sidebar_pending_rejects_malformed_limits_without_leasing(
    db: SessionDB, malformed: object
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {
                "name": "session_sidebar_pending",
                "arguments": {"limit": malformed},
            },
            request_id=41,
        )

    assert payload["result"]["isError"] is True
    assert coordinator.sidebar_claim_limits == []


@pytest.mark.parametrize("failed_index", [0, 1])
def test_session_sidebar_pending_cleans_one_bad_claim_and_returns_other_good_claim(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
    failed_index: int,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = claims
    original_get = store.get_sidebar_candidate_for_delivery
    failed_source = claims[failed_index].source_session_id

    def selective_get(source_session_id: str):
        if source_session_id == failed_source:
            raise ValueError("raw traceback token=must-not-leak")
        return original_get(source_session_id)

    monkeypatch.setattr(store, "get_sidebar_candidate_for_delivery", selective_get)
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 5})

    good_claim = claims[1 - failed_index]
    assert [job["lease_token"] for job in response["jobs"]] == [
        good_claim.lease_token
    ]
    failed = store.get_sidebar_job_for_source(failed_source)
    assert failed is not None
    assert failed["state"] == "sidebar_failed"
    assert failed["error_code"] == "source_identity_mismatch"


def test_session_sidebar_pending_marker_preflight_failure_never_claims(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    monkeypatch.setattr(
        "session_bridge.mcp_server.resolve_marker_key",
        lambda: (_ for _ in ()).throw(RuntimeError("marker secret")),
    )
    app = create_app(
        catalog=UnifiedCatalog(db, store),
        coordinator=coordinator,
        store=store,
        config=BridgeConfig(),
        token=TOKEN,
        marker_key=None,
    )
    with _test_client(app) as client:
        marker_failure = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 5}},
            request_id=43,
        )
    assert marker_failure["result"]["isError"] is True
    assert coordinator.sidebar_claim_limits == []


def test_session_sidebar_pending_claim_failure_does_not_advance_heartbeat(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)

    class FailingClaimCoordinator(_FakeCoordinator):
        async def claim_sidebar_jobs_for_delivery(
            self, *, limit: int
        ) -> tuple[SidebarDeliveryClaim, ...]:
            self.sidebar_claim_limits.append(limit)
            raise RuntimeError("claim unavailable")

    coordinator = FailingClaimCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    store.record_sidebar_broker_heartbeat(now=123.0)
    before = store.get_state("session-bridge:sidebar:broker-heartbeat")

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 5}},
            request_id=45,
        )

    assert response["result"]["isError"] is True
    assert coordinator.sidebar_claim_limits == [5]
    assert store.get_state("session-bridge:sidebar:broker-heartbeat") == before


def test_session_sidebar_pending_cleanup_failure_rolls_back_batch_safely(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = claims
    original_get = store.get_sidebar_candidate_for_delivery
    original_fail = store.fail_sidebar_job

    def fail_second_source(source_session_id: str):
        if source_session_id == claims[1].source_session_id:
            raise RuntimeError("traceback marker=must-not-leak")
        return original_get(source_session_id)

    cleanup_calls = 0

    def flaky_cleanup(**kwargs: Any):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("lease token " + claims[1].lease_token)
        return original_fail(**kwargs)

    monkeypatch.setattr(store, "get_sidebar_candidate_for_delivery", fail_second_source)
    monkeypatch.setattr(store, "fail_sidebar_job", flaky_cleanup)
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 5}},
            request_id=45,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_pending_failed" in serialized
    assert "must-not-leak" not in serialized
    assert all(claim.lease_token not in serialized for claim in claims)
    assert all(
        store.get_sidebar_job_for_source(claim.source_session_id)["state"]
        != "sidebar_leased"
        for claim in claims
    )


def test_session_sidebar_pending_first_cleanup_failure_rolls_back_later_claims(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = claims
    original_get = store.get_sidebar_candidate_for_delivery
    original_fail = store.fail_sidebar_job

    def fail_first_source(source_session_id: str):
        if source_session_id == claims[0].source_session_id:
            raise RuntimeError("first source traceback token=must-not-leak")
        return original_get(source_session_id)

    cleanup_calls = 0

    def fail_first_cleanup(**kwargs: Any):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("first cleanup lease=" + claims[0].lease_token)
        return original_fail(**kwargs)

    monkeypatch.setattr(store, "get_sidebar_candidate_for_delivery", fail_first_source)
    monkeypatch.setattr(store, "fail_sidebar_job", fail_first_cleanup)
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 5}},
            request_id=46,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_pending_failed" in serialized
    assert "must-not-leak" not in serialized
    assert all(claim.lease_token not in serialized for claim in claims)
    assert all(
        store.get_sidebar_job_for_source(claim.source_session_id)["state"]
        != "sidebar_leased"
        for claim in claims
    )


def test_session_sidebar_commit_has_two_argument_schema_and_is_idempotent(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    thread_id = "22222222-2222-4222-8222-222222222222"

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        tools = _rpc(client, "tools/list")["result"]["tools"]
        schema = next(
            tool["inputSchema"]
            for tool in tools
            if tool["name"] == "session_sidebar_commit"
        )
        first = _call_tool(
            client,
            "session_sidebar_commit",
            {
                "lease_token": "plaintext-opaque-lease",
                "codex_thread_id": thread_id,
            },
        )
        replay = _call_tool(
            client,
            "session_sidebar_commit",
            {
                "lease_token": "plaintext-opaque-lease",
                "codex_thread_id": thread_id,
            },
        )

    assert set(schema["properties"]) == {"lease_token", "codex_thread_id"}
    assert set(first) == {"state", "codex_thread_id"}
    assert replay == first == {
        "state": "sidebar_visible",
        "codex_thread_id": thread_id,
    }
    assert coordinator.sidebar_commits == [
        ("plaintext-opaque-lease", thread_id),
        ("plaintext-opaque-lease", thread_id),
    ]


def test_session_sidebar_bind_has_two_argument_schema_and_is_idempotent(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    thread_id = "33333333-3333-4333-8333-333333333333"

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        tools = _rpc(client, "tools/list")["result"]["tools"]
        schema = next(
            tool["inputSchema"]
            for tool in tools
            if tool["name"] == "session_sidebar_bind"
        )
        first = _call_tool(
            client,
            "session_sidebar_bind",
            {
                "lease_token": "plaintext-opaque-lease",
                "codex_thread_id": thread_id,
            },
        )
        replay = _call_tool(
            client,
            "session_sidebar_bind",
            {
                "lease_token": "plaintext-opaque-lease",
                "codex_thread_id": thread_id,
            },
        )

    assert set(schema["properties"]) == {"lease_token", "codex_thread_id"}
    assert replay == first == {
        "state": "sidebar_leased",
        "codex_thread_id": thread_id,
    }
    assert coordinator.sidebar_binds == [
        ("plaintext-opaque-lease", thread_id),
        ("plaintext-opaque-lease", thread_id),
    ]


def test_claude_visibility_status_is_read_only_and_exposes_fixed_health_contract(
    db: SessionDB,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )
    config = BridgeConfig(
        claude_visibility=ClaudeVisibilityConfig(
            enabled=True,
            continuous=True,
            daily_registration_limit=25,
            reserved_cost_per_attempt_usd="0.02",
            emergency_daily_cost_usd="0.50",
        )
    )
    app = create_app(
        catalog=UnifiedCatalog(db, store),
        coordinator=coordinator,
        store=store,
        config=config,
        token=TOKEN,
        marker_key=MARKER_KEY,
    )

    with _test_client(app) as client:
        payload = _call_tool(client, "session_claude_visibility_status", {})
        listed = _rpc(client, "tools/list")

    assert payload["enabled"] is True
    assert payload["continuous"] is True
    assert payload["counts"] == {
        "claude_pending": 0,
        "claude_leased": 0,
        "claude_retry": 0,
        "claude_visible": 0,
        "claude_failed": 0,
    }
    assert payload["cost_gates"] == {
        "daily_registration_limit": 25,
        "attempts_remaining": 25,
        "reserved_cost_per_attempt_usd": "0.02",
        "emergency_daily_cost_usd": "0.50",
        "reserved_cost_remaining_usd": "0.50",
        "registration_limit_reached": False,
        "emergency_cost_limit_reached": False,
    }
    assert payload["degraded_reasons"] == []
    assert payload["last_cycle"] == {"tracked": False, "value": None}
    assert payload["last_empty_cycle"] == {"tracked": False, "value": None}
    assert payload["last_registrar_result"] == {"tracked": False, "value": None}
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert not any(
        fragment in name
        for name in names
        for fragment in ("claude_pending", "claude_claim", "claude_create",
                         "claude_bind", "claude_commit", "claude_fail")
    )


def test_session_sidebar_fail_has_fixed_schema_and_rejects_arbitrary_errors(
    db: SessionDB,
) -> None:
    token = "fixed-error-lease-token"
    now = time.time()
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: token,
        sidebar_jitter=lambda _bound: 0.0,
    )
    db.ensure_session("claude:fail-source", source="cli")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - 100.0, "claude:fail-source"),
        )
    )
    store.enqueue_sidebar_job(
        SidebarCandidate(
            source_session_id="claude:fail-source",
            provider=Provider.CLAUDE,
            bridge_id=sidebar_bridge_id("claude:fail-source"),
            title="[Claude] Fail source",
            cwd="C:/work/fail",
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=now - 100.0,
        )
    )
    store.claim_sidebar_jobs(now=now, limit=1)
    coordinator = _FakeCoordinator(
        bridge_id="unused", source_id="claude:fail-source", target_id="codex:unused"
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        tools = _rpc(client, "tools/list")["result"]["tools"]
        schema = next(
            tool["inputSchema"]
            for tool in tools
            if tool["name"] == "session_sidebar_fail"
        )
        arbitrary = _rpc(
            client,
            "tools/call",
            {
                "name": "session_sidebar_fail",
                "arguments": {
                    "lease_token": token,
                    "error_code": "Traceback: token=super-secret",
                },
            },
            request_id=42,
        )
        failed = _call_tool(
            client,
            "session_sidebar_fail",
            {"lease_token": token, "error_code": "desktop_offline"},
        )

    assert set(schema["properties"]) == {"lease_token", "error_code"}
    assert arbitrary["result"]["isError"] is True
    assert token not in json.dumps(arbitrary)
    assert "super-secret" not in json.dumps(arbitrary)
    assert set(failed) == {"state", "error_code"}
    assert failed == {"state": "sidebar_retry", "error_code": "desktop_offline"}


def test_session_status_exposes_only_sanitized_sidebar_observability(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = ()
    store.record_sidebar_broker_heartbeat(now=100.0)
    coordinator.health = lambda: {
        "running": True,
        "marker": "signed-marker-must-not-leak",
        "lease_token": "lease-must-not-leak",
        "recent_error_codes": ["provider_refresh_failed"],
    }

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        _call_tool(client, "session_sidebar_pending", {"limit": 5})
        status = _call_tool(client, "session_status", {})

    serialized = json.dumps(status)
    assert "signed-marker-must-not-leak" not in serialized
    assert "lease-must-not-leak" not in serialized
    sidebar = status["sidebar"]
    assert set(sidebar) == {
        "eligible_by_provider",
        "counts",
        "oldest_pending_age_seconds",
        "last_heartbeat_at",
        "last_visible_task_id",
        "recent_error_codes",
        "delivery_latency_seconds",
    }
    assert sidebar["eligible_by_provider"] == {"claude": 1, "hermes": 0}
    assert sidebar["counts"]["sidebar_pending"] == 1
    assert sidebar["last_heartbeat_at"] is not None
    assert sidebar["recent_error_codes"] == []
    assert sidebar["delivery_latency_seconds"] == {
        "p50": None,
        "p95": None,
        "p99": None,
    }


@pytest.mark.parametrize(
    ("task_id", "expected"),
    (
        (
            "safe.native-task_1",
            "task:" + hashlib.sha256(b"safe.native-task_1").hexdigest()[:16],
        ),
        ("a", "task:" + hashlib.sha256(b"a").hexdigest()[:16]),
        (
            "sk-proj-secret-value",
            "task:" + hashlib.sha256(b"sk-proj-secret-value").hexdigest()[:16],
        ),
        ("C:/private/native-task", None),
        ("secret\nsecond-line", None),
        ("a" * 513, None),
    ),
)
def test_session_status_task_id_is_validated_then_opaque(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
    expected: str | None,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    original_status = store.sidebar_delivery_status

    def sidebar_status():
        return {**original_status(), "last_visible_task_id": task_id}

    monkeypatch.setattr(store, "sidebar_delivery_status", sidebar_status)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        status = _call_tool(client, "session_status", {})

    assert status["sidebar"]["last_visible_task_id"] == expected
    if len(task_id) > 8:
        assert task_id not in json.dumps(status)


def test_session_status_uses_explicit_schemas_and_never_stringifies_unknowns(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("status sanitizer must never stringify unknown objects")

    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.health = lambda: {
        "running": True,
        "watcher_state": "running",
        "recent_error_codes": ["provider_refresh_failed", Hostile()],
        "last_error": "token=hidden-in-safe-looking-field",
        "nested": {
            "traceback": "Traceback: marker=hidden",
            "exception": Hostile(),
        },
        "providers": {
            "claude": {
                "last_success": 900.0,
                "lag_seconds": 100.0,
                "degraded_reason": None,
                "diagnostic": "lease=hidden",
            }
        },
    }
    catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr(
        catalog,
        "status",
        lambda: {
            "providers": {
                "claude": {
                    "sessions": 1,
                    "degraded": 0,
                    "raw_error": "token=hidden",
                },
                "attacker": {"sessions": 99, "degraded": 0},
            },
            "total_sessions": 1,
            "traceback": Hostile(),
        },
    )
    app = create_app(
        catalog=catalog,
        coordinator=coordinator,
        store=store,
        config=BridgeConfig(),
        token=TOKEN,
        marker_key=MARKER_KEY,
    )

    with _test_client(app) as client:
        status = _call_tool(client, "session_status", {})

    assert set(status) == {"health", "catalog", "sidebar"}
    assert status["health"] == {
        "running": True,
        "providers": {
            "claude": {
                "last_success": 900.0,
                "lag_seconds": 100.0,
                "degraded_reason": None,
            }
        },
        "watcher_state": "running",
        "recent_error_codes": ["provider_refresh_failed"],
    }
    assert status["catalog"] == {
        "providers": {"claude": {"sessions": 1, "degraded": 0}},
        "total_sessions": 1,
    }
    serialized = json.dumps(status)
    assert "hidden" not in serialized
    assert "traceback" not in serialized.casefold()
    assert "last_error" not in serialized


class _McpSidebarVerifier:
    def __init__(self, verified: VerifiedSidebarThread) -> None:
        self.verified = verified
        self.verify_calls: list[str] = []

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        self.verify_calls.append(thread_id)
        assert expected.bridge_id == self.verified.bridge_id
        assert expected.source_session_id == self.verified.source_session_id
        return self.verified

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        return None


def test_session_sidebar_commit_binds_exact_indexed_codex_lineage_once(
    db: SessionDB,
) -> None:
    token = "lineage-opaque-lease-token"
    now = 1_000.0
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: token,
    )
    source = _projection(
        Provider.CLAUDE,
        "lineage-source",
        title="Lineage source",
        cwd="C:/work/lineage",
        timestamp=900.0,
    )
    store.upsert_projection(source)
    source_id = "claude:lineage-source"
    bridge_id = sidebar_bridge_id(source_id)
    candidate = SidebarCandidate(
        source_session_id=source_id,
        provider=Provider.CLAUDE,
        bridge_id=bridge_id,
        title="[Claude] Lineage source",
        cwd="C:/work/lineage",
        git_root=None,
        git_branch="main",
        git_head=None,
        worktree_id=None,
        eligible_at=900.0,
    )
    store.enqueue_sidebar_job(candidate)
    store.claim_sidebar_jobs(now=now, limit=1)
    thread_id = "33333333-3333-4333-8333-333333333333"
    store.upsert_projection(
        _projection(
            Provider.CODEX,
            thread_id,
            title="Native sidebar placeholder",
            cwd="C:/work/lineage",
            timestamp=950.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
    )
    verified = VerifiedSidebarThread(thread_id, source_id, bridge_id)
    verifier = _McpSidebarVerifier(verified)
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(sidebar=SidebarConfig(enabled=True)),
        store=store,
        adapters={},
        sidebar_verifier=verifier,
        clock=lambda: now,
    )

    with _test_client(
        create_app(
            catalog=UnifiedCatalog(db, store),
            coordinator=coordinator,
            store=store,
            config=BridgeConfig(),
            token=TOKEN,
            marker_key=MARKER_KEY,
        )
    ) as client:
        first = _call_tool(
            client,
            "session_sidebar_commit",
            {"lease_token": token, "codex_thread_id": thread_id},
        )
        replay = _call_tool(
            client,
            "session_sidebar_commit",
            {"lease_token": token, "codex_thread_id": thread_id},
        )

    assert replay == first
    assert verifier.verify_calls == [thread_id, thread_id]
    resolved = UnifiedCatalog(db, store).resolve_continuation(
        session_id=source_id,
        bridge_id=None,
        target_provider="codex",
    )
    assert resolved == {
        "source_session_id": source_id,
        "target_session_id": f"codex:{thread_id}",
        "target_provider": "codex",
        "bridge_id": bridge_id,
    }
    links = db._conn.execute(
        "SELECT * FROM session_links WHERE bridge_id = ?", (bridge_id,)
    ).fetchall()
    assert len(links) == 1


def test_native_hermes_sidebar_lineage_resolves_for_codex_continuation(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 1_000.0)
    source_id = "hermes-native-source"
    db.create_session(source_id, "tui", cwd="C:/work/hermes")
    bridge_id = sidebar_bridge_id(source_id)
    thread_id = "44444444-4444-4444-8444-444444444444"
    target_id = f"codex:{thread_id}"
    store.upsert_projection(
        _projection(
            Provider.CODEX,
            thread_id,
            title="Native Hermes sidebar placeholder",
            cwd="C:/work/hermes",
            timestamp=950.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
    )
    store.create_link(
        SessionLink(
            id="native-hermes-sidebar-link",
            from_session_id=source_id,
            to_session_id=target_id,
            relation=Relation.MIRRORS,
            bridge_id=bridge_id,
            source_cursor=None,
            source_hash=None,
            created_at=1_000.0,
        )
    )

    resolved = UnifiedCatalog(db, store).resolve_continuation(
        session_id=source_id,
        bridge_id=None,
        target_provider="codex",
    )

    assert resolved == {
        "source_session_id": source_id,
        "target_session_id": target_id,
        "target_provider": "codex",
        "bridge_id": bridge_id,
    }


def test_session_continue_is_idempotent_for_identical_snapshot_and_budget(
    db: SessionDB,
    tmp_path: Path,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    exact_cwd = os.path.abspath(str(tmp_path / "exact-source"))
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id,
        source_id=source_id,
        target_id=target_id,
        exact_cwd=exact_cwd,
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        first = _call_tool(
            client,
            "session_continue",
            {"bridge_id": bridge_id, "context_budget_chars": 24_000},
        )
        second = _call_tool(
            client,
            "session_continue",
            {"bridge_id": bridge_id, "context_budget_chars": 24_000},
        )

    assert first == second
    assert first["pack_id"] == "pack-stable"
    assert first["payload"] == "immutable context payload"
    assert first["exact_cwd"] == exact_cwd
    assert set(first) == {
        "session_id",
        "target_session_id",
        "target_provider",
        "bridge_id",
        "pack_id",
        "payload",
        "budget_chars",
        "immutable_at",
        "relation",
        "warnings",
        "exact_cwd",
    }
    assert all(request.bridge_id == bridge_id for request in coordinator.continue_requests)


@pytest.mark.parametrize("exact_cwd", [object(), "relative/source", "C:/source/../other"])
def test_session_continue_rejects_noncanonical_fake_exact_cwd(
    db: SessionDB,
    exact_cwd: object,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id,
        source_id=source_id,
        target_id=target_id,
    )
    coordinator.exact_cwd = exact_cwd  # type: ignore[assignment]

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {
                "name": "session_continue",
                "arguments": {"bridge_id": bridge_id},
            },
            request_id=19,
        )

    assert payload["result"]["isError"] is True
    serialized = json.dumps(payload, sort_keys=True)
    assert "relative/source" not in serialized
    assert "C:/source/../other" not in serialized


class _BlockedContinuationCoordinator(_FakeCoordinator):
    async def continue_session(self, request: Any) -> ContinueResult:
        self.continue_requests.append(request)
        try:
            raise RuntimeError(self.raw_cwd)
        except RuntimeError as raw_error:
            raise ContinuationBlockedError(
                "source_identity_mismatch",
                "source_identity_mismatch: exact source worktree snapshot is unavailable",
            ) from raw_error


def test_session_continue_legacy_block_does_not_leak_alternate_cwd(
    db: SessionDB,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    alternate_cwd = "C:/private/alternate-worktree"
    raw_cwd = "C:/private/raw-source-worktree"
    coordinator = _BlockedContinuationCoordinator(
        bridge_id=bridge_id,
        source_id=source_id,
        target_id=target_id,
        exact_cwd=alternate_cwd,
    )
    coordinator.raw_cwd = raw_cwd

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {
                "name": "session_continue",
                "arguments": {"bridge_id": bridge_id},
            },
            request_id=20,
        )

    assert payload["result"]["isError"] is True
    serialized = json.dumps(payload, sort_keys=True)
    assert "source_identity_mismatch" in serialized
    assert alternate_cwd not in serialized
    assert raw_cwd not in serialized


def test_session_mirror_dry_run_is_side_effect_free_and_manual_enqueue_is_durable(
    db: SessionDB,
) -> None:
    store = _seed_external(db, Provider.CLAUDE, "mirror-me", timestamp=300.0)
    coordinator = _FakeCoordinator(
        bridge_id="unused",
        source_id="claude:mirror-me",
        target_id="codex:unused",
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        dry_run = _call_tool(
            client,
            "session_mirror",
            {
                "session_id": "claude:mirror-me",
                "target_provider": "codex",
                "dry_run": True,
            },
        )
        assert store.mirror_job_counts()["queued"] == 0
        queued = _call_tool(
            client,
            "session_mirror",
            {
                "session_id": "claude:mirror-me",
                "target_provider": "codex",
                "dry_run": False,
            },
        )

    assert dry_run["would_enqueue"] is True
    assert queued["state"] == "queued"
    assert queued["authority"] == "manual"
    assert store.mirror_job_counts()["queued"] == 1
    authority = store.get_state(
        f"session-bridge:mirror-authority:{queued['job_id']}"
    )
    assert authority is not None
    assert authority["require_unmapped"] is True


def test_bearer_token_must_exist_and_be_at_least_32_bytes(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing"):
        resolve_bearer_token(environ={}, token_file=tmp_path / "missing")
    with pytest.raises(ValueError, match="32 bytes"):
        resolve_bearer_token(
            environ={"HERMES_SESSION_BRIDGE_TOKEN": "short"},
            token_file=tmp_path / "unused",
        )

    token_file = tmp_path / "bridge.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    _restrict_secret_file(token_file)
    assert resolve_bearer_token(environ={}, token_file=token_file) == TOKEN.encode()


def test_marker_key_is_loaded_from_its_own_restricted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker_key_file = tmp_path / "marker-key"
    marker_key_file.write_bytes(MARKER_KEY)
    _restrict_secret_file(marker_key_file)

    monkeypatch.setenv("HERMES_SESSION_BRIDGE_TOKEN", TOKEN)

    assert resolve_marker_key(marker_key_file=marker_key_file) == MARKER_KEY


def test_marker_key_must_exist_and_be_at_least_32_bytes(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="marker key file is missing"):
        resolve_marker_key(marker_key_file=tmp_path / "missing")

    marker_key_file = tmp_path / "marker-key"
    marker_key_file.write_bytes(b"short")
    _restrict_secret_file(marker_key_file)

    with pytest.raises(ValueError, match="32 bytes"):
        resolve_marker_key(marker_key_file=marker_key_file)


def test_marker_key_uses_a_bounded_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker_key_file = tmp_path / "marker-key"
    marker_key_file.write_bytes(MARKER_KEY)
    _restrict_secret_file(marker_key_file)

    def forbid_path_reopen(_path: Path) -> bytes:
        raise AssertionError("marker key must be consumed from its validated descriptor")

    monkeypatch.setattr(Path, "read_bytes", forbid_path_reopen)

    assert resolve_marker_key(marker_key_file=marker_key_file) == MARKER_KEY


def test_marker_key_rejects_oversized_file(tmp_path: Path) -> None:
    marker_key_file = tmp_path / "marker-key"
    marker_key_file.write_bytes(b"x" * 4097)
    _restrict_secret_file(marker_key_file)

    with pytest.raises(ValueError, match="too large"):
        resolve_marker_key(marker_key_file=marker_key_file)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_marker_key_rejects_group_or_world_readable_file(tmp_path: Path) -> None:
    marker_key_file = tmp_path / "marker-key"
    marker_key_file.write_bytes(MARKER_KEY)
    marker_key_file.chmod(0o644)

    with pytest.raises(PermissionError, match="permissions"):
        resolve_marker_key(marker_key_file=marker_key_file)


def test_windows_token_acl_allows_only_current_user_and_system() -> None:
    current = "S-1-5-21-1000"
    allowed = [
        {"identity": current, "type": "Allow"},
        {"identity": "S-1-5-18", "type": "Allow"},
    ]
    _validate_windows_token_acl(
        current_sid=current,
        owner_sid=current,
        rules=allowed,
    )

    with pytest.raises(PermissionError, match="unauthorized principal"):
        _validate_windows_token_acl(
            current_sid=current,
            owner_sid=current,
            rules=[
                *allowed,
                {"identity": "S-1-5-21-2000", "type": "Allow"},
            ],
        )


def test_app_rejects_short_explicit_tokens(db: SessionDB) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )

    with pytest.raises(ValueError, match="32 bytes"):
        create_app(
            catalog=UnifiedCatalog(db, store),
            coordinator=coordinator,
            store=store,
            config=replace(
                BridgeConfig(),
                service=replace(BridgeConfig().service, host="::1"),
            ),
            token="too-short",
        )
