from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path

import pytest

from hermes_state import SessionDB
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    derive_claude_visibility_identity,
)
from session_bridge.mirror_float import ClaudeMirrorFloatWorker
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore

_SECRET = b"mirror-float-test-secret"


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _message(event_id: str, content: str, *, role: str = "user") -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role=role,
        content=content,
        timestamp=10.0 + len(event_id),
        tool_calls=None,
        tool_call_id=None,
    )


def _projection(
    *messages: ProjectedMessage,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "native-1",
    last_active: float = 20.0,
    native_path: str | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} session",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=last_active,
        messages=messages,
        native_path=native_path or f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
        git_branch=None,
        parser_version=3,
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _identity(
    suffix: str,
    *,
    provider: Provider = Provider.CODEX,
) -> tuple[ClaudeVisibilityCandidate, object]:
    source_session_id = (
        f"codex:source-{suffix}"
        if provider is Provider.CODEX
        else f"hermes-source-{suffix}"
    )
    candidate = ClaudeVisibilityCandidate(
        source_session_id=source_session_id,
        source_provider=provider,
        native_name=f"[{provider.value.title()}] Request {suffix}",
        source_cwd="C:/work/project",
        git_root="C:/work/project",
        git_branch="main",
        git_head=f"head-{suffix}",
        worktree_id=f"worktree-{suffix}",
        eligible_at=100.0,
    )
    return candidate, derive_claude_visibility_identity(candidate, _SECRET)


def _seed_visible_mirror(
    db: SessionDB,
    store: SessionBridgeStore,
    tmp_path: Path,
    *,
    suffix: str = "1",
    provider: Provider = Provider.CODEX,
    source_last_active: float = 5_000.0,
    mirror_mtime: float = 1_000.0,
    create_mirror_file: bool = True,
    origin_bridge_id: str | None = None,
):
    candidate, identity = _identity(suffix, provider=provider)
    if provider is Provider.CODEX:
        store.upsert_projection(
            _projection(
                _message(f"source-{suffix}", "meaningful request"),
                provider=Provider.CODEX,
                native_id=candidate.source_session_id.removeprefix("codex:"),
                last_active=source_last_active,
            )
        )
    else:
        db.create_session(session_id=candidate.source_session_id, source="hermes")
        db.append_message(
            session_id=candidate.source_session_id,
            role="user",
            content="meaningful request",
            timestamp=source_last_active,
        )
    store.enqueue_claude_visibility_job(candidate, identity, _SECRET)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 100.0
    )
    mirror_path = tmp_path / f"{identity.claude_uuid}.jsonl"
    if create_mirror_file:
        mirror_path.write_text("{}\n", encoding="utf-8")
        os.utime(mirror_path, (mirror_mtime, mirror_mtime))
    store.upsert_projection(
        _projection(
            _message(f"target-{suffix}", "signed registration"),
            native_id=identity.claude_uuid,
            native_path=str(mirror_path),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=origin_bridge_id or identity.bridge_id,
        )
    )
    return identity, mirror_path


