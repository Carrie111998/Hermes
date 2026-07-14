from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest
from mcp.shared.version import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import ContinueResult
from session_bridge.mcp_server import (
    EXPECTED_TOOLS,
    _validate_windows_token_acl,
    create_app,
    resolve_bearer_token,
)
from session_bridge.models import (
    ContextPack,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore


TOKEN = "bridge-test-token-with-at-least-32-bytes"


@pytest.fixture
def db(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


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


class _FakeCoordinator:
    def __init__(self, *, bridge_id: str, source_id: str, target_id: str) -> None:
        self.bridge_id = bridge_id
        self.source_id = source_id
        self.target_id = target_id
        self.started = 0
        self.stopped = 0
        self.continue_requests: list[Any] = []

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
        )

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


def test_tools_list_exposes_exactly_the_five_approved_tools(db: SessionDB) -> None:
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
    }


def test_all_five_tools_are_callable_and_search_filters_are_forwarded(
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


def test_session_continue_is_idempotent_for_identical_snapshot_and_budget(
    db: SessionDB,
) -> None:
    store, bridge_id, source_id, target_id = _seed_linked_pair(db)
    coordinator = _FakeCoordinator(
        bridge_id=bridge_id, source_id=source_id, target_id=target_id
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
    assert all(request.bridge_id == bridge_id for request in coordinator.continue_requests)


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
    token_file.chmod(0o600)
    if os.name == "nt":
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
                str(token_file),
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
                str(token_file),
                "/remove:g",
                "*S-1-3-4",
                "*S-1-5-32-544",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    assert resolve_bearer_token(environ={}, token_file=token_file) == TOKEN.encode()


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
