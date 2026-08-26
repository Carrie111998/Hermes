from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from types import MappingProxyType
from typing import Any, Mapping

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
    SidebarHydrationClaim,
)
from session_bridge.mcp_server import (
    EXPECTED_TOOLS,
    _claude_visibility_status_payload,
    _sidebar_status,
    _status_payload,
    _validate_windows_token_acl,
    create_app,
    resolve_bearer_token,
    resolve_marker_key,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    HydrationMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarJobState,
    decode_bridge_marker,
)
from session_bridge.preview import build_session_preview
from session_bridge.sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    encode_hydration_marker,
    sidebar_bridge_id,
)
from session_bridge.sidebar_reconciliation import SidebarReconciliationState
from session_bridge.store import SessionBridgeStore


TOKEN = "bridge-test-token-with-at-least-32-bytes"
MARKER_KEY = b"marker-key-material-with-at-least-32-bytes"


def _sidebar_delivery_claim(
    *,
    lease_token: str,
    source_session_id: str,
    bridge_id: str,
    recovered_thread_id: str | None = None,
    create_eligible: bool | None = None,
    rename_required: bool = False,
    create_reserved: bool = False,
) -> SidebarDeliveryClaim:
    state = (
        SidebarReconciliationState.RECOVERED
        if recovered_thread_id is not None
        else SidebarReconciliationState.ABSENCE_PROVEN
    )
    return SidebarDeliveryClaim(
        lease_token=lease_token,
        source_session_id=source_session_id,
        bridge_id=bridge_id,
        reconciliation_state=state,
        reconciliation_generation="scan:1",
        reconciliation_proof_digest="3" * 64,
        recovered_thread_id=recovered_thread_id,
        create_eligible=(
            recovered_thread_id is None
            if create_eligible is None
            else create_eligible
        ),
        rename_required=rename_required,
        create_reserved=create_reserved,
    )


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
        title="Fix private token sk-abcdefghijklmnopqrstuvwxyz123456",
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
        _sidebar_delivery_claim(
            lease_token=raw["lease_token"],
            source_session_id=raw["source_session_id"],
            bridge_id=raw["bridge_id"],
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
        self.sidebar_hydration_claims: tuple[SidebarHydrationClaim, ...] = ()
        self.sidebar_claim_limits: list[int] = []
        self.sidebar_hydration_claim_limits: list[int] = []
        self.sidebar_binds: list[tuple[str, str]] = []
        self.sidebar_commits: list[tuple[str, str]] = []
        self.sidebar_reserves: list[tuple[str, str, str]] = []
        self.sidebar_reserve_result: Mapping[str, Any] = {
            "state": "sidebar_leased",
            "create_reserved": True,
        }

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

    async def claim_sidebar_hydration_for_delivery(
        self,
        *,
        limit: int = 1,
    ) -> tuple[SidebarHydrationClaim, ...]:
        self.sidebar_hydration_claim_limits.append(limit)
        return self.sidebar_hydration_claims[:limit]

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

    async def reserve_sidebar_create_authoritatively(
        self,
        *,
        lease_token: str,
        reconciliation_proof_digest: str,
        reconciliation_generation: str,
    ) -> Mapping[str, Any]:
        self.sidebar_reserves.append(
            (
                lease_token,
                reconciliation_proof_digest,
                reconciliation_generation,
            )
        )
        return self.sidebar_reserve_result

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
    *,
    config: BridgeConfig | None = None,
):
    return create_app(
        catalog=UnifiedCatalog(db, store),
        coordinator=coordinator,
        store=store,
        config=BridgeConfig() if config is None else config,
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
    db.append_message("hermes-one", "user", "local session", timestamp=110.0)

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


def test_tools_list_exposes_exactly_the_fifteen_approved_tools(db: SessionDB) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _rpc(client, "tools/list")

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert (
        names
        == EXPECTED_TOOLS
        == {
            "session_search",
            "session_get",
            "session_continue",
            "session_mirror",
            "session_status",
            "session_claude_visibility_status",
            "session_sidebar_pending",
            "session_sidebar_reserve",
            "session_sidebar_bind",
            "session_sidebar_commit",
            "session_sidebar_fail",
            "session_sidebar_hydration_pending",
            "session_sidebar_hydration_reserve",
            "session_sidebar_hydration_commit",
            "session_sidebar_hydration_fail",
        }
    )


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
    assert continued["warnings"] == ["source_refresh_timeout_using_catalog_snapshot"]
    assert coordinator.continue_requests[0].context_budget_chars == 100_000
    assert mirrored["dry_run"] is True
    assert mirrored["would_enqueue"] is False
    assert mirrored["reason"] == "already_mapped"
    assert status["health"]["recent_error_codes"] == ["provider_refresh_failed"]
    assert "must-not-leak" not in json.dumps(status)
    assert "C:/private/session.jsonl" not in json.dumps(status)


def test_session_sidebar_pending_accepts_exactly_one_and_returns_only_broker_fields(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        _sidebar_delivery_claim(
            lease_token="plaintext-opaque-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(
            client,
            "session_sidebar_pending",
            {"limit": 1},
        )

    assert coordinator.sidebar_claim_limits == [1]
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
        "reconciliation_state",
        "reconciliation_generation",
        "reconciliation_proof_digest",
        "rename_required",
        "recovered_thread_id",
        "create_eligible",
        "create_reserved",
    }
    assert job["lease_token"] == "plaintext-opaque-lease"
    assert job["title"].startswith("[Claude] ")
    assert job["provider"] == "claude"
    assert job["cwd"] == "C:/work/sidebar-tree"
    assert job["git_root"] == "C:/repo/sidebar"
    assert job["git_branch"] == "main"
    assert job["git_head"] is None
    assert job["worktree_id"] is None
    assert job["reconciliation_state"] == "absence_proven"
    assert job["reconciliation_generation"] == "scan:1"
    assert job["reconciliation_proof_digest"] == "3" * 64
    assert job["rename_required"] is False
    assert job["recovered_thread_id"] is None
    assert job["create_eligible"] is True
    assert job["create_reserved"] is False
    assert "reconcile_required" not in job
    assert "search_required" not in job
    prompt = job["registration_prompt"]
    assert prompt.startswith("# Imported Claude Code Session")
    assert prompt.index("## Last 5 Messages") < prompt.index("## Bridge Registration")
    assert "Fix the sidebar registration broker" in prompt
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


@pytest.mark.parametrize("malformation", ["blocked", "missing_proof"])
def test_session_sidebar_pending_never_exposes_non_authoritative_claim(
    db: SessionDB,
    malformation: str,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    leased = store.claim_sidebar_jobs(now=time.time(), limit=1)[0]
    claim = _sidebar_delivery_claim(
        lease_token=leased["lease_token"],
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
    )
    if malformation == "blocked":
        claim = replace(
            claim,
            reconciliation_state=SidebarReconciliationState.BLOCKED,
            create_eligible=False,
        )
    else:
        claim = replace(claim, reconciliation_proof_digest="")
    coordinator.sidebar_claims = (claim,)

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    assert response == {"jobs": []}


def test_session_sidebar_pending_uses_snapshot_title_not_candidate_title(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            ("Raw snapshot title", candidate.source_session_id),
        )
    )
    snapshot = store.get_sidebar_preview_source(candidate.source_session_id)
    direct_preview = build_session_preview(
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
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        _sidebar_delivery_claim(
            lease_token="snapshot-title-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    prompt = response["jobs"][0]["registration_prompt"]
    assert "Title: Raw snapshot title" in prompt
    assert "Title: [Claude] Fix private token [REDACTED]" not in prompt
    assert "Title: [Claude] [Claude]" not in prompt
    assert f"Preview digest: {direct_preview.digest}" in prompt


def test_session_sidebar_pending_ignores_disabled_preview_flag_and_stays_readable(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        _sidebar_delivery_claim(
            lease_token="disabled-preview-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
        ),
    )

    with _test_client(
        _create_test_app(
            db,
            store,
            coordinator,
            config=BridgeConfig(sidebar=SidebarConfig(readable_preview_enabled=False)),
        )
    ) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    prompt = response["jobs"][0]["registration_prompt"]
    assert prompt.startswith("# Imported Claude Code Session")
    assert "## Bridge Registration" in prompt
    assert "This is a Hermes Session Bridge placeholder registration." in prompt


@pytest.mark.parametrize("supplied", [0, -1, 2, 5, 99])
def test_session_sidebar_pending_rejects_every_integer_except_one_without_leasing(
    db: SessionDB,
    supplied: int,
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
                "arguments": {"limit": supplied},
            },
            request_id=47,
        )

    assert payload["result"]["isError"] is True
    assert coordinator.sidebar_claim_limits == []


def test_session_sidebar_pending_defaults_to_exactly_one(db: SessionDB) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        assert _call_tool(client, "session_sidebar_pending", {}) == {"jobs": []}

    assert coordinator.sidebar_claim_limits == [1]


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
        _sidebar_delivery_claim(
            lease_token="reserved-opaque-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            rename_required=True,
            recovered_thread_id="44444444-4444-4444-8444-444444444444",
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


def test_session_sidebar_hydration_tools_have_exact_schemas_and_commit_exact_task(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    now = time.time()
    sidebar_lease = store.claim_sidebar_jobs(now=now, limit=1)[0]
    thread_id = "hydration-thread-1"
    store.commit_sidebar_job(
        lease_token=sidebar_lease["lease_token"],
        codex_thread_id=thread_id,
        now=now + 1.0,
    )
    snapshot = store.get_sidebar_preview_source(candidate.source_session_id)
    payload = HydrationMarkerPayload(
        bridge_id=candidate.bridge_id,
        codex_thread_id=thread_id,
        preview_digest="a" * 64,
        preview_version=1,
        source_cursor=snapshot["source_cursor"],
        source_hash=snapshot["source_hash"],
        source_session_id=candidate.source_session_id,
    )
    marker = encode_hydration_marker(payload, MARKER_KEY)
    store.seed_sidebar_hydration_job(
        candidate.source_session_id,
        candidate.bridge_id,
        thread_id,
        snapshot["source_cursor"],
        snapshot["source_hash"],
        1,
        payload.preview_digest,
        marker,
        now + 2.0,
    )
    raw_claim = store.claim_sidebar_hydration_jobs(now=now + 3.0)[0]
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id=f"codex:{thread_id}",
    )
    coordinator.sidebar_hydration_claims = (
        SidebarHydrationClaim(
            lease_token=raw_claim["lease_token"],
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            codex_thread_id=thread_id,
            source_cursor=snapshot["source_cursor"],
            source_hash=snapshot["source_hash"],
            preview_version=1,
            preview_digest=payload.preview_digest,
            hydration_marker=marker,
            hydration_message=(
                "# Imported Claude Code Session\n\n"
                "This is an authenticated in-place Session Bridge hydration.\n"
                "Do not perform project work during this maintenance turn.\n"
                "Do not call session_continue during this maintenance turn.\n"
                f"Hydration marker: {marker}\n"
                "After the marker is recorded, reply only: HYDRATED"
            ),
            cwd=candidate.cwd,
            git_root=candidate.git_root,
            send_reserved=False,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        tools = {
            tool["name"]: tool["inputSchema"]
            for tool in _rpc(client, "tools/list")["result"]["tools"]
        }
        pending = _call_tool(
            client,
            "session_sidebar_hydration_pending",
            {"limit": 1},
        )
        reserved = _call_tool(
            client,
            "session_sidebar_hydration_reserve",
            {"lease_token": raw_claim["lease_token"]},
        )
        committed = _call_tool(
            client,
            "session_sidebar_hydration_commit",
            {
                "lease_token": raw_claim["lease_token"],
                "codex_thread_id": thread_id,
                "hydration_marker": marker,
            },
        )

    assert set(tools["session_sidebar_hydration_pending"]["properties"]) == {"limit"}
    assert set(tools["session_sidebar_hydration_reserve"]["properties"]) == {
        "lease_token"
    }
    assert set(tools["session_sidebar_hydration_commit"]["properties"]) == {
        "lease_token",
        "codex_thread_id",
        "hydration_marker",
    }
    assert set(tools["session_sidebar_hydration_fail"]["properties"]) == {
        "lease_token",
        "error_code",
        "codex_thread_id",
    }
    assert coordinator.sidebar_hydration_claim_limits == [1]
    job = pending["jobs"][0]
    public_fields = set(job)
    assert public_fields == {
        "lease_token",
        "codex_thread_id",
        "hydration_message",
        "hydration_marker",
        "cwd",
        "git_root",
        "send_reserved",
    }
    skill = (
        Path(__file__).resolve().parents[2]
        / "session_bridge"
        / "assets"
        / "session-sidebar-sync"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    required_line = next(
        line for line in skill.splitlines() if "Required lease fields:" in line
    )
    skill_required_fields = set(re.findall(r"`([a-z_]+)`", required_line))
    assert skill_required_fields == public_fields
    assert job["codex_thread_id"] == thread_id
    assert job["hydration_marker"] == marker
    assert job["hydration_message"].startswith("# Imported Claude Code Session")
    assert "Do not perform project work during this maintenance turn." in job["hydration_message"]
    assert "Do not call session_continue during this maintenance turn." in job["hydration_message"]
    assert "Call session_continue(session_id=" not in job["hydration_message"]
    assert job["hydration_message"].endswith(
        "After the marker is recorded, reply only: HYDRATED"
    )
    assert "native_path" not in json.dumps(job)
    assert reserved == {"state": "hydration_leased", "send_reserved": True}
    assert committed == {
        "state": "hydration_visible",
        "codex_thread_id": thread_id,
    }


def test_session_sidebar_reserve_durably_records_pre_create_boundary(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    arguments = {
        "lease_token": "opaque-lease-token",
        "reconciliation_proof_digest": "3" * 64,
        "reconciliation_generation": "scan:1",
    }

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        tools = _rpc(client, "tools/list")["result"]["tools"]
        schema = next(
            tool["inputSchema"]
            for tool in tools
            if tool["name"] == "session_sidebar_reserve"
        )
        first = _call_tool(
            client,
            "session_sidebar_reserve",
            arguments,
        )
        replay = _call_tool(
            client,
            "session_sidebar_reserve",
            arguments,
        )

    assert set(schema["properties"]) == {
        "lease_token",
        "reconciliation_proof_digest",
        "reconciliation_generation",
    }
    assert replay == first == {"state": "sidebar_leased", "create_reserved": True}
    assert coordinator.sidebar_reserves == [
        ("opaque-lease-token", "3" * 64, "scan:1"),
        ("opaque-lease-token", "3" * 64, "scan:1"),
    ]


def test_session_sidebar_reserve_returns_fresh_recovery_without_create(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_reserve_result = {
        "state": "recovered",
        "codex_thread_id": "codex-thread-recovered",
    }

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        result = _call_tool(
            client,
            "session_sidebar_reserve",
            {
                "lease_token": "opaque-lease-token",
                "reconciliation_proof_digest": "3" * 64,
                "reconciliation_generation": "scan:1",
            },
        )

    assert result == {
        "state": "recovered",
        "codex_thread_id": "codex-thread-recovered",
        "create_reserved": False,
    }


@pytest.mark.parametrize(
    "malformation",
    [
        "missing_field",
        "extra_field",
        "wrong_state",
        "false_reserved",
        "malformed_recovered_id",
    ],
)
def test_session_sidebar_reserve_rejects_malformed_coordinator_confirmation(
    db: SessionDB,
    malformation: str,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    malformed_results: dict[str, Mapping[str, Any]] = {
        "missing_field": {"state": "sidebar_leased"},
        "extra_field": {
            "state": "sidebar_leased",
            "create_reserved": True,
            "unexpected": True,
        },
        "wrong_state": {"state": "visible", "create_reserved": True},
        "false_reserved": {"state": "sidebar_leased", "create_reserved": False},
        "malformed_recovered_id": {
            "state": "recovered",
            "codex_thread_id": " bad ",
        },
    }
    coordinator.sidebar_reserve_result = malformed_results[malformation]
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {
                "name": "session_sidebar_reserve",
                "arguments": {
                    "lease_token": "opaque-lease-token",
                    "reconciliation_proof_digest": "3" * 64,
                    "reconciliation_generation": "scan:1",
                },
            },
            request_id=44,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_reserve_failed" in serialized
    assert "create_reserved" not in serialized


def test_session_sidebar_pending_exposes_durable_create_boundary(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        _sidebar_delivery_claim(
            lease_token="create-reserved-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            create_reserved=True,
        ),
    )

    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {})

    assert response["jobs"][0]["create_reserved"] is True


def test_session_sidebar_pending_returns_only_bounded_redacted_readable_preview(
    db: SessionDB,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    db._execute_write(
        lambda conn: conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, 1)",
            [
                (
                    candidate.source_session_id,
                    "assistant",
                    f"assistant message {index}",
                    901.0 + index,
                )
                for index in range(6)
            ]
            + [
                (
                    candidate.source_session_id,
                    "tool",
                    "provider tool output must stay private",
                    907.0,
                ),
                (
                    candidate.source_session_id,
                    "user",
                    "Use sk-abcdefghijklmnopqrstuvwxyz123456 for the pending work",
                    908.0,
                ),
            ],
        )
    )
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (
        _sidebar_delivery_claim(
            lease_token="bounded-candidate-lease",
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
        ),
    )

    with _test_client(
        _create_test_app(
            db,
            store,
            coordinator,
            config=BridgeConfig(
                sidebar=SidebarConfig(readable_preview_enabled=True)
            ),
        )
    ) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    assert [job["lease_token"] for job in response["jobs"]] == [
        "bounded-candidate-lease"
    ]
    prompt = response["jobs"][0]["registration_prompt"]
    assert "# Imported Claude Code Session" in prompt
    assert "assistant message 1" not in prompt
    for index in range(2, 6):
        assert f"assistant message {index}" in prompt
    assert "provider tool output must stay private" not in prompt
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in prompt
    assert "[REDACTED]" in prompt
    assert "C:/claude/sidebar-source.jsonl" not in prompt


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
        _sidebar_delivery_claim(
            lease_token=claims[0]["lease_token"],
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
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
def test_session_sidebar_pending_settles_one_bad_claim_and_returns_no_job(
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
    coordinator.sidebar_claims = (claims[failed_index],)
    original_get = store.get_sidebar_candidate_for_delivery
    failed_source = claims[failed_index].source_session_id

    def selective_get(source_session_id: str):
        if source_session_id == failed_source:
            raise ValueError("raw traceback token=must-not-leak")
        return original_get(source_session_id)

    monkeypatch.setattr(store, "get_sidebar_candidate_for_delivery", selective_get)
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        response = _call_tool(client, "session_sidebar_pending", {"limit": 1})

    assert response == {"jobs": []}
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
            {"name": "session_sidebar_pending", "arguments": {"limit": 1}},
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
            {"name": "session_sidebar_pending", "arguments": {"limit": 1}},
            request_id=45,
        )

    assert response["result"]["isError"] is True
    assert coordinator.sidebar_claim_limits == [1]
    assert store.get_state("session-bridge:sidebar:broker-heartbeat") == before


def test_session_sidebar_pending_cleanup_failure_rolls_back_single_claim_safely(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = (claims[0],)
    original_get = store.get_sidebar_candidate_for_delivery
    original_fail = store.fail_sidebar_job

    def fail_claim_source(source_session_id: str):
        if source_session_id == claims[0].source_session_id:
            raise RuntimeError("traceback marker=must-not-leak")
        return original_get(source_session_id)

    cleanup_calls = 0

    def flaky_cleanup(**kwargs: Any):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("lease token " + claims[0].lease_token)
        return original_fail(**kwargs)

    monkeypatch.setattr(store, "get_sidebar_candidate_for_delivery", fail_claim_source)
    monkeypatch.setattr(store, "fail_sidebar_job", flaky_cleanup)
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 1}},
            request_id=45,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_pending_failed" in serialized
    assert "must-not-leak" not in serialized
    assert claims[0].lease_token not in serialized
    assert cleanup_calls == 2
    assert (
        store.get_sidebar_job_for_source(claims[0].source_session_id)["state"]
        != "sidebar_leased"
    )


def test_session_sidebar_pending_rejects_overlimit_coordinator_and_rolls_back_leases(
    db: SessionDB,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)

    class OverlimitCoordinator(_FakeCoordinator):
        async def claim_sidebar_jobs_for_delivery(
            self,
            *,
            limit: int,
        ) -> tuple[SidebarDeliveryClaim, ...]:
            self.sidebar_claim_limits.append(limit)
            return self.sidebar_claims

    coordinator = OverlimitCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    coordinator.sidebar_claims = claims
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 1}},
            request_id=46,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_pending_failed" in serialized
    assert all(claim.lease_token not in serialized for claim in claims)
    assert coordinator.sidebar_claim_limits == [1]
    assert all(
        store.get_sidebar_job_for_source(claim.source_session_id)["state"]
        != "sidebar_leased"
        for claim in claims
    )


def test_session_sidebar_pending_rejects_list_result_and_rolls_back_real_lease(
    db: SessionDB,
) -> None:
    store, claims = _seed_claimed_sidebar_pair(db)

    class ListResultCoordinator(_FakeCoordinator):
        async def claim_sidebar_jobs_for_delivery(
            self,
            *,
            limit: int,
        ) -> list[SidebarDeliveryClaim]:
            self.sidebar_claim_limits.append(limit)
            return [claims[0]]

    coordinator = ListResultCoordinator(
        bridge_id=claims[0].bridge_id,
        source_id=claims[0].source_session_id,
        target_id="codex:unused",
    )
    with _test_client(_create_test_app(db, store, coordinator)) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "session_sidebar_pending", "arguments": {"limit": 1}},
            request_id=47,
        )

    serialized = json.dumps(payload)
    assert payload["result"]["isError"] is True
    assert "sidebar_pending_failed" in serialized
    assert claims[0].lease_token not in serialized
    assert coordinator.sidebar_claim_limits == [1]
    assert store.get_sidebar_job_for_source(claims[0].source_session_id)["state"] != (
        "sidebar_leased"
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
    assert (
        replay
        == first
        == {
            "state": "sidebar_visible",
            "codex_thread_id": thread_id,
        }
    )
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
    assert (
        replay
        == first
        == {
            "state": "sidebar_leased",
            "codex_thread_id": thread_id,
        }
    )
    assert coordinator.sidebar_binds == [
        ("plaintext-opaque-lease", thread_id),
        ("plaintext-opaque-lease", thread_id),
    ]


def test_claude_visibility_status_is_read_only_and_exposes_fixed_health_contract(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
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
    observed_status_calls: list[float] = []

    def populated_status(now: float) -> dict[str, Any]:
        observed_status_calls.append(now)
        return {
            "counts": {
                "claude_pending": 2,
                "claude_leased": 1,
                "claude_retry": 1,
                "claude_visible": 3,
                "claude_failed": 1,
            },
            "retry_codes": {"future_retry_code": 1},
            "failed_codes": {"marker_conflict": 1},
            "usage": {
                "local_day": "2026-07-17",
                "attempts": 4,
                "reserved_cost_usd": "0.08",
            },
            "fatal": [
                {
                    "code": "unknown_job_state",
                    "state": "future_state",
                    "error_code": None,
                    "count": 1,
                }
            ],
            "last_cycle": {
                "tracked": True,
                "value": {
                    "at": 120.0,
                    "sequence": 9,
                    "status": "retry",
                    "error_code": "native_transcript_not_indexed",
                    "empty_verified": False,
                },
            },
            "last_empty_cycle": {"tracked": True, "value": 100.0},
            "last_registrar_result": {
                "tracked": True,
                "value": {
                    "at": 119.0,
                    "sequence": 8,
                    "status": "registered",
                    "error_code": None,
                },
            },
        }

    monkeypatch.setattr(store, "claude_visibility_status", populated_status)
    assert db._conn is not None
    changes_before = db._conn.total_changes
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
        "claude_pending": 2,
        "claude_leased": 1,
        "claude_retry": 1,
        "claude_visible": 3,
        "claude_failed": 1,
    }
    assert payload["cost_gates"] == {
        "daily_registration_limit": 25,
        "attempts_remaining": 21,
        "reserved_cost_per_attempt_usd": "0.02",
        "emergency_daily_cost_usd": "0.50",
        "reserved_cost_remaining_usd": "0.42",
        "registration_limit_reached": False,
        "emergency_cost_limit_reached": False,
    }
    assert payload["retry_codes"] == {"future_retry_code": 1}
    assert payload["failed_codes"] == {"marker_conflict": 1}
    assert payload["degraded_reasons"] == [
        "marker_conflict",
        "unknown_job_state",
        "unknown_retry_code",
    ]
    assert payload["last_cycle"]["value"]["sequence"] == 9
    assert payload["last_empty_cycle"] == {"tracked": True, "value": 100.0}
    assert payload["last_registrar_result"]["value"] == {
        "at": 119.0,
        "sequence": 8,
        "status": "registered",
        "error_code": None,
    }
    assert len(observed_status_calls) == 1
    assert db._conn.total_changes == changes_before
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert not any(
        fragment in name
        for name in names
        for fragment in (
            "claude_pending",
            "claude_claim",
            "claude_create",
            "claude_bind",
            "claude_commit",
            "claude_fail",
        )
    )


def test_claude_visibility_status_rejects_missing_count_fields() -> None:
    raw = {
        "counts": {},
        "retry_codes": {},
        "failed_codes": {},
        "fatal": [],
        "usage": {
            "local_day": "2026-08-03",
            "attempts": 0,
            "reserved_cost_usd": "0",
        },
    }
    config = ClaudeVisibilityConfig(enabled=True, continuous=True)

    payload = _claude_visibility_status_payload(raw, config)

    assert payload["degraded_reasons"] == ["invalid_status"]


def test_claude_visibility_status_rejects_coercible_counts_and_shapes_heartbeats() -> (
    None
):
    config = ClaudeVisibilityConfig(
        enabled=True,
        continuous=True,
        daily_registration_limit=25,
        reserved_cost_per_attempt_usd="0.02",
        emergency_daily_cost_usd="0.50",
    )
    raw = {
        "counts": {
            "claude_pending": True,
            "claude_leased": 1.0,
            "claude_retry": "2",
            "claude_visible": 3,
            "claude_failed": 0,
        },
        "retry_codes": {"native_transcript_not_indexed": "4"},
        "failed_codes": {},
        "usage": {
            "local_day": "2026-07-17",
            "attempts": "5",
            "reserved_cost_usd": "0.10",
        },
        "fatal": [],
        "last_cycle": {
            "tracked": True,
            "value": {
                "at": 120.0,
                "sequence": "9",
                "status": "retry",
                "error_code": "native_transcript_not_indexed",
                "empty_verified": False,
                "secret": "must-not-leak",
            },
            "outer_secret": "must-not-leak",
        },
        "last_empty_cycle": {"tracked": 1, "value": 100.0},
        "last_registrar_result": {
            "tracked": True,
            "value": {
                "at": 119.0,
                "sequence": 8,
                "status": "registered",
                "error_code": None,
                "secret": "must-not-leak",
            },
        },
    }

    payload = _claude_visibility_status_payload(raw, config)

    assert payload["counts"] == {
        "claude_pending": 0,
        "claude_leased": 0,
        "claude_retry": 0,
        "claude_visible": 3,
        "claude_failed": 0,
    }
    assert payload["retry_codes"] == {"native_transcript_not_indexed": 0}
    assert payload["usage"]["attempts"] == 0
    assert payload["last_cycle"] == {"tracked": False, "value": None}
    assert payload["last_empty_cycle"] == {"tracked": False, "value": None}
    assert payload["last_registrar_result"] == {
        "tracked": False,
        "value": None,
    }
    assert payload["degraded_reasons"] == ["invalid_status"]


def test_claude_visibility_status_degrades_oversized_heartbeat_timestamp() -> None:
    config = ClaudeVisibilityConfig(enabled=True, continuous=True)
    raw = {
        "counts": {},
        "retry_codes": {},
        "failed_codes": {},
        "usage": {"attempts": 0, "reserved_cost_usd": "0"},
        "fatal": [],
        "last_empty_cycle": {"tracked": True, "value": 10**1000},
    }

    payload = _claude_visibility_status_payload(raw, config)

    assert payload["last_empty_cycle"] == {"tracked": False, "value": None}
    assert payload["degraded_reasons"] == ["invalid_status"]


def test_claude_visibility_status_exposes_and_degrades_unlinked_lineage() -> None:
    config = ClaudeVisibilityConfig(
        enabled=True,
        continuous=True,
        daily_registration_limit=25,
        reserved_cost_per_attempt_usd="0.02",
        emergency_daily_cost_usd="0.50",
    )
    raw = {
        "counts": {
            "claude_pending": 0,
            "claude_leased": 0,
            "claude_retry": 0,
            "claude_visible": 2,
            "claude_failed": 0,
        },
        "retry_codes": {},
        "failed_codes": {},
        "usage": {
            "local_day": "2026-07-21",
            "attempts": 0,
            "reserved_cost_usd": "0",
        },
        "fatal": [],
        "lineage": {
            "unlinked_visible": 2,
            "repairable": 1,
            "blocked": 1,
            "blocker_codes": {"claude_lineage_missing_source": 1},
        },
    }

    payload = _claude_visibility_status_payload(raw, config)

    assert payload["lineage"] == raw["lineage"]
    assert payload["degraded_reasons"] == [
        "claude_lineage_missing_source",
        "unlinked_visible_lineage",
    ]


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
    thread_id = "55555555-5555-4555-8555-555555555555"

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
            {
                "lease_token": token,
                "error_code": "desktop_offline",
                "codex_thread_id": thread_id,
            },
        )

    assert set(schema["properties"]) == {
        "lease_token",
        "error_code",
        "codex_thread_id",
    }
    assert set(schema["required"]) == {"lease_token", "error_code"}
    assert arbitrary["result"]["isError"] is True
    assert token not in json.dumps(arbitrary)
    assert "super-secret" not in json.dumps(arbitrary)
    assert failed == {
        "state": "sidebar_retry",
        "error_code": "desktop_offline",
        "codex_thread_id": thread_id,
    }
    row = store.get_sidebar_job_for_source("claude:fail-source")
    assert row is not None
    assert row["codex_thread_id"] == thread_id


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
        _call_tool(client, "session_sidebar_pending", {"limit": 1})
        status = _call_tool(client, "session_status", {})

    serialized = json.dumps(status)
    assert "signed-marker-must-not-leak" not in serialized
    assert "lease-must-not-leak" not in serialized
    sidebar = status["sidebar"]
    assert set(sidebar) == {
        "eligible_by_provider",
        "counts",
        "blocking_failed_count",
        "terminally_resolved_failed_count",
        "ineffective_terminal_resolution_count",
        "terminal_resolution_ledger_valid",
        "terminal_resolutions",
        "execution_blockers",
        "oldest_eligible_age_seconds",
        "oldest_pending_age_seconds",
        "last_heartbeat_at",
        "heartbeat_stale",
        "oldest_job_overdue",
        "degraded_reasons",
        "broker",
        "last_visible_task_id",
        "recent_error_codes",
        "reconciliation_counts",
        "reconciliation_blocked_codes",
        "oldest_reconciliation_wait_age_seconds",
        "reconciliation_scan_age_seconds",
        "recovered_existing_total",
        "created_new_total",
        "delivery_latency_seconds",
        "stage_latency_seconds",
        "scheduler",
        "recovery",
        "placement",
        "hydration",
    }
    assert sidebar["eligible_by_provider"] == {"claude": 1, "hermes": 0}
    assert sidebar["counts"]["sidebar_pending"] == 1
    assert sidebar["blocking_failed_count"] == 0
    assert sidebar["terminally_resolved_failed_count"] == 0
    assert sidebar["ineffective_terminal_resolution_count"] == 0
    assert sidebar["terminal_resolution_ledger_valid"] is True
    assert sidebar["terminal_resolutions"] == {
        "total": 0,
        "effective": 0,
        "ineffective": 0,
        "by_resolution_code": {
            "native_thread_unrecoverable": 0,
            "precutover_create_unrecoverable": 0,
            "native_create_unrecoverable": 0,
        },
    }
    assert sidebar["execution_blockers"] == []
    assert sidebar["last_heartbeat_at"] is not None
    assert sidebar["recent_error_codes"] == []
    assert sidebar["delivery_latency_seconds"] == {
        "p50": None,
        "p95": None,
        "p99": None,
    }
    assert sidebar["stage_latency_seconds"] == {
        "source_to_index": {"p50": None, "p95": None},
        "index_to_queue": {"p50": None, "p95": None},
        "queue_to_visible": {"p50": None, "p95": None},
        "source_to_visible": {"p50": None, "p95": None},
    }
    assert sidebar["scheduler"] == {
        "fresh_claims_since_oldest": 0,
        "next_lane": "fresh",
    }
    assert sidebar["placement"]["generation"] == 1
    assert sidebar["placement"]["verified_visible"] == 0
    assert sidebar["placement"]["mismatch_count"] == 0
    assert sidebar["placement"]["canary"] == {
        "status": "not_run",
        "verified_at": None,
    }
    assert sidebar["recovery"] == {
        "lane": None,
        "status": None,
        "last_cycle_at": None,
    }


def test_session_status_exposes_only_sanitized_hydration_observability(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate = _seed_sidebar_source(db)
    coordinator = _FakeCoordinator(
        bridge_id=candidate.bridge_id,
        source_id=candidate.source_session_id,
        target_id="codex:unused",
    )
    private = "C:/private/source.jsonl secret-preview"
    monkeypatch.setattr(
        store,
        "sidebar_hydration_status",
        lambda now: {
            "counts": {
                "hydration_pending": 2,
                "hydration_leased": 1,
                "hydration_retry": 3,
                "hydration_visible": 4,
                "hydration_failed": 5,
                "hostile": 99,
            },
            "oldest_pending_age_seconds": 42.5,
            "active_lease": True,
            "reserved_reconciliation": 2,
            "recent_error_codes": ["hydration_send_ambiguous", private],
            "lease_token": "must-not-leak",
            "hydration_marker": "must-not-leak",
            "preview": private,
        },
    )
    config = BridgeConfig(
        sidebar=SidebarConfig(legacy_hydration_enabled=True),
    )

    with _test_client(
        _create_test_app(db, store, coordinator, config=config)
    ) as client:
        status = _call_tool(client, "session_status", {})

    hydration = status["sidebar"]["hydration"]
    assert hydration == {
        "enabled": True,
        "counts": {
            "pending": 2,
            "leased": 1,
            "retry": 3,
            "committed": 4,
            "ambiguous": 0,
            "failed": 5,
        },
        "oldest_pending_age_seconds": 42.5,
        "active_lease": True,
        "reserved_reconciliation": 2,
        "recent_error_codes": ["hydration_send_ambiguous"],
    }
    rendered = json.dumps(status)
    assert "lease_token" not in rendered
    assert "hydration_marker" not in rendered
    assert private not in rendered


def test_sidebar_status_preserves_all_fixed_terminal_resolution_codes() -> None:
    status = _sidebar_status({
        "counts": {"sidebar_failed": 3},
        "blocking_failed_count": 0,
        "terminally_resolved_failed_count": 3,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": 3,
            "effective": 3,
            "ineffective": 0,
            "by_resolution_code": {
                "native_thread_unrecoverable": 1,
                "precutover_create_unrecoverable": 1,
                "native_create_unrecoverable": 1,
            },
        },
        "execution_blockers": [],
    })

    assert status["terminal_resolution_ledger_valid"] is True
    assert status["terminal_resolutions"]["by_resolution_code"] == {
        "native_thread_unrecoverable": 1,
        "precutover_create_unrecoverable": 1,
        "native_create_unrecoverable": 1,
    }
    assert status["execution_blockers"] == []


def test_sidebar_status_canonicalizes_needs_attention_without_mutating_payload() -> None:
    payload = {
        "counts": {"sidebar_failed": 3, "needs_attention": 1},
        "blocking_failed_count": 2,
    }
    original = json.dumps(payload, sort_keys=True)

    status = _sidebar_status(payload)

    assert status["counts"]["needs_attention"] == status["blocking_failed_count"] == 2
    assert json.dumps(payload, sort_keys=True) == original


def test_sidebar_status_accepts_immutable_counts_without_mutating_them() -> None:
    counts = MappingProxyType({"sidebar_failed": 3, "needs_attention": 1})
    payload = {"counts": counts, "blocking_failed_count": 2}

    status = _sidebar_status(payload)

    assert status["counts"]["needs_attention"] == status["blocking_failed_count"] == 2
    assert payload["counts"] == counts


@pytest.mark.parametrize(
    ("by_resolution_code", "effective"),
    (
        (
            {
                "native_thread_unrecoverable": 1,
                "precutover_create_unrecoverable": 0,
                "future_resolution_code": 0,
            },
            1,
        ),
        (
            {
                "native_thread_unrecoverable": 1,
                "precutover_create_unrecoverable": 0,
            },
            2,
        ),
    ),
)
def test_sidebar_status_rejects_unknown_or_mismatched_resolution_counts(
    by_resolution_code: dict[str, int],
    effective: int,
) -> None:
    status = _sidebar_status({
        "counts": {"sidebar_failed": 2},
        "blocking_failed_count": 0,
        "terminally_resolved_failed_count": effective,
        "ineffective_terminal_resolution_count": 0,
        "terminal_resolution_ledger_valid": True,
        "terminal_resolutions": {
            "total": effective,
            "effective": effective,
            "ineffective": 0,
            "by_resolution_code": by_resolution_code,
        },
        "execution_blockers": [],
    })

    assert status["terminal_resolution_ledger_valid"] is False
    assert status["terminal_resolutions"]["by_resolution_code"] == {
        "native_thread_unrecoverable": 1,
        "precutover_create_unrecoverable": 0,
        "native_create_unrecoverable": 0,
    }
    assert status["execution_blockers"] == [
        "sidebar_terminal_resolution_ledger_invalid"
    ]


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

    def sidebar_status(**_kwargs: object):
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


def test_sidebar_status_shapes_placement_without_secret_identity_fields() -> None:
    status = _sidebar_status({
        "counts": {},
        "placement": {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 12,
            "mismatch_count": 0,
            "source_cwd": "C:\\private\\source",
            "marker": "HERMES_SESSION_BRIDGE_V1:secret",
            "lease_token": "secret-token",
            "task_id": "secret-task",
            "canary": {
                "status": "passed",
                "verified_at": 1234.0,
                "canary_identity_digest": "a" * 64,
            },
        },
    })

    assert status["placement"] == {
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
        "generation": 1,
        "verified_visible": 12,
        "mismatch_count": 0,
        "canary": {"status": "passed", "verified_at": 1234.0},
    }
    encoded = json.dumps(status)
    for secret in (
        "private",
        "HERMES_SESSION_BRIDGE_V1",
        "secret-token",
        "secret-task",
        "a" * 64,
    ):
        assert secret not in encoded


def test_sidebar_status_shapes_bounded_reconciliation_health() -> None:
    status = _sidebar_status({
        "counts": {},
        "reconciliation_counts": {
            "recovered": 1,
            "absence_proven": 2,
            "blocked": 3,
            "private_state": 999,
        },
        "reconciliation_blocked_codes": {
            "marker_conflict": 1,
            "native_create_ambiguous": 2,
            "bridge_temporarily_unavailable": 0,
            "provider-secret-error": 999,
        },
        "oldest_reconciliation_wait_age_seconds": 40.0,
        "reconciliation_scan_age_seconds": 10.0,
        "recovered_existing_total": 4,
        "created_new_total": 5,
        "signed_marker": "HERMES_SESSION_BRIDGE_V1:secret",
        "proof_digest": "a" * 64,
        "reconciliation_generation": "private-generation",
    })

    assert status["reconciliation_counts"] == {
        "recovered": 1,
        "absence_proven": 2,
        "blocked": 3,
    }
    assert status["reconciliation_blocked_codes"] == {
        "marker_conflict": 1,
        "native_create_ambiguous": 2,
        "bridge_temporarily_unavailable": 0,
    }
    assert status["oldest_reconciliation_wait_age_seconds"] == 40.0
    assert status["reconciliation_scan_age_seconds"] == 10.0
    assert status["recovered_existing_total"] == 4
    assert status["created_new_total"] == 5
    encoded = json.dumps(status)
    assert "HERMES_SESSION_BRIDGE_V1" not in encoded
    assert "proof_digest" not in encoded
    assert "private-generation" not in encoded
    assert "provider-secret-error" not in encoded


@pytest.mark.parametrize(
    "unsafe",
    ("C:\\unsafe\x00path", "C:\\unsafe\x85path", "C:\\unsafe\u2028path", "C:\\unsafe\u2029path"),
)
def test_sidebar_status_omits_placement_with_unsafe_inbox_path(unsafe: str) -> None:
    status = _sidebar_status({
        "counts": {},
        "placement": {
            "inbox_cwd": unsafe,
            "generation": 1,
            "verified_visible": 1,
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": 1234.0},
        },
    })

    assert "placement" not in status
    assert unsafe not in json.dumps(status)


def test_sidebar_status_preserves_only_broker_thresholds_and_identity() -> None:
    status = _sidebar_status({
        "counts": {"pending": 1},
        "oldest_eligible_age_seconds": 301.0,
        "oldest_pending_age_seconds": 12.0,
        "heartbeat_stale": True,
        "oldest_job_overdue": True,
        "degraded_reasons": [
            "broker_heartbeat_stale",
            "oldest_pending_stale",
            "private exception",
        ],
        "broker": {
            "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
            "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
            "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
            "messages": ["private source payload"],
            "lease_token": "private token",
        },
        "messages": ["private source payload"],
    })

    assert status["heartbeat_stale"] is True
    assert status["oldest_job_overdue"] is True
    assert status["degraded_reasons"] == [
        "broker_heartbeat_stale",
        "oldest_pending_stale",
    ]
    assert status["broker"] == {
        "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
    }
    rendered = repr(status)
    assert "messages" not in rendered
    assert "private" not in rendered


@pytest.mark.parametrize("field", ("thread_id", "project_id", "cwd"))
@pytest.mark.parametrize("unsafe", ("bad\x00value", "bad\x85value", "bad\u2028value", "bad\u2029value"))
def test_sidebar_status_drops_unsafe_broker_identity_text(
    field: str,
    unsafe: str,
) -> None:
    broker = {
        "thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
    }
    broker[field] = unsafe

    status = _sidebar_status({"counts": {}, "broker": broker})

    assert field not in status["broker"]


def test_sidebar_status_sanitizes_negative_canary_verified_at() -> None:
    status = _sidebar_status({
        "counts": {},
        "placement": {
            "inbox_cwd": "C:\\Users\\diego\\.hermes",
            "generation": 1,
            "verified_visible": 12,
            "mismatch_count": 0,
            "canary": {"status": "passed", "verified_at": -0.001},
        },
    })

    assert status["placement"]["canary"] == {
        "status": "not_run",
        "verified_at": None,
    }
    assert "-0.001" not in json.dumps(status)


def test_session_status_adds_evidence_from_one_sequential_composite_observation(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )
    config = BridgeConfig(
        service=replace(BridgeConfig().service, catalog_scan_seconds=17),
        claude_visibility=ClaudeVisibilityConfig(enabled=True, continuous=True),
    )
    catalog = UnifiedCatalog(db, store)
    observed_sources: list[str] = []
    raw_health = {
        "running": True,
        "providers": {
            "claude": {
                "last_success": 99.0,
                "lag_seconds": 50.0,
                "degraded_reason": None,
            },
        },
        "watcher_state": "running",
        "queue_counts": {
            "queued": 0,
            "running": 0,
            "retry": 0,
            "succeeded": 0,
            "manual_failure": 0,
        },
        "mirror_mode": "manual",
        "recent_error_codes": [],
    }
    raw_catalog = {
        "providers": {
            "claude": {"sessions": 1, "degraded": 0},
            "codex": {"sessions": 1, "degraded": 0},
            "hermes": {"sessions": 0, "degraded": 0},
        },
        "total_sessions": 2,
    }
    raw_sidebar = store.sidebar_delivery_status(
        inbox_cwd=config.sidebar.inbox_cwd,
        placement_generation=config.sidebar.placement_generation,
    )
    raw_hydration = store.sidebar_hydration_status(100.0)
    raw_visibility = {
        "counts": {
            "claude_pending": 0,
            "claude_leased": 0,
            "claude_retry": 1,
            "claude_visible": 0,
            "claude_failed": 0,
        },
        "retry_codes": {"creation_ambiguous": 1},
        "failed_codes": {},
        "fatal": [],
        "usage": {"local_day": None, "attempts": 0, "reserved_cost_usd": "0"},
    }

    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("evidence builder must never stringify unknown objects")

    privacy_canaries: dict[str, object] = {
        "transcript": "PRIVATE_TRANSCRIPT_CANARY",
        "token": "PRIVATE_TOKEN_CANARY",
        "path": "C:\\private\\path-canary",
        "repository": "PRIVATE_REPOSITORY_CANARY",
        "branch": "PRIVATE_BRANCH_CANARY",
        "session_id": "PRIVATE_SESSION_CANARY",
        "native_id": "PRIVATE_NATIVE_CANARY",
        "thread_id": "PRIVATE_THREAD_CANARY",
        "lease_token": "PRIVATE_LEASE_CANARY",
        "hydration_marker": "PRIVATE_MARKER_CANARY",
        "pack": "PRIVATE_PACK_CANARY",
        "idempotency_key": "PRIVATE_IDEMPOTENCY_CANARY",
        "traceback": "PRIVATE_TRACEBACK_CANARY",
        "sql": "PRIVATE_SQL_CANARY",
        "hostile": Hostile(),
    }
    for raw_source in (
        raw_health,
        raw_catalog,
        raw_sidebar,
        raw_hydration,
        raw_visibility,
    ):
        raw_source.update(privacy_canaries)

    def health() -> dict[str, Any]:
        observed_sources.append("health")
        return deepcopy(raw_health)

    def catalog_status() -> dict[str, Any]:
        observed_sources.append("catalog")
        return deepcopy(raw_catalog)

    def sidebar_status(**_kwargs: object) -> dict[str, Any]:
        observed_sources.append("sidebar")
        return deepcopy(raw_sidebar)

    def hydration_status(_now: float) -> dict[str, Any]:
        observed_sources.append("hydration")
        return deepcopy(raw_hydration)

    def visibility_status(_now: float) -> dict[str, Any]:
        observed_sources.append("visibility")
        return deepcopy(raw_visibility)

    coordinator.health = health  # type: ignore[method-assign]
    monkeypatch.setattr(catalog, "status", catalog_status)
    monkeypatch.setattr(store, "sidebar_delivery_status", sidebar_status)
    monkeypatch.setattr(store, "sidebar_hydration_status", hydration_status)
    monkeypatch.setattr(store, "claude_visibility_status", visibility_status)
    timestamps = iter(float(value) for value in range(100, 113))
    monkeypatch.setattr("session_bridge.mcp_server.time.time", lambda: next(timestamps))
    app = create_app(
        catalog=catalog,
        coordinator=coordinator,
        store=store,
        config=config,
        token=TOKEN,
        marker_key=MARKER_KEY,
    )

    with _test_client(app) as client:
        status = _call_tool(client, "session_status", {})

    expected_legacy = _status_payload(
        raw_health,
        raw_catalog,
        raw_sidebar,
        raw_hydration,
        hydration_enabled=config.sidebar.legacy_hydration_enabled,
    )
    assert observed_sources == [
        "health",
        "catalog",
        "sidebar",
        "hydration",
        "visibility",
    ]
    assert status["health"] == expected_legacy["health"]
    assert status["catalog"] == expected_legacy["catalog"]
    assert status["sidebar"] == expected_legacy["sidebar"]
    evidence = status["evidence_v1"]
    assert evidence["schema_version"] == 1
    assert evidence["queues"]["claude_visibility"]["work_state"]["state"] == "error"
    assert evidence["queues"]["claude_visibility"]["work_state"]["code"] == (
        "creation_ambiguous"
    )
    assert evidence["catalog"]["providers"]["claude"]["freshness"] == {
        "state": "healthy",
        "code": "within_freshness_limit",
    }
    started = evidence["observation_started_at"]
    completed = evidence["observation_completed_at"]
    assert started < completed
    observed_times = [
        evidence["service"]["observed_at"],
        evidence["catalog"]["aggregate"]["observed_at"],
        evidence["queues"]["sidebar_registration"]["work_state"]["observed_at"],
        evidence["queues"]["sidebar_hydration"]["work_state"]["observed_at"],
        evidence["queues"]["claude_visibility"]["work_state"]["observed_at"],
    ]
    assert observed_times == sorted(observed_times)
    assert len(set(observed_times)) == len(observed_times)
    for observed_at in (
        evidence["service"]["observed_at"],
        evidence["catalog"]["aggregate"]["observed_at"],
        evidence["queues"]["sidebar_registration"]["work_state"]["observed_at"],
        evidence["queues"]["sidebar_hydration"]["work_state"]["observed_at"],
        evidence["queues"]["claude_visibility"]["work_state"]["observed_at"],
    ):
        assert started <= observed_at <= completed
    serialized_evidence = json.dumps(evidence)
    for canary in privacy_canaries.values():
        if type(canary) is str:
            assert canary not in serialized_evidence


def test_session_status_is_read_only_across_sqlite_memory_and_mutation_seams(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
    )
    config = BridgeConfig(
        claude_visibility=ClaudeVisibilityConfig(enabled=True, continuous=True)
    )
    raw_visibility = {
        "counts": {
            "claude_pending": 1,
            "claude_leased": 0,
            "claude_retry": 0,
            "claude_visible": 2,
            "claude_failed": 0,
        },
        "retry_codes": {},
        "failed_codes": {},
        "fatal": [],
        "usage": {"local_day": "2026-07-31", "attempts": 2, "reserved_cost_usd": "0"},
        "lineage": {
            "unlinked_visible": 0,
            "repairable": 0,
            "blocked": 0,
            "blocker_codes": {},
        },
    }
    monkeypatch.setattr(
        store, "claude_visibility_status", lambda _now: deepcopy(raw_visibility)
    )
    assert db._conn is not None
    before_changes = db._conn.total_changes
    before_tables = [
        tuple(row)
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    before_rows = {
        table[0]: tuple(
            tuple(row)
            for row in db._conn.execute(f'SELECT * FROM "{table[0]}"').fetchall()
        )
        for table in before_tables
        if not table[0].startswith("fts_") and not table[0].startswith("sqlite_")
    }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("session_status invoked a write-capable seam")

    for name in (
        "claim_sidebar_jobs",
        "claim_sidebar_hydration_jobs",
        "reserve_sidebar_creation",
        "reserve_sidebar_hydration",
        "bind_sidebar_thread",
        "commit_sidebar_job",
        "fail_sidebar_job",
        "enqueue_sidebar_job",
        "enqueue_sidebar_hydration",
    ):
        if hasattr(store, name):
            monkeypatch.setattr(store, name, forbidden)
    for name in (
        "continue_session",
        "claim_sidebar_jobs_for_delivery",
        "claim_sidebar_hydration_for_delivery",
        "commit_sidebar_job",
        "bind_sidebar_thread",
    ):
        if hasattr(coordinator, name):
            monkeypatch.setattr(coordinator, name, forbidden)

    protected_memory = {
        key: deepcopy(value)
        for key, value in coordinator.__dict__.items()
        if key not in {"started", "stopped"}
    }
    app = create_app(
        catalog=UnifiedCatalog(db, store),
        coordinator=coordinator,
        store=store,
        config=config,
        token=TOKEN,
        marker_key=MARKER_KEY,
    )
    with _test_client(app) as client:
        status = _call_tool(client, "session_status", {})

    assert status["evidence_v1"]["schema_version"] == 1
    assert db._conn.total_changes == before_changes
    assert {
        key: value
        for key, value in coordinator.__dict__.items()
        if key in protected_memory
    } == protected_memory
    after_rows = {
        table[0]: tuple(
            tuple(row)
            for row in db._conn.execute(f'SELECT * FROM "{table[0]}"').fetchall()
        )
        for table in before_tables
        if not table[0].startswith("fts_") and not table[0].startswith("sqlite_")
    }
    assert after_rows == before_rows


def test_session_status_uses_explicit_schemas_and_never_stringifies_unknowns(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError(
                "status sanitizer must never stringify unknown objects"
            )

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

    assert set(status) == {"health", "catalog", "sidebar", "evidence_v1"}
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


def _seed_mcp_sidebar_commit_case(
    db: SessionDB,
    *,
    root: Path,
    projected_cwd: Path,
) -> tuple[
    SessionBridgeStore,
    SessionBridgeCoordinator,
    BridgeConfig,
    _McpSidebarVerifier,
    str,
    str,
    str,
    str,
]:
    token = "lineage-opaque-lease-token"
    now = 1_000.0
    inbox = root / "profile"
    source_cwd = root / "source"
    inbox.mkdir(parents=True)
    source_cwd.mkdir()
    projected_cwd.mkdir(exist_ok=True)
    store = SessionBridgeStore(
        db,
        clock=lambda: now,
        sidebar_token_factory=lambda: token,
    )
    source = _projection(
        Provider.CLAUDE,
        "lineage-source",
        title="Lineage source",
        cwd=str(source_cwd),
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
        cwd=str(source_cwd),
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
            cwd=str(projected_cwd),
            timestamp=950.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
    )
    verified_projection = _projection(
        Provider.CODEX,
        thread_id,
        title="Native sidebar placeholder",
        cwd=str(projected_cwd),
        timestamp=950.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=bridge_id,
    )
    verified = VerifiedSidebarThread(
        thread_id,
        source_id,
        bridge_id,
        projection=verified_projection,
    )
    verifier = _McpSidebarVerifier(verified)
    config = BridgeConfig(
        sidebar=SidebarConfig(
            enabled=True,
            inbox_cwd=str(inbox),
            placement_generation=1,
        )
    )
    coordinator = SessionBridgeCoordinator(
        config=config,
        store=store,
        adapters={},
        sidebar_verifier=verifier,
        clock=lambda: now,
    )
    return (
        store,
        coordinator,
        config,
        verifier,
        token,
        thread_id,
        source_id,
        bridge_id,
    )


def test_session_sidebar_commit_binds_exact_indexed_codex_lineage_once(
    db: SessionDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "commit-success" / "profile"
    (
        store,
        coordinator,
        config,
        verifier,
        token,
        thread_id,
        source_id,
        bridge_id,
    ) = _seed_mcp_sidebar_commit_case(
        db,
        root=tmp_path / "commit-success",
        projected_cwd=inbox,
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: inbox)

    with _test_client(
        create_app(
            catalog=UnifiedCatalog(db, store),
            coordinator=coordinator,
            store=store,
            config=config,
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
    job = store.get_sidebar_job_for_source(source_id)
    assert job is not None
    assert job["placement_generation"] == 1
    assert job["placement_verified_at"] == 1_000.0
    status = store.sidebar_delivery_status(
        inbox_cwd=str(inbox),
        placement_generation=1,
    )
    assert status["placement"]["verified_visible"] == 1


def test_session_sidebar_commit_rejects_exact_marker_in_wrong_project_without_visibility(
    db: SessionDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "commit-wrong-project"
    inbox = root / "profile"
    wrong_project = root / "wrong-project"
    (
        store,
        coordinator,
        config,
        verifier,
        token,
        thread_id,
        source_id,
        bridge_id,
    ) = _seed_mcp_sidebar_commit_case(
        db,
        root=root,
        projected_cwd=wrong_project,
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: inbox)

    with _test_client(
        create_app(
            catalog=UnifiedCatalog(db, store),
            coordinator=coordinator,
            store=store,
            config=config,
            token=TOKEN,
            marker_key=MARKER_KEY,
        )
    ) as client:
        payload = _rpc(
            client,
            "tools/call",
            {
                "name": "session_sidebar_commit",
                "arguments": {
                    "lease_token": token,
                    "codex_thread_id": thread_id,
                },
            },
            request_id=49,
        )

    assert payload["result"]["isError"] is True
    error_text = payload["result"]["content"][0]["text"]
    assert error_text.endswith("placement_mismatch")
    assert str(inbox) not in error_text
    assert str(wrong_project) not in error_text
    assert str(root / "source") not in error_text
    assert verifier.verify_calls == [thread_id]
    job = store.get_sidebar_job_for_source(source_id)
    assert job is not None
    assert job["state"] == "sidebar_leased"
    assert job["codex_thread_id"] == thread_id
    assert job["placement_generation"] is None
    assert job["placement_verified_at"] is None
    assert job["visible_at"] is None
    assert job["completion_digest"] is None
    assert (
        db._conn.execute(
            "SELECT 1 FROM session_links WHERE bridge_id = ?",
            (bridge_id,),
        ).fetchone()
        is None
    )
    status = store.sidebar_delivery_status(
        inbox_cwd=str(inbox),
        placement_generation=1,
    )
    assert status["placement"]["verified_visible"] == 0


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
    assert all(
        request.bridge_id == bridge_id for request in coordinator.continue_requests
    )


@pytest.mark.parametrize(
    "exact_cwd", [object(), "relative/source", "C:/source/../other"]
)
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
    authority = store.get_state(f"session-bridge:mirror-authority:{queued['job_id']}")
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
        raise AssertionError(
            "marker key must be consumed from its validated descriptor"
        )

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


def test_mcp_status_names_an_abandoned_repair_lease() -> None:
    """degraded_reasons must carry the specific code, not 'invalid_status'."""

    config = ClaudeVisibilityConfig(
        enabled=True,
        continuous=True,
        daily_registration_limit=25,
        reserved_cost_per_attempt_usd="0.02",
        emergency_daily_cost_usd="0.50",
    )
    raw = {
        "counts": {
            "claude_pending": 0,
            "claude_leased": 1,
            "claude_retry": 0,
            "claude_visible": 0,
            "claude_failed": 0,
        },
        "retry_codes": {},
        "failed_codes": {},
        "usage": {
            "local_day": "2026-08-26",
            "attempts": 1,
            "reserved_cost_usd": "0.02",
        },
        "fatal": [
            {
                "code": "reconciliation_repair_abandoned",
                "state": "claude_leased",
                "error_code": "bridge_conflict",
                "count": 1,
            }
        ],
    }

    payload = _claude_visibility_status_payload(raw, config)

    assert payload["degraded_reasons"] == ["reconciliation_repair_abandoned"]


def _repair_raw(repair_rows):
    return {
        "counts": {
            "claude_pending": 0,
            "claude_leased": 1,
            "claude_retry": 0,
            "claude_visible": 0,
            "claude_failed": 0,
        },
        "retry_codes": {},
        "failed_codes": {},
        "usage": {
            "local_day": "2026-08-26",
            "attempts": 1,
            "reserved_cost_usd": "0.02",
        },
        "fatal": [
            {
                "code": "reconciliation_repair_abandoned",
                "state": "claude_leased",
                "error_code": "bridge_conflict",
                "count": 1,
            }
        ],
        "repair_required": repair_rows,
    }


def _repair_config():
    return ClaudeVisibilityConfig(
        enabled=True,
        continuous=True,
        daily_registration_limit=25,
        reserved_cost_per_attempt_usd="0.02",
        emergency_daily_cost_usd="0.50",
    )


def test_mcp_status_names_the_stuck_job_and_its_repair_command() -> None:
    """An agent polling MCP must learn WHICH job, not just what is wrong.

    Nothing frees an abandoned repair lease automatically, so the status surface
    is the whole discovery path.  Reporting only the reason forces a shell-out to
    the CLI to answer the very next question.
    """

    payload = _claude_visibility_status_payload(
        _repair_raw(
            [
                {
                    "job_id": "job-7",
                    "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
                    "error_code": "bridge_conflict",
                }
            ]
        ),
        _repair_config(),
    )

    assert payload["degraded_reasons"] == ["reconciliation_repair_abandoned"]
    assert payload["repair_required"] == [
        {
            "job_id": "job-7",
            "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
            "error_code": "bridge_conflict",
            "command": (
                "hermes-session-bridge claude-visibility-repair-failed "
                "--job-id job-7 "
                "--reserved-claude-uuid d8ae024c-1111-2222-3333-444455556666 "
                "--error-code bridge_conflict "
                "--apply --confirm-exact-terminal-repair"
            ),
        }
    ]


def test_mcp_status_degrades_on_a_malformed_repair_row() -> None:
    """This payload is a fixed public contract; malformed evidence degrades."""

    payload = _claude_visibility_status_payload(
        _repair_raw([{"job_id": "job-7"}]), _repair_config()
    )

    assert "invalid_status" in payload["degraded_reasons"]
    assert payload["repair_required"] == []


def test_mcp_status_omits_a_command_it_cannot_build_safely() -> None:
    """Same withholding rule as the CLI: a pastable line is a promise."""

    payload = _claude_visibility_status_payload(
        _repair_raw(
            [
                {
                    "job_id": "job-7 --apply; rm -rf /",
                    "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
                    "error_code": "bridge_conflict",
                }
            ]
        ),
        _repair_config(),
    )

    assert payload["repair_required"][0]["command"] is None


def test_both_surfaces_render_one_identical_repair_command() -> None:
    """The CLI and MCP must not each grow their own copy of this line.

    Three readers independently re-deriving one rule is the defect this whole
    change exists to remove; a second command renderer would reintroduce it.
    """

    from session_bridge.cli import _claude_visibility_repair_required

    rows = [
        {
            "job_id": "job-7",
            "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
            "error_code": "bridge_conflict",
        }
    ]

    cli_rows = _claude_visibility_repair_required({"repair_required": rows})
    mcp_rows = _claude_visibility_status_payload(
        _repair_raw(rows), _repair_config()
    )["repair_required"]

    assert cli_rows == mcp_rows


def test_no_command_is_offered_for_a_code_the_repair_cli_refuses() -> None:
    """The repair verb declares choices=("bridge_conflict",).

    Rendering any other code produces a line argparse rejects outright -- a
    pastable command that cannot run is worse than none, so the row is still
    reported but carries no command.  Found by an arming mutant that survived.
    """

    payload = _claude_visibility_status_payload(
        _repair_raw(
            [
                {
                    "job_id": "job-7",
                    "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
                    "error_code": "duplicate_uuid",
                }
            ]
        ),
        _repair_config(),
    )

    assert payload["repair_required"] == [
        {
            "job_id": "job-7",
            "reserved_claude_uuid": "d8ae024c-1111-2222-3333-444455556666",
            "error_code": "duplicate_uuid",
            "command": None,
        }
    ]