def test_store_lists_only_visible_claude_visibility_mirrors(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    identity, _ = _seed_visible_mirror(db, store, tmp_path, suffix="1")
    registered_candidate, registered_identity = _identity("2")
    store.upsert_projection(
        _projection(
            _message("source-2", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-2",
        )
    )
    store.enqueue_claude_visibility_job(
        registered_candidate, registered_identity, _SECRET
    )

    rows = store.list_visible_claude_visibility_mirrors()

    assert rows == [
        {
            "source_session_id": "codex:source-1",
            "claude_uuid": identity.claude_uuid,
        }
    ]


def test_floats_mirror_to_source_activity(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _, mirror_path = _seed_visible_mirror(
        db, store, tmp_path, source_last_active=5_000.0, mirror_mtime=1_000.0
    )
    worker = ClaudeMirrorFloatWorker(store, min_interval_seconds=900.0)

    result = worker.run_once()

    assert result == {
        "examined": 1,
        "floated": 1,
        "skipped": 0,
        "registered": 0,
        "throttled": 0,
    }
    assert mirror_path.stat().st_mtime == pytest.approx(5_000.0)


def _registry_records(registry_root: Path) -> list[dict]:
    import json

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(registry_root.glob("local_*.json"))
    ]


def test_creates_ccd_registry_record_for_visible_mirror(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    identity, _ = _seed_visible_mirror(
        db, store, tmp_path, source_last_active=5_000.0, mirror_mtime=1_000.0
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    worker = ClaudeMirrorFloatWorker(
        store,
        min_interval_seconds=900.0,
        registry_root=registry,
        id_factory=lambda: "11111111-2222-4333-8444-555555555555",
    )

    result = worker.run_once()

    assert result["registered"] == 1
    records = _registry_records(registry)
    assert len(records) == 1
    record = records[0]
    assert record["sessionId"] == "local_11111111-2222-4333-8444-555555555555"
    assert record["cliSessionId"] == identity.claude_uuid
    assert record["lastActivityAt"] == 5_000_000
    assert record["title"] == "claude session"
    assert record["cwd"] == "C:/workspace/project"
    assert record["isArchived"] is False
    assert record["permissionMode"] == "default"


def test_registry_record_is_idempotent_by_cli_session_id(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _seed_visible_mirror(
        db, store, tmp_path, source_last_active=5_000.0, mirror_mtime=1_000.0
    )
    registry = tmp_path / "registry"
    registry.mkdir()

    first = ClaudeMirrorFloatWorker(
        store, min_interval_seconds=900.0, registry_root=registry
    ).run_once()
    second = ClaudeMirrorFloatWorker(
        store, min_interval_seconds=900.0, registry_root=registry
    ).run_once()

    assert first["registered"] == 1
    assert second["registered"] == 0
    assert len(_registry_records(registry)) == 1


def test_registry_record_floats_on_new_source_activity(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _seed_visible_mirror(
        db,
        store,
        tmp_path,
        suffix="1",
        source_last_active=5_000.0,
        mirror_mtime=1_000.0,
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    ClaudeMirrorFloatWorker(
        store, min_interval_seconds=900.0, registry_root=registry
    ).run_once()

    store.upsert_projection(
        _projection(
            _message("source-1-later", "another meaningful request"),
            provider=Provider.CODEX,
            native_id="source-1",
            last_active=9_000.0,
        )
    )
    result = ClaudeMirrorFloatWorker(
        store, min_interval_seconds=900.0, registry_root=registry
    ).run_once()

    assert result["floated"] == 1
    records = _registry_records(registry)
    assert len(records) == 1
    assert records[0]["lastActivityAt"] == 9_000_000


def test_registry_tolerates_malformed_foreign_record(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _seed_visible_mirror(
        db, store, tmp_path, source_last_active=5_000.0, mirror_mtime=1_000.0
    )
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "local_broken.json").write_text("{not json", encoding="utf-8")
    foreign = registry / "local_foreign.json"
    foreign.write_text('{"sessionId": "local_foreign"}', encoding="utf-8")

    result = ClaudeMirrorFloatWorker(
        store, min_interval_seconds=900.0, registry_root=registry
    ).run_once()

    assert result["registered"] == 1
    assert foreign.read_text(encoding="utf-8") == '{"sessionId": "local_foreign"}'


def test_run_once_is_internally_throttled(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _seed_visible_mirror(
        db, store, tmp_path, source_last_active=5_000.0, mirror_mtime=1_000.0
    )
    clock = {"now": 0.0}
    worker = ClaudeMirrorFloatWorker(
        store,
        min_interval_seconds=900.0,
        run_min_interval_seconds=300.0,
        monotonic=lambda: clock["now"],
    )

    first = worker.run_once()
    clock["now"] = 10.0
    second = worker.run_once()
    clock["now"] = 400.0
    third = worker.run_once()

    assert first["examined"] == 1
    assert second == {
        "examined": 0,
        "floated": 0,
        "skipped": 0,
        "registered": 0,
        "throttled": 1,
    }
    assert third["examined"] == 1


def test_discover_registry_root_picks_single_leaf(tmp_path) -> None:
    from session_bridge.mirror_float import discover_ccd_registry_root

    base = tmp_path / "claude-code-sessions"
    leaf = base / "org-a" / "user-b"
    leaf.mkdir(parents=True)
    (leaf / "local_x.json").write_text("{}", encoding="utf-8")
    empty = base / "org-a" / "user-c"
    empty.mkdir(parents=True)

    assert discover_ccd_registry_root(base) == leaf
    assert discover_ccd_registry_root(tmp_path / "absent") is None


def test_skips_bump_within_min_interval(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _, mirror_path = _seed_visible_mirror(
        db, store, tmp_path, source_last_active=1_500.0, mirror_mtime=1_000.0
    )
    worker = ClaudeMirrorFloatWorker(store, min_interval_seconds=900.0)

    result = worker.run_once()

    assert result == {
        "examined": 1,
        "floated": 0,
        "skipped": 0,
        "registered": 0,
        "throttled": 0,
    }
    assert mirror_path.stat().st_mtime == pytest.approx(1_000.0)


def test_skips_missing_mirror_file_without_raising(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _seed_visible_mirror(db, store, tmp_path, create_mirror_file=False)
    worker = ClaudeMirrorFloatWorker(store, min_interval_seconds=900.0)

    result = worker.run_once()

    assert result == {
        "examined": 1,
        "floated": 0,
        "skipped": 1,
        "registered": 0,
        "throttled": 0,
    }


def test_refuses_mirror_with_foreign_origin(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _, mirror_path = _seed_visible_mirror(
        db,
        store,
        tmp_path,
        origin_bridge_id="sidebar:not-a-visibility-mirror",
    )
    worker = ClaudeMirrorFloatWorker(store, min_interval_seconds=900.0)

    result = worker.run_once()

    assert result == {
        "examined": 1,
        "floated": 0,
        "skipped": 1,
        "registered": 0,
        "throttled": 0,
    }
    assert mirror_path.stat().st_mtime == pytest.approx(1_000.0)


def test_config_parses_claude_visibility_float_activity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.config import BridgeConfig

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "session_bridge": {
                "claude_visibility": {"enabled": True, "float_activity": True}
            }
        },
    )

    config = BridgeConfig.load(path=tmp_path / "session_bridge.toml")

    assert config.claude_visibility.float_activity is True
    assert ClaudeMirrorFloatWorker is not None


def test_config_float_activity_defaults_false_and_rejects_non_bool(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.config import BridgeConfig

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"session_bridge": {}},
    )
    assert (
        BridgeConfig.load(
            path=tmp_path / "session_bridge.toml"
        ).claude_visibility.float_activity
        is False
    )

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"session_bridge": {"claude_visibility": {"float_activity": "yes"}}},
    )
    with pytest.raises(ValueError, match="float_activity"):
        BridgeConfig.load(path=tmp_path / "session_bridge.toml")


def _serve_runtime_coordinator(db, monkeypatch, *, float_activity: bool):
    from dataclasses import replace

    from session_bridge.catalog import UnifiedCatalog
    from session_bridge.cli import ProductionBackend
    from session_bridge.config import BridgeConfig, ClaudeVisibilityConfig

    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    backend = ProductionBackend(
        replace(
            BridgeConfig(),
            claude_visibility=replace(
                ClaudeVisibilityConfig(),
                enabled=True,
                float_activity=float_activity,
            ),
        )
    )
    backend._db = db
    backend._store = store
    backend._catalog = UnifiedCatalog(db, store)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable", lambda name: (name,)
    )
    return backend._provider_runtime(
        targets=False, catalog_only=False, providers=(Provider.CLAUDE,)
    )


def test_serve_runtime_wires_mirror_float_when_configured(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = Path("C:/sentinel-registry")
    monkeypatch.setattr(
        "session_bridge.cli.discover_ccd_registry_root", lambda base: sentinel
    )
    coordinator = _serve_runtime_coordinator(db, monkeypatch, float_activity=True)
    assert isinstance(coordinator._mirror_float, ClaudeMirrorFloatWorker)
    assert coordinator._mirror_float._registry_root == sentinel


def test_serve_runtime_omits_mirror_float_when_disabled(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _serve_runtime_coordinator(db, monkeypatch, float_activity=False)
    assert coordinator._mirror_float is None


def test_hermes_source_resolves_via_canonical_fallback(db, tmp_path) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _, mirror_path = _seed_visible_mirror(
        db,
        store,
        tmp_path,
        provider=Provider.HERMES,
        source_last_active=5_000.0,
        mirror_mtime=1_000.0,
    )
    worker = ClaudeMirrorFloatWorker(store, min_interval_seconds=900.0)

    result = worker.run_once()

    assert result == {
        "examined": 1,
        "floated": 1,
        "skipped": 0,
        "registered": 0,
        "throttled": 0,
    }
    assert mirror_path.stat().st_mtime == pytest.approx(5_000.0)
