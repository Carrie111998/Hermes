from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hermes_state import SCHEMA_VERSION, SessionDB
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityIdentity,
    derive_claude_visibility_identity,
)
from session_bridge.claude_visibility_codes import (
    CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES,
)
from session_bridge.mirror import (
    DiscoveryMode,
    EligibilityContext,
    MirrorCandidate,
    MirrorPolicy,
    enqueue_mirror_job,
)
from session_bridge.catalog import UnifiedCatalog
from session_bridge.models import (
    BridgeMarkerPayload,
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarHydrationState,
    SidebarJobState,
    canonical_session_id,
    encode_bridge_marker,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    sidebar_bridge_id,
    sidebar_create_recovery_key,
    sidebar_idempotency_key,
)
from session_bridge.sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
from session_bridge.store import (
    SIDEBAR_EXCLUSION_REASONS,
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_RETRYABLE_ERRORS,
    LocalSessionOwnsCanonicalId,
    SessionBridgeStore,
    _EXTERNAL_ACTIVITY_KEY_PREFIX,
    _external_activity_state_key,
    sidebar_precreate_terminal_evidence_digest,
    sidebar_terminal_evidence_digest,
    sidebar_unbound_terminal_evidence_digest,
)
from session_bridge.worktree import WorktreeSnapshot, capture_worktree_snapshot


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _import_projection(
    native_id: str, *, provider: Provider = Provider.CODEX
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"[Codex] imported {native_id}",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=20.0,
        messages=(),
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
        git_branch=None,
        parser_version=3,
        origin_kind=OriginKind.NATIVE,
        origin_bridge_id=None,
    )


def test_hermes_visibility_sources_dedupe_across_profiles(db, monkeypatch) -> None:
    """The same Hermes session in two databases must not kill the whole lane.

    The root/profile split writes some sessions to both this store's own db and
    a profile db. Raising on that made 7 duplicated identities out of 20,846
    surface as a generic `provider_degraded`, disabling Claude visibility
    entirely. The primary copy (yielded first) wins.
    """
    from contextlib import contextmanager

    class _Projection:
        def __init__(self, last_active: float) -> None:
            self.last_active = last_active

    class _Source:
        def __init__(self, session_id: str, last_active: float) -> None:
            self.source_session_id = session_id
            self.projection = _Projection(last_active)

    shared = "20260806_175034_62c1bb"
    primary = [_Source(shared, 30.0), _Source("only-primary", 20.0)]
    profile = [_Source(shared, 10.0), _Source("only-profile", 25.0)]
    batches = iter([primary, profile])

    @contextmanager
    def _fake_databases(self):
        yield [("default", object(), False), ("main", object(), True)]

    monkeypatch.setattr(
        SessionBridgeStore, "_native_hermes_databases", _fake_databases
    )
    monkeypatch.setattr(
        SessionBridgeStore, "_profile_catalog_compatible", lambda self, database: True
    )
    monkeypatch.setattr(
        SessionBridgeStore,
        "_list_claude_visibility_hermes_sources_from_db",
        lambda self, database, *, after, limit: next(batches),
    )
    monkeypatch.setattr(
        SessionBridgeStore, "_recorded_worktree_snapshots", lambda self, identities: {}
    )
    monkeypatch.setattr(
        SessionBridgeStore,
        "_with_recorded_worktree_snapshot",
        lambda self, source, snapshot: source,
    )
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)

    result = store.list_claude_visibility_hermes_sources(0.0, None)

    ids = [source.source_session_id for source in result]
    assert ids == [shared, "only-profile", "only-primary"]
    # the PRIMARY copy survived, not the profile one
    assert next(s for s in result if s.source_session_id == shared).projection.last_active == 30.0


def test_upsert_rejects_canonical_id_owned_by_a_different_source(db) -> None:
    """A genuine collision -- another provider's session holds the canonical id."""
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    projection = _import_projection("019d8db5-d883-7813-b219-09645d5c1aac")
    store.upsert_projection(projection)
    session_id = canonical_session_id(Provider.CODEX, projection.native_id)
    with db._lock:
        db._conn.execute(
            "DELETE FROM external_sessions WHERE session_id = ?", (session_id,)
        )
        db._conn.execute(
            "UPDATE sessions SET source = 'claude' WHERE id = ?", (session_id,)
        )
        db._conn.commit()

    with pytest.raises(ValueError, match="session ID collision") as excinfo:
        store.upsert_projection(projection)
    # A foreign owner is a real conflict, NOT the benign local-owner condition.
    assert not isinstance(excinfo.value, LocalSessionOwnsCanonicalId)


def test_locally_owned_canonical_id_is_distinguishable(db) -> None:
    """Hermes' own codex-provider sessions occupy `codex:<native_id>` too.

    Both systems legitimately claim one id for the same underlying thread, with
    different message representations, so the local row is authoritative and is
    never adopted. It must be distinguishable from a real collision so scans can
    exclude it instead of failing -- 1,586 such rows were degrading the Codex
    provider and starving every downstream lane.
    """
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    projection = _import_projection("019f6bfe-86a5-70d3-8b3f-abe07500dd98")
    store.upsert_projection(projection)
    session_id = canonical_session_id(Provider.CODEX, projection.native_id)
    with db._lock:
        db._conn.execute(
            "DELETE FROM external_sessions WHERE session_id = ?", (session_id,)
        )
        db._conn.commit()

    with pytest.raises(LocalSessionOwnsCanonicalId):
        store.upsert_projection(projection)

    # and the local content is left untouched
    with db._lock:
        row = db._conn.execute(
            "SELECT source FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    assert row["source"] == Provider.CODEX.value


def _claude_visibility_identity(
    suffix: str = "1",
) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
    candidate = ClaudeVisibilityCandidate(
        source_session_id=f"codex:source-{suffix}",
        source_provider=Provider.CODEX,
        native_name=f"[Codex] Request {suffix}",
        source_cwd="C:/work/project",
        git_root="C:/work/project",
        git_branch="main",
        git_head=f"head-{suffix}",
        worktree_id=f"worktree-{suffix}",
        eligible_at=100.0,
    )
    return candidate, derive_claude_visibility_identity(
        candidate, _CLAUDE_MARKER_SECRET
    )


def _claude_characterization_identity(
    operation_id: str,
) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
    candidate = ClaudeVisibilityCandidate(
        source_session_id=f"codex:{operation_id}",
        source_provider=Provider.CODEX,
        native_name="[Codex] Verify native Claude session visibility and exact-ID resume metadata.",
        source_cwd=f"C:/characterization/claude-visibility-{operation_id}",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    return candidate, derive_claude_visibility_identity(
        candidate, _CLAUDE_MARKER_SECRET
    )


def _claude_visibility_hermes_identity(
    suffix: str,
) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
    candidate = ClaudeVisibilityCandidate(
        source_session_id=f"hermes-source-{suffix}",
        source_provider=Provider.HERMES,
        native_name=f"[Hermes] Request {suffix}",
        source_cwd="C:/work/project",
        git_root="C:/work/project",
        git_branch="main",
        git_head=f"head-{suffix}",
        worktree_id=f"worktree-{suffix}",
        eligible_at=100.0,
    )
    return candidate, derive_claude_visibility_identity(
        candidate, _CLAUDE_MARKER_SECRET
    )


_CLAUDE_MARKER_SECRET = b"store-claude-visibility-secret"


def _assert_authenticated_claude_lineage_cursor(
    cursor: object,
    *,
    mode: str,
    after_visible_at: float,
    after_job_id: str,
    high_water_visible_at: float,
    high_water_job_id: str,
) -> None:
    assert isinstance(cursor, Mapping)
    assert set(cursor) == {
        "version",
        "schema_version",
        "operation",
        "mode",
        "after_visible_at",
        "after_job_id",
        "high_water_visible_at",
        "high_water_job_id",
        "signature",
    }
    assert cursor["version"] == 1
    assert cursor["schema_version"] == SCHEMA_VERSION
    assert cursor["operation"] == "claude_visibility_lineage_reconcile"
    assert cursor["mode"] == mode
    assert cursor["after_visible_at"] == after_visible_at
    assert cursor["after_job_id"] == after_job_id
    assert cursor["high_water_visible_at"] == high_water_visible_at
    assert cursor["high_water_job_id"] == high_water_job_id
    signature = cursor["signature"]
    assert isinstance(signature, str)
    assert len(signature) == 64
    assert set(signature) <= set("0123456789abcdef")


def _enqueue_claude_visibility_job(
    store: SessionBridgeStore,
    candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity,
) -> dict[str, object]:
    return store.enqueue_claude_visibility_job(
        candidate, identity, _CLAUDE_MARKER_SECRET
    )


def _seed_claude_visibility_native_source(
    db: SessionDB,
    store: SessionBridgeStore,
    candidate: ClaudeVisibilityCandidate,
) -> None:
    if candidate.source_provider is Provider.CODEX:
        store.upsert_projection(
            _projection(
                _message(f"source-{candidate.source_session_id}", "meaningful request"),
                provider=Provider.CODEX,
                native_id=candidate.source_session_id.removeprefix("codex:"),
            )
        )
        return
    db.create_session(
        candidate.source_session_id,
        source="cli",
        cwd=candidate.source_cwd,
    )


def _seed_profile_native_hermes_source(
    profile_path,
    candidate: ClaudeVisibilityCandidate,
    *,
    source: str = "tui",
) -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_db = SessionDB(profile_path)
    try:
        profile_db.create_session(
            candidate.source_session_id,
            source,
            cwd=candidate.source_cwd,
        )
        profile_db.append_message(
            candidate.source_session_id,
            "user",
            "meaningful profile request",
            timestamp=100.0,
        )
    finally:
        profile_db.close()


def _seed_exact_profile_shadow(
    db: SessionDB,
    candidate: ClaudeVisibilityCandidate,
    *,
    profile: str = "main",
    extra_config: Mapping[str, object] | None = None,
) -> None:
    model_config = {"_session_bridge_profile": profile, **(extra_config or {})}
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO sessions (
                   id, source, model_config, started_at, cwd
               ) VALUES (?, 'session_bridge_profile', ?, 100, ?)""",
            (
                candidate.source_session_id,
                json.dumps(model_config, sort_keys=True, separators=(",", ":")),
                candidate.source_cwd,
            ),
        )
    )


def _profile_aware_claude_visibility_store(
    db: SessionDB,
    profile_paths: tuple[tuple[str, Path], ...],
) -> SessionBridgeStore:
    return SessionBridgeStore(
        db,
        clock=lambda: 200.0,
        local_timezone=timezone.utc,
        hermes_profile_db_paths=lambda: profile_paths,
    )


def _corrupt_claude_visibility_source_identity(
    db: SessionDB,
    candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity,
    case: str,
) -> None:
    source_session_id = candidate.source_session_id

    def _write(conn) -> None:
        if case == "job_provider":
            conn.execute(
                "UPDATE session_claude_visibility_jobs SET source_provider = ? WHERE id = ?",
                (
                    (
                        Provider.HERMES.value
                        if candidate.source_provider is Provider.CODEX
                        else Provider.CODEX.value
                    ),
                    identity.job_id,
                ),
            )
        elif case == "session_source":
            conn.execute(
                "UPDATE sessions SET source = ? WHERE id = ?",
                (
                    "cli"
                    if candidate.source_provider is Provider.CODEX
                    else Provider.CODEX.value,
                    source_session_id,
                ),
            )
        elif case == "external_provider":
            conn.execute(
                "UPDATE external_sessions SET provider = 'claude' WHERE session_id = ?",
                (source_session_id,),
            )
        elif case == "external_native_id":
            conn.execute(
                "UPDATE external_sessions SET native_id = 'wrong-native-id' WHERE session_id = ?",
                (source_session_id,),
            )
        elif case == "origin_kind":
            conn.execute(
                "UPDATE external_sessions SET origin_kind = 'bridge_placeholder' WHERE session_id = ?",
                (source_session_id,),
            )
        elif case == "origin_bridge_id":
            conn.execute(
                "UPDATE external_sessions SET origin_bridge_id = 'wrong-origin' WHERE session_id = ?",
                (source_session_id,),
            )
        elif case == "unexpected_external":
            conn.execute(
                """INSERT INTO external_sessions (
                       session_id, provider, native_id, native_status,
                       first_indexed_at, last_indexed_at, parser_version,
                       origin_kind, origin_bridge_id
                   ) VALUES (?, 'codex', ?, 'active', 1, 1, 1, 'native', NULL)""",
                (source_session_id, f"unexpected-{source_session_id}"),
            )
        elif case == "incoming_lineage":
            origin_id = f"origin-{source_session_id}"
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', 1)",
                (origin_id,),
            )
            conn.execute(
                """INSERT INTO session_links (
                       id, from_session_id, to_session_id, relation, bridge_id,
                       created_at
                   ) VALUES (?, ?, ?, 'continues', ?, 1)""",
                (
                    f"incoming-{identity.job_id}",
                    origin_id,
                    source_session_id,
                    f"incoming-{identity.bridge_id}",
                ),
            )
        else:
            raise AssertionError(case)

    db._execute_write(_write)


def _seed_unlinked_claude_visibility_lineage(
    db: SessionDB,
    store: SessionBridgeStore,
    *,
    suffix: str,
    visible_at: float,
    source_provider: Provider = Provider.CODEX,
) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message(f"target-{suffix}", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?, visible_at = ?,
                   updated_at = ? WHERE id = ?""",
            ("e" * 64, visible_at, visible_at, identity.job_id),
        )
    )
    return candidate, identity


def test_fresh_schema_has_current_version_and_sidebar_terminal_ledgers(db) -> None:
    assert _rows(db, "SELECT version FROM schema_version") == [
        {"version": SCHEMA_VERSION}
    ]
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_exclusions",),
    ) == [{"name": "session_sidebar_exclusions"}]
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_terminal_resolutions",),
    ) == [{"name": "session_sidebar_terminal_resolutions"}]
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_claude_visibility_characterization_events",),
    ) == [{"name": "session_claude_visibility_characterization_events"}]
    assert "indexed_at" in {
        row["name"]
        for row in _rows(db, 'PRAGMA table_info("session_sidebar_jobs")')
    }
    assert [
        row["name"]
        for row in _rows(
            db, 'PRAGMA table_info("session_sidebar_terminal_resolutions")'
        )
    ] == [
        "job_id",
        "idempotency_key",
        "source_session_id",
        "bridge_id",
        "codex_thread_id",
        "failure_state",
        "failure_code",
        "failure_attempts",
        "failure_next_attempt_at",
        "failure_updated_at",
        "resolution_code",
        "evidence_kind",
        "evidence_version",
        "evidence_digest",
        "resolved_at",
    ]
    foreign_keys = _rows(
        db, 'PRAGMA foreign_key_list("session_sidebar_terminal_resolutions")'
    )
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["table"] == "session_sidebar_jobs"
    assert foreign_keys[0]["from"] == "job_id"
    assert foreign_keys[0]["to"] == "id"
    assert foreign_keys[0]["on_update"] == "RESTRICT"
    assert foreign_keys[0]["on_delete"] == "RESTRICT"
    assert {
        ("job_id",),
        ("idempotency_key",),
        ("source_session_id",),
        ("bridge_id",),
        ("codex_thread_id",),
    } <= _unique_column_sets(db, "session_sidebar_terminal_resolutions")
    table_sql = _rows(
        db,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_terminal_resolutions",),
    )[0]["sql"]
    normalized = " ".join(table_sql.split())
    for required in (
        "failure_state = 'sidebar_failed'",
        "failure_code = 'native_create_ambiguous'",
        "failure_attempts >= 0",
        "resolution_code = 'native_thread_unrecoverable'",
        "evidence_kind = 'codex_app_server_read_not_loaded_resume_no_rollout'",
        "evidence_version = 1",
        "length(evidence_digest) = 64",
        "evidence_digest NOT GLOB '*[^0-9a-f]*'",
        "resolved_at >= failure_updated_at",
    ):
        assert required in normalized
    assert {
        row["name"]
        for row in _rows(
            db,
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_terminal_resolutions'",
        )
    } == {
        "trg_session_sidebar_terminal_resolutions_no_replacement",
        "trg_session_sidebar_terminal_resolutions_no_update",
        "trg_session_sidebar_terminal_resolutions_no_delete",
        "trg_session_sidebar_terminal_resolutions_no_precreate_overlap",
        "trg_session_sidebar_terminal_resolutions_no_unbound_overlap",
    }
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_precreate_resolutions",),
    ) == [{"name": "session_sidebar_precreate_resolutions"}]
    assert [
        row["name"]
        for row in _rows(
            db, 'PRAGMA table_info("session_sidebar_precreate_resolutions")'
        )
    ] == [
        "job_id",
        "idempotency_key",
        "source_session_id",
        "bridge_id",
        "failure_state",
        "failure_code",
        "failure_attempts",
        "failure_next_attempt_at",
        "failure_updated_at",
        "cutover_applied_at",
        "reservation_reserved_at",
        "resolution_code",
        "evidence_kind",
        "evidence_version",
        "evidence_digest",
        "resolved_at",
    ]
    assert {
        row["name"]
        for row in _rows(
            db,
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_precreate_resolutions'",
        )
    } == {
        "trg_session_sidebar_precreate_resolutions_no_replacement",
        "trg_session_sidebar_precreate_resolutions_no_update",
        "trg_session_sidebar_precreate_resolutions_no_delete",
        "trg_session_sidebar_precreate_resolutions_no_unbound_overlap",
    }
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_unbound_resolutions",),
    ) == [{"name": "session_sidebar_unbound_resolutions"}]
    assert [
        row["name"]
        for row in _rows(
            db, 'PRAGMA table_info("session_sidebar_unbound_resolutions")'
        )
    ] == [
        "job_id",
        "idempotency_key",
        "source_session_id",
        "bridge_id",
        "failure_state",
        "failure_code",
        "failure_attempts",
        "failure_next_attempt_at",
        "failure_updated_at",
        "reservation_reserved_at",
        "resolution_code",
        "evidence_kind",
        "evidence_version",
        "evidence_digest",
        "resolved_at",
    ]
    assert {
        row["name"]
        for row in _rows(
            db,
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_unbound_resolutions'",
        )
    } == {
        "trg_session_sidebar_unbound_resolutions_no_replacement",
        "trg_session_sidebar_unbound_resolutions_no_update",
        "trg_session_sidebar_unbound_resolutions_no_delete",
    }


def test_sidebar_indexed_at_column_reconciles_into_existing_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-sidebar-indexed-at.db"
    initial = SessionDB(path)
    initial.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE session_sidebar_jobs DROP COLUMN indexed_at")
        assert "indexed_at" not in {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("session_sidebar_jobs")'
            ).fetchall()
        }

    reopened = SessionDB(path)
    try:
        assert "indexed_at" in {
            row["name"]
            for row in _rows(
                reopened,
                'PRAGMA table_info("session_sidebar_jobs")',
            )
        }
    finally:
        reopened.close()


def test_current_database_additively_repairs_terminal_ledger_without_data_loss(
    tmp_path,
) -> None:
    path = tmp_path / "existing-current.db"
    first = SessionDB(path)
    try:
        first.ensure_session("hermes:existing-terminal-ledger", source="cli")
        store = SessionBridgeStore(first)
        queued = store.enqueue_sidebar_job(
            SidebarCandidate(
                source_session_id="hermes:existing-terminal-ledger",
                provider=Provider.HERMES,
                bridge_id=sidebar_bridge_id("hermes:existing-terminal-ledger"),
                title="[Hermes] existing database",
                cwd=str(tmp_path),
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
                eligible_at=100.0,
            )
        )
        before = _rows(
            first,
            "SELECT * FROM session_sidebar_jobs WHERE id = ?",
            (queued["id"],),
        )
        assert _rows(first, "SELECT version FROM schema_version") == [
            {"version": SCHEMA_VERSION}
        ]
    finally:
        first.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE session_sidebar_terminal_resolutions")
        connection.execute("DROP TABLE session_sidebar_precreate_resolutions")
        connection.execute("DROP TABLE session_sidebar_unbound_resolutions")
        connection.commit()
    finally:
        connection.close()

    for _ in range(2):
        reopened = SessionDB(path)
        try:
            assert _rows(reopened, "SELECT version FROM schema_version") == [
                {"version": SCHEMA_VERSION}
            ]
            assert (
                _rows(
                    reopened,
                    "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                    (queued["id"],),
                )
                == before
            )
            assert _rows(
                reopened,
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("session_sidebar_terminal_resolutions",),
            ) == [{"name": "session_sidebar_terminal_resolutions"}]
            assert {
                row["name"]
                for row in _rows(
                    reopened,
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'session_sidebar_terminal_resolutions'",
                )
            } == {
                "trg_session_sidebar_terminal_resolutions_no_replacement",
                "trg_session_sidebar_terminal_resolutions_no_update",
                "trg_session_sidebar_terminal_resolutions_no_delete",
                "trg_session_sidebar_terminal_resolutions_no_precreate_overlap",
                "trg_session_sidebar_terminal_resolutions_no_unbound_overlap",
            }
            assert _rows(
                reopened,
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("session_sidebar_precreate_resolutions",),
            ) == [{"name": "session_sidebar_precreate_resolutions"}]
            assert {
                row["name"]
                for row in _rows(
                    reopened,
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'session_sidebar_precreate_resolutions'",
                )
            } == {
                "trg_session_sidebar_precreate_resolutions_no_replacement",
                "trg_session_sidebar_precreate_resolutions_no_update",
                "trg_session_sidebar_precreate_resolutions_no_delete",
                "trg_session_sidebar_precreate_resolutions_no_unbound_overlap",
            }
            assert _rows(
                reopened,
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("session_sidebar_unbound_resolutions",),
            ) == [{"name": "session_sidebar_unbound_resolutions"}]
            assert {
                row["name"]
                for row in _rows(
                    reopened,
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'session_sidebar_unbound_resolutions'",
                )
            } == {
                "trg_session_sidebar_unbound_resolutions_no_replacement",
                "trg_session_sidebar_unbound_resolutions_no_update",
                "trg_session_sidebar_unbound_resolutions_no_delete",
            }
        finally:
            reopened.close()


_CLAUDE_VISIBILITY_JOB_COLUMNS = [
    "id",
    "source_session_id",
    "bridge_id",
    "idempotency_key",
    "reserved_claude_uuid",
    "native_name",
    "source_provider",
    "source_cwd",
    "git_root",
    "git_branch",
    "git_head",
    "worktree_id",
    "signed_marker",
    "state",
    "attempts",
    "next_attempt_at",
    "lease_digest",
    "lease_expires_at",
    "lease_kind",
    "error_code",
    "error_detail",
    "completion_digest",
    "operator_cleared_at",
    "eligible_at",
    "created_at",
    "updated_at",
    "visible_at",
]
_CLAUDE_REGISTRATION_USAGE_COLUMNS = [
    "local_day",
    "job_id",
    "attempt_ordinal",
    "reserved_estimated_cost_usd",
    "reserved_at",
]
_CLAUDE_RECONCILIATION_COLUMNS = [
    "job_id",
    "reserved_claude_uuid",
    "attempt_ordinal",
    "outcome",
    "evidence_digest",
    "checked_at",
    "consumed_at",
]


def _unique_column_sets(db: SessionDB, table: str) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for index in _rows(db, f'PRAGMA index_list("{table}")'):
        if index["unique"]:
            columns = _rows(db, f'PRAGMA index_info("{index["name"]}")')
            unique_sets.add(tuple(column["name"] for column in columns))
    return unique_sets


def _insert_claude_visibility_job(
    db: SessionDB,
    *,
    job_id: str,
    source_session_id: str,
    state: str = "claude_pending",
    bridge_id: str | None = None,
    idempotency_key: str | None = None,
    reserved_claude_uuid: str | None = None,
) -> None:
    visible = state == "claude_visible"
    db._conn.execute(
        """INSERT INTO session_claude_visibility_jobs (
           id, source_session_id, bridge_id, idempotency_key,
           reserved_claude_uuid, native_name, source_provider, source_cwd,
           signed_marker, state, next_attempt_at, completion_digest,
           eligible_at, created_at, updated_at, visible_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'hermes', ?, ?, ?, 1, ?, 1, 1, 1, ?)""",
        (
            job_id,
            source_session_id,
            bridge_id or f"bridge:{job_id}",
            idempotency_key or f"idempotency:{job_id}",
            reserved_claude_uuid or f"00000000-0000-4000-8000-{job_id[-12:]:0>12}",
            f"Native {job_id}",
            "C:/source",
            f"signed:{job_id}",
            state,
            f"completion:{job_id}" if visible else None,
            2 if visible else None,
        ),
    )


def _insert_claude_registration_usage(db: SessionDB, job_id: str) -> None:
    db._conn.execute(
        """INSERT INTO session_claude_registration_usage (
           local_day, job_id, attempt_ordinal, reserved_estimated_cost_usd,
           reserved_at
           ) VALUES ('2026-07-17', ?, 1, '0.02', 1)""",
        (job_id,),
    )


def _create_exact_v23_claude_db(path) -> sqlite3.Connection:
    """Create the Claude tables exactly as shipped by schema v23."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (23);
        CREATE TABLE session_claude_visibility_jobs (
            id TEXT PRIMARY KEY,
            source_session_id TEXT NOT NULL UNIQUE,
            bridge_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            reserved_claude_uuid TEXT NOT NULL UNIQUE,
            native_name TEXT NOT NULL,
            source_provider TEXT NOT NULL CHECK (source_provider IN ('codex', 'hermes')),
            source_cwd TEXT NOT NULL,
            git_root TEXT,
            git_branch TEXT,
            git_head TEXT,
            worktree_id TEXT,
            signed_marker TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'claude_pending', 'claude_leased', 'claude_retry',
                    'claude_visible', 'claude_failed'
                )
            ),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at REAL NOT NULL,
            lease_digest TEXT,
            lease_expires_at REAL,
            error_code TEXT,
            error_detail TEXT,
            completion_digest TEXT,
            eligible_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            visible_at REAL,
            CHECK (
                (state = 'claude_leased' AND lease_digest IS NOT NULL
                 AND lease_expires_at IS NOT NULL)
                OR (state != 'claude_leased' AND lease_digest IS NULL
                    AND lease_expires_at IS NULL)
            ),
            CHECK (
                state != 'claude_visible'
                OR (completion_digest IS NOT NULL AND visible_at IS NOT NULL)
            )
        );
        CREATE TABLE session_claude_registration_usage (
            local_day TEXT NOT NULL,
            job_id TEXT NOT NULL REFERENCES session_claude_visibility_jobs(id),
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
            reserved_estimated_cost_usd TEXT NOT NULL,
            reserved_at REAL NOT NULL,
            UNIQUE(job_id, attempt_ordinal)
        );
        """
    )
    return connection


def _insert_v23_claude_job(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    state: str,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> str:
    job_id = f"v23-{suffix}"
    leased = state == "claude_leased"
    connection.execute(
        """INSERT INTO session_claude_visibility_jobs (
           id, source_session_id, bridge_id, idempotency_key,
           reserved_claude_uuid, native_name, source_provider, source_cwd,
           signed_marker, state, attempts, next_attempt_at, lease_digest,
           lease_expires_at, error_code, error_detail, eligible_at, created_at,
           updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'hermes', 'C:/source', ?, ?, 1, 100,
                     ?, ?, ?, ?, 1, 1, 100)""",
        (
            job_id,
            f"hermes:{job_id}",
            f"bridge:{job_id}",
            f"idempotency:{job_id}",
            f"00000000-0000-4000-8000-{suffix[-12:]:0>12}",
            f"Native {job_id}",
            f"signed:{job_id}",
            state,
            "legacy-lease" if leased else None,
            999 if leased else None,
            error_code,
            error_detail,
        ),
    )
    return job_id


def test_fresh_claude_visibility_schema_has_exact_columns_states_and_uniques(
    db,
) -> None:
    assert [
        row["name"]
        for row in _rows(db, 'PRAGMA table_info("session_claude_visibility_jobs")')
    ] == _CLAUDE_VISIBILITY_JOB_COLUMNS
    table_sql = _rows(
        db,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_claude_visibility_jobs",),
    )[0]["sql"]
    normalized = " ".join(table_sql.split())
    assert (
        "state IN ( 'claude_pending', 'claude_leased', 'claude_retry', "
        "'claude_visible', 'claude_failed' )"
    ) in normalized
    unique_sets = _unique_column_sets(db, "session_claude_visibility_jobs")
    assert {
        ("source_session_id",),
        ("bridge_id",),
        ("idempotency_key",),
        ("reserved_claude_uuid",),
    } <= unique_sets
    assert [
        row["name"]
        for row in _rows(
            db,
            'PRAGMA table_info("session_claude_registration_usage")',
        )
    ] == _CLAUDE_REGISTRATION_USAGE_COLUMNS
    assert [
        row["name"]
        for row in _rows(
            db, 'PRAGMA table_info("session_claude_visibility_reconciliations")'
        )
    ] == _CLAUDE_RECONCILIATION_COLUMNS
    assert (
        "job_id",
        "attempt_ordinal",
    ) in _unique_column_sets(db, "session_claude_registration_usage")
    assert (
        _rows(
            db,
            'PRAGMA foreign_key_list("session_claude_visibility_jobs")',
        )
        == []
    )


def test_claude_registration_usage_migrates_existing_database_idempotently(
    tmp_path,
) -> None:
    path = tmp_path / "existing-state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE preexisting_sentinel (value TEXT NOT NULL);
        INSERT INTO preexisting_sentinel (value) VALUES ('preserved');
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (21);
        """
    )
    connection.close()

    first = SessionDB(path)
    first.close()
    migrated = SessionDB(path)
    try:
        assert _rows(migrated, "SELECT * FROM preexisting_sentinel") == [
            {"value": "preserved"}
        ]
        assert [
            row["name"]
            for row in _rows(
                migrated,
                'PRAGMA table_info("session_claude_visibility_jobs")',
            )
        ] == _CLAUDE_VISIBILITY_JOB_COLUMNS
        assert [
            row["name"]
            for row in _rows(
                migrated,
                'PRAGMA table_info("session_claude_registration_usage")',
            )
        ] == _CLAUDE_REGISTRATION_USAGE_COLUMNS
        assert (
            "job_id",
            "attempt_ordinal",
        ) in _unique_column_sets(migrated, "session_claude_registration_usage")
        assert (
            _rows(
                migrated,
                'PRAGMA foreign_key_list("session_claude_visibility_jobs")',
            )
            == []
        )
    finally:
        migrated.close()


def test_v24_migration_invalidates_legacy_authorization_and_ambiguous_lease(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-v23.db"
    original = _create_exact_v23_claude_db(path)
    _insert_v23_claude_job(
        original,
        suffix="absence",
        state="claude_retry",
        error_code="exact_id_absent_reconciled",
        error_detail="absence-evidence:" + "a" * 64,
    )
    _insert_v23_claude_job(original, suffix="lease", state="claude_leased")
    original.commit()
    original.close()

    migrated = SessionDB(path)
    try:
        rows = _rows(
            migrated,
            """SELECT id, state, lease_digest, lease_kind, error_code, error_detail
               FROM session_claude_visibility_jobs ORDER BY id""",
        )
        assert all(row["state"] == "claude_retry" for row in rows)
        assert all(
            row["lease_digest"] is None and row["lease_kind"] is None for row in rows
        )
        assert any(
            "authorization sentinel invalidated" in row["error_detail"] for row in rows
        )
        assert any("active lease invalidated" in row["error_detail"] for row in rows)
        assert (
            _rows(migrated, "SELECT * FROM session_claude_visibility_reconciliations")
            == []
        )
        with pytest.raises(sqlite3.IntegrityError, match="lease fields"):
            migrated._conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_leased', lease_digest = 'digest',
                       lease_expires_at = 200, lease_kind = 'bogus'
                   WHERE id = 'v23-absence'"""
            )
    finally:
        migrated.close()


def test_v24_bridge_migration_is_independent_of_fts_schema_version(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy-v23-no-fts.db"
    original = _create_exact_v23_claude_db(path)
    job_id = _insert_v23_claude_job(original, suffix="nofts", state="claude_leased")
    # Simulate an independently stale core/FTS migration counter. The bridge
    # tables themselves retain the exact v23 shape under test.
    original.execute("UPDATE schema_version SET version = 10")
    original.commit()
    original.close()
    monkeypatch.setattr(SessionDB, "_sqlite_supports_fts5", lambda self, cursor: False)

    first = SessionDB(path)
    assert _rows(first, "SELECT version FROM schema_version") == [{"version": 10}]
    assert {
        row["migration_name"]
        for row in _rows(
            first,
            "SELECT migration_name FROM session_bridge_migrations",
        )
    } == {
        "claude_visibility_security_v24",
        "claude_auth_recovery_call_started_v25",
        "claude_characterization_abort_max_attempts_v27",
        "claude_characterization_events_v28",
        "claude_characterization_event_orphan_quarantine_v29",
        "sidebar_resolution_orphan_quarantine_v30",
        "sidebar_reconciliation_proof_orphan_quarantine_v31",
    }
    first._conn.execute(
        """UPDATE session_claude_visibility_jobs
           SET state = 'claude_leased', lease_digest = 'valid-digest',
               lease_expires_at = 999, lease_kind = 'launch'
           WHERE id = ?""",
        (job_id,),
    )
    first._conn.commit()
    first.close()

    reopened = SessionDB(path)
    try:
        assert _rows(reopened, "SELECT version FROM schema_version") == [
            {"version": 10}
        ]
        assert _rows(
            reopened,
            """SELECT state, lease_digest, lease_kind
               FROM session_claude_visibility_jobs WHERE id = ?""",
            (job_id,),
        ) == [
            {
                "state": "claude_leased",
                "lease_digest": "valid-digest",
                "lease_kind": "launch",
            }
        ]
    finally:
        reopened.close()


def test_auth_recovery_call_started_upgrade_is_explicit_and_queryable(tmp_path) -> None:
    path = tmp_path / "auth-recovery-c214.db"
    legacy = SessionDB(path)
    legacy._conn.executescript(
        """DROP TABLE session_claude_auth_recoveries;
        CREATE TABLE session_claude_auth_recoveries (
            job_id TEXT PRIMARY KEY,
            reserved_claude_uuid TEXT NOT NULL,
            operation_id TEXT NOT NULL UNIQUE,
            evidence_digest TEXT NOT NULL,
            prompt_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('leased', 'retry', 'completed')),
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
            next_attempt_at REAL NOT NULL,
            lease_digest TEXT UNIQUE,
            lease_expires_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            FOREIGN KEY (job_id, reserved_claude_uuid)
                REFERENCES session_claude_visibility_jobs(id, reserved_claude_uuid)
                ON DELETE CASCADE
        );
        DELETE FROM session_bridge_migrations
        WHERE migration_name = 'claude_auth_recovery_call_started_v25';
        """
    )
    legacy._conn.commit()
    legacy.close()

    upgraded = SessionDB(path)
    try:
        assert "call_started_at" in {
            row["name"]
            for row in _rows(
                upgraded, 'PRAGMA table_info("session_claude_auth_recoveries")'
            )
        }
        assert _rows(
            upgraded,
            """SELECT migration_name FROM session_bridge_migrations
               WHERE migration_name = 'claude_auth_recovery_call_started_v25'""",
        ) == [{"migration_name": "claude_auth_recovery_call_started_v25"}]

        store = SessionBridgeStore(
            upgraded, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        candidate, identity = _claude_visibility_identity("c214-upgrade")
        _enqueue_claude_visibility_job(store, candidate, identity)
        launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
        store.fail_claude_visibility_job(
            identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
        )
        recovery = store.claim_claude_auth_recovery(
            job_id=identity.job_id,
            reserved_claude_uuid=identity.claude_uuid,
            operation_id="6ae1c4de-0000-4000-8000-000000000009",
            evidence_digest="a" * 64,
            prompt_digest="b" * 64,
            now=100.0,
            lease_seconds=10,
            daily_limit=25,
            cost_limit="1.00",
            reserved_cost="0.02",
            max_attempts=5,
        )
        started = store.begin_claude_auth_recovery(
            identity.job_id, recovery["lease_digest"]
        )
        assert started["call_started_at"] == 100.0
    finally:
        upgraded.close()


@pytest.mark.parametrize("legacy_value", ["0.02", "0.0200000", "1000000.000000"])
def test_v24_migration_canonicalizes_safe_legacy_money(tmp_path, legacy_value) -> None:
    path = tmp_path / f"legacy-money-{legacy_value}.db"
    original = _create_exact_v23_claude_db(path)
    job_id = _insert_v23_claude_job(original, suffix="money", state="claude_pending")
    original.execute(
        """INSERT INTO session_claude_registration_usage
           VALUES ('2026-07-17', ?, 1, ?, 1)""",
        (job_id, legacy_value),
    )
    original.commit()
    original.close()

    migrated = SessionDB(path)
    try:
        assert _rows(
            migrated,
            "SELECT reserved_estimated_cost_usd FROM session_claude_registration_usage",
        ) == [{"reserved_estimated_cost_usd": f"{Decimal(legacy_value):.6f}"}]
    finally:
        migrated.close()


@pytest.mark.parametrize("legacy_value", ["0.0000001", "1000000.000001", "NaN"])
def test_v24_migration_rejects_unsafe_legacy_money(tmp_path, legacy_value) -> None:
    path = tmp_path / f"unsafe-money-{legacy_value}.db"
    original = _create_exact_v23_claude_db(path)
    job_id = _insert_v23_claude_job(original, suffix="unsafe", state="claude_pending")
    original.execute(
        """INSERT INTO session_claude_registration_usage
           VALUES ('2026-07-17', ?, 1, ?, 1)""",
        (job_id, legacy_value),
    )
    original.commit()
    original.close()

    with pytest.raises(
        RuntimeError,
        match=r"unsafe session_claude_registration_usage row 1.*reserved_estimated_cost_usd",
    ):
        SessionDB(path)


def test_claude_reconciliation_table_enforces_outcome_and_evidence_checks(
    db: SessionDB,
) -> None:
    candidate, identity = _claude_visibility_identity()
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    _enqueue_claude_visibility_job(store, candidate, identity)

    for outcome, evidence in (("forged", "a" * 64), ("absent", "not-sha256")):
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                """INSERT INTO session_claude_visibility_reconciliations
                   VALUES (?, ?, 0, ?, ?, 100, NULL)""",
                (identity.job_id, identity.claude_uuid, outcome, evidence),
            )
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            """INSERT INTO session_claude_visibility_reconciliations
               VALUES (?, ?, 0, 'absent', ?, 100, NULL)""",
            (
                identity.job_id,
                "00000000-0000-4000-8000-000000000000",
                "a" * 64,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="lease fields"):
        db._conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_leased', lease_digest = 'forged',
                   lease_expires_at = 200
               WHERE id = ?""",
            (identity.job_id,),
        )


@pytest.mark.parametrize("state", ["claude_pending", "claude_visible"])
def test_delete_session_preserves_claude_visibility_job_and_usage_audit(
    db: SessionDB,
    state: str,
) -> None:
    source_session_id = f"hermes:delete-{state}"
    job_id = f"job-delete-{state}"
    db.create_session(source_session_id, source="cli")
    _insert_claude_visibility_job(
        db,
        job_id=job_id,
        source_session_id=source_session_id,
        state=state,
    )
    _insert_claude_registration_usage(db, job_id)
    reserved_uuid = _rows(
        db,
        "SELECT reserved_claude_uuid FROM session_claude_visibility_jobs WHERE id = ?",
        (job_id,),
    )[0]["reserved_claude_uuid"]
    db._conn.execute(
        """INSERT INTO session_claude_visibility_reconciliations
           VALUES (?, ?, 0, 'exact_match', ?, 1, NULL)""",
        (job_id, reserved_uuid, "a" * 64),
    )
    assert _rows(db, "PRAGMA foreign_keys") == [{"foreign_keys": 1}]

    assert db.delete_session(source_session_id) is True

    assert db.get_session(source_session_id) is None
    assert _rows(
        db,
        "SELECT id, source_session_id, state FROM session_claude_visibility_jobs",
    ) == [
        {
            "id": job_id,
            "source_session_id": source_session_id,
            "state": state,
        }
    ]
    assert _rows(
        db,
        "SELECT job_id, attempt_ordinal FROM session_claude_registration_usage",
    ) == [{"job_id": job_id, "attempt_ordinal": 1}]
    assert _rows(
        db,
        "SELECT job_id, outcome FROM session_claude_visibility_reconciliations",
    ) == [{"job_id": job_id, "outcome": "exact_match"}]


@pytest.mark.parametrize("state", ["claude_pending", "claude_visible"])
def test_prune_sessions_preserves_claude_visibility_job_and_usage_audit(
    db: SessionDB,
    state: str,
) -> None:
    source_session_id = f"hermes:prune-{state}"
    job_id = f"job-prune-{state}"
    db.create_session(source_session_id, source="cli")
    db.end_session(source_session_id, "completed")
    _insert_claude_visibility_job(
        db,
        job_id=job_id,
        source_session_id=source_session_id,
        state=state,
    )
    _insert_claude_registration_usage(db, job_id)
    reserved_uuid = _rows(
        db,
        "SELECT reserved_claude_uuid FROM session_claude_visibility_jobs WHERE id = ?",
        (job_id,),
    )[0]["reserved_claude_uuid"]
    db._conn.execute(
        """INSERT INTO session_claude_visibility_reconciliations
           VALUES (?, ?, 0, 'exact_match', ?, 1, NULL)""",
        (job_id, reserved_uuid, "a" * 64),
    )
    assert _rows(db, "PRAGMA foreign_keys") == [{"foreign_keys": 1}]

    assert (
        db.prune_sessions(
            older_than_days=None,
            started_before=10**12,
        )
        == 1
    )

    assert db.get_session(source_session_id) is None
    assert _rows(
        db,
        "SELECT id, source_session_id, state FROM session_claude_visibility_jobs",
    ) == [
        {
            "id": job_id,
            "source_session_id": source_session_id,
            "state": state,
        }
    ]
    assert _rows(
        db,
        "SELECT job_id, attempt_ordinal FROM session_claude_registration_usage",
    ) == [{"job_id": job_id, "attempt_ordinal": 1}]
    assert _rows(
        db,
        "SELECT job_id, outcome FROM session_claude_visibility_reconciliations",
    ) == [{"job_id": job_id, "outcome": "exact_match"}]


def test_claude_visibility_job_rejects_invalid_state(db: SessionDB) -> None:
    db.create_session("hermes:invalid-state", source="cli")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_claude_visibility_job(
            db,
            job_id="job-invalid-state",
            source_session_id="hermes:invalid-state",
            state="invalid-state",
        )


@pytest.mark.parametrize(
    "column",
    [
        "source_session_id",
        "bridge_id",
        "idempotency_key",
        "reserved_claude_uuid",
    ],
)
def test_claude_visibility_job_rejects_each_duplicate_identity(
    db: SessionDB,
    column: str,
) -> None:
    db.create_session("hermes:unique-one", source="cli")
    db.create_session("hermes:unique-two", source="cli")
    first = {
        "source_session_id": "hermes:unique-one",
        "bridge_id": "bridge:shared",
        "idempotency_key": "idempotency:shared",
        "reserved_claude_uuid": "00000000-0000-4000-8000-000000000001",
    }
    second = {
        "source_session_id": "hermes:unique-two",
        "bridge_id": "bridge:different",
        "idempotency_key": "idempotency:different",
        "reserved_claude_uuid": "00000000-0000-4000-8000-000000000002",
    }
    second[column] = first[column]
    _insert_claude_visibility_job(db, job_id="job-unique-one", **first)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_claude_visibility_job(db, job_id="job-unique-two", **second)


def test_claude_registration_usage_rejects_duplicate_job_attempt(
    db: SessionDB,
) -> None:
    db.create_session("hermes:usage-unique", source="cli")
    _insert_claude_visibility_job(
        db,
        job_id="job-usage-unique",
        source_session_id="hermes:usage-unique",
    )
    _insert_claude_registration_usage(db, "job-usage-unique")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_claude_registration_usage(db, "job-usage-unique")


def test_mirror_worker_lock_serializes_independent_store_instances(db) -> None:
    first = SessionBridgeStore(db)
    second_db = SessionDB(db.db_path)
    second = SessionBridgeStore(second_db)
    first_lock = None
    second_lock = None
    try:
        first_lock = first.try_acquire_mirror_worker_lock()
        assert first_lock is not None
        assert second.try_acquire_mirror_worker_lock() is None

        first_lock.release()
        first_lock = None
        second_lock = second.try_acquire_mirror_worker_lock()
        assert second_lock is not None
    finally:
        if first_lock is not None:
            first_lock.release()
        if second_lock is not None:
            second_lock.release()
        second_db.close()


def test_sidebar_worker_lock_serializes_independent_store_instances(db) -> None:
    first = SessionBridgeStore(db)
    second_db = SessionDB(db.db_path)
    second = SessionBridgeStore(second_db)
    first_lock = None
    second_lock = None
    try:
        first_lock = first.try_acquire_sidebar_worker_lock()
        assert first_lock is not None
        assert second.try_acquire_sidebar_worker_lock() is None

        first_lock.release()
        first_lock = None
        second_lock = second.try_acquire_sidebar_worker_lock()
        assert second_lock is not None
    finally:
        if first_lock is not None:
            first_lock.release()
        if second_lock is not None:
            second_lock.release()
        second_db.close()


def _message(
    event_id: str,
    content: str,
    *,
    role: str | None = None,
    tool_calls=None,
    tool_call_id: str | None = None,
    timestamp: float | None = None,
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role=role or ("assistant" if tool_calls else "user"),
        content=content,
        timestamp=10.0 + len(event_id) if timestamp is None else timestamp,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def _projection(
    *messages: ProjectedMessage,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "native-1",
    cursor: str = "cursor-1",
    native_hash: str = "hash-1",
    last_active: float = 20.0,
    git_branch: str | None = None,
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
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=cursor,
        native_hash=native_hash,
        git_branch=git_branch,
        parser_version=3,
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _rows(db: SessionDB, sql: str, params=()):
    with db._lock:
        return [dict(row) for row in db._conn.execute(sql, params).fetchall()]


def _seed_continuation_snapshot_rows(db: SessionDB, count: int) -> None:
    def _write(conn):
        for index in range(count):
            suffix = f"{index:04d}"
            bridge_id = f"bridge-{suffix}"
            source_id = f"claude:source-{suffix}"
            target_id = f"codex:target-{suffix}"
            pack_id = f"pack-{suffix}"
            conn.executemany(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                [(source_id, "claude", 1.0), (target_id, "codex", 1.0)],
            )
            conn.execute(
                """INSERT INTO session_context_packs (
                   id, bridge_id, source_session_id, target_session_id,
                   source_cursor, source_hash, budget_chars, payload,
                   created_at, immutable_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pack_id,
                    bridge_id,
                    source_id,
                    target_id,
                    f"source-cursor-{suffix}",
                    f"source-hash-{suffix}",
                    4000,
                    "handoff",
                    2.0,
                    3.0,
                ),
            )
            conn.execute(
                """INSERT INTO session_links (
                   id, from_session_id, to_session_id, relation, bridge_id,
                   source_cursor, source_hash, created_at, hydrated_at
                   ) VALUES (?, ?, ?, 'continues', ?, ?, ?, ?, ?)""",
                (
                    f"link-{suffix}",
                    source_id,
                    target_id,
                    bridge_id,
                    f"source-cursor-{suffix}",
                    f"source-hash-{suffix}",
                    2.0,
                    3.0,
                ),
            )
            snapshot = {
                "version": 1,
                "pack_id": pack_id,
                "source_session_id": source_id,
                "source_cursor": f"source-cursor-{suffix}",
                "source_hash": f"source-hash-{suffix}",
                "target_session_id": target_id,
                "target_cursor": f"target-cursor-{suffix}",
                "target_hash": f"target-hash-{suffix}",
            }
            conn.execute(
                "INSERT INTO session_bridge_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    f"session-bridge:continuation:{bridge_id}",
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                    4.0,
                ),
            )

    db._execute_write(_write)


def _enqueue_manual_job(
    store: SessionBridgeStore,
    source_session_id: str,
    target_provider: Provider,
    *,
    generation: int = 1,
):
    return enqueue_mirror_job(
        store,
        source_session_id,
        target_provider,
        policy=MirrorPolicy(generation=generation),
        manual_authorized=True,
    )


def _enqueue_automatic_job(
    store: SessionBridgeStore,
    projection: SessionProjection,
    *,
    now: float,
):
    policy = MirrorPolicy(automatic_creation=True)
    source_session_id = canonical_session_id(projection.provider, projection.native_id)
    target = (
        Provider.CODEX if projection.provider is Provider.CLAUDE else Provider.CLAUDE
    )
    candidate = MirrorCandidate(
        source_session_id=source_session_id,
        target_provider=target,
        last_active=projection.last_active,
        projection=projection,
    )
    context = EligibilityContext(
        now=now,
        discovery_mode=DiscoveryMode.INITIAL_BACKFILL,
        continuous_watermark=None,
        existing_target_mappings=frozenset(),
        policy=policy,
    )
    return enqueue_mirror_job(
        store,
        source_session_id,
        target,
        policy=policy,
        candidate=candidate,
        context=context,
    )


def _capture_mapping_selects(db: SessionDB, operation) -> list[str]:
    statements: list[str] = []
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.set_trace_callback(statements.append)
    try:
        operation()
    finally:
        with db._lock:
            conn.set_trace_callback(None)
    return [
        statement
        for statement in statements
        if "FROM external_message_map" in statement
        and statement.lstrip().upper().startswith("SELECT")
    ]


def test_list_native_projections_is_newest_first_with_minimal_exact_evidence(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    older = _projection(
        _message(
            "assistant-event",
            "assistant transcript must not be materialized",
            role="assistant",
            timestamp=19.0,
        ),
        replace(
            _message(
                "shared-event",
                "   ",
                role="user",
                timestamp=20.0,
            ),
            ordinal=0,
        ),
        replace(
            _message(
                "shared-event",
                "first meaningful user turn",
                role="user",
                timestamp=21.0,
            ),
            ordinal=1,
        ),
        _message(
            "later-user-event",
            "later meaningful user turn",
            role="user",
            timestamp=22.0,
        ),
        native_id="older",
        last_active=40.0,
        cursor="older-cursor",
        native_hash="older-hash",
        git_branch="feature/older",
    )
    newer = _projection(
        _message("new-event", "newer", timestamp=30.0),
        provider=Provider.CODEX,
        native_id="newer",
        last_active=50.0,
        cursor="newer-cursor",
        native_hash="newer-hash",
    )
    bridged = _projection(
        _message("bridged-event", "must be excluded", timestamp=90.0),
        native_id="bridged",
        last_active=90.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-excluded",
    )
    for projection in (older, newer, bridged):
        store.upsert_projection(projection)

    projections = store.list_native_projections(after=35.0, limit=2)

    assert [projection.native_id for projection in projections] == ["newer", "older"]
    reconstructed = projections[1]
    assert reconstructed.provider is Provider.CLAUDE
    assert reconstructed.last_active == 40.0
    assert reconstructed.native_cursor == "older-cursor"
    assert reconstructed.native_hash == "older-hash"
    assert reconstructed.git_branch == "feature/older"
    assert tuple(reconstructed.messages) == (
        ProjectedMessage(
            native_event_id="shared-event",
            ordinal=1,
            role="user",
            content="f",
            timestamp=21.0,
        ),
    )


def test_list_native_projections_filters_all_eligibility_predicates_before_limit(db):
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    projections = (
        _projection(
            _message("bridge", "bridge"),
            native_id="bridge",
            last_active=100.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-origin",
        ),
        _projection(
            _message("mapped", "mapped"),
            native_id="mapped",
            last_active=90.0,
        ),
        _projection(
            _message("active-job", "active job"),
            native_id="active-job",
            last_active=80.0,
        ),
        _projection(
            _message("assistant-only", "not a user", role="assistant"),
            _message("blank-user", "\u2003\n\t", role="user"),
            native_id="meaningless",
            last_active=70.0,
        ),
        _projection(
            _message("eligible", "eligible"),
            native_id="eligible",
            last_active=60.0,
        ),
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="mapped-target",
            last_active=50.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="mapped-target-origin",
        ),
    )
    for projection in projections:
        store.upsert_projection(projection)
    store.create_link(
        SessionLink(
            id="mapped-link",
            from_session_id="claude:mapped",
            to_session_id="codex:mapped-target",
            relation=Relation.MIRRORS,
            bridge_id="mapped-bridge",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )
    _enqueue_manual_job(store, "claude:active-job", Provider.CODEX)

    page = store.list_native_projections(after=0.0, limit=1)

    assert [projection.native_id for projection in page] == ["eligible"]
    assert page.has_more is False
    assert page.next_cursor is None


def test_list_native_projections_keyset_page_prevents_older_candidate_starvation(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id, last_active in (
        ("newest", 50.0),
        ("tie-a", 40.0),
        ("tie-b", 40.0),
        ("oldest", 30.0),
    ):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                native_id=native_id,
                last_active=last_active,
            )
        )

    first = store.list_native_projections(after=0.0, limit=2)
    second = store.list_native_projections(
        after=0.0,
        limit=2,
        cursor=first.next_cursor,
    )

    assert [projection.native_id for projection in first] == ["newest", "tie-a"]
    assert first.has_more is True
    assert first.next_cursor == (40.0, "claude:tie-a")
    assert [projection.native_id for projection in second] == ["tie-b", "oldest"]
    assert second.has_more is False
    assert second.next_cursor is None


def test_list_native_projections_applies_inclusive_cutoff_and_bounded_limit(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("boundary", "boundary"), last_active=40.0)
    )

    assert [
        projection.native_id
        for projection in store.list_native_projections(after=40.0, limit=1)
    ] == ["native-1"]
    with pytest.raises(ValueError, match="after"):
        store.list_native_projections(after=float("nan"), limit=1)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_native_projections(after=0.0, limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_native_projections(after=0.0, limit=1001)


def test_list_existing_target_mappings_returns_exact_provider_pairs(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("source", "source"), native_id="source")
    )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target",
        )
    )
    store.create_link(
        SessionLink(
            id="mapping-link",
            from_session_id="claude:source",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="mapping-bridge",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )

    assert store.list_existing_target_mappings([
        "claude:source",
        "claude:unmapped",
    ]) == frozenset({("claude:source", Provider.CODEX)})
    assert store.list_existing_target_mappings([]) == frozenset()
    with pytest.raises(TypeError, match="sequence"):
        store.list_existing_target_mappings("claude:source")
    with pytest.raises(ValueError, match="at most 1000"):
        store.list_existing_target_mappings([
            f"claude:source-{index}" for index in range(1001)
        ])


def test_first_import_is_idempotent_and_append_only(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    first = _projection(_message("e1", "first"), _message("e2", "second"))

    first_result = store.upsert_projection(first)
    replay_result = store.upsert_projection(first)
    appended_result = store.upsert_projection(
        replace(
            first,
            messages=(*first.messages, _message("e3", "third")),
            native_cursor="cursor-2",
            native_hash="hash-2",
        )
    )

    assert first_result.first_seen is True
    assert first_result.inserted_messages == 2
    assert replay_result.first_seen is False
    assert replay_result.inserted_messages == 0
    assert appended_result.inserted_messages == 1
    assert db.session_count() == 1
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "first",
        "second",
        "third",
    ]
    assert len(_rows(db, "SELECT * FROM external_message_map")) == 3
    session = db.get_session("claude:native-1")
    assert session["source"] == "claude"
    assert session["title"] == "claude session"
    assert session["message_count"] == 3
    assert (
        store.get_external_session("claude:native-1")["last_native_cursor"]
        == "cursor-2"
    )


def test_delta_mapping_lookup_queries_only_incoming_composite_identity(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(*(_message(f"history-{index}", "old") for index in range(25)))
    )

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                _message("delta-1", "new"),
                cursor="delta-cursor",
                native_hash="delta-hash",
            )
        ),
    )

    assert len(selects) == 1
    assert "(native_event_id, ordinal) IN" in selects[0]
    assert "delta-1" in selects[0]
    assert "history-1" not in selects[0]


def test_empty_delta_skips_message_mapping_lookup(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("history", "old")))

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                cursor="empty-cursor",
                native_hash="empty-hash",
            )
        ),
    )

    assert selects == []


def test_rebuild_skips_message_mapping_lookup(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("history", "old")))

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                _message("rebuilt", "new"),
                cursor="rebuilt-cursor",
                native_hash="rebuilt-hash",
            ),
            rebuild=True,
        ),
    )

    assert selects == []


def test_mapping_lookup_chunks_large_incoming_projection(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    incoming = tuple(
        _message(f"incoming-{index}", "new", role="assistant") for index in range(401)
    )

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(_projection(*incoming)),
    )

    assert len(selects) == 2
    assert all("(native_event_id, ordinal) IN" in statement for statement in selects)


def test_projection_persists_updates_and_preserves_git_branch_on_none(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    store.upsert_projection(
        _projection(_message("e1", "first"), git_branch="feature/first")
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/first"

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-2",
            last_active=21.0,
            git_branch="feature/second",
        )
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/second"

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-3",
            last_active=22.0,
        )
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/second"


@pytest.mark.parametrize("git_branch", ["", " \t "])
def test_projection_preserves_git_branch_on_blank_delta(db, git_branch):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("e1", "first"), git_branch="feature/current")
    )

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-2",
            last_active=21.0,
            git_branch=git_branch,
        )
    )

    assert db.get_session("claude:native-1")["git_branch"] == "feature/current"


def test_projection_strips_a_non_empty_git_branch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            git_branch="  feature/normalized\t",
        )
    )

    assert db.get_session("claude:native-1")["git_branch"] == "feature/normalized"


def test_rebuild_replaces_only_mapped_messages_and_preserves_first_index_time(db):
    now = [100.0]
    store = SessionBridgeStore(db, clock=lambda: now[0])
    store.upsert_projection(
        _projection(_message("e1", "old one"), _message("e2", "old two"))
    )
    db.append_message(
        "claude:native-1",
        "user",
        "ordinary Hermes continuation",
        timestamp=1_000.0,
    )
    now[0] = 200.0

    result = store.upsert_projection(
        _projection(
            _message("e1", "rebuilt one"),
            cursor="cursor-rebuilt",
            native_hash="hash-rebuilt",
        ),
        rebuild=True,
    )

    assert result.rebuilt is True
    assert result.inserted_messages == 1
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "ordinary Hermes continuation",
        "rebuilt one",
    ]
    external = store.get_external_session("claude:native-1")
    assert external["first_indexed_at"] == 100.0
    assert external["last_indexed_at"] == 200.0
    assert db.get_session("claude:native-1")["message_count"] == 2


def test_projection_rolls_back_every_bridge_write_on_mid_insert_failure(
    db, monkeypatch
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("e1", "committed"), git_branch="feature/committed")
    )
    original = db._insert_message_rows_with_ids

    def insert_then_fail(conn, session_id, messages):
        original(conn, session_id, messages[:1])
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(db, "_insert_message_rows_with_ids", insert_then_fail)

    with pytest.raises(RuntimeError, match="injected write failure"):
        store.upsert_projection(
            _projection(
                _message("e1", "committed"),
                _message("e2", "must roll back"),
                cursor="cursor-uncommitted",
                native_hash="hash-uncommitted",
                git_branch="feature/must-roll-back",
            )
        )

    assert [m["content"] for m in db.get_messages("claude:native-1")] == ["committed"]
    assert len(_rows(db, "SELECT * FROM external_message_map")) == 1
    assert (
        store.get_external_session("claude:native-1")["last_native_cursor"]
        == "cursor-1"
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/committed"


@pytest.mark.parametrize("existing_source", ["cli", "claude"])
def test_projection_refuses_to_adopt_an_untracked_colliding_session(
    db, existing_source
):
    db.create_session("claude:native-1", existing_source)
    db.append_message("claude:native-1", "user", "must survive")
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="collision"):
        store.upsert_projection(_projection(_message("e1", "external")))

    assert db.get_session("claude:native-1")["source"] == existing_source
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "must survive"
    ]
    assert store.get_external_session("claude:native-1") is None


def test_projection_refuses_a_different_external_identity_collision(db):
    db.create_session("claude:native-1", "codex")
    with db._lock:
        db._conn.execute(
            """INSERT INTO external_sessions (
               session_id, provider, native_id, first_indexed_at, last_indexed_at,
               parser_version, origin_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("claude:native-1", "codex", "different", 1.0, 1.0, 1, "native"),
        )
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="collision"):
        store.upsert_projection(_projection(_message("e1", "external")))

    assert store.get_external_session("claude:native-1")["provider"] == "codex"


def test_projection_normalizes_native_identity_before_persisting_and_comparing(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    first = store.upsert_projection(
        _projection(_message("e1", "external"), native_id="  native-1  ")
    )
    replay = store.upsert_projection(
        _projection(_message("e1", "external"), native_id="native-1")
    )

    assert first.session_id == "claude:native-1"
    assert replay.inserted_messages == 0
    assert store.get_external_session("claude:native-1")["native_id"] == "native-1"


def test_projection_preserves_placeholder_on_replay_and_non_human_native_deltas(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    placeholder = _projection(
        _message("e1", "placeholder"),
        cursor="placeholder-cursor",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )
    store.upsert_projection(placeholder)
    exact_replay = store.upsert_projection(placeholder)

    native_user_replay = store.upsert_projection(
        _projection(
            _message("e1", "placeholder"),
            cursor="native-replay-cursor",
            native_hash="native-replay-hash",
        )
    )
    store.upsert_projection(
        _projection(
            _message("e2", "assistant delta", role="assistant"),
            _message(
                "e3",
                "",
                role="assistant",
                tool_calls=[{"id": "call-1", "name": "Read"}],
            ),
            _message(
                "e4",
                "tool result delta",
                role="tool",
                tool_call_id="call-1",
            ),
            cursor="native-non-human-cursor",
            native_hash="native-non-human-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert exact_replay.inserted_messages == 0
    assert native_user_replay.inserted_messages == 0
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "native-non-human-cursor"


def test_indexed_claude_visibility_target_creates_unified_catalog_lineage(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("lineage")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-lineage",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 100.0
    )

    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )

    links = _rows(
        db,
        """SELECT from_session_id, to_session_id, relation, bridge_id
             FROM session_links WHERE bridge_id = ?""",
        (identity.bridge_id,),
    )
    assert links == [
        {
            "from_session_id": candidate.source_session_id,
            "to_session_id": f"claude:{identity.claude_uuid}",
            "relation": "mirrors",
            "bridge_id": identity.bridge_id,
        }
    ]
    assert UnifiedCatalog(db, store).resolve_continuation(
        session_id=candidate.source_session_id,
        bridge_id=None,
        target_provider="claude",
    ) == {
        "bridge_id": identity.bridge_id,
        "source_session_id": candidate.source_session_id,
        "target_session_id": f"claude:{identity.claude_uuid}",
        "target_provider": "claude",
    }


def test_resolve_continuation_retries_transient_lock(db, monkeypatch) -> None:
    """The resume read must wait out a transient WAL lock, not error on it.

    Regression for the desktop "Resume failed: handler error: database is
    locked". ``resolve_continuation`` previously ran a bare unwrapped SELECT,
    so a single ``database is locked`` from a concurrent checkpoint bubbled
    straight up. It now routes through ``SessionDB._execute_read``, which
    retries with jitter like the write path.
    """
    import hermes_state

    monkeypatch.setattr(hermes_state.time, "sleep", lambda *_a, **_k: None)

    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("transient-lock")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-transient-lock",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 100.0
    )
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )

    # Inject exactly one transient lock error on the continuation SELECT.
    # sqlite3.Connection.execute is read-only, so wrap the connection in a
    # thin proxy that trips once, then delegates everything to the real one.
    class _FlakyConn:
        def __init__(self, real):
            self._real = real
            self.failed = False

        def execute(self, sql, *args, **kwargs):
            if "FROM session_links AS link" in sql and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    flaky = _FlakyConn(db._conn)
    monkeypatch.setattr(db, "_conn", flaky)

    resolved = UnifiedCatalog(db, store).resolve_continuation(
        session_id=candidate.source_session_id,
        bridge_id=None,
        target_provider="claude",
    )
    assert flaky.failed is True  # the lock actually fired
    assert resolved["target_session_id"] == f"claude:{identity.claude_uuid}"


def test_claude_visibility_commit_finalizes_preindexed_target_lineage_atomically(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("preindexed-lineage")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-preindexed-lineage",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )

    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    committed = store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 100.0
    )

    assert committed["state"] == "claude_visible"
    assert _rows(
        db,
        """SELECT from_session_id, to_session_id, relation, bridge_id
             FROM session_links WHERE bridge_id = ?""",
        (identity.bridge_id,),
    ) == [
        {
            "from_session_id": candidate.source_session_id,
            "to_session_id": f"claude:{identity.claude_uuid}",
            "relation": "mirrors",
            "bridge_id": identity.bridge_id,
        }
    ]
    assert (
        UnifiedCatalog(db, store).resolve_continuation(
            session_id=candidate.source_session_id,
            bridge_id=None,
            target_provider="claude",
        )["target_session_id"]
        == f"claude:{identity.claude_uuid}"
    )


@pytest.mark.parametrize("source_provider", [Provider.CODEX, Provider.HERMES])
def test_claude_visibility_commit_accepts_exact_native_or_profile_shadow_source(
    db: SessionDB,
    tmp_path: Path,
    source_provider: Provider,
) -> None:
    suffix = f"profile-commit-{source_provider.value}"
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    if source_provider is Provider.HERMES:
        _seed_profile_native_hermes_source(profile_path, candidate)
        _seed_exact_profile_shadow(db, candidate)
    else:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        SessionDB(profile_path).close()
    store = _profile_aware_claude_visibility_store(db, (("main", profile_path),))
    if source_provider is Provider.CODEX:
        _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("profile-target", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    claim = store.claim_claude_visibility_job(200.0, 60, 25, "0.50", "0.02")

    committed = store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 200.0
    )

    assert committed["state"] == "claude_visible"
    assert _rows(
        db,
        "SELECT from_session_id, to_session_id FROM session_links WHERE bridge_id = ?",
        (identity.bridge_id,),
    ) == [
        {
            "from_session_id": candidate.source_session_id,
            "to_session_id": f"claude:{identity.claude_uuid}",
        }
    ]


@pytest.mark.parametrize("source_provider", [Provider.CODEX, Provider.HERMES])
def test_claude_visibility_status_and_reconcile_accept_profile_aware_source(
    db: SessionDB,
    tmp_path: Path,
    source_provider: Provider,
) -> None:
    suffix = f"profile-reconcile-{source_provider.value}"
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    if source_provider is Provider.HERMES:
        _seed_profile_native_hermes_source(profile_path, candidate)
        _seed_exact_profile_shadow(db, candidate)
    else:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        SessionDB(profile_path).close()
    store = _profile_aware_claude_visibility_store(db, (("main", profile_path),))
    if source_provider is Provider.CODEX:
        _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("profile-history-target", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?, visible_at = 150,
                   updated_at = 150 WHERE id = ?""",
            ("b" * 64, identity.job_id),
        )
    )

    assert store.claude_visibility_status(200.0)["lineage"] == {
        "unlinked_visible": 1,
        "repairable": 1,
        "blocked": 0,
        "blocker_codes": {},
    }
    repaired = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )

    assert repaired["repaired"] == 1
    assert repaired["remaining"] == 0
    assert repaired["complete"] is True
    assert _rows(
        db,
        "SELECT bridge_id FROM session_links WHERE bridge_id = ?",
        (identity.bridge_id,),
    ) == [{"bridge_id": identity.bridge_id}]


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("extra_shadow_config", "claude_lineage_source_identity_mismatch"),
        ("wrong_profile", "claude_lineage_source_identity_mismatch"),
        ("duplicate_profile", "claude_lineage_source_identity_mismatch"),
        ("malformed_external_table", "claude_lineage_source_identity_mismatch"),
        ("malformed_links_table", "claude_lineage_source_identity_mismatch"),
        ("profile_external", "claude_lineage_source_provenance_mismatch"),
        ("profile_incoming", "claude_lineage_source_provenance_mismatch"),
    ],
)
def test_profile_shadow_source_mismatch_precedes_target_and_is_atomic(
    db: SessionDB,
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    candidate, identity = _claude_visibility_hermes_identity(f"profile-{case}")
    main_path = tmp_path / "profiles" / "main" / "state.db"
    _seed_profile_native_hermes_source(main_path, candidate)
    profile_paths: tuple[tuple[str, Path], ...] = (("main", main_path),)
    if case == "extra_shadow_config":
        _seed_exact_profile_shadow(db, candidate, extra_config={"extra": True})
    elif case == "wrong_profile":
        _seed_exact_profile_shadow(db, candidate, profile="other")
    else:
        _seed_exact_profile_shadow(db, candidate)
    if case == "duplicate_profile":
        other_path = tmp_path / "profiles" / "other" / "state.db"
        _seed_profile_native_hermes_source(other_path, candidate)
        profile_paths = (("main", main_path), ("other", other_path))
    elif case in {"malformed_external_table", "malformed_links_table"}:
        table = (
            "external_sessions"
            if case == "malformed_external_table"
            else "session_links"
        )
        with sqlite3.connect(main_path) as legacy_conn:
            legacy_conn.execute("PRAGMA foreign_keys = OFF")
            legacy_conn.execute(f"DROP TABLE {table}")
            legacy_conn.execute(f"CREATE TABLE {table} (legacy_id TEXT)")
    elif case in {"profile_external", "profile_incoming"}:
        profile_db = SessionDB(main_path)
        try:
            if case == "profile_external":
                profile_db._execute_write(
                    lambda conn: conn.execute(
                        """INSERT INTO external_sessions (
                               session_id, provider, native_id, native_status,
                               first_indexed_at, last_indexed_at, parser_version,
                               origin_kind, origin_bridge_id
                           ) VALUES (?, 'codex', ?, 'active', 1, 1, 1, 'native', NULL)""",
                        (candidate.source_session_id, f"external-{case}"),
                    )
                )
            else:
                profile_db.create_session("profile-origin", "tui", cwd="C:/work")
                profile_db._execute_write(
                    lambda conn: conn.execute(
                        """INSERT INTO session_links (
                               id, from_session_id, to_session_id, relation,
                               bridge_id, created_at
                           ) VALUES ('profile-incoming', 'profile-origin', ?,
                                     'continues', 'profile-incoming-bridge', 1)""",
                        (candidate.source_session_id,),
                    )
                )
        finally:
            profile_db.close()
    store = _profile_aware_claude_visibility_store(db, profile_paths)
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(200.0, 60, 25, "0.50", "0.02")
    before_job = _rows(
        db,
        """SELECT state, lease_digest, completion_digest, visible_at
             FROM session_claude_visibility_jobs WHERE id = ?""",
        (identity.job_id,),
    )[0]

    with pytest.raises(ValueError, match=expected_code):
        store.commit_claude_visibility_job(
            identity.job_id, claim.lease_digest, "c" * 64, 200.0
        )

    assert (
        _rows(
            db,
            """SELECT state, lease_digest, completion_digest, visible_at
             FROM session_claude_visibility_jobs WHERE id = ?""",
            (identity.job_id,),
        )[0]
        == before_job
    )
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )


@pytest.mark.parametrize("table", ["external_sessions", "session_links"])
def test_profile_shadow_status_and_reconcile_fail_closed_on_legacy_bridge_table(
    db: SessionDB,
    tmp_path: Path,
    table: str,
) -> None:
    candidate, identity = _claude_visibility_hermes_identity(f"legacy-profile-{table}")
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    _seed_profile_native_hermes_source(profile_path, candidate)
    _seed_exact_profile_shadow(db, candidate)
    store = _profile_aware_claude_visibility_store(db, (("main", profile_path),))
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message(f"legacy-target-{table}", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?,
                   visible_at = 150, updated_at = 150 WHERE id = ?""",
            ("d" * 64, identity.job_id),
        )
    )
    with sqlite3.connect(profile_path) as legacy_conn:
        legacy_conn.execute("PRAGMA foreign_keys = OFF")
        legacy_conn.execute(f"DROP TABLE {table}")
        legacy_conn.execute(f"CREATE TABLE {table} (legacy_id TEXT)")

    assert store.claude_visibility_status(200.0)["lineage"] == {
        "unlinked_visible": 1,
        "repairable": 0,
        "blocked": 1,
        "blocker_codes": {"claude_lineage_source_identity_mismatch": 1},
    }
    before = _rows(
        db,
        "SELECT id, bridge_id FROM session_links ORDER BY id",
    )
    reconciled = store.reconcile_claude_visibility_lineage(
        limit=1,
        marker_secret=_CLAUDE_MARKER_SECRET,
        apply=True,
    )

    assert reconciled["repaired"] == 0
    assert reconciled["blocker_codes"] == {"claude_lineage_source_identity_mismatch": 1}
    assert (
        _rows(
            db,
            "SELECT id, bridge_id FROM session_links ORDER BY id",
        )
        == before
        == []
    )


def test_claude_visibility_commit_rejects_invalid_digest_before_target_without_poisoning(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("invalid-digest-before-target")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-invalid-digest-before-target",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    before = _rows(
        db,
        """SELECT state, lease_digest, lease_expires_at, lease_kind,
                  completion_digest, visible_at
             FROM session_claude_visibility_jobs WHERE id = ?""",
        (identity.job_id,),
    )[0]

    with pytest.raises(ValueError, match="transcript digest"):
        store.commit_claude_visibility_job(
            identity.job_id,
            claim.lease_digest,
            "not-a-sha256-digest",
            100.0,
        )

    assert (
        _rows(
            db,
            """SELECT state, lease_digest, lease_expires_at, lease_kind,
                  completion_digest, visible_at
             FROM session_claude_visibility_jobs WHERE id = ?""",
            (identity.job_id,),
        )[0]
        == before
    )

    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )

    committed = store.commit_claude_visibility_job(
        identity.job_id,
        claim.lease_digest,
        "a" * 64,
        100.0,
    )
    assert committed["completion_digest"] == "a" * 64
    assert _rows(
        db,
        "SELECT from_session_id, to_session_id FROM session_links WHERE bridge_id = ?",
        (identity.bridge_id,),
    ) == [
        {
            "from_session_id": candidate.source_session_id,
            "to_session_id": f"claude:{identity.claude_uuid}",
        }
    ]


@pytest.mark.parametrize(
    ("source_provider", "case", "expected_code"),
    [
        (Provider.CODEX, "job_provider", "claude_lineage_source_identity_mismatch"),
        (Provider.CODEX, "session_source", "claude_lineage_source_identity_mismatch"),
        (
            Provider.CODEX,
            "external_provider",
            "claude_lineage_source_identity_mismatch",
        ),
        (
            Provider.CODEX,
            "external_native_id",
            "claude_lineage_source_identity_mismatch",
        ),
        (
            Provider.CODEX,
            "origin_kind",
            "claude_lineage_source_provenance_mismatch",
        ),
        (
            Provider.CODEX,
            "origin_bridge_id",
            "claude_lineage_source_provenance_mismatch",
        ),
        (Provider.HERMES, "job_provider", "claude_lineage_source_identity_mismatch"),
        (Provider.HERMES, "session_source", "claude_lineage_source_identity_mismatch"),
        (
            Provider.HERMES,
            "unexpected_external",
            "claude_lineage_source_identity_mismatch",
        ),
        (
            Provider.HERMES,
            "incoming_lineage",
            "claude_lineage_source_provenance_mismatch",
        ),
    ],
)
def test_claude_visibility_historical_lineage_validates_exact_native_source_identity(
    db: SessionDB,
    source_provider: Provider,
    case: str,
    expected_code: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    suffix = f"source-identity-{source_provider.value}-{case}"
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?, visible_at = 100,
                   updated_at = 100 WHERE id = ?""",
            ("b" * 64, identity.job_id),
        )
    )
    _corrupt_claude_visibility_source_identity(db, candidate, identity, case)

    result = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )

    assert result["scanned"] == 1
    assert result["repairable"] == 0
    assert result["repaired"] == 0
    assert result["remaining"] == 1
    assert result["blocker_codes"] == {expected_code: 1}
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )


@pytest.mark.parametrize("source_provider", [Provider.CODEX, Provider.HERMES])
@pytest.mark.parametrize("entrypoint", ["commit", "reconcile", "upsert"])
def test_claude_visibility_source_identity_mismatch_is_atomic_across_finalizers(
    db: SessionDB,
    source_provider: Provider,
    entrypoint: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    suffix = f"atomic-{source_provider.value}-{entrypoint}"
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    target = _projection(
        _message("target-user", "signed registration"),
        native_id=identity.claude_uuid,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    claim = None
    if entrypoint == "commit":
        store.upsert_projection(target)
        claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    elif entrypoint == "reconcile":
        store.upsert_projection(target)
        db._execute_write(
            lambda conn: conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', completion_digest = ?,
                       visible_at = 100, updated_at = 100 WHERE id = ?""",
                ("c" * 64, identity.job_id),
            )
        )
    else:
        db._execute_write(
            lambda conn: conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', completion_digest = ?,
                       visible_at = 100, updated_at = 100 WHERE id = ?""",
                ("c" * 64, identity.job_id),
            )
        )
    _corrupt_claude_visibility_source_identity(
        db, candidate, identity, "session_source"
    )
    before_job = _rows(
        db,
        "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    )[0]

    if entrypoint == "commit":
        assert claim is not None
        with pytest.raises(ValueError, match="claude_lineage_source_identity_mismatch"):
            store.commit_claude_visibility_job(
                identity.job_id, claim.lease_digest, "d" * 64, 100.0
            )
    elif entrypoint == "reconcile":
        result = store.reconcile_claude_visibility_lineage(
            limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
        )
        assert result["blocker_codes"] == {"claude_lineage_source_identity_mismatch": 1}
    else:
        with pytest.raises(ValueError, match="claude_lineage_source_identity_mismatch"):
            store.upsert_projection(target)

    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )[0]
        == before_job
    )
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )
    if entrypoint == "upsert":
        assert store.get_external_session(f"claude:{identity.claude_uuid}") is None


@pytest.mark.parametrize("source_provider", [Provider.CODEX, Provider.HERMES])
def test_claude_visibility_commit_before_target_rejects_source_mismatch_without_poisoning(
    db: SessionDB,
    source_provider: Provider,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    suffix = f"source-before-target-{source_provider.value}"
    candidate, identity = (
        _claude_visibility_identity(suffix)
        if source_provider is Provider.CODEX
        else _claude_visibility_hermes_identity(suffix)
    )
    _seed_claude_visibility_native_source(db, store, candidate)
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    _corrupt_claude_visibility_source_identity(
        db, candidate, identity, "session_source"
    )
    before = _rows(
        db,
        "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    )[0]

    with pytest.raises(ValueError, match="claude_lineage_source_identity_mismatch"):
        store.commit_claude_visibility_job(
            identity.job_id, claim.lease_digest, "f" * 64, 100.0
        )

    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )[0]
        == before
    )
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )

    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET source = ? WHERE id = ?",
            (
                (Provider.CODEX.value if source_provider is Provider.CODEX else "cli"),
                candidate.source_session_id,
            ),
        )
    )
    committed = store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "f" * 64, 100.0
    )
    assert committed["state"] == "claude_visible"
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        != []
    )


def test_claude_visibility_historical_lineage_reconciliation_is_bounded_and_idempotent(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("historical-lineage")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-historical-lineage",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "b" * 64, 100.0
    )
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
        )
    )

    dry_run = store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=False
    )
    assert dry_run == {
        "scanned": 1,
        "repairable": 1,
        "repaired": 0,
        "remaining": 1,
        "blocker_codes": {},
        "next_cursor": None,
        "has_more": False,
        "complete": False,
    }
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )

    applied = store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )
    replay = store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )
    assert applied == {
        "scanned": 1,
        "repairable": 1,
        "repaired": 1,
        "remaining": 0,
        "blocker_codes": {},
        "next_cursor": None,
        "has_more": False,
        "complete": True,
    }
    assert replay == {
        "scanned": 0,
        "repairable": 0,
        "repaired": 0,
        "remaining": 0,
        "blocker_codes": {},
        "next_cursor": None,
        "has_more": False,
        "complete": True,
    }
    assert store.claude_visibility_status(100.0)["lineage"] == {
        "unlinked_visible": 0,
        "repairable": 0,
        "blocked": 0,
        "blocker_codes": {},
    }


def test_claude_visibility_lineage_cursor_advances_past_blocker_to_later_repair(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0, local_timezone=timezone.utc)
    blocked_candidate, blocked_identity = _seed_unlinked_claude_visibility_lineage(
        db, store, suffix="cursor-blocker", visible_at=100.0
    )
    repair_candidate, repair_identity = _seed_unlinked_claude_visibility_lineage(
        db, store, suffix="cursor-repair", visible_at=101.0
    )
    _corrupt_claude_visibility_source_identity(
        db, blocked_candidate, blocked_identity, "session_source"
    )

    first = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )
    second = store.reconcile_claude_visibility_lineage(
        limit=1,
        marker_secret=_CLAUDE_MARKER_SECRET,
        apply=True,
        cursor=first["next_cursor"],
    )

    first_cursor = first.pop("next_cursor")
    assert first == {
        "scanned": 1,
        "repairable": 0,
        "repaired": 0,
        "remaining": 2,
        "blocker_codes": {"claude_lineage_source_identity_mismatch": 1},
        "has_more": True,
        "complete": False,
    }
    _assert_authenticated_claude_lineage_cursor(
        first_cursor,
        mode="apply",
        after_visible_at=100.0,
        after_job_id=blocked_identity.job_id,
        high_water_visible_at=101.0,
        high_water_job_id=repair_identity.job_id,
    )
    assert second == {
        "scanned": 1,
        "repairable": 1,
        "repaired": 1,
        "remaining": 1,
        "blocker_codes": {},
        "next_cursor": None,
        "has_more": False,
        "complete": False,
    }
    assert _rows(
        db,
        "SELECT bridge_id FROM session_links WHERE bridge_id = ?",
        (repair_identity.bridge_id,),
    ) == [{"bridge_id": repair_identity.bridge_id}]
    assert (
        _rows(
            db,
            "SELECT bridge_id FROM session_links WHERE bridge_id = ?",
            (blocked_identity.bridge_id,),
        )
        == []
    )


def test_claude_visibility_lineage_cursor_is_stable_for_equal_timestamps_and_replay(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0, local_timezone=timezone.utc)
    identities = [
        _seed_unlinked_claude_visibility_lineage(
            db, store, suffix=f"equal-cursor-{index}", visible_at=100.0
        )[1]
        for index in range(3)
    ]
    ordered = sorted(identity.job_id for identity in identities)

    first = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=False
    )
    second = store.reconcile_claude_visibility_lineage(
        limit=1,
        marker_secret=_CLAUDE_MARKER_SECRET,
        apply=False,
        cursor=first["next_cursor"],
    )
    replay = store.reconcile_claude_visibility_lineage(
        limit=1,
        marker_secret=_CLAUDE_MARKER_SECRET,
        apply=False,
        cursor=first["next_cursor"],
    )
    third = store.reconcile_claude_visibility_lineage(
        limit=1,
        marker_secret=_CLAUDE_MARKER_SECRET,
        apply=False,
        cursor=second["next_cursor"],
    )

    _assert_authenticated_claude_lineage_cursor(
        first["next_cursor"],
        mode="dry_run",
        after_visible_at=100.0,
        after_job_id=ordered[0],
        high_water_visible_at=100.0,
        high_water_job_id=ordered[2],
    )
    assert second == replay
    _assert_authenticated_claude_lineage_cursor(
        second["next_cursor"],
        mode="dry_run",
        after_visible_at=100.0,
        after_job_id=ordered[1],
        high_water_visible_at=100.0,
        high_water_job_id=ordered[2],
    )
    assert third["scanned"] == 1
    assert third["next_cursor"] is None
    assert third["has_more"] is False
    assert third["complete"] is False


def test_claude_visibility_lineage_cursor_is_mode_bound_before_mutation(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0, local_timezone=timezone.utc)
    identities = [
        _seed_unlinked_claude_visibility_lineage(
            db, store, suffix=f"cursor-mode-{index}", visible_at=100.0 + index
        )[1]
        for index in range(2)
    ]
    dry_run = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=False
    )
    before = _rows(
        db,
        "SELECT bridge_id FROM session_links ORDER BY bridge_id",
    )

    with pytest.raises(ValueError, match="Claude lineage reconciliation cursor"):
        store.reconcile_claude_visibility_lineage(
            limit=1,
            marker_secret=_CLAUDE_MARKER_SECRET,
            apply=True,
            cursor=dry_run["next_cursor"],
        )

    assert before == []
    assert (
        _rows(
            db,
            "SELECT bridge_id FROM session_links ORDER BY bridge_id",
        )
        == before
    )
    assert all(
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
        for identity in identities
    )


def test_claude_visibility_lineage_cursor_rejects_forged_anchored_values_before_mutation(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0, local_timezone=timezone.utc)
    identities = [
        _seed_unlinked_claude_visibility_lineage(
            db, store, suffix=f"cursor-forge-{index}", visible_at=100.0
        )[1]
        for index in range(3)
    ]
    ordered = sorted(identity.job_id for identity in identities)
    first = store.reconcile_claude_visibility_lineage(
        limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )
    cursor = dict(first["next_cursor"])
    forged_anchor = {**cursor, "after_job_id": ordered[1]}
    signature = str(cursor["signature"])
    forged_signature = {
        **cursor,
        "signature": signature[:-1] + ("0" if signature[-1] != "0" else "1"),
    }
    forged_schema = {**cursor, "schema_version": SCHEMA_VERSION + 1}
    before = _rows(
        db,
        "SELECT id, bridge_id FROM session_links ORDER BY id",
    )

    for forged in (forged_anchor, forged_signature, forged_schema):
        with pytest.raises(ValueError, match="Claude lineage reconciliation cursor"):
            store.reconcile_claude_visibility_lineage(
                limit=1,
                marker_secret=_CLAUDE_MARKER_SECRET,
                apply=True,
                cursor=forged,
            )

        assert (
            _rows(
                db,
                "SELECT id, bridge_id FROM session_links ORDER BY id",
            )
            == before
        )


@pytest.mark.parametrize(
    "cursor",
    [
        {},
        {"after_visible_at": 100.0},
        {
            "after_visible_at": float("nan"),
            "after_job_id": "job-a",
            "high_water_visible_at": 100.0,
            "high_water_job_id": "job-b",
        },
        {
            "after_visible_at": 101.0,
            "after_job_id": "job-b",
            "high_water_visible_at": 100.0,
            "high_water_job_id": "job-a",
        },
        {
            "after_visible_at": 100.0,
            "after_job_id": "forged-job",
            "high_water_visible_at": 100.0,
            "high_water_job_id": "also-forged",
        },
    ],
)
def test_claude_visibility_lineage_cursor_rejects_malformed_or_unanchored_values(
    db: SessionDB,
    cursor: object,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0, local_timezone=timezone.utc)

    with pytest.raises(ValueError, match="Claude lineage reconciliation cursor"):
        store.reconcile_claude_visibility_lineage(
            limit=1,
            marker_secret=_CLAUDE_MARKER_SECRET,
            apply=False,
            cursor=cursor,
        )


def test_claude_visibility_status_blocks_clean_gate_on_unlinked_visible_job(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("unlinked-status")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-unlinked-status",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "c" * 64, 100.0
    )

    assert store.claude_visibility_status(100.0)["lineage"] == {
        "unlinked_visible": 1,
        "repairable": 0,
        "blocked": 1,
        "blocker_codes": {"claude_lineage_target_missing": 1},
    }


def test_registered_characterization_is_exact_id_only_and_skips_production_lineage(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "11111111-1111-4111-8111-111111111111"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)

    recorded = store.record_claude_visibility_characterization(
        job_id=identity.job_id,
        operation_id=operation_id,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
        idempotency_key=identity.idempotency_key,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        evidence_digest="a" * 64,
        marker_secret=_CLAUDE_MARKER_SECRET,
        cleanup_completed=False,
    )

    assert recorded["status"] == "registered"
    assert (
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").status
        == "no_due_job"
    )
    assert store.inspect_due_claude_visibility_reconciliation(100.0).status == (
        "no_due_job"
    )
    assert store.claim_claude_visibility_reconciliation(100.0, 60).status == (
        "no_due_job"
    )
    claim = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        expected_job_id=identity.job_id,
    )
    assert claim.claimed
    assert claim.reserved_claude_uuid == identity.claude_uuid

    store.upsert_projection(
        _projection(
            _message("characterization-target", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    committed = store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "b" * 64, 100.0
    )

    assert committed["state"] == "claude_visible"
    assert committed["reserved_claude_uuid"] == identity.claude_uuid
    assert store.claude_visibility_status(100.0)["lineage"] == {
        "unlinked_visible": 0,
        "repairable": 0,
        "blocked": 0,
        "blocker_codes": {},
    }
    assert (
        _rows(
            db, "SELECT * FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
        )
        == []
    )


def test_characterization_enqueue_registers_atomically_before_generic_claim(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "12121212-1212-4212-8212-121212121212"
    candidate, identity = _claude_characterization_identity(operation_id)

    registered = store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )

    assert registered == {
        "status": "registered",
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
    }
    assert _rows(
        db,
        """SELECT event_kind
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
        (identity.job_id,),
    ) == [{"event_kind": "registered"}]
    assert (
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").status
        == "no_due_job"
    )
    assert store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        expected_job_id=identity.job_id,
    ).claimed


def test_characterization_enqueue_atomically_refuses_unrelated_open_work(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    unrelated_candidate, unrelated_identity = _claude_visibility_identity("other-open")
    _enqueue_claude_visibility_job(store, unrelated_candidate, unrelated_identity)
    operation_id = "17171717-1717-4717-8717-171717171717"
    candidate, identity = _claude_characterization_identity(operation_id)

    with pytest.raises(ValueError, match="characterization requires idle delivery"):
        store.enqueue_claude_visibility_characterization(
            candidate,
            identity,
            _CLAUDE_MARKER_SECRET,
            operation_id=operation_id,
            evidence_digest="a" * 64,
        )

    assert (
        _rows(
            db,
            "SELECT id FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )
        == []
    )
    assert (
        _rows(
            db,
            """SELECT job_id
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
            (identity.job_id,),
        )
        == []
    )


def test_characterization_enqueue_exact_replay_ignores_later_unrelated_open_work(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "18181818-1818-4818-8818-181818181818"
    candidate, identity = _claude_characterization_identity(operation_id)
    expected = store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )
    unrelated_candidate, unrelated_identity = _claude_visibility_identity("later-open")
    _enqueue_claude_visibility_job(store, unrelated_candidate, unrelated_identity)

    replayed = store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="b" * 64,
    )

    assert replayed == expected
    assert _rows(
        db,
        """SELECT event_kind, COUNT(*) AS count
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?
           GROUP BY event_kind""",
        (identity.job_id,),
    ) == [{"event_kind": "registered", "count": 1}]


def test_characterization_enqueue_backfills_exact_preledger_retry_without_mutation(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "19191919-1919-4919-8919-191919191919"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_retry', attempts = 7,
                   next_attempt_at = 100, error_code = 'creation_ambiguous',
                   error_detail = 'legacy ambiguous create', updated_at = 99
               WHERE id = ?""",
            (identity.job_id,),
        )
    )
    before = _rows(
        db,
        "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    )

    registered = store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )

    assert registered == {
        "status": "registered",
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
    }
    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )
        == before
    )
    assert _rows(
        db,
        """SELECT event_kind, operation_id, evidence_digest
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
        (identity.job_id,),
    ) == [
        {
            "event_kind": "registered",
            "operation_id": operation_id,
            "evidence_digest": "a" * 64,
        }
    ]
    assert (
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").status
        == "no_due_job"
    )
    exact = store.claim_claude_visibility_reconciliation(
        100.0, 60, expected_job_id=identity.job_id
    )
    assert exact.claimed is True
    assert exact.job_id == identity.job_id
    assert exact.reserved_claude_uuid == identity.claude_uuid
    assert exact.requires_exact_id_reconciliation is True


def test_characterization_enqueue_rejects_preledger_unexpired_lease(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "20202020-2020-4020-8020-202020202020"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_leased', attempts = 7,
                   lease_digest = ?, lease_expires_at = 200,
                   lease_kind = 'reconciliation', updated_at = 99
               WHERE id = ?""",
            ("f" * 64, identity.job_id),
        )
    )

    with pytest.raises(ValueError, match="registration race"):
        store.enqueue_claude_visibility_characterization(
            candidate,
            identity,
            _CLAUDE_MARKER_SECRET,
            operation_id=operation_id,
            evidence_digest="a" * 64,
        )

    assert (
        _rows(
            db,
            """SELECT event_kind
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
            (identity.job_id,),
        )
        == []
    )


@pytest.mark.parametrize("legacy_state", ["claude_failed", "claude_visible"])
def test_characterization_enqueue_backfills_exact_preledger_terminal_state(
    db, legacy_state: str
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = (
        "21212121-2121-4121-8121-212121212121"
        if legacy_state == "claude_failed"
        else "22222222-2222-4222-8222-222222222223"
    )
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)
    if legacy_state == "claude_failed":
        update = """UPDATE session_claude_visibility_jobs
                    SET state = 'claude_failed', attempts = 7,
                        error_code = 'max_attempts_exhausted', updated_at = 99
                    WHERE id = ?"""
    else:
        update = """UPDATE session_claude_visibility_jobs
                    SET state = 'claude_visible', attempts = 1,
                        completion_digest = ?, visible_at = 98, updated_at = 99
                    WHERE id = ?"""
    db._execute_write(
        lambda conn: conn.execute(
            update,
            (identity.job_id,)
            if legacy_state == "claude_failed"
            else ("c" * 64, identity.job_id),
        )
    )
    before = _rows(
        db,
        "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    )

    result = store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )

    assert result["status"] == "registered"
    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )
        == before
    )
    assert _rows(
        db,
        """SELECT event_kind
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
        (identity.job_id,),
    ) == [{"event_kind": "registered"}]


def test_characterization_enqueue_rolls_back_job_when_registration_append_fails(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "13131313-1313-4313-8313-131313131313"
    candidate, identity = _claude_characterization_identity(operation_id)
    db._execute_write(
        lambda conn: conn.execute(
            """CREATE TRIGGER fail_characterization_registration
               BEFORE INSERT ON session_claude_visibility_characterization_events
               WHEN NEW.event_kind = 'registered'
               BEGIN
                 SELECT RAISE(ABORT, 'forced registration failure');
               END"""
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced registration failure"):
        store.enqueue_claude_visibility_characterization(
            candidate,
            identity,
            _CLAUDE_MARKER_SECRET,
            operation_id=operation_id,
            evidence_digest="a" * 64,
        )

    assert (
        _rows(
            db,
            "SELECT id FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )
        == []
    )
    assert (
        _rows(
            db,
            """SELECT job_id
           FROM session_claude_visibility_characterization_events
           WHERE job_id = ?""",
            (identity.job_id,),
        )
        == []
    )


def test_authenticated_completed_characterization_is_terminal_not_visible_lineage(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 150.0, local_timezone=timezone.utc)
    operation_id = "22222222-2222-4222-8222-222222222222"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("cleaned-characterization-target", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?, visible_at = 100,
                   updated_at = 100 WHERE id = ?""",
            ("c" * 64, identity.job_id),
        )
    )
    assert store.claude_visibility_status(150.0)["lineage"]["blocker_codes"] == {
        "claude_lineage_missing_source": 1
    }

    recorded = store.record_claude_visibility_characterization(
        job_id=identity.job_id,
        operation_id=operation_id,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
        idempotency_key=identity.idempotency_key,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        evidence_digest="d" * 64,
        marker_secret=_CLAUDE_MARKER_SECRET,
        cleanup_completed=True,
    )

    assert recorded["status"] == "cleanup_completed"
    status = store.claude_visibility_status(150.0)
    assert status["counts"] == {
        "claude_pending": 0,
        "claude_leased": 0,
        "claude_retry": 0,
        "claude_visible": 0,
        "claude_failed": 0,
    }
    assert status["lineage"] == {
        "unlinked_visible": 0,
        "repairable": 0,
        "blocked": 0,
        "blocker_codes": {},
    }
    assert (
        store.reconcile_claude_visibility_lineage(
            limit=25, marker_secret=_CLAUDE_MARKER_SECRET, apply=False
        )["complete"]
        is True
    )


def test_characterization_events_are_append_only_and_identity_bound(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "33333333-3333-4333-8333-333333333333"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)

    with pytest.raises(ValueError, match="characterization identity mismatch"):
        store.record_claude_visibility_characterization(
            job_id=identity.job_id,
            operation_id=operation_id,
            source_session_id=candidate.source_session_id,
            bridge_id=identity.bridge_id,
            idempotency_key=identity.idempotency_key,
            reserved_claude_uuid="44444444-4444-4444-8444-444444444444",
            native_name=candidate.native_name,
            source_cwd=candidate.source_cwd,
            signed_marker=identity.signed_marker,
            evidence_digest="e" * 64,
            marker_secret=_CLAUDE_MARKER_SECRET,
            cleanup_completed=False,
        )

    store.record_claude_visibility_characterization(
        job_id=identity.job_id,
        operation_id=operation_id,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
        idempotency_key=identity.idempotency_key,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        evidence_digest="e" * 64,
        marker_secret=_CLAUDE_MARKER_SECRET,
        cleanup_completed=False,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_claude_visibility_characterization_events "
                "SET evidence_digest = ? WHERE job_id = ?",
                ("f" * 64, identity.job_id),
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db._execute_write(
            lambda conn: conn.execute(
                "DELETE FROM session_claude_visibility_characterization_events "
                "WHERE job_id = ?",
                (identity.job_id,),
            )
        )


def test_characterization_exact_absence_abort_is_audited_terminal_and_idempotent(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "44444444-4444-4444-8444-444444444444"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)

    def record(*, launch_aborted: bool = False) -> dict[str, object]:
        return store.record_claude_visibility_characterization(
            job_id=identity.job_id,
            operation_id=operation_id,
            source_session_id=candidate.source_session_id,
            bridge_id=identity.bridge_id,
            idempotency_key=identity.idempotency_key,
            reserved_claude_uuid=identity.claude_uuid,
            native_name=candidate.native_name,
            source_cwd=candidate.source_cwd,
            signed_marker=identity.signed_marker,
            evidence_digest="a" * 64,
            marker_secret=_CLAUDE_MARKER_SECRET,
            cleanup_completed=False,
            launch_aborted=launch_aborted,
        )

    assert record(launch_aborted=True) == {
        "status": "reconciliation_required",
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
    }
    assert _rows(
        db,
        "SELECT event_kind FROM session_claude_visibility_characterization_events "
        "WHERE job_id = ? ORDER BY created_at, event_kind",
        (identity.job_id,),
    ) == [{"event_kind": "registered"}]

    launch = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        expected_job_id=identity.job_id,
    )
    assert launch.launch_permitted is True
    store.retry_claude_visibility_job(
        identity.job_id,
        launch.lease_digest,
        "creation_ambiguous",
        100.0,
        "launch result unknown",
    )
    reconciliation = store.claim_claude_visibility_reconciliation(
        100.0,
        60,
        expected_job_id=identity.job_id,
    )
    assert reconciliation.requires_exact_id_reconciliation is True
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "b" * 64,
    )

    assert record(launch_aborted=True) == {
        "status": "launch_aborted",
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
    }
    assert record(launch_aborted=True) == {
        "status": "already_aborted",
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
    }
    assert _rows(
        db,
        "SELECT event_kind, evidence_digest "
        "FROM session_claude_visibility_characterization_events "
        "WHERE job_id = ? ORDER BY event_kind",
        (identity.job_id,),
    ) == [
        {"event_kind": "launch_aborted", "evidence_digest": "b" * 64},
        {"event_kind": "registered", "evidence_digest": "a" * 64},
    ]
    assert _rows(
        db,
        "SELECT outcome, evidence_digest, consumed_at "
        "FROM session_claude_visibility_reconciliations WHERE job_id = ?",
        (identity.job_id,),
    ) == [
        {
            "outcome": "absent",
            "evidence_digest": "b" * 64,
            "consumed_at": 100.0,
        }
    ]
    assert store.claude_visibility_status(100.0)["counts"] == {
        "claude_pending": 0,
        "claude_leased": 0,
        "claude_retry": 0,
        "claude_visible": 0,
        "claude_failed": 0,
    }
    assert (
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").status
        == "no_due_job"
    )
    assert (
        store.claim_claude_visibility_job(
            100.0,
            60,
            25,
            "0.50",
            "0.02",
            expected_job_id=identity.job_id,
        ).status
        == "no_due_job"
    )
    assert (
        store.claim_claude_visibility_reconciliation(
            100.0, 60, expected_job_id=identity.job_id
        ).status
        == "no_due_job"
    )

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    assert store.claude_visibility_status(100.0)["last_empty_cycle"] == {
        "tracked": True,
        "value": 100.0,
    }

    next_candidate, next_identity = _claude_visibility_identity("after-abort")
    assert store.enqueue_claude_visibility_batch_if_idle(
        [(next_candidate, next_identity)], _CLAUDE_MARKER_SECRET
    ) == {"status": "inserted", "inserted": 1, "duplicates": 0}
    next_claim = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        expected_job_id=next_identity.job_id,
    )
    assert next_claim.claimed is True
    assert next_claim.job_id == next_identity.job_id


def test_characterization_pending_without_launch_can_reconcile_exact_absence_for_abort(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "45454545-4545-4545-8545-454545454545"
    candidate, identity = _claude_characterization_identity(operation_id)
    store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )

    reconciliation = store.claim_claude_visibility_reconciliation(
        100.0,
        60,
        expected_job_id=identity.job_id,
    )

    assert reconciliation.claimed is True
    assert reconciliation.lease_kind == "reconciliation"
    assert reconciliation.attempt_ordinal == 0
    assert reconciliation.launch_permitted is False
    assert reconciliation.registration_reserved is False
    assert reconciliation.requires_exact_id_reconciliation is True
    assert _rows(
        db,
        "SELECT attempts, lease_kind FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    ) == [{"attempts": 0, "lease_kind": "reconciliation"}]
    assert _rows(db, "SELECT * FROM session_claude_registration_usage") == []

    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        0,
        "b" * 64,
    )
    terminal = store.record_claude_visibility_characterization(
        job_id=identity.job_id,
        operation_id=operation_id,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
        idempotency_key=identity.idempotency_key,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        evidence_digest="a" * 64,
        marker_secret=_CLAUDE_MARKER_SECRET,
        cleanup_completed=False,
        launch_aborted=True,
    )

    assert terminal["status"] == "launch_aborted"
    assert store.claude_visibility_status(100.0)["counts"] == {
        "claude_pending": 0,
        "claude_leased": 0,
        "claude_retry": 0,
        "claude_visible": 0,
        "claude_failed": 0,
    }


def test_legacy_characterization_table_allows_store_launch_abort_after_v28(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v26-characterization-store.db"
    database = SessionDB(path)
    store = SessionBridgeStore(
        database, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    operation_id = "47474747-4747-4747-8747-474747474747"
    candidate, identity = _claude_characterization_identity(operation_id)
    store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )
    reconciliation = store.claim_claude_visibility_reconciliation(
        100.0, 60, expected_job_id=identity.job_id
    )
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        0,
        "b" * 64,
    )
    database.close()

    legacy = sqlite3.connect(path)
    try:
        legacy.execute("PRAGMA foreign_keys=ON")
        legacy.executescript(
            """DROP TRIGGER trg_claude_characterization_event_identity;
            DROP TRIGGER trg_claude_characterization_cleanup_order;
            DROP TRIGGER trg_claude_characterization_abort_order;
            DROP TRIGGER trg_claude_characterization_event_no_update;
            DROP TRIGGER trg_claude_characterization_event_no_delete;
            ALTER TABLE session_claude_visibility_characterization_events
                RENAME TO session_claude_visibility_characterization_events_v27;
            CREATE TABLE session_claude_visibility_characterization_events (
                job_id TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK (
                    event_kind IN ('registered', 'cleanup_completed')
                ),
                operation_id TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                bridge_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                reserved_claude_uuid TEXT NOT NULL,
                evidence_digest TEXT NOT NULL CHECK (
                    length(evidence_digest) = 64
                    AND evidence_digest NOT GLOB '*[^0-9a-f]*'
                ),
                created_at REAL NOT NULL,
                PRIMARY KEY (job_id, event_kind),
                UNIQUE (operation_id, event_kind),
                UNIQUE (source_session_id, event_kind),
                UNIQUE (bridge_id, event_kind),
                UNIQUE (idempotency_key, event_kind),
                UNIQUE (reserved_claude_uuid, event_kind),
                FOREIGN KEY (job_id, reserved_claude_uuid)
                    REFERENCES session_claude_visibility_jobs(
                        id, reserved_claude_uuid
                    ) ON DELETE RESTRICT
            );
            INSERT INTO session_claude_visibility_characterization_events
            SELECT *
            FROM session_claude_visibility_characterization_events_v27;
            DROP TABLE session_claude_visibility_characterization_events_v27;
            DELETE FROM session_bridge_migrations
            WHERE migration_name = 'claude_characterization_events_v28';
            UPDATE schema_version SET version = 27;
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    upgraded = SessionDB(path)
    try:
        migrated_store = SessionBridgeStore(
            upgraded, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        terminal = migrated_store.record_claude_visibility_characterization(
            job_id=identity.job_id,
            operation_id=operation_id,
            source_session_id=candidate.source_session_id,
            bridge_id=identity.bridge_id,
            idempotency_key=identity.idempotency_key,
            reserved_claude_uuid=identity.claude_uuid,
            native_name=candidate.native_name,
            source_cwd=candidate.source_cwd,
            signed_marker=identity.signed_marker,
            evidence_digest="a" * 64,
            marker_secret=_CLAUDE_MARKER_SECRET,
            cleanup_completed=False,
            launch_aborted=True,
        )
        assert terminal["status"] == "launch_aborted"
        assert _rows(
            upgraded,
            """SELECT event_kind FROM
                   session_claude_visibility_characterization_events
               WHERE job_id = ? ORDER BY event_kind""",
            (identity.job_id,),
        ) == [
            {"event_kind": "launch_aborted"},
            {"event_kind": "registered"},
        ]
    finally:
        upgraded.close()


def test_characterization_abort_consumes_exact_absence_after_max_attempt_failure(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "46464646-4646-4646-8646-464646464646"
    candidate, identity = _claude_characterization_identity(operation_id)
    store.enqueue_claude_visibility_characterization(
        candidate,
        identity,
        _CLAUDE_MARKER_SECRET,
        operation_id=operation_id,
        evidence_digest="a" * 64,
    )
    launch = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        max_attempts=1,
        expected_job_id=identity.job_id,
    )
    store.retry_claude_visibility_job(
        identity.job_id,
        launch.lease_digest,
        "creation_ambiguous",
        100.0,
        "launch result unknown",
    )
    reconciliation = store.claim_claude_visibility_reconciliation(
        100.0,
        60,
        expected_job_id=identity.job_id,
    )
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "b" * 64,
    )
    exhausted = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "0.50",
        "0.02",
        max_attempts=1,
        expected_job_id=identity.job_id,
    )
    assert exhausted.status == "max_attempts_exhausted"

    terminal = store.record_claude_visibility_characterization(
        job_id=identity.job_id,
        operation_id=operation_id,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
        idempotency_key=identity.idempotency_key,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        evidence_digest="a" * 64,
        marker_secret=_CLAUDE_MARKER_SECRET,
        cleanup_completed=False,
        launch_aborted=True,
    )

    assert terminal["status"] == "launch_aborted"
    assert _rows(
        db,
        """SELECT state, attempts, error_code, lease_digest
           FROM session_claude_visibility_jobs WHERE id = ?""",
        (identity.job_id,),
    ) == [
        {
            "state": "claude_failed",
            "attempts": 1,
            "error_code": "max_attempts_exhausted",
            "lease_digest": None,
        }
    ]
    assert _rows(
        db,
        """SELECT outcome, consumed_at
           FROM session_claude_visibility_reconciliations WHERE job_id = ?""",
        (identity.job_id,),
    ) == [{"outcome": "absent", "consumed_at": 100.0}]


def test_characterization_rejects_cleanup_and_abort_in_one_event(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    operation_id = "55555555-5555-4555-8555-555555555555"
    candidate, identity = _claude_characterization_identity(operation_id)
    _enqueue_claude_visibility_job(store, candidate, identity)

    with pytest.raises(ValueError, match="cleanup and abort are mutually exclusive"):
        store.record_claude_visibility_characterization(
            job_id=identity.job_id,
            operation_id=operation_id,
            source_session_id=candidate.source_session_id,
            bridge_id=identity.bridge_id,
            idempotency_key=identity.idempotency_key,
            reserved_claude_uuid=identity.claude_uuid,
            native_name=candidate.native_name,
            source_cwd=candidate.source_cwd,
            signed_marker=identity.signed_marker,
            evidence_digest="a" * 64,
            marker_secret=_CLAUDE_MARKER_SECRET,
            cleanup_completed=True,
            launch_aborted=True,
        )


def test_claude_visibility_historical_lineage_reconciliation_is_concurrent_safe(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("concurrent-lineage")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id="source-concurrent-lineage",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "d" * 64, 100.0
    )
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
        )
    )
    barrier = Barrier(2)

    def _repair() -> dict[str, object]:
        barrier.wait()
        return store.reconcile_claude_visibility_lineage(
            limit=1, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: _repair(), range(2)))

    assert sum(int(result["repaired"]) for result in results) == 1
    assert _rows(
        db,
        "SELECT id FROM session_links WHERE bridge_id = ?",
        (identity.bridge_id,),
    ) == [
        {
            "id": (
                "claude-visibility-link:"
                + hashlib.sha256(
                    (
                        f"{identity.bridge_id}\0{candidate.source_session_id}\0"
                        f"claude:{identity.claude_uuid}"
                    ).encode()
                ).hexdigest()
            )
        }
    ]


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing_source", "claude_lineage_missing_source"),
        ("wrong_target", "claude_lineage_target_identity_mismatch"),
        ("wrong_provenance", "claude_lineage_target_provenance_mismatch"),
        ("duplicate_target", "claude_lineage_target_duplicate"),
        ("conflicting_link", "claude_lineage_conflict"),
    ],
)
def test_claude_visibility_commit_lineage_mismatch_rolls_back_without_partial_write(
    db,
    case: str,
    expected_code: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(f"rollback-{case}")
    if case != "missing_source":
        store.upsert_projection(
            _projection(
                _message("source-user", "meaningful request"),
                provider=Provider.CODEX,
                native_id=f"source-rollback-{case}",
            )
        )
    _enqueue_claude_visibility_job(store, candidate, identity)
    if case == "wrong_target":
        store.upsert_projection(
            _projection(
                _message("wrong-target-user", "signed registration"),
                native_id=f"wrong-{identity.claude_uuid}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=identity.bridge_id,
            )
        )
    else:
        store.upsert_projection(
            _projection(
                _message("target-user", "signed registration"),
                native_id=identity.claude_uuid,
                origin_kind=(
                    OriginKind.NATIVE
                    if case == "wrong_provenance"
                    else OriginKind.BRIDGE_PLACEHOLDER
                ),
                origin_bridge_id=(
                    None if case == "wrong_provenance" else identity.bridge_id
                ),
            )
        )
    if case == "duplicate_target":
        store.upsert_projection(
            _projection(
                _message("duplicate-target-user", "signed registration"),
                native_id=f"duplicate-{identity.claude_uuid}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=identity.bridge_id,
            )
        )
    if case == "conflicting_link":
        db._execute_write(
            lambda conn: conn.execute(
                """INSERT INTO session_links (
                       id, from_session_id, to_session_id, relation, bridge_id,
                       created_at
                   ) VALUES (?, ?, ?, 'continues', ?, 99)""",
                (
                    f"conflict:{identity.job_id}",
                    candidate.source_session_id,
                    f"claude:{identity.claude_uuid}",
                    identity.bridge_id,
                ),
            )
        )
    before_links = _rows(
        db, "SELECT * FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
    )
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")

    with pytest.raises(ValueError, match=expected_code):
        store.commit_claude_visibility_job(
            identity.job_id,
            claim.lease_digest,
            "e" * 64,
            100.0,
        )

    job = _rows(
        db,
        """SELECT state, completion_digest, visible_at
             FROM session_claude_visibility_jobs WHERE id = ?""",
        (identity.job_id,),
    )[0]
    assert job == {
        "state": "claude_leased",
        "completion_digest": None,
        "visible_at": None,
    }
    assert (
        _rows(
            db, "SELECT * FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
        )
        == before_links
    )


def test_claude_visibility_historical_lineage_missing_source_remains_blocked(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("historical-missing-source")
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_visible', completion_digest = ?, visible_at = 100,
                   updated_at = 100
               WHERE id = ?""",
            ("f" * 64, identity.job_id),
        )
    )

    result = store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    )

    assert result == {
        "scanned": 1,
        "repairable": 0,
        "repaired": 0,
        "remaining": 1,
        "blocker_codes": {"claude_lineage_missing_source": 1},
        "next_cursor": None,
        "has_more": False,
        "complete": False,
    }
    assert (
        _rows(
            db,
            "SELECT id FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        )
        == []
    )


@pytest.mark.parametrize("diverged_at", [None, 140.0])
def test_claude_visibility_continued_lineage_remains_healthy_and_idempotent(
    db,
    diverged_at: float | None,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 150.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(
        f"continued-lineage-{diverged_at}"
    )
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id=f"source-continued-lineage-{diverged_at}",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(150.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "1" * 64, 150.0
    )
    target = _projection(
        _message("target-user", "signed registration"),
        native_id=identity.claude_uuid,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    store.upsert_projection(target)
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_links
               SET relation = 'continues', hydrated_at = 130, diverged_at = ?
               WHERE bridge_id = ?""",
            (diverged_at, identity.bridge_id),
        )
    )

    store.upsert_projection(target)

    assert store.claude_visibility_status(150.0)["lineage"] == {
        "unlinked_visible": 0,
        "repairable": 0,
        "blocked": 0,
        "blocker_codes": {},
    }
    assert store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    ) == {
        "scanned": 0,
        "repairable": 0,
        "repaired": 0,
        "remaining": 0,
        "blocker_codes": {},
        "next_cursor": None,
        "has_more": False,
        "complete": True,
    }
    assert _rows(
        db,
        """SELECT relation, hydrated_at, diverged_at
             FROM session_links WHERE bridge_id = ?""",
        (identity.bridge_id,),
    ) == [
        {
            "relation": "continues",
            "hydrated_at": 130.0,
            "diverged_at": diverged_at,
        }
    ]
    assert (
        UnifiedCatalog(db, store).resolve_continuation(
            session_id=candidate.source_session_id,
            bridge_id=None,
            target_provider="claude",
        )["target_session_id"]
        == f"claude:{identity.claude_uuid}"
    )


@pytest.mark.parametrize("conflict", ["wrong_link_id", "forks", "extra_link"])
def test_claude_visibility_historical_link_identity_conflict_blocks_repair(
    db,
    conflict: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 150.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(f"historical-{conflict}")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id=f"source-historical-{conflict}",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(150.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "2" * 64, 150.0
    )
    target = _projection(
        _message("target-user", "signed registration"),
        native_id=identity.claude_uuid,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    store.upsert_projection(target)
    if conflict == "wrong_link_id":
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_links SET id = ? WHERE bridge_id = ?",
                (f"wrong:{identity.job_id}", identity.bridge_id),
            )
        )
    elif conflict == "forks":
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_links SET relation = 'forks' WHERE bridge_id = ?",
                (identity.bridge_id,),
            )
        )
    else:
        db._execute_write(
            lambda conn: conn.execute(
                """INSERT INTO session_links (
                       id, from_session_id, to_session_id, relation, bridge_id,
                       created_at
                   ) VALUES (?, ?, ?, 'continues', ?, 149)""",
                (
                    f"extra:{identity.job_id}",
                    candidate.source_session_id,
                    f"claude:{identity.claude_uuid}",
                    identity.bridge_id,
                ),
            )
        )
    before = _rows(
        db, "SELECT * FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
    )

    assert store.claude_visibility_status(150.0)["lineage"] == {
        "unlinked_visible": 1,
        "repairable": 0,
        "blocked": 1,
        "blocker_codes": {"claude_lineage_conflict": 1},
    }
    assert store.reconcile_claude_visibility_lineage(
        limit=10, marker_secret=_CLAUDE_MARKER_SECRET, apply=True
    ) == {
        "scanned": 1,
        "repairable": 0,
        "repaired": 0,
        "remaining": 1,
        "blocker_codes": {"claude_lineage_conflict": 1},
        "next_cursor": None,
        "has_more": False,
        "complete": False,
    }
    assert (
        _rows(
            db, "SELECT * FROM session_links WHERE bridge_id = ?", (identity.bridge_id,)
        )
        == before
    )


def test_new_native_user_delta_promotes_placeholder(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", "  genuine later user  "),
            cursor="human-cursor",
            native_hash="human-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert result.inserted_messages == 1
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"


@pytest.mark.parametrize("content", ["", "   ", "\t\n"])
def test_empty_pending_user_does_not_promote_placeholder(db, content):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", content),
            cursor="empty-user-cursor",
            native_hash="empty-user-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert result.inserted_messages == 1
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"


def test_explicit_placeholder_to_continuation_is_monotonic_for_same_bridge(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    placeholder = _projection(
        _message("e1", "bridge marker"),
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )
    store.upsert_projection(placeholder)

    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="continuation-cursor",
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
        )
    )
    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="placeholder-replay-cursor",
        )
    )
    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="native-replay-cursor",
            origin_kind=OriginKind.NATIVE,
            origin_bridge_id=None,
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "native-replay-cursor"


def test_rebuild_uses_explicit_provenance_without_inferring_from_marker_user(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "original bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    store.upsert_projection(
        _projection(
            _message("rebuilt-marker", "original bridge marker"),
            cursor="rebuilt-cursor",
            native_hash="rebuilt-hash",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        ),
        rebuild=True,
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"


def test_projection_rejects_conflicting_bridge_ids_atomically(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "native")))
    store.upsert_projection(
        _projection(
            _message("e1", "native"),
            cursor="upgraded-cursor",
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
            origin_bridge_id="bridge-1",
        )
    )

    conflicts = (
        (OriginKind.BRIDGE_CONTINUATION, "bridge-2"),
        (OriginKind.BRIDGE_PLACEHOLDER, "bridge-2"),
    )
    for origin_kind, origin_bridge_id in conflicts:
        with pytest.raises(ValueError, match="provenance"):
            store.upsert_projection(
                _projection(
                    _message("e1", "native"),
                    _message("e2", "must roll back"),
                    cursor="conflicting-cursor",
                    origin_kind=origin_kind,
                    origin_bridge_id=origin_bridge_id,
                )
            )

    external = store.get_external_session("claude:native-1")
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "upgraded-cursor"
    assert [row["content"] for row in db.get_messages("claude:native-1")] == ["native"]


def test_observably_stale_projection_cannot_regress_external_metadata(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "newest", timestamp=10.0),
            cursor="new-cursor",
            native_hash="new-hash",
            last_active=100.0,
            git_branch="feature/newest",
        )
    )

    with pytest.raises(ValueError, match="stale"):
        store.upsert_projection(
            replace(
                _projection(
                    _message("e2", "older", timestamp=11.0),
                    cursor="old-cursor",
                    native_hash="old-hash",
                    last_active=90.0,
                    git_branch="feature/stale",
                ),
                native_path="C:/claude/old-path.jsonl",
                native_status="archived",
                parser_version=1,
            )
        )

    external = store.get_external_session("claude:native-1")
    assert external["last_native_cursor"] == "new-cursor"
    assert external["last_native_hash"] == "new-hash"
    assert external["native_status"] == "active"
    assert db.get_session("claude:native-1")["git_branch"] == "feature/newest"
    assert [row["content"] for row in db.get_messages("claude:native-1")] == ["newest"]


def test_rebuild_allows_truncation_and_replaces_external_activity_watermark(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "before truncation", timestamp=80.0),
            cursor="cursor-before",
            native_hash="hash-before",
            last_active=100.0,
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", "after truncation", timestamp=40.0),
            cursor="cursor-after",
            native_hash="hash-after",
            last_active=50.0,
        ),
        rebuild=True,
    )

    external = store.get_external_session("claude:native-1")
    assert result.rebuilt is True
    assert external["last_native_cursor"] == "cursor-after"
    assert external["last_native_hash"] == "hash-after"
    watermark = _rows(
        db,
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        ("session-bridge:external-activity:claude:native-1",),
    )[0]
    assert json.loads(watermark["value_json"]) == {"last_active": 50.0}


def test_orphan_activity_watermark_does_not_block_fresh_rediscovery(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "before deletion"),
            cursor="cursor-before",
            last_active=100.0,
        )
    )
    assert db.delete_session("claude:native-1") is True

    rediscovered = store.upsert_projection(
        _projection(
            _message("e2", "fresh discovery"),
            cursor="cursor-after",
            last_active=50.0,
        )
    )

    assert rediscovered.first_seen is True
    assert store.get_external_session("claude:native-1")["last_native_cursor"] == (
        "cursor-after"
    )
    watermark = _rows(
        db,
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        ("session-bridge:external-activity:claude:native-1",),
    )[0]
    assert json.loads(watermark["value_json"]) == {"last_active": 50.0}


def test_imported_messages_are_discoverable_through_existing_fts(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "quartzbridge uniqueterm")))

    matches = db.search_messages("quartzbridge")

    assert [match["session_id"] for match in matches] == ["claude:native-1"]


def test_low_level_mirror_job_is_idempotent_but_public_claim_fails_closed(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))

    first = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=4
    )
    replay = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=4
    )

    assert replay == first
    assert len(_rows(db, "SELECT * FROM session_mirror_jobs")) == 1
    with pytest.raises(TypeError, match="policy"):
        store.claim_due_jobs(  # type: ignore[missing-argument]
            now=100.0, limit=10
        )
    assert _rows(db, "SELECT * FROM session_mirror_jobs")[0]["state"] == "queued"

    assert store.claim_due_jobs(now=100.0, limit=10, policy=MirrorPolicy()) == []
    failed = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert failed["state"] == "manual_failure"
    assert failed["attempts"] == 0
    assert failed["error_code"] == "authority_missing"


def test_mirror_jobs_can_be_listed_by_state_with_a_deterministic_bound(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("source-c", "source-a", "source-b"):
        session_id = f"claude:{native_id}"
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(
            store.enqueue_mirror_job(session_id, Provider.CODEX, policy_generation=1)
        )
    retry_id = jobs[1]["id"]
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_mirror_jobs SET state = 'retry' WHERE id = ?",
            (retry_id,),
        )
    )

    first = store.list_mirror_jobs(
        [MirrorJobState.RETRY, MirrorJobState.QUEUED], limit=2
    )
    replay = store.list_mirror_jobs(["queued", "retry"], limit=2)

    expected_ids = sorted(job["id"] for job in jobs)[:2]
    assert [job["id"] for job in first] == expected_ids
    assert replay == first


def test_mirror_job_listing_validates_state_filters_and_limit(db):
    store = SessionBridgeStore(db)

    assert store.list_mirror_jobs([], limit=1) == []
    with pytest.raises(ValueError, match="mirror job state"):
        store.list_mirror_jobs(["unknown"], limit=1)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_mirror_jobs([MirrorJobState.QUEUED], limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_mirror_jobs([MirrorJobState.QUEUED], limit=1001)


def test_mirror_job_counts_have_a_stable_all_states_shape(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("queued", "running", "failed"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        store.enqueue_mirror_job(
            f"claude:{native_id}", Provider.CODEX, policy_generation=1
        )
    db._execute_write(
        lambda conn: conn.executescript(
            """UPDATE session_mirror_jobs SET state = 'running'
               WHERE source_session_id = 'claude:running';
               UPDATE session_mirror_jobs SET state = 'manual_failure'
               WHERE source_session_id = 'claude:failed';"""
        )
    )

    assert store.mirror_job_counts() == {
        "queued": 1,
        "running": 1,
        "retry": 0,
        "succeeded": 0,
        "manual_failure": 1,
    }


def test_atomic_rate_limited_claim_prevents_two_store_oversubscription(tmp_path):
    path = tmp_path / "shared-state.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(first_db, clock=lambda: 100.0)
        second = SessionBridgeStore(second_db, clock=lambda: 100.0)
        for native_id in ("source-a", "source-b"):
            first.upsert_projection(
                _projection(_message(native_id, native_id), native_id=native_id)
            )
            _enqueue_manual_job(first, f"claude:{native_id}", Provider.CODEX)
        barrier = Barrier(2)

        def _claim(store):
            barrier.wait()
            return store.claim_due_jobs_with_limits(
                now=100.0,
                limit=1,
                policy=MirrorPolicy(creates_per_minute=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_claim, (first, second)))

        claimed = [job for batch in results for job in batch]
        assert len(claimed) == 1
        assert claimed[0]["claim_authority"] == "manual"
        assert first.get_state("session-bridge:mirror-rate") == {
            "version": 1,
            "attempted_at": [100.0],
        }
        assert len(first.list_mirror_jobs([MirrorJobState.QUEUED])) == 1
    finally:
        second_db.close()
        first_db.close()


def test_atomic_claim_keeps_manual_authority_when_automatic_breaker_halts(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    automatic_projection = _projection(
        _message("auto", "automatic"), native_id="automatic"
    )
    manual_projection = _projection(_message("manual", "manual"), native_id="manual")
    store.upsert_projection(automatic_projection)
    store.upsert_projection(manual_projection)
    automatic = _enqueue_automatic_job(store, automatic_projection, now=100.0)
    manual = _enqueue_manual_job(store, "claude:manual", Provider.CODEX)
    store.accumulate_mirror_breaker_progress(attempts=1, errors=1)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=2,
        policy=MirrorPolicy(automatic_creation=True),
    )

    assert [job["id"] for job in claimed] == [manual["id"]]
    assert claimed[0]["claim_authority"] == "manual"
    rows = {
        job["id"]: job
        for job in store.list_mirror_jobs([
            MirrorJobState.QUEUED,
            MirrorJobState.RUNNING,
        ])
    }
    assert rows[automatic["id"]]["state"] == "queued"


def test_atomic_store_claim_revokes_safe_manual_job_after_mapping_appears(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    enqueue_mirror_job(
        store,
        "claude:native-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
    )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target",
        )
    )
    store.create_link(
        SessionLink(
            id="safe-manual-mapped",
            from_session_id="claude:native-1",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="safe-manual-mapped",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(),
    )

    assert claimed == []
    failed = store.list_mirror_jobs([MirrorJobState.MANUAL_FAILURE])
    assert len(failed) == 1
    assert failed[0]["error_code"] == "manual_authority_revoked"


def test_atomic_claim_accepts_an_exact_job_id_scope(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("outside", "selected"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(_enqueue_manual_job(store, f"claude:{native_id}", Provider.CODEX))

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(),
        job_ids=[jobs[1]["id"]],
    )

    assert [job["id"] for job in claimed] == [jobs[1]["id"]]
    assert (
        store.claim_due_jobs_with_limits(
            now=100.0,
            limit=1,
            policy=MirrorPolicy(),
            job_ids=[],
        )
        == []
    )
    queued = store.list_mirror_jobs([MirrorJobState.QUEUED])
    assert [job["id"] for job in queued] == [jobs[0]["id"]]
    with pytest.raises(TypeError, match="sequence"):
        store.claim_due_jobs_with_limits(
            now=100.0,
            limit=1,
            policy=MirrorPolicy(),
            job_ids=jobs[0]["id"],
        )


def test_rollout_limited_manual_claims_use_global_breaker_with_auto_off(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("rollout-one", "rollout-two"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(
            enqueue_mirror_job(
                store,
                f"claude:{native_id}",
                Provider.CODEX,
                policy=MirrorPolicy(),
                manual_authorized=True,
                require_unmapped=True,
                rollout_limited=True,
            )
        )
    policy = MirrorPolicy(
        automatic_creation=False,
        creates_per_minute=6,
        stop_after_attempts=1,
        stop_error_rate=0.5,
    )

    claimed = store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy)

    assert len(claimed) == 1
    assert claimed[0]["claim_authority"] == "manual"
    assert claimed[0]["rollout_limited"] is True
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy) == []

    store.retry_job(
        claimed[0]["id"],
        code="provider_failed",
        detail="fixed failure",
        next_attempt_at=120.0,
    )

    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 1}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy) == []
    assert jobs[1]["id"] in {
        job["id"] for job in store.list_mirror_jobs([MirrorJobState.QUEUED])
    }


def test_rollout_limited_claim_resets_a_healthy_completed_batch_with_auto_off(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("rollout", "rollout")))
    job = enqueue_mirror_job(
        store,
        "claude:native-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
        rollout_limited=True,
    )
    store.accumulate_mirror_breaker_progress(attempts=1, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=False,
            stop_after_attempts=1,
            stop_error_rate=0.5,
        ),
    )

    assert [claimed_job["id"] for claimed_job in claimed] == [job["id"]]
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_atomic_claim_resets_completed_healthy_breaker_before_automatic_claim(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("auto", "automatic"))
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.accumulate_mirror_breaker_progress(attempts=2, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=True,
            stop_after_attempts=2,
            stop_error_rate=0.5,
        ),
    )

    assert [job["id"] for job in claimed] == [automatic["id"]]
    assert claimed[0]["claim_authority"] == "automatic"
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_atomic_claim_reserves_breaker_attempt_and_never_overshoots_cap(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("automatic-one", "automatic-two"):
        projection = _projection(
            _message(native_id, native_id),
            native_id=native_id,
            last_active=30.0,
        )
        store.upsert_projection(projection)
        jobs.append(_enqueue_automatic_job(store, projection, now=100.0))
    store.accumulate_mirror_breaker_progress(attempts=19, errors=0)
    policy = MirrorPolicy(
        automatic_creation=True,
        creates_per_minute=6,
        stop_after_attempts=20,
        stop_error_rate=0.01,
    )

    claimed = store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy)

    assert len(claimed) == 1
    assert store.get_mirror_breaker_progress() == {"attempts": 20, "errors": 0}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy) == []

    store.retry_job(
        claimed[0]["id"],
        code="provider_failed",
        detail="fixed failure",
        next_attempt_at=120.0,
    )
    assert store.get_mirror_breaker_progress() == {"attempts": 20, "errors": 1}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy) == []
    assert jobs[1]["id"] in {
        row["id"] for row in store.list_mirror_jobs([MirrorJobState.QUEUED])
    }


def test_zero_error_threshold_resets_a_completed_error_free_batch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("zero", "zero"), native_id="zero")
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.accumulate_mirror_breaker_progress(attempts=2, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=True,
            stop_after_attempts=2,
            stop_error_rate=0,
        ),
    )

    assert [job["id"] for job in claimed] == [automatic["id"]]
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_breaker_progress_accumulates_and_resets_across_two_stores(tmp_path):
    path = tmp_path / "shared-breaker.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(first_db, clock=lambda: 100.0)
        second = SessionBridgeStore(second_db, clock=lambda: 100.0)
        barrier = Barrier(2)

        def _add(store, attempts, errors):
            barrier.wait()
            return store.accumulate_mirror_breaker_progress(
                attempts=attempts, errors=errors
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_add, first, 1, 1),
                executor.submit(_add, second, 2, 0),
            ]
            for future in futures:
                future.result()

        assert first.get_mirror_breaker_progress() == {
            "attempts": 3,
            "errors": 1,
        }
        assert second.accumulate_mirror_breaker_progress(
            attempts=0, errors=0, reset=True
        ) == {"attempts": 0, "errors": 0}
        assert first.get_mirror_breaker_progress() == {
            "attempts": 0,
            "errors": 0,
        }
    finally:
        second_db.close()
        first_db.close()


def test_breaker_progress_state_is_strict_and_never_overwritten_on_corruption(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    corrupt = {"version": 1, "attempts": 1, "errors": 0, "extra": True}
    store.set_state("session-bridge:mirror-breaker", corrupt)

    with pytest.raises(ValueError, match="breaker progress"):
        store.get_mirror_breaker_progress()
    with pytest.raises(ValueError, match="breaker progress"):
        store.accumulate_mirror_breaker_progress(attempts=1, errors=0)

    assert store.get_state("session-bridge:mirror-breaker") == corrupt


def test_exact_origin_bridge_lookup_is_provider_scoped(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for provider, native_id in (
        (Provider.CLAUDE, "claude-target"),
        (Provider.CODEX, "codex-target"),
    ):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=provider,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id="bridge-shared",
            )
        )

    claude = store.find_external_session_by_origin_bridge(
        "bridge-shared", Provider.CLAUDE
    )
    codex = store.find_external_session_by_origin_bridge(
        "bridge-shared", Provider.CODEX
    )

    assert claude is not None and claude["native_id"] == "claude-target"
    assert codex is not None and codex["native_id"] == "codex-target"
    assert (
        store.find_external_session_by_origin_bridge("missing", Provider.CODEX) is None
    )


def test_exact_origin_bridge_lookup_rejects_duplicate_provenance(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id="bridge-duplicate",
            )
        )

    with pytest.raises(ValueError, match="duplicate bridge provenance"):
        store.find_external_session_by_origin_bridge("bridge-duplicate", Provider.CODEX)


def test_exact_origin_bridge_lookup_ignores_unauthenticated_native_rows(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("native", "native"), native_id="native-with-marker")
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE external_sessions SET origin_bridge_id = ? WHERE session_id = ?",
            ("untrusted-bridge", "claude:native-with-marker"),
        )
    )

    assert (
        store.find_external_session_by_origin_bridge(
            "untrusted-bridge", Provider.CLAUDE
        )
        is None
    )


def test_guarded_public_claim_preserves_retry_order_and_cas_transitions(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("e1", "source")))
    first = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX, generation=4)

    claimed = store.claim_due_jobs(now=100.0, limit=10, policy=MirrorPolicy())
    assert claimed[0]["state"] == "running"
    assert claimed[0]["attempts"] == 1
    assert store.claim_due_jobs(now=100.0, limit=10, policy=MirrorPolicy()) == []

    attempt_key = f"session-bridge:attempt:{first['id']}"
    store.set_state(attempt_key, {"version": 1, "attempts": 1})
    store.retry_job(
        first["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )
    assert store.get_state(attempt_key) is None
    current_time[0] = 119.0
    assert store.claim_due_jobs(now=119.0, limit=10, policy=MirrorPolicy()) == []
    current_time[0] = 120.0
    reclaimed = store.claim_due_jobs(now=120.0, limit=10, policy=MirrorPolicy())
    assert reclaimed[0]["attempts"] == 2


def test_public_claim_delegation_pauses_automatic_and_claims_manual(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    automatic_projection = _projection(
        _message("auto", "automatic"), native_id="automatic"
    )
    manual_projection = _projection(_message("manual", "manual"), native_id="manual")
    store.upsert_projection(automatic_projection)
    store.upsert_projection(manual_projection)
    automatic = _enqueue_automatic_job(store, automatic_projection, now=100.0)
    manual = _enqueue_manual_job(store, "claude:manual", Provider.CODEX)

    claimed = store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    rows = {row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")}

    assert [job["id"] for job in claimed] == [manual["id"]]
    assert rows[automatic["id"]]["state"] == "queued"


def test_public_claim_delegation_terminalizes_revoked_automatic_when_enabled(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("auto", "automatic"))
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.upsert_projection(
        replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-late",
        )
    )

    claimed = store.claim_due_jobs(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(automatic_creation=True),
    )
    durable = {
        row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")
    }[automatic["id"]]

    assert claimed == []
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "automatic_authority_revoked"


def test_retry_state_only_allows_exact_replay_or_manual_exhaustion(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.retry_job(
        job["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )

    with pytest.raises(ValueError, match="running"):
        store.retry_job(
            job["id"],
            code="different",
            detail="conflicting replay",
            next_attempt_at=130.0,
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "retry"
    assert persisted["error_code"] == "timeout"
    assert persisted["next_attempt_at"] == 120.0

    store.fail_job_manually(job["id"], code="exhausted", detail="operator required")
    terminal = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert terminal["state"] == "manual_failure"
    assert terminal["error_code"] == "exhausted"


def test_completing_a_job_records_target_and_idempotent_mirror_link(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="  target-1  ",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    store.complete_job(
        job["id"],
        target_native_id="  target-1  ",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )
    store.complete_job(
        job["id"],
        target_native_id="  target-1  ",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    completed = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert completed["state"] == "succeeded"
    assert completed["target_native_id"] == "target-1"
    links = _rows(db, "SELECT * FROM session_links")
    assert len(links) == 1
    assert links[0]["relation"] == "mirrors"
    assert links[0]["bridge_id"] == "bridge-1"


def test_completion_requires_running_state_and_rolls_back(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )

    with pytest.raises(ValueError, match="running"):
        store.complete_job(
            job["id"],
            target_native_id="target-1",
            target_session_id="codex:target-1",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "queued"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


def test_completion_rejects_ordinary_target_identity_and_rolls_back(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    db.create_session("codex:ordinary", "cli")
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    with pytest.raises(ValueError, match="cataloged target"):
        store.complete_job(
            job["id"],
            target_native_id="ordinary",
            target_session_id="codex:ordinary",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "running"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


@pytest.mark.parametrize(
    ("origin_kind", "origin_bridge_id"),
    [
        (OriginKind.NATIVE, None),
        (OriginKind.BRIDGE_PLACEHOLDER, "bridge-other"),
    ],
)
def test_completion_requires_authenticated_exact_bridge_provenance(
    db, origin_kind, origin_bridge_id
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
        )
    )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    with pytest.raises(ValueError, match="bridge provenance"):
        store.complete_job(
            job["id"],
            target_native_id="target-1",
            target_session_id="codex:target-1",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "running"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


def test_conflicting_completion_replay_cannot_retarget_succeeded_job(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    for native_id in ("target-1", "target-2"):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=f"bridge-{native_id[-1]}",
            )
        )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.complete_job(
        job["id"],
        target_native_id="target-1",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    with pytest.raises(ValueError, match="conflicting completion"):
        store.complete_job(
            job["id"],
            target_native_id="target-2",
            target_session_id="codex:target-2",
            bridge_id="bridge-2",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["target_native_id"] == "target-1"
    assert [row["bridge_id"] for row in _rows(db, "SELECT * FROM session_links")] == [
        "bridge-1"
    ]


def test_terminal_jobs_cannot_be_retried_or_overwritten(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    succeeded = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.complete_job(
        succeeded["id"],
        target_native_id="target-1",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    with pytest.raises(ValueError, match="terminal"):
        store.retry_job(
            succeeded["id"],
            code="late-timeout",
            detail="must not revive",
            next_attempt_at=200.0,
        )
    with pytest.raises(ValueError, match="terminal"):
        store.fail_job_manually(
            succeeded["id"], code="late-failure", detail="must not overwrite"
        )

    store.upsert_projection(
        _projection(_message("s2", "terminal"), native_id="native-terminal")
    )
    terminal = _enqueue_manual_job(store, "claude:native-terminal", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.fail_job_manually(terminal["id"], code="manual", detail="operator required")
    store.fail_job_manually(terminal["id"], code="manual", detail="operator required")
    with pytest.raises(ValueError, match="terminal"):
        store.retry_job(
            terminal["id"],
            code="retry",
            detail="must not revive",
            next_attempt_at=300.0,
        )

    rows = {row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")}
    assert rows[succeeded["id"]]["state"] == "succeeded"
    assert rows[succeeded["id"]]["error_code"] is None
    assert rows[terminal["id"]]["state"] == "manual_failure"
    assert rows[terminal["id"]]["error_code"] == "manual"


def test_links_are_unique_and_divergence_is_marked(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
        )
    )
    link = SessionLink(
        id="link-1",
        from_session_id="claude:native-1",
        to_session_id="codex:target-1",
        relation=Relation.CONTINUES,
        bridge_id="bridge-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        created_at=50.0,
    )

    first = store.create_link(link)
    replay = store.create_link(replace(link, id="link-duplicate"))
    store.mark_diverged("bridge-1", at=123.0)

    assert replay == first
    links = _rows(db, "SELECT * FROM session_links")
    assert len(links) == 1
    assert links[0]["id"] == "link-1"
    assert links[0]["diverged_at"] == 123.0


def test_mirror_link_transitions_to_continues_from_an_immutable_pack(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="immutable handoff",
            created_at=60.0,
            immutable_at=90.0,
        )
    )

    transitioned = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    replay = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )

    assert replay == transitioned
    assert transitioned["id"] == "link-mirror"
    assert transitioned["relation"] == "continues"
    assert transitioned["source_cursor"] == "cursor-snapshot"
    assert transitioned["source_hash"] == "hash-snapshot"
    assert transitioned["hydrated_at"] == 100.0
    assert store.get_context_pack("bridge-1", budget_chars=4000)["immutable_at"] == 90.0
    assert store.get_continuation_snapshot("bridge-1") == {
        "version": 1,
        "pack_id": "pack-1",
        "source_session_id": "claude:native-1",
        "source_cursor": "cursor-snapshot",
        "source_hash": "hash-snapshot",
        "target_session_id": "codex:target-1",
        "target_cursor": "target-cursor",
        "target_hash": "target-hash",
    }
    assert len(_rows(db, "SELECT * FROM session_links")) == 1


def test_mirror_link_transition_atomically_freezes_and_hydrates_mutable_pack(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-mutable",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="not frozen",
            created_at=60.0,
        )
    )

    transitioned = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-mutable",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    current_time[0] = 200.0
    replay = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-mutable",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )

    assert replay == transitioned
    assert transitioned["relation"] == "continues"
    assert transitioned["source_cursor"] == "cursor-snapshot"
    assert transitioned["source_hash"] == "hash-snapshot"
    assert transitioned["hydrated_at"] == 100.0
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] == 100.0


def test_mirror_link_transition_rejects_pack_link_identity_mismatch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                cursor="target-cursor",
                native_hash="target-hash",
            )
        )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-a",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-wrong-target",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-b",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="wrong target",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="identity"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-wrong-target",
            target_cursor="target-cursor",
            target_hash="target-hash",
        )

    assert _rows(
        db,
        "SELECT relation, source_cursor, source_hash, hydrated_at FROM session_links",
    ) == [
        {
            "relation": "mirrors",
            "source_cursor": None,
            "source_hash": None,
            "hydrated_at": None,
        }
    ]
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None


def test_mirror_link_transition_rejects_conflicting_continues_row(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    for link_id, relation, cursor, source_hash in (
        ("link-mirror", Relation.MIRRORS, None, None),
        ("link-continues", Relation.CONTINUES, "other-cursor", "other-hash"),
    ):
        store.create_link(
            SessionLink(
                id=link_id,
                from_session_id="claude:native-1",
                to_session_id="codex:target-1",
                relation=relation,
                bridge_id="bridge-1",
                source_cursor=cursor,
                source_hash=source_hash,
                created_at=50.0,
            )
        )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="immutable handoff",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="conflicting continues"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="target-cursor",
            target_hash="target-hash",
        )

    assert {
        (row["id"], row["relation"], row["source_cursor"])
        for row in _rows(db, "SELECT * FROM session_links")
    } == {
        ("link-mirror", "mirrors", None),
        ("link-continues", "continues", "other-cursor"),
    }
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None


def test_continuation_target_baseline_conflict_rolls_back_exact_state(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="source-cursor",
            source_hash="source-hash",
            budget_chars=4000,
            payload="handoff",
            created_at=60.0,
        )
    )
    store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    before_pack = store.get_context_pack("bridge-1", budget_chars=4000)
    before_link = _rows(db, "SELECT * FROM session_links")[0]
    before_snapshot = store.get_continuation_snapshot("bridge-1")
    current_time[0] = 200.0

    with pytest.raises(ValueError, match="target baseline"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="different-cursor",
            target_hash="different-hash",
        )

    assert store.get_context_pack("bridge-1", budget_chars=4000) == before_pack
    assert _rows(db, "SELECT * FROM session_links")[0] == before_link
    assert store.get_continuation_snapshot("bridge-1") == before_snapshot


def test_continuation_transition_rejects_noncurrent_target_baseline(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="current-target-cursor",
            native_hash="current-target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="source-cursor",
            source_hash="source-hash",
            budget_chars=4000,
            payload="handoff",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="cataloged target snapshot"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="stale-target-cursor",
            target_hash="stale-target-hash",
        )

    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None
    link = _rows(db, "SELECT * FROM session_links")[0]
    assert link["relation"] == "mirrors" and link["hydrated_at"] is None
    assert store.get_continuation_snapshot("bridge-1") is None


@pytest.mark.parametrize(
    "invalid_snapshot",
    [
        {"version": 2},
        {
            "version": 1,
            "pack_id": "pack-1",
            "source_session_id": "claude:source",
            "source_cursor": "source-cursor",
            "source_hash": "source-hash",
            "target_session_id": "codex:target",
            "target_cursor": "target-cursor",
            "target_hash": "",
        },
    ],
)
def test_continuation_snapshot_getter_rejects_invalid_schema(db, invalid_snapshot):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state("session-bridge:continuation:bridge-invalid", invalid_snapshot)

    with pytest.raises(ValueError, match="continuation snapshot"):
        store.get_continuation_snapshot("bridge-invalid")


def test_continuation_snapshot_getter_rejects_false_durable_identity(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state(
        "session-bridge:continuation:bridge-false",
        {
            "version": 1,
            "pack_id": "pack-missing",
            "source_session_id": "claude:source",
            "source_cursor": "source-cursor",
            "source_hash": "source-hash",
            "target_session_id": "codex:target",
            "target_cursor": "target-cursor",
            "target_hash": "target-hash",
        },
    )

    with pytest.raises(ValueError, match="durable identity"):
        store.get_continuation_snapshot("bridge-false")


def test_continuation_snapshot_listing_is_empty_and_strictly_bounded(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    assert store.list_continuation_snapshots() == []
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_continuation_snapshots(limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_continuation_snapshots(limit=1001)


def test_continuation_snapshot_listing_is_deterministic_and_bounded(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for suffix in ("b", "a"):
        source_id = f"source-{suffix}"
        target_id = f"target-{suffix}"
        bridge_id = f"bridge-{suffix}"
        store.upsert_projection(
            _projection(
                _message(f"source-event-{suffix}", "source"), native_id=source_id
            )
        )
        store.upsert_projection(
            _projection(
                _message(f"target-event-{suffix}", "target"),
                provider=Provider.CODEX,
                native_id=target_id,
                cursor=f"target-cursor-{suffix}",
                native_hash=f"target-hash-{suffix}",
            )
        )
        store.create_link(
            SessionLink(
                id=f"link-{suffix}",
                from_session_id=f"claude:{source_id}",
                to_session_id=f"codex:{target_id}",
                relation=Relation.MIRRORS,
                bridge_id=bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=50.0,
            )
        )
        store.put_context_pack(
            ContextPack(
                id=f"pack-{suffix}",
                bridge_id=bridge_id,
                source_session_id=f"claude:{source_id}",
                target_session_id=f"codex:{target_id}",
                source_cursor=f"source-cursor-{suffix}",
                source_hash=f"source-hash-{suffix}",
                budget_chars=4000,
                payload="handoff",
                created_at=60.0,
            )
        )
        store.transition_link_to_continues(
            bridge_id,
            pack_id=f"pack-{suffix}",
            target_cursor=f"target-cursor-{suffix}",
            target_hash=f"target-hash-{suffix}",
        )

    snapshots = store.list_continuation_snapshots(limit=2)

    assert [snapshot["bridge_id"] for snapshot in snapshots] == [
        "bridge-a",
        "bridge-b",
    ]
    assert store.list_continuation_snapshots(limit=1) == [snapshots[0]]
    assert set(snapshots[0]) == {
        "bridge_id",
        "version",
        "pack_id",
        "source_session_id",
        "source_cursor",
        "source_hash",
        "target_session_id",
        "target_cursor",
        "target_hash",
    }


def test_continuation_snapshot_listing_fails_closed_on_corruption(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state("session-bridge:continuation:bridge-corrupt", {"version": 2})

    with pytest.raises(ValueError, match="continuation snapshot"):
        store.list_continuation_snapshots()


def test_continuation_snapshot_listing_paginates_past_one_thousand(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    _seed_continuation_snapshot_rows(db, 1002)

    first = store.list_continuation_snapshots(limit=1000)
    second = store.list_continuation_snapshots(
        limit=1000, after_bridge_id=first[-1]["bridge_id"]
    )

    assert len(first) == 1000
    assert first[0]["bridge_id"] == "bridge-0000"
    assert first[-1]["bridge_id"] == "bridge-0999"
    assert [snapshot["bridge_id"] for snapshot in second] == [
        "bridge-1000",
        "bridge-1001",
    ]


def test_continuation_snapshot_listing_rejects_noncanonical_cursor_and_state_key(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    _seed_continuation_snapshot_rows(db, 1)
    with pytest.raises(ValueError, match="after bridge ID"):
        store.list_continuation_snapshots(after_bridge_id=" bridge-0000 ")

    valid = store.get_state("session-bridge:continuation:bridge-0000")
    assert valid is not None
    store.set_state("session-bridge:continuation: bridge-0000 ", valid)

    with pytest.raises(ValueError, match="canonical bridge ID"):
        store.list_continuation_snapshots()


@pytest.mark.parametrize(
    ("target_cursor", "target_hash", "message"),
    [("", "target-hash", "target cursor"), ("target-cursor", " ", "target hash")],
)
def test_continuation_transition_requires_nonempty_target_baseline(
    db, target_cursor, target_hash, message
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match=message):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor=target_cursor,
            target_hash=target_hash,
        )


def test_session_launch_metadata_returns_only_title_and_cwd(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("event", "message")))

    metadata = store.get_session_launch_metadata("claude:native-1")

    assert metadata == {
        "title": "claude session",
        "cwd": "C:/workspace/project",
    }
    assert "native_path" not in metadata
    assert store.get_session_launch_metadata("missing") is None


def test_session_launch_metadata_validates_input_and_persisted_types(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    with pytest.raises(ValueError, match="session ID"):
        store.get_session_launch_metadata(" ")

    store.upsert_projection(_projection(_message("event", "message")))
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (b"invalid-title", "claude:native-1"),
        )
    )

    with pytest.raises(ValueError, match="launch metadata"):
        store.get_session_launch_metadata("claude:native-1")


def test_hydrated_context_packs_become_immutable(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
        )
    )
    store.create_link(
        SessionLink(
            id="link-1",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=50.0,
        )
    )
    pack = ContextPack(
        id="pack-1",
        bridge_id="bridge-1",
        source_session_id="claude:native-1",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="original snapshot",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    store.mark_hydrated(
        "bridge-1", source_cursor="cursor-1", source_hash="hash-1", pack_id="pack-1"
    )
    replay = store.put_context_pack(
        replace(pack, id="pack-2", payload="mutated snapshot", created_at=70.0)
    )

    assert replay["id"] == "pack-1"
    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["payload"] == "original snapshot"
    assert persisted["immutable_at"] == 100.0
    assert _rows(db, "SELECT hydrated_at FROM session_links")[0]["hydrated_at"] == 100.0


def test_context_pack_semantic_replay_rejects_source_identity_mismatch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("source-a", "source-b"):
        store.upsert_projection(
            _projection(_message(f"event-{native_id}", native_id), native_id=native_id)
        )
    pack = ContextPack(
        id="pack-a",
        bridge_id="bridge-1",
        source_session_id="claude:source-a",
        target_session_id=None,
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="source A payload",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="source identity"):
        store.put_context_pack(
            replace(
                pack,
                id="pack-b",
                source_session_id="claude:source-b",
                payload="source B payload",
            )
        )

    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["source_session_id"] == "claude:source-a"
    assert persisted["payload"] == "source A payload"


def test_context_pack_replay_rejects_conflicting_non_null_target(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                provider=Provider.CODEX,
                native_id=native_id,
            )
        )
    pack = ContextPack(
        id="pack-a",
        bridge_id="bridge-1",
        source_session_id="claude:native-1",
        target_session_id="codex:target-a",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="target A payload",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="target identity"):
        store.put_context_pack(
            replace(
                pack,
                id="pack-b",
                target_session_id="codex:target-b",
                payload="target B payload",
            )
        )

    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["target_session_id"] == "codex:target-a"
    assert persisted["payload"] == "target A payload"


def test_mark_hydrated_rejects_orphan_pack_without_locking_it(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
        )
    )
    pack = ContextPack(
        id="orphan-pack",
        bridge_id="bridge-orphan",
        source_session_id="claude:native-1",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="orphan",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="matching link"):
        store.mark_hydrated(
            "bridge-orphan",
            source_cursor="cursor-1",
            source_hash="hash-1",
            pack_id="orphan-pack",
        )

    assert (
        store.get_context_pack("bridge-orphan", budget_chars=4000)["immutable_at"]
        is None
    )


def test_mark_hydrated_rejects_pack_whose_identity_does_not_match_link(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("source-a", "source-b"):
        store.upsert_projection(
            _projection(_message(f"event-{native_id}", native_id), native_id=native_id)
        )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
        )
    )
    store.create_link(
        SessionLink(
            id="link-1",
            from_session_id="claude:source-a",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=50.0,
        )
    )
    mismatched = ContextPack(
        id="pack-mismatch",
        bridge_id="bridge-1",
        source_session_id="claude:source-b",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="wrong source",
        created_at=60.0,
    )
    store.put_context_pack(mismatched)

    with pytest.raises(ValueError, match="matching link"):
        store.mark_hydrated(
            "bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            pack_id="pack-mismatch",
        )

    assert store.get_context_pack("bridge-1", budget_chars=4000)["immutable_at"] is None


def test_state_values_are_canonical_snapshots_on_write_and_read(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    value = {"z": [1, {"nested": True}], "a": {"value": "kept"}}

    store.set_state("scan", value)
    value["z"][1]["nested"] = False
    first_read = store.get_state("scan")
    first_read["a"]["value"] = "changed"

    assert store.get_state("scan") == {
        "a": {"value": "kept"},
        "z": [1, {"nested": True}],
    }
    raw_json = _rows(db, "SELECT value_json FROM session_bridge_state")[0]["value_json"]
    assert raw_json == json.dumps(
        {"a": {"value": "kept"}, "z": [1, {"nested": True}]},
        sort_keys=True,
        separators=(",", ":"),
    )


def test_bridge_summaries_are_batched_and_include_catalog_state(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))
    db.create_session("ordinary", "cli")

    summaries = store.get_bridge_summaries([
        "claude:native-1",
        "ordinary",
        "missing",
        "claude:native-1",
    ])

    assert set(summaries) == {"claude:native-1", "ordinary"}
    assert summaries["claude:native-1"]["bridge_provider"] == "claude"
    assert summaries["claude:native-1"]["bridge_mirror_state"] == "catalog_only"
    assert summaries["ordinary"] == {
        "bridge_provider": "hermes",
        "bridge_mirror_state": None,
    }


def test_native_hermes_snapshot_identity_is_stable_and_tracks_messages(db):
    store = SessionBridgeStore(db)
    db.create_session("hermes-native", "tui", cwd="C:/workspace/project")
    db.append_message(
        "hermes-native",
        "user",
        "continue this native Hermes session",
        timestamp=100.0,
    )

    first = store.get_native_session_snapshot("hermes-native")
    replay = store.get_native_session_snapshot("hermes-native")

    assert first == replay
    assert first is not None
    assert first["session_id"] == "hermes-native"
    assert first["provider"] == Provider.HERMES.value
    assert first["cursor"].startswith("hermes:")
    assert len(first["source_hash"]) == 64

    db.append_message(
        "hermes-native",
        "assistant",
        "native continuation ready",
        timestamp=101.0,
    )
    advanced = store.get_native_session_snapshot("hermes-native")

    assert advanced is not None
    assert advanced["cursor"] != first["cursor"]
    assert advanced["source_hash"] != first["source_hash"]


def test_named_profile_hermes_session_is_a_sidebar_candidate_and_snapshot(db, tmp_path):
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    profile_path.parent.mkdir(parents=True)
    profile_db = SessionDB(profile_path)
    try:
        profile_db.create_session(
            "hermes-profile-native",
            "tui",
            cwd="C:/workspace/profile-project",
        )
        profile_db.append_message(
            "hermes-profile-native",
            "user",
            "ship the cross-profile sidebar bridge",
            timestamp=100.0,
        )
        profile_db._execute_write(
            lambda conn: (
                conn.execute("DROP TABLE external_sessions"),
                conn.execute("DROP TABLE session_links"),
            )
        )
    finally:
        profile_db.close()

    store = SessionBridgeStore(
        db,
        hermes_profile_db_paths=lambda: (("main", profile_path),),
    )

    page = store.list_sidebar_candidates(after=0.0, limit=10)
    snapshot = store.get_native_session_snapshot("hermes-profile-native")
    metadata = store.get_session_launch_metadata("hermes-profile-native")

    assert [source.source_session_id for source in page] == ["hermes-profile-native"]
    assert page[0].projection.messages[0].content == (
        "ship the cross-profile sidebar bridge"
    )
    assert page[0].indexed_at is None
    assert snapshot is not None
    assert snapshot["session_id"] == "hermes-profile-native"
    assert snapshot["profile"] == "main"
    assert metadata == {
        "title": None,
        "cwd": "C:/workspace/profile-project",
        "profile": "main",
    }

    candidate = SidebarCandidate(
        source_session_id="hermes-profile-native",
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id("hermes-profile-native"),
        title="[Hermes] ship the cross-profile sidebar bridge",
        cwd="C:/workspace/profile-project",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    queued = store.enqueue_sidebar_job(candidate)
    assert queued["created"] is True
    assert _rows(
        db, "SELECT source FROM sessions WHERE id = ?", ("hermes-profile-native",)
    ) == [{"source": "session_bridge_profile"}]

    db._execute_write(
        lambda conn: (
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                ("codex:profile-target", "codex", 101.0),
            ),
            conn.execute(
                """INSERT INTO session_links (
                       id, from_session_id, to_session_id, relation, bridge_id,
                       created_at, hydrated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "profile-sidebar-link",
                    "hermes-profile-native",
                    "codex:profile-target",
                    Relation.MIRRORS.value,
                    candidate.bridge_id,
                    101.0,
                    None,
                ),
            ),
        )
    )

    read = UnifiedCatalog(db, store).get("hermes-profile-native")
    assert read["session"]["profile"] == "main"
    assert read["session"]["mirror_state"] == "mirrored"
    assert read["session"]["links"] == [
        {
            "id": "profile-sidebar-link",
            "from_session_id": "hermes-profile-native",
            "to_session_id": "codex:profile-target",
            "relation": "mirrors",
            "bridge_id": candidate.bridge_id,
            "created_at": 101.0,
            "hydrated_at": None,
            "diverged_at": None,
        }
    ]
    assert read["messages"][0]["content"] == ("ship the cross-profile sidebar bridge")


def test_named_profile_missing_cwd_can_be_durably_excluded(db, tmp_path):
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    profile_path.parent.mkdir(parents=True)
    profile_db = SessionDB(profile_path)
    try:
        profile_db.create_session("hermes-profile-missing-cwd", "tui", cwd=None)
        profile_db.append_message(
            "hermes-profile-missing-cwd",
            "user",
            "preserve this historical profile session",
            timestamp=100.0,
        )
        profile_db._execute_write(
            lambda conn: (
                conn.execute("DROP TABLE external_sessions"),
                conn.execute("DROP TABLE session_links"),
            )
        )
    finally:
        profile_db.close()

    store = SessionBridgeStore(
        db,
        hermes_profile_db_paths=lambda: (("main", profile_path),),
    )
    assert [source.source_session_id for source in store.list_sidebar_candidates(0, 10)] == [
        "hermes-profile-missing-cwd"
    ]

    result = store.record_sidebar_exclusion(
        "hermes-profile-missing-cwd",
        Provider.HERMES,
        "source_cwd_missing",
        now=200.0,
    )

    assert result["created"] is True
    assert _rows(
        db,
        "SELECT source, cwd FROM sessions WHERE id = ?",
        ("hermes-profile-missing-cwd",),
    ) == [{"source": "session_bridge_profile", "cwd": None}]
    assert list(store.list_sidebar_candidates(0, 10)) == []


def test_native_hermes_snapshot_canonicalizes_binary_and_nonfinite_content(db):
    store = SessionBridgeStore(db)
    db.create_session("hermes-structured", "tui")
    db.append_message(
        "hermes-structured",
        "user",
        b"binary content",
        timestamp=100.0,
    )
    db.append_message(
        "hermes-structured",
        "assistant",
        [float("nan"), float("inf"), float("-inf")],
        timestamp=101.0,
    )

    first = store.get_native_session_snapshot("hermes-structured")
    replay = store.get_native_session_snapshot("hermes-structured")

    assert first == replay
    assert first is not None
    assert first["cursor"].startswith("hermes:2:")
    assert len(first["source_hash"]) == 64


def test_native_hermes_snapshot_rejects_external_sessions(db):
    store = SessionBridgeStore(db)
    store.upsert_projection(_projection(_message("external", "source")))

    assert store.get_native_session_snapshot("claude:native-1") is None


def test_claude_visibility_hermes_inventory_is_independent_and_stable(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    for session_id, source, timestamp in (
        ("hermes-pending", "cli", 105.0),
        ("hermes-visible", "cli", 104.0),
        ("hermes-excluded", "cli", 103.0),
        ("hermes-automation", "cron", 102.0),
        ("hermes-bridge", "cli", 101.0),
        ("hermes-origin", "cli", 100.0),
    ):
        db.create_session(session_id, source, cwd="C:/workspace/project")
        db.append_message(
            session_id, "user", f"meaningful {session_id}", timestamp=timestamp
        )

    def sidebar_candidate(session_id: str) -> SidebarCandidate:
        return SidebarCandidate(
            source_session_id=session_id,
            provider=Provider.HERMES,
            bridge_id=sidebar_bridge_id(session_id),
            title=f"[Hermes] {session_id}",
            cwd="C:/workspace/project",
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
            eligible_at=100.0,
        )

    pending = sidebar_candidate("hermes-pending")
    visible = sidebar_candidate("hermes-visible")
    store.enqueue_sidebar_job(pending)
    store.enqueue_sidebar_job(visible)
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_sidebar_jobs
              SET state = 'sidebar_visible', codex_thread_id = ?, visible_at = ?,
                  completion_digest = ?
            WHERE source_session_id = ?""",
            ("visible-thread", 200.0, "a" * 64, visible.source_session_id),
        )
    )
    store.record_sidebar_exclusion(
        "hermes-excluded", Provider.HERMES, "source_cwd_missing", now=200.0
    )
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               created_at, hydrated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "visibility-bridge-link",
                "hermes-origin",
                "hermes-bridge",
                Relation.CONTINUES.value,
                "bridge:visibility",
                101.0,
                101.0,
            ),
        )
    )
    store.upsert_projection(
        _projection(
            _message("native-claude", "must not pollute Hermes inventory"),
            provider=Provider.CLAUDE,
            native_id="native-claude",
            last_active=106.0,
        )
    )

    sources = store.list_claude_visibility_hermes_sources(after=0.0, limit=20)

    assert [source.source_session_id for source in sources] == [
        "hermes-pending",
        "hermes-visible",
        "hermes-excluded",
        "hermes-automation",
        "hermes-bridge",
        "hermes-origin",
    ]
    by_id = {source.source_session_id: source for source in sources}
    assert by_id["hermes-automation"].automation_only is True
    assert (
        by_id["hermes-bridge"].projection.origin_kind is OriginKind.BRIDGE_CONTINUATION
    )
    assert by_id["hermes-bridge"].projection.origin_bridge_id == "bridge:visibility"
    assert len(by_id) == len(sources)


def test_claude_visibility_hermes_inventory_uses_exact_recorded_worktree_identity(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    records = (
        (
            "hermes-git-one",
            "C:/missing/worktree-one",
            "C:/missing/repo-one",
            "feature/one",
            "a" * 40,
            "worktree:v1:" + "1" * 64,
        ),
        (
            "hermes-git-two",
            "C:/missing/worktree-two",
            "C:/missing/repo-two",
            "feature/two",
            "b" * 40,
            "worktree:v1:" + "2" * 64,
        ),
    )
    for index, (session_id, cwd, git_root, branch, head, worktree_id) in enumerate(
        records
    ):
        db.create_session(session_id, "cli", cwd=cwd)
        db.update_session_cwd(
            session_id, cwd, git_branch=branch, git_repo_root=git_root
        )
        db.append_message(
            session_id,
            "user",
            f"meaningful request {session_id}",
            timestamp=100.0 + index,
        )
        candidate = SidebarCandidate(
            source_session_id=session_id,
            provider=Provider.HERMES,
            bridge_id=sidebar_bridge_id(session_id),
            title=f"[Hermes] {session_id}",
            cwd=cwd,
            git_root=git_root,
            git_branch=branch,
            git_head=head,
            worktree_id=worktree_id,
            eligible_at=100.0 + index,
        )
        snapshot = WorktreeSnapshot(
            cwd=cwd,
            git_root=git_root,
            branch=branch,
            head=head,
            worktree_id=worktree_id,
        )
        store.enqueue_sidebar_job(
            candidate,
            worktree_snapshot=snapshot if index == 0 else None,
        )

    db.create_session("hermes-non-git", "cli", cwd="C:/missing/non-git")
    db.append_message(
        "hermes-non-git", "user", "meaningful non git request", timestamp=99.0
    )

    sources = store.list_claude_visibility_hermes_sources(after=0.0, limit=None)

    by_id = {source.source_session_id: source for source in sources}
    for session_id, cwd, git_root, branch, head, worktree_id in records:
        source = by_id[session_id]
        assert source.projection.cwd == cwd
        assert source.git_root == git_root
        assert source.projection.git_branch == branch
        assert source.git_head == head
        assert source.worktree_id == worktree_id
    assert by_id["hermes-non-git"].git_head is None
    assert by_id["hermes-non-git"].worktree_id is None


def test_claude_visibility_codex_inventory_reuses_full_indexed_projections(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    native = _projection(
        _message(
            "native-user",
            "Build the indexed Codex inventory",
            role="user",
            timestamp=101.0,
        ),
        _message(
            "native-assistant",
            "Assistant content must not be materialized",
            role="assistant",
            timestamp=102.0,
        ),
        provider=Provider.CODEX,
        native_id="indexed-native",
        last_active=120.0,
        cursor="native-cursor",
        native_hash="native-hash",
        git_branch="feature/native",
    )
    placeholder = _projection(
        _message(
            "placeholder-user",
            "Authenticated bridge placeholder",
            role="user",
            timestamp=111.0,
        ),
        provider=Provider.CODEX,
        native_id="indexed-placeholder",
        last_active=130.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge:indexed-placeholder",
    )
    claude = _projection(
        _message("claude-user", "Claude must not pollute Codex inventory"),
        provider=Provider.CLAUDE,
        native_id="native-claude",
        last_active=140.0,
    )
    for projection in (native, placeholder, claude):
        store.upsert_projection(projection)

    sources = store.list_claude_visibility_codex_sources(after=100.0, limit=None)

    assert [source.source_session_id for source in sources] == [
        "codex:indexed-placeholder",
        "codex:indexed-native",
    ]
    by_id = {source.projection.native_id: source for source in sources}
    assert tuple(by_id["indexed-native"].projection.messages) == (
        ProjectedMessage(
            native_event_id="native-user",
            ordinal=0,
            role="user",
            content="Build the indexed Codex inventory",
            timestamp=101.0,
        ),
    )
    assert by_id["indexed-native"].projection.native_cursor == "native-cursor"
    assert by_id["indexed-native"].projection.native_hash == "native-hash"
    assert by_id["indexed-native"].projection.git_branch == "feature/native"
    assert (
        by_id["indexed-placeholder"].projection.origin_kind
        is OriginKind.BRIDGE_PLACEHOLDER
    )
    assert (
        by_id["indexed-placeholder"].projection.origin_bridge_id
        == "bridge:indexed-placeholder"
    )


def test_claude_visibility_source_ids_cover_existing_jobs(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    assert store.list_claude_visibility_source_ids() == frozenset()
    candidate, identity = _claude_visibility_identity("known-source")

    _enqueue_claude_visibility_job(store, candidate, identity)

    assert store.list_claude_visibility_source_ids() == frozenset({
        candidate.source_session_id
    })


def _sidebar_candidate(
    db: SessionDB,
    *,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "sidebar-source",
    eligible_at: float = 100.0,
) -> SidebarCandidate:
    if provider is Provider.CLAUDE:
        source_session_id = canonical_session_id(provider, native_id)
        SessionBridgeStore(db, clock=lambda: eligible_at).upsert_projection(
            _projection(
                _message(f"message-{native_id}", "build the sidebar feature"),
                provider=provider,
                native_id=native_id,
                last_active=eligible_at,
            )
        )
    elif provider is Provider.HERMES:
        source_session_id = canonical_session_id(provider, native_id)
        db.ensure_session(source_session_id, source="cli", started_at=eligible_at)
    else:
        raise ValueError("sidebar candidate helper supports Claude or Hermes")
    return SidebarCandidate(
        source_session_id=source_session_id,
        provider=provider,
        bridge_id=sidebar_bridge_id(source_session_id),
        title=f"[{provider.value}] source",
        cwd="C:/workspace/project",
        git_root="C:/workspace/project",
        git_branch="feature/sidebar",
        git_head="a" * 40,
        worktree_id="worktree-1",
        eligible_at=eligible_at,
    )


def _visible_sidebar_for_hydration(
    db: SessionDB,
    *,
    native_id: str = "hydration-source",
) -> tuple[SessionBridgeStore, SidebarCandidate]:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "sidebar-visible-lease",
            "hydration-lease",
            "hydration-reconcile-lease",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=native_id)
    store.enqueue_sidebar_job(candidate)
    sidebar_lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=sidebar_lease["lease_token"],
        codex_thread_id=f"codex-{native_id}",
        now=110.0,
    )
    return store, candidate


def _seed_hydration(
    store: SessionBridgeStore,
    candidate: SidebarCandidate,
    *,
    now: float = 120.0,
) -> dict[str, object]:
    return store.seed_sidebar_hydration_job(
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        codex_thread_id=f"codex-{candidate.source_session_id.removeprefix('claude:')}",
        source_cursor="cursor-1",
        source_hash="hash-1",
        preview_version=1,
        preview_digest="a" * 64,
        hydration_marker="HERMES_SESSION_HYDRATION_V1:canonical.marker",
        now=now,
    )


def test_sidebar_hydration_seed_is_visible_only_idempotent_and_isolated(db) -> None:
    store, candidate = _visible_sidebar_for_hydration(db)
    sidebar_before = store.get_sidebar_job_for_source(candidate.source_session_id)

    first = _seed_hydration(store, candidate)
    replay = _seed_hydration(store, candidate, now=125.0)

    assert replay == first
    assert first["state"] == SidebarHydrationState.PENDING.value
    assert store.get_sidebar_job_for_source(candidate.source_session_id) == sidebar_before
    with pytest.raises(ValueError, match="identity"):
        store.seed_sidebar_hydration_job(
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            codex_thread_id="different-task",
            source_cursor="cursor-1",
            source_hash="hash-1",
            preview_version=1,
            preview_digest="a" * 64,
            hydration_marker="HERMES_SESSION_HYDRATION_V1:canonical.marker",
            now=130.0,
        )

    pending_store = SessionBridgeStore(db)
    pending_candidate = _sidebar_candidate(db, native_id="hydration-pending")
    pending_store.enqueue_sidebar_job(pending_candidate)
    with pytest.raises(ValueError, match="visible"):
        pending_store.seed_sidebar_hydration_job(
            source_session_id=pending_candidate.source_session_id,
            bridge_id=pending_candidate.bridge_id,
            codex_thread_id="codex-pending",
            source_cursor="cursor-1",
            source_hash="hash-1",
            preview_version=1,
            preview_digest="b" * 64,
            hydration_marker="HERMES_SESSION_HYDRATION_V1:pending.marker",
            now=130.0,
        )


def test_sidebar_hydration_inventory_requires_recent_exact_visible_lineage(
    db,
) -> None:
    now = 1_000_000.0
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "inventory-token-1",
            "inventory-token-2",
            "inventory-token-3",
            "inventory-token-4",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidates = {
        name: _sidebar_candidate(
            db,
            native_id=f"inventory-{name}",
            eligible_at=eligible_at,
        )
        for name, eligible_at in (
            ("eligible", now - 100.0),
            ("old", now - 100_000.0),
            ("missing-lineage", now - 200.0),
            ("already-seeded", now - 300.0),
        )
    }
    for candidate in candidates.values():
        store.enqueue_sidebar_job(candidate)
    claims = store.claim_sidebar_jobs(now=now, limit=4)
    for index, claim in enumerate(claims):
        source_id = str(claim["source_session_id"])
        candidate = next(
            item
            for item in candidates.values()
            if item.source_session_id == source_id
        )
        thread_id = f"inventory-thread-{candidate.source_session_id.split(':')[-1]}"
        store.commit_sidebar_job(
            lease_token=str(claim["lease_token"]),
            codex_thread_id=thread_id,
            now=now + index + 1.0,
        )
        if candidate is candidates["missing-lineage"]:
            continue
        target_id = _seed_sidebar_codex_target(
            store,
            candidate,
            thread_id,
        )
        store.create_link(
            SessionLink(
                id=f"inventory-link-{index}",
                from_session_id=candidate.source_session_id,
                to_session_id=target_id,
                relation=Relation.MIRRORS,
                bridge_id=candidate.bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=now + index + 1.5,
            )
        )
    seeded_candidate = candidates["already-seeded"]
    seeded_job = store.get_sidebar_job_for_source(seeded_candidate.source_session_id)
    assert seeded_job is not None
    store.seed_sidebar_hydration_job(
        source_session_id=seeded_candidate.source_session_id,
        bridge_id=seeded_candidate.bridge_id,
        codex_thread_id=str(seeded_job["codex_thread_id"]),
        source_cursor="inventory-cursor",
        source_hash="inventory-hash",
        preview_version=1,
        preview_digest="b" * 64,
        hydration_marker="HERMES_SESSION_HYDRATION_V1:inventory.marker",
        now=now + 10.0,
    )

    inventory = store.list_sidebar_hydration_candidates(
        now=now,
        backfill_days=1,
        limit=100,
    )

    eligible = candidates["eligible"]
    eligible_job = store.get_sidebar_job_for_source(eligible.source_session_id)
    assert inventory == [
        {
            "job_id": eligible_job["id"],
            "source_session_id": eligible.source_session_id,
            "bridge_id": eligible.bridge_id,
            "codex_thread_id": eligible_job["codex_thread_id"],
            "eligible_at": eligible.eligible_at,
            "visible_at": eligible_job["visible_at"],
        }
    ]


def test_sidebar_hydration_inventory_excludes_tasks_visible_before_signed_cutover(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: 100.0,
        sidebar_token_factory=_token_factory(
            "inventory-pre-cutover-token",
            "inventory-post-cutover-token",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    pre_cutover = _sidebar_candidate(
        db,
        native_id="inventory-pre-cutover",
        eligible_at=90.0,
    )
    post_cutover = _sidebar_candidate(
        db,
        native_id="inventory-post-cutover",
        eligible_at=80.0,
    )

    store.enqueue_sidebar_job(pre_cutover)
    pre_claim = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=str(pre_claim["lease_token"]),
        codex_thread_id="inventory-pre-cutover-thread",
        now=300.0,
    )

    store.enqueue_sidebar_job(post_cutover)
    post_claim = store.claim_sidebar_jobs(now=450.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=str(post_claim["lease_token"]),
        codex_thread_id="inventory-post-cutover-thread",
        now=600.0,
    )

    for index, (candidate, thread_id) in enumerate(
        (
            (pre_cutover, "inventory-pre-cutover-thread"),
            (post_cutover, "inventory-post-cutover-thread"),
        )
    ):
        target_id = _seed_sidebar_codex_target(store, candidate, thread_id)
        store.create_link(
            SessionLink(
                id=f"inventory-cutover-link-{index}",
                from_session_id=candidate.source_session_id,
                to_session_id=target_id,
                relation=Relation.MIRRORS,
                bridge_id=candidate.bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=700.0 + index,
            )
        )

    store.set_state(
        "session-bridge:sidebar:create-reservation-cutover:v1",
        {
            "version": 1,
            "applied_at": 500.0,
            "quarantined_job_ids": [],
        },
    )

    inventory = store.list_sidebar_hydration_candidates(
        now=1_000.0,
        backfill_days=1,
        limit=100,
    )

    post_job = store.get_sidebar_job_for_source(post_cutover.source_session_id)
    assert post_job is not None
    assert post_job["created_at"] < 500.0
    assert inventory == [
        {
            "job_id": post_job["id"],
            "source_session_id": post_cutover.source_session_id,
            "bridge_id": post_cutover.bridge_id,
            "codex_thread_id": post_job["codex_thread_id"],
            "eligible_at": post_cutover.eligible_at,
            "visible_at": post_job["visible_at"],
        }
    ]


def test_sidebar_hydration_reservation_survives_ambiguity_and_never_resends(db) -> None:
    store, candidate = _visible_sidebar_for_hydration(
        db,
        native_id="hydration-ambiguous",
    )
    seeded = _seed_hydration(store, candidate)
    sidebar_before = store.get_sidebar_job_for_source(candidate.source_session_id)

    claim = store.claim_sidebar_hydration_jobs(now=125.0, limit=1)[0]
    assert store.claim_sidebar_hydration_jobs(now=125.0, limit=1) == []
    assert claim["send_reserved"] is False
    reserved = store.reserve_sidebar_hydration_send(
        lease_token=claim["lease_token"],
        now=126.0,
    )
    replay = store.reserve_sidebar_hydration_send(
        lease_token=claim["lease_token"],
        now=127.0,
    )
    assert replay["send_reserved_at"] == reserved["send_reserved_at"] == 126.0

    failed = store.fail_sidebar_hydration_job(
        lease_token=claim["lease_token"],
        error_code="hydration_send_ambiguous",
        codex_thread_id=seeded["codex_thread_id"],
        now=128.0,
    )
    fixed_error_code = failed["error_code"]
    assert fixed_error_code in {
        "marker_conflict",
        "source_identity_mismatch",
        "source_cwd_missing",
        "native_task_not_indexed",
        "hydration_send_ambiguous",
    }
    assert failed["state"] == SidebarHydrationState.RETRY.value
    assert failed["send_reserved_at"] == 126.0
    public_status = store.sidebar_hydration_status(now=128.0)
    assert public_status["health_counts"] == {
        "pending": 0,
        "leased": 0,
        "retry": 0,
        "committed": 0,
        "ambiguous": 1,
        "failed": 0,
    }
    assert "HERMES_SESSION_BRIDGE_V1:" not in repr(public_status)
    assert "HERMES_SESSION_HYDRATION_V1:" not in repr(public_status)

    assert store.claim_sidebar_hydration_jobs(now=129.0, limit=1) == []
    reclaimed = store.claim_sidebar_hydration_jobs(now=143.0, limit=1)[0]
    assert reclaimed["send_reserved"] is True
    assert reclaimed["source_cursor"] == "cursor-1"
    assert reclaimed["source_hash"] == "hash-1"
    assert reclaimed["preview_digest"] == "a" * 64
    with pytest.raises(ValueError, match="marker"):
        store.commit_sidebar_hydration_job(
            lease_token=reclaimed["lease_token"],
            codex_thread_id=seeded["codex_thread_id"],
            hydration_marker="HERMES_SESSION_HYDRATION_V1:different.marker",
            now=130.0,
        )

    committed = store.commit_sidebar_hydration_job(
        lease_token=reclaimed["lease_token"],
        codex_thread_id=seeded["codex_thread_id"],
        hydration_marker=seeded["hydration_marker"],
        now=131.0,
    )
    replay_commit = store.commit_sidebar_hydration_job(
        lease_token=reclaimed["lease_token"],
        codex_thread_id=seeded["codex_thread_id"],
        hydration_marker=seeded["hydration_marker"],
        now=132.0,
    )
    assert replay_commit == committed
    assert committed["state"] == SidebarHydrationState.VISIBLE.value
    assert committed["completion_digest"] == hmac.new(
        b"session-sidebar-hydration-completion-v1",
        b"hydration-reconcile-lease",
        hashlib.sha256,
    ).hexdigest()
    assert store.get_sidebar_job_for_source(candidate.source_session_id) == sidebar_before
    status = store.sidebar_hydration_status(now=132.0)
    assert status["counts"][SidebarHydrationState.VISIBLE.value] == 1
    assert status["active_lease"] is False


def test_sidebar_hydration_status_counts_expired_null_error_lease_as_retry(db) -> None:
    store, candidate = _visible_sidebar_for_hydration(
        db,
        native_id="hydration-expired-null-retry",
    )
    _seed_hydration(store, candidate)
    lease = store.claim_sidebar_hydration_jobs(now=125.0, limit=1)[0]

    with pytest.raises(ValueError, match="hydration lease has expired"):
        store.reserve_sidebar_hydration_send(
            lease_token=lease["lease_token"],
            now=1_000.0,
        )

    assert store.sidebar_hydration_status(now=1_000.0)["health_counts"] == {
        "pending": 0,
        "leased": 0,
        "retry": 1,
        "committed": 0,
        "ambiguous": 0,
        "failed": 0,
    }


def test_sidebar_hydration_operator_recovers_exact_proven_absent_send(db) -> None:
    _visible_store, candidate = _visible_sidebar_for_hydration(
        db,
        native_id="hydration-proven-absent",
    )
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "proven-absent-1",
            "proven-absent-2",
            "proven-absent-3",
            "proven-absent-4",
            "proven-absent-5",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    seeded = _seed_hydration(store, candidate)

    for attempt in range(5):
        attempt_at = 125.0 + attempt * 20.0
        claim = store.claim_sidebar_hydration_jobs(
            now=attempt_at,
            limit=1,
        )[0]
        if attempt == 0:
            store.reserve_sidebar_hydration_send(
                lease_token=claim["lease_token"],
                now=125.5,
            )
        failed = store.fail_sidebar_hydration_job(
            lease_token=claim["lease_token"],
            error_code="hydration_send_ambiguous",
            codex_thread_id=str(seeded["codex_thread_id"]),
            now=attempt_at + 0.75,
        )

    assert failed["state"] == SidebarHydrationState.FAILED.value
    recovered = store.recover_absent_sidebar_hydration_send(
        source_session_id=candidate.source_session_id,
        codex_thread_id=str(seeded["codex_thread_id"]),
        hydration_marker=str(seeded["hydration_marker"]),
        evidence_digest="e" * 64,
        observed_turn_count=1,
        now=200.0,
    )

    assert recovered["state"] == SidebarHydrationState.PENDING.value
    assert recovered["attempts"] == 0
    assert recovered["send_reserved_at"] is None
    assert recovered["error_code"] is None
    evidence = store.get_state(
        "session-bridge:sidebar:hydration-absent-recovery:"
        + str(seeded["id"])
    )
    assert evidence == {
        "version": 1,
        "source_session_id": candidate.source_session_id,
        "codex_thread_id": seeded["codex_thread_id"],
        "hydration_marker_digest": hashlib.sha256(
            str(seeded["hydration_marker"]).encode("utf-8")
        ).hexdigest(),
        "evidence_digest": "e" * 64,
        "observed_turn_count": 1,
        "recovered_at": 200.0,
    }


def _token_factory(*tokens: str):
    iterator = iter(tokens)
    return lambda: next(iterator)


def _record_absence_proof(
    store: SessionBridgeStore,
    lease_token: str,
    *,
    completed_at: float = 100.0,
    expires_at: float = 10_000.0,
    placement_generation: int = 1,
    delivery_generation: int = 1,
    generation: str = "scan:1",
) -> dict[str, Any]:
    evidence = SidebarReconciliationEvidence.create(
        state=SidebarReconciliationState.ABSENCE_PROVEN,
        generation=generation,
        completed_at=completed_at,
        expires_at=expires_at,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=0,
        recovered_thread_id=None,
        fixed_reason=None,
    )
    return store.record_sidebar_reconciliation_proof(
        lease_token=lease_token,
        evidence=evidence,
        marker_digest=evidence.marker_digest,
        placement_generation=placement_generation,
        delivery_generation=delivery_generation,
        now=completed_at,
    )


def _failed_bound_ambiguous_sidebar(
    db: SessionDB,
    *,
    native_id: str,
    token: str,
    thread_id: str,
) -> tuple[SessionBridgeStore, SidebarCandidate, dict[str, object], dict[str, object]]:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(token, f"{token}-next"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=native_id)
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key=f"hermes-session-bridge-create-v1:{native_id}",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=150.0,
    )
    return store, candidate, failed, reservation


def _failed_bound_not_indexed_sidebar(
    db: SessionDB,
    *,
    native_id: str,
    thread_id: str,
) -> tuple[SessionBridgeStore, SidebarCandidate, dict[str, object], dict[str, object]]:
    tokens = tuple(f"{native_id}-token-{attempt}" for attempt in range(1, 7))
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(*tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=native_id)
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key=f"hermes-session-bridge-create-v1:{native_id}",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )
    failed: dict[str, object] | None = None
    for _attempt in range(5):
        failed = store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="native_task_not_indexed",
            codex_thread_id=thread_id,
            now=150.0 if failed is None else float(failed["next_attempt_at"]),
        )
        if failed["state"] == SidebarJobState.RETRY.value:
            lease = store.claim_sidebar_jobs(
                now=float(failed["next_attempt_at"]),
                limit=1,
            )[0]
    assert failed is not None
    assert failed["state"] == SidebarJobState.FAILED.value
    return store, candidate, failed, reservation


def _acknowledge_terminal_resolution(
    store: SessionBridgeStore,
    failed: dict[str, object],
    *,
    evidence_digest: str | None = None,
    now: float = 200.0,
) -> dict[str, object]:
    if evidence_digest is None:
        evidence_digest = _canonical_terminal_evidence_for_test(store, failed)
    return store.acknowledge_sidebar_terminal_resolution(
        job_id=failed["id"],
        codex_thread_id=failed["codex_thread_id"],
        expected_error_code=failed["error_code"],
        expected_attempts=failed["attempts"],
        expected_next_attempt_at=failed["next_attempt_at"],
        expected_updated_at=failed["updated_at"],
        evidence_digest=evidence_digest,
        now=now,
    )


def _canonical_terminal_evidence_for_test(
    store: SessionBridgeStore,
    failed: dict[str, object],
) -> str:
    job = store.get_sidebar_job_by_id(str(failed["id"]))
    assert job is not None
    reservation = store.get_sidebar_create_reservation(str(failed["source_session_id"]))
    assert reservation is not None
    return sidebar_terminal_evidence_digest(job=job, reservation=reservation)


def _drift_terminal_evidence_snapshot(
    db: SessionDB,
    failed: dict[str, object],
    reservation: dict[str, object],
    *,
    snapshot: str,
    field: str,
) -> None:
    if snapshot == "job":
        db._execute_write(
            lambda conn: conn.execute(
                f"UPDATE session_sidebar_jobs SET {field} = {field} + 1 WHERE id = ?",
                (failed["id"],),
            )
        )
        return
    changed = dict(reservation)
    changed[field] = (
        f"{changed[field]}-drift"
        if field == "recovery_key"
        else float(changed[field]) + 1.0
    )
    state_key = (
        "session-bridge:sidebar-create:"
        + hashlib.sha256(str(failed["source_session_id"]).encode("utf-8")).hexdigest()
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_bridge_state SET value_json = ?, updated_at = ? "
            "WHERE key = ?",
            (
                json.dumps(
                    changed,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                160.0,
                state_key,
            ),
        )
    )


def _insert_terminal_resolution_directly(
    db: SessionDB,
    failed: dict[str, object],
    *,
    evidence_digest: str,
    resolved_at: float,
) -> None:
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_sidebar_terminal_resolutions (
                   job_id, idempotency_key, source_session_id, bridge_id,
                   codex_thread_id, failure_state, failure_code,
                   failure_attempts, failure_next_attempt_at,
                   failure_updated_at, resolution_code, evidence_kind,
                   evidence_version, evidence_digest, resolved_at
               ) SELECT id, idempotency_key, source_session_id, bridge_id,
                        codex_thread_id, state, error_code, attempts,
                        next_attempt_at, updated_at, ?, ?, ?, ?, ?
                   FROM session_sidebar_jobs WHERE id = ?""",
            (
                "native_thread_unrecoverable",
                "codex_app_server_read_not_loaded_resume_no_rollout",
                1,
                evidence_digest,
                resolved_at,
                failed["id"],
            ),
        )
    )


def _seed_sidebar_codex_target(
    store: SessionBridgeStore,
    candidate: SidebarCandidate,
    thread_id: str,
    *,
    bridge_id: str | None = None,
) -> str:
    store.upsert_projection(
        _projection(
            _message(f"target-{thread_id}", "Hermes Session Bridge placeholder"),
            provider=Provider.CODEX,
            native_id=thread_id,
            last_active=150.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id or candidate.bridge_id,
        )
    )
    return f"codex:{thread_id}"


def test_sidebar_exclusion_recording_is_idempotent_counted_and_bounded(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db)

    first = store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )
    replay = store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=200.0,
    )

    expected_digest = hashlib.sha256(
        (
            candidate.source_session_id
            + "\0"
            + candidate.provider.value
            + "\0source_cwd_missing"
        ).encode("utf-8")
    ).hexdigest()
    assert first == {
        "source_session_id": candidate.source_session_id,
        "reason_code": "source_cwd_missing",
        "created": True,
    }
    assert replay == {**first, "created": False}
    assert _rows(db, "SELECT * FROM session_sidebar_exclusions") == [
        {
            "source_session_id": candidate.source_session_id,
            "provider": Provider.CLAUDE.value,
            "reason_code": "source_cwd_missing",
            "source_identity_digest": expected_digest,
            "excluded_at": 125.0,
            "updated_at": 125.0,
        }
    ]
    assert SIDEBAR_EXCLUSION_REASONS == frozenset({"source_cwd_missing"})
    assert store.sidebar_exclusion_counts() == {
        "total": 1,
        "by_reason": {"source_cwd_missing": 1},
    }


def test_sidebar_delivery_status_reports_exclusions_without_degradation(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db, native_id="status-exclusion")
    store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )

    status = store.sidebar_delivery_status(now=200.0)

    assert status["counts"]["sidebar_excluded"] == 1
    assert status["recent_error_codes"] == []
    assert status["oldest_pending_age_seconds"] is None
    assert status["delivery_latency_seconds"] == {
        "p50": None,
        "p95": None,
        "p99": None,
    }


def test_sidebar_delivery_status_distinguishes_eligible_and_actionable_ages(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("distinct-sidebar-age"),
    )
    candidate = _sidebar_candidate(
        db,
        native_id="distinct-sidebar-age",
        eligible_at=600.0,
    )
    store.enqueue_sidebar_job(candidate)
    assert len(store.claim_sidebar_jobs(now=900.0, limit=1)) == 1

    status = store.sidebar_delivery_status(now=1_001.0)

    assert status["oldest_eligible_age_seconds"] == 401.0
    assert status["oldest_pending_age_seconds"] == 101.0


def test_sidebar_delivery_status_reports_scheduler_and_recovery_progress(db) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: 150.0,
        sidebar_token_factory=_token_factory("observability-lease"),
    )
    candidate = _sidebar_candidate(db, native_id="observability-source")
    store.enqueue_sidebar_job(candidate)

    assert len(store.claim_sidebar_jobs(now=125.0, limit=1)) == 1
    store.record_sidebar_recovery_progress(
        lane="hydration",
        status="visible",
        now=140.0,
    )

    status = store.sidebar_delivery_status(now=150.0)

    assert status["scheduler"] == {
        "fresh_claims_since_oldest": 1,
        "next_lane": "fresh",
    }
    assert status["recovery"] == {
        "lane": "hydration",
        "status": "visible",
        "last_cycle_at": 140.0,
    }
    rendered = json.dumps(status, sort_keys=True)
    assert "observability-source" not in rendered
    assert "observability-lease" not in rendered


@pytest.mark.parametrize(
    ("lane", "status"),
    (
        ("unknown", "idle"),
        ("hydration", "unknown"),
        ("registration\nsecret", "visible"),
    ),
)
def test_sidebar_recovery_progress_rejects_unfixed_values(
    db,
    lane: str,
    status: str,
) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="sidebar recovery"):
        store.record_sidebar_recovery_progress(
            lane=lane,
            status=status,
            now=125.0,
        )

    assert store.get_state("session-bridge:sidebar:recovery-progress:v1") is None


def test_sidebar_exclusion_replay_fails_closed_on_corrupted_digest(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db, native_id="corrupt-exclusion")
    store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_exclusions SET source_identity_digest = ?",
            ("0" * 64,),
        )
    )

    with pytest.raises(ValueError, match="conflicting sidebar exclusion"):
        store.record_sidebar_exclusion(
            candidate.source_session_id,
            candidate.provider,
            "source_cwd_missing",
            now=200.0,
        )

    assert _rows(
        db,
        "SELECT source_identity_digest, excluded_at, updated_at "
        "FROM session_sidebar_exclusions",
    ) == [
        {
            "source_identity_digest": "0" * 64,
            "excluded_at": 125.0,
            "updated_at": 125.0,
        }
    ]


def test_sidebar_candidates_exclude_persisted_rows_before_limit(db) -> None:
    store = SessionBridgeStore(db)
    older = _sidebar_candidate(db, native_id="older-valid", eligible_at=100.0)
    newer = _sidebar_candidate(db, native_id="newer-excluded", eligible_at=200.0)
    store.record_sidebar_exclusion(
        newer.source_session_id,
        newer.provider,
        "source_cwd_missing",
        now=250.0,
    )

    page = store.list_sidebar_candidates(after=0.0, limit=1)

    assert [candidate.source_session_id for candidate in page] == [
        older.source_session_id
    ]
    assert page.has_more is False


def test_sidebar_all_history_candidates_are_newest_first(db) -> None:
    store = SessionBridgeStore(db)
    store.upsert_projection(
        _projection(
            _message("old", content="recover old"),
            native_id="old",
            last_active=1.0,
        )
    )
    store.upsert_projection(
        _projection(
            _message("new", content="recover new"),
            native_id="new",
            last_active=2.0,
        )
    )

    page = store.list_sidebar_candidates(after=None, limit=10)

    assert [row.source_session_id for row in page] == ["claude:new", "claude:old"]


def test_sidebar_hydration_all_history_includes_visible_task_older_than_3650_days(
    db,
) -> None:
    oldest_visible_at = 10.0
    newest_visible_at = 20.0
    now = newest_visible_at + 3_651 * 86_400.0
    store = SessionBridgeStore(
        db,
        clock=lambda: oldest_visible_at,
        sidebar_token_factory=_token_factory(
            "all-history-hydration-old-token",
            "all-history-hydration-new-token",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidates = {
        native_id: _sidebar_candidate(
            db,
            native_id=f"all-history-hydration-{native_id}",
            eligible_at=visible_at,
        )
        for native_id, visible_at in (
            ("old", oldest_visible_at),
            ("new", newest_visible_at),
        )
    }
    for candidate in candidates.values():
        store.enqueue_sidebar_job(candidate)
    for claim in store.claim_sidebar_jobs(now=newest_visible_at, limit=2):
        candidate = next(
            item
            for item in candidates.values()
            if item.source_session_id == claim["source_session_id"]
        )
        visible_at = (
            newest_visible_at
            if candidate is candidates["new"]
            else oldest_visible_at
        )
        thread_id = f"{candidate.source_session_id}-thread"
        store.commit_sidebar_job(
            lease_token=str(claim["lease_token"]),
            codex_thread_id=thread_id,
            now=visible_at,
        )
        target_id = _seed_sidebar_codex_target(store, candidate, thread_id)
        store.create_link(
            SessionLink(
                id=f"{candidate.source_session_id}-link",
                from_session_id=candidate.source_session_id,
                to_session_id=target_id,
                relation=Relation.MIRRORS,
                bridge_id=candidate.bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=visible_at,
            )
        )

    first_page = store.list_sidebar_hydration_candidates(
        now=now,
        backfill_days=None,
        limit=1,
    )
    second_page = store.list_sidebar_hydration_candidates(
        now=now,
        backfill_days=None,
        limit=1,
        after_visible_at=float(first_page[-1]["visible_at"]),
        after_job_id=str(first_page[-1]["job_id"]),
    )

    assert [row["source_session_id"] for row in (*first_page, *second_page)] == [
        candidates["new"].source_session_id,
        candidates["old"].source_session_id,
    ]


def test_sidebar_enqueue_is_source_idempotent_and_preserves_one_bridge(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)

    first = store.enqueue_sidebar_job(candidate)
    replay = store.enqueue_sidebar_job(candidate)

    assert first["created"] is True
    assert replay["created"] is False
    assert {key: value for key, value in replay.items() if key != "created"} == {
        key: value for key, value in first.items() if key != "created"
    }
    assert first["idempotency_key"] == sidebar_idempotency_key(
        candidate.source_session_id
    )
    assert first["bridge_id"] == candidate.bridge_id
    assert first["state"] == SidebarJobState.PENDING.value
    assert first["eligible_at"] == candidate.eligible_at
    assert first["indexed_at"] == 125.0
    assert len(_rows(db, "SELECT * FROM session_sidebar_jobs")) == 1


def test_sidebar_enqueue_persists_explicit_indexed_at(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db, eligible_at=100.0)

    queued = store.enqueue_sidebar_job(candidate, indexed_at=110.0)

    assert queued["eligible_at"] == 100.0
    assert queued["indexed_at"] == 110.0
    assert queued["created_at"] == 125.0


@pytest.mark.parametrize(
    "indexed_at",
    (True, -1.0, 99.0, float("nan"), float("inf")),
)
def test_sidebar_enqueue_rejects_invalid_indexed_at(db, indexed_at: object) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db, eligible_at=100.0)

    with pytest.raises((TypeError, ValueError)):
        store.enqueue_sidebar_job(candidate, indexed_at=indexed_at)  # type: ignore[arg-type]

    assert _rows(db, "SELECT * FROM session_sidebar_jobs") == []


def test_sidebar_worktree_snapshot_roundtrips_without_git_metadata(
    db: SessionDB,
    tmp_path,
) -> None:
    source = tmp_path / "ordinary-source"
    source.mkdir()
    snapshot = capture_worktree_snapshot(str(source))
    candidate = replace(
        _sidebar_candidate(db, native_id="ordinary-source"),
        cwd=snapshot.cwd,
        git_root=snapshot.git_root,
        git_branch=snapshot.branch,
        git_head=snapshot.head,
        worktree_id=snapshot.worktree_id,
    )
    store = SessionBridgeStore(db, clock=lambda: 125.0)

    store.enqueue_sidebar_job(candidate, worktree_snapshot=snapshot)

    assert store.get_worktree_snapshot(candidate.source_session_id) == snapshot


def test_sidebar_enqueue_persists_versioned_delivery_candidate_across_restart(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    state_key = (
        "session-bridge:sidebar-delivery:"
        + hashlib.sha256(candidate.source_session_id.encode()).hexdigest()
    )

    store.enqueue_sidebar_job(candidate)
    row = _rows(
        db,
        "SELECT key, value_json FROM session_bridge_state WHERE key = ?",
        (state_key,),
    )[0]
    reopened_db = SessionDB(db.db_path)
    try:
        recovered = SessionBridgeStore(reopened_db).get_sidebar_candidate_for_delivery(
            candidate.source_session_id
        )
    finally:
        reopened_db.close()

    assert row["key"] == state_key
    assert json.loads(row["value_json"]) == {
        "version": 1,
        "source_session_id": candidate.source_session_id,
        "provider": candidate.provider.value,
        "bridge_id": candidate.bridge_id,
        "title": candidate.title,
        "cwd": candidate.cwd,
        "git_root": candidate.git_root,
        "git_branch": candidate.git_branch,
        "git_head": candidate.git_head,
        "worktree_id": candidate.worktree_id,
        "eligible_at": candidate.eligible_at,
    }
    assert recovered == candidate


def test_sidebar_duplicate_enqueue_never_overwrites_delivery_candidate(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    store.enqueue_sidebar_job(candidate)
    before = _rows(
        db,
        "SELECT key, value_json, updated_at FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )

    replay = store.enqueue_sidebar_job(candidate)
    with pytest.raises(ValueError, match="conflicting sidebar delivery candidate"):
        store.enqueue_sidebar_job(replace(candidate, title="[Claude] different"))

    after = _rows(
        db,
        "SELECT key, value_json, updated_at FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )
    assert replay["created"] is False
    assert after == before


def test_sidebar_delivery_candidate_is_redacted_at_enqueue(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = replace(
        _sidebar_candidate(db),
        title="[Claude] rotate sk-1234567890abcdefghijkl",
        cwd="C:/workspace?token=must-not-persist",
    )

    store.enqueue_sidebar_job(candidate)
    row = _rows(
        db,
        "SELECT value_json FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )[0]
    recovered = store.get_sidebar_candidate_for_delivery(candidate.source_session_id)

    assert "sk-1234567890abcdefghijkl" not in row["value_json"]
    assert "must-not-persist" not in row["value_json"]
    assert "[REDACTED]" in row["value_json"]
    assert recovered.title == "[Claude] rotate [REDACTED]"
    assert recovered.cwd == "C:/workspace?token=[REDACTED]"


def test_sidebar_delivery_candidate_missing_or_malformed_state_fails_closed(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    store.enqueue_sidebar_job(candidate)
    state_key = (
        "session-bridge:sidebar-delivery:"
        + hashlib.sha256(candidate.source_session_id.encode()).hexdigest()
    )

    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM session_bridge_state WHERE key = ?", (state_key,)
        )
    )
    with pytest.raises(ValueError, match="missing sidebar delivery candidate"):
        store.get_sidebar_candidate_for_delivery(candidate.source_session_id)

    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO session_bridge_state (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (state_key, '{"version":2}', 126.0),
        )
    )
    with pytest.raises(ValueError, match="invalid sidebar delivery candidate"):
        store.get_sidebar_candidate_for_delivery(candidate.source_session_id)


def test_sidebar_enqueue_rejects_a_conflicting_source_bridge_identity(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)

    conflicting = replace(candidate, bridge_id="sidebar:" + "0" * 64)

    with pytest.raises(ValueError, match="bridge ID"):
        store.enqueue_sidebar_job(conflicting)
    assert _rows(db, "SELECT * FROM session_sidebar_jobs") == []
    links_for_source = store.get_bridge_summaries([candidate.source_session_id])[
        candidate.source_session_id
    ]["bridge_links"]
    assert len(links_for_source) <= 1


def test_sidebar_claims_are_ordered_bounded_and_serialize_active_batches_at_rest(
    db,
) -> None:
    tokens = ("opaque-token-a", "opaque-token-b", "opaque-token-c")
    store = SessionBridgeStore(
        db,
        clock=lambda: 200.0,
        sidebar_token_factory=_token_factory(*tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    for native_id, eligible_at in (
        ("later", 30.0),
        ("tie-b", 10.0),
        ("tie-a", 10.0),
    ):
        store.enqueue_sidebar_job(
            _sidebar_candidate(
                db,
                native_id=native_id,
                eligible_at=eligible_at,
            )
        )
    expected = _rows(
        db,
        "SELECT id FROM session_sidebar_jobs "
        "ORDER BY eligible_at DESC, id DESC LIMIT 2",
    )

    claimed = store.claim_sidebar_jobs(now=200.0, limit=2)

    assert [job["id"] for job in claimed] == [row["id"] for row in expected]
    assert [job["lease_token"] for job in claimed] == list(tokens[:2])
    assert all(job["lease_expires_at"] == 500.0 for job in claimed)
    persisted = {
        row["id"]: row for row in _rows(db, "SELECT * FROM session_sidebar_jobs")
    }
    for job, token in zip(claimed, tokens[:2], strict=True):
        row = persisted[job["id"]]
        assert row["lease_digest"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.values()
        assert "lease_token" not in row
    assert store.claim_sidebar_jobs(now=200.0, limit=1) == []
    for claim in claimed:
        store.fail_sidebar_job(
            lease_token=claim["lease_token"],
            error_code="sqlite_busy",
            now=201.0,
        )
    assert store.claim_sidebar_jobs(now=202.0, limit=1)[0]["lease_token"] == tokens[2]
    assert store.claim_sidebar_jobs(now=202.0, limit=1) == []


def test_sidebar_claim_prioritizes_ready_retry_before_older_pending(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("initial-token", "recovery-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    retry = _sidebar_candidate(db, native_id="retry-first", eligible_at=200.0)
    store.enqueue_sidebar_job(retry)
    lease = store.claim_sidebar_jobs(now=300.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="rename_failed",
        now=301.0,
    )
    pending = _sidebar_candidate(db, native_id="older-pending", eligible_at=100.0)
    store.enqueue_sidebar_job(pending)

    claimed = store.claim_sidebar_jobs(now=400.0, limit=1)

    assert claimed[0]["source_session_id"] == retry.source_session_id
    assert claimed[0]["lease_token"] == "recovery-token"
    assert store.get_sidebar_job_for_source(pending.source_session_id)["state"] == (
        SidebarJobState.PENDING.value
    )


def test_sidebar_pending_claims_three_newest_then_oldest(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "fresh-token-1",
            "fresh-token-2",
            "fresh-token-3",
            "oldest-token",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidates = [
        _sidebar_candidate(
            db,
            native_id=f"lane-{eligible_at}",
            eligible_at=float(eligible_at),
        )
        for eligible_at in (10, 20, 30, 40, 50)
    ]
    for candidate in candidates:
        store.enqueue_sidebar_job(candidate)

    claimed = store.claim_sidebar_jobs(now=100.0, limit=4)

    assert [job["source_session_id"] for job in claimed] == [
        candidates[4].source_session_id,
        candidates[3].source_session_id,
        candidates[2].source_session_id,
        candidates[0].source_session_id,
    ]
    assert [job["lease_token"] for job in claimed] == [
        "fresh-token-1",
        "fresh-token-2",
        "fresh-token-3",
        "oldest-token",
    ]


def test_sidebar_pending_claim_lane_survives_store_restart(tmp_path) -> None:
    path = tmp_path / "sidebar-pending-lane.db"
    first_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("first-token", "second-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        candidates = [
            _sidebar_candidate(
                first_db,
                native_id=f"restart-lane-{eligible_at}",
                eligible_at=float(eligible_at),
            )
            for eligible_at in (10, 20, 30, 40, 50)
        ]
        for candidate in candidates:
            first.enqueue_sidebar_job(candidate)
        initial = first.claim_sidebar_jobs(now=100.0, limit=2)
        assert [job["source_session_id"] for job in initial] == [
            candidates[4].source_session_id,
            candidates[3].source_session_id,
        ]
        for index, job in enumerate(initial):
            first.commit_sidebar_job(
                lease_token=job["lease_token"],
                codex_thread_id=f"restart-thread-{index}",
                now=110.0 + index,
            )
    finally:
        first_db.close()

    reopened_db = SessionDB(path)
    try:
        reopened = SessionBridgeStore(
            reopened_db,
            sidebar_token_factory=_token_factory("third-token", "oldest-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )

        continued = reopened.claim_sidebar_jobs(now=120.0, limit=2)

        assert [job["source_session_id"] for job in continued] == [
            candidates[2].source_session_id,
            candidates[0].source_session_id,
        ]
    finally:
        reopened_db.close()


def test_sidebar_pending_claim_lane_rolls_back_with_failed_batch(db) -> None:
    failing = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("duplicate-token", "duplicate-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    older = _sidebar_candidate(db, native_id="rollback-older", eligible_at=10.0)
    newer = _sidebar_candidate(db, native_id="rollback-newer", eligible_at=20.0)
    failing.enqueue_sidebar_job(older)
    failing.enqueue_sidebar_job(newer)

    with pytest.raises(ValueError, match="duplicate"):
        failing.claim_sidebar_jobs(now=100.0, limit=2)

    assert failing.get_state("session-bridge:sidebar:pending-lane:v1") is None
    assert failing.sidebar_job_counts()[SidebarJobState.PENDING.value] == 2
    recovered = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("recovered-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    claim = recovered.claim_sidebar_jobs(now=100.0, limit=1)[0]
    assert claim["source_session_id"] == newer.source_session_id


@pytest.mark.parametrize("limit", [0, 11, True, 1.5])
def test_sidebar_claim_validates_the_fixed_batch_bound(db, limit) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="between 1 and 10"):
        store.claim_sidebar_jobs(now=100.0, limit=limit)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, "100"])
def test_sidebar_claim_rejects_nonfinite_times(db, now) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="finite"):
        store.claim_sidebar_jobs(now=now, limit=1)


@pytest.mark.parametrize(
    "lease_seconds",
    [299, 301, 300.0, True, False, "300", None],
)
def test_sidebar_claim_rejects_every_nonexact_lease_duration(db, lease_seconds) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="exactly 300"):
        store.claim_sidebar_jobs(now=100.0, limit=1, lease_seconds=lease_seconds)


def test_sidebar_claim_accepts_only_exact_integer_five_minute_lease(db) -> None:
    store = SessionBridgeStore(db)

    assert store.claim_sidebar_jobs(now=100.0, limit=1, lease_seconds=300) == []


def test_sidebar_claim_allows_only_one_active_delivery_across_store_instances(
    tmp_path,
) -> None:
    path = tmp_path / "shared-sidebar-state.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("first-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        second = SessionBridgeStore(
            second_db,
            sidebar_token_factory=_token_factory("second-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        for native_id in ("atomic-a", "atomic-b"):
            first.enqueue_sidebar_job(
                _sidebar_candidate(first_db, native_id=native_id, eligible_at=10.0)
            )
        barrier = Barrier(2)

        def _claim(store):
            barrier.wait()
            return store.claim_sidebar_jobs(now=100.0, limit=1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(_claim, (first, second)))

        claimed = [job for batch in batches for job in batch]
        assert len(claimed) == 1
        assert len({job["id"] for job in claimed}) == 1
        assert claimed[0]["lease_token"] in {"first-token", "second-token"}
        assert first.sidebar_job_counts()[SidebarJobState.PENDING.value] == 1
    finally:
        second_db.close()
        first_db.close()


def test_expired_sidebar_lease_is_reclaimed_by_first_claim_with_bound_thread(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("expired-token", "recovered-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="expired-bound", eligible_at=20.0)
    store.enqueue_sidebar_job(candidate)
    first = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.bind_sidebar_thread(
        lease_token=first["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=150.0,
    )

    recovered = store.claim_sidebar_jobs(now=400.0, limit=1)[0]

    assert recovered["source_session_id"] == candidate.source_session_id
    assert recovered["lease_token"] == "recovered-token"
    assert recovered["codex_thread_id"] == "codex-bound-thread"
    assert recovered["state"] == SidebarJobState.LEASED.value
    with pytest.raises(ValueError, match="lease token"):
        store.commit_sidebar_job(
            lease_token=first["lease_token"],
            codex_thread_id="codex-bound-thread",
            now=400.0,
        )


def test_sidebar_commit_requires_exact_unexpired_token_and_is_idempotent(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("commit-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="commit")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    committed = store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-thread-1",
        now=399.999,
    )
    replay = store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-thread-1",
        now=400.0,
    )

    assert replay == committed
    assert committed["state"] == SidebarJobState.VISIBLE.value
    assert committed["codex_thread_id"] == "codex-thread-1"
    assert committed["visible_at"] == 399.999
    assert committed["lease_digest"] is None
    assert committed["lease_expires_at"] is None
    assert committed["completion_digest"] == hashlib.sha256(b"commit-token").hexdigest()


def test_sidebar_bind_persists_exact_thread_across_retry_and_rejects_rebind(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("bind-token", "retry-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bind-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    bound = store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=150.0,
    )
    replay = store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=151.0,
    )
    with pytest.raises(ValueError, match="conflicting Codex thread identity"):
        store.bind_sidebar_thread(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-replacement-thread",
            now=152.0,
        )

    assert replay == bound
    assert bound["state"] == SidebarJobState.LEASED.value
    assert bound["codex_thread_id"] == "codex-bound-thread"

    retried = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="bridge_temporarily_unavailable",
        now=160.0,
    )
    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["codex_thread_id"] == "codex-bound-thread"
    reclaimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert reclaimed["lease_token"] == "retry-token"
    assert reclaimed["codex_thread_id"] == "codex-bound-thread"


def test_sidebar_create_reservation_is_lease_validated_and_survives_reopen(
    tmp_path,
) -> None:
    path = tmp_path / "sidebar-create-reservation.db"
    first_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("reservation-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        candidate = _sidebar_candidate(first_db, native_id="create-reservation")
        first.enqueue_sidebar_job(candidate)
        lease = first.claim_sidebar_jobs(now=100.0, limit=1)[0]
        proof = _record_absence_proof(first, lease["lease_token"])

        reserved = first.reserve_sidebar_create(
            lease_token=lease["lease_token"],
            recovery_key="hermes-session-bridge-create-v1:recovery-key",
            reconciliation_proof_digest=proof["proof_digest"],
            reconciliation_generation=proof["reconciliation_generation"],
            now=110.0,
        )
        replay = first.reserve_sidebar_create(
            lease_token=lease["lease_token"],
            recovery_key="hermes-session-bridge-create-v1:recovery-key",
            reconciliation_proof_digest=proof["proof_digest"],
            reconciliation_generation=proof["reconciliation_generation"],
            now=111.0,
        )

        assert replay == reserved
        assert reserved["source_session_id"] == candidate.source_session_id
        assert reserved["bridge_id"] == candidate.bridge_id
        assert reserved["reserved_at"] == 110.0
        with pytest.raises(ValueError, match="create reservation"):
            first.reserve_sidebar_create(
                lease_token=lease["lease_token"],
                recovery_key="hermes-session-bridge-create-v1:different",
                reconciliation_proof_digest=proof["proof_digest"],
                reconciliation_generation=proof["reconciliation_generation"],
                now=112.0,
            )
    finally:
        first_db.close()

    reopened_db = SessionDB(path)
    try:
        reopened = SessionBridgeStore(reopened_db)
        assert (
            reopened.get_sidebar_create_reservation(candidate.source_session_id)
            == reserved
        )
    finally:
        reopened_db.close()


def test_concurrent_sidebar_create_reserve_has_one_exact_replay(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("concurrent-create-reserve"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="concurrent-create-reserve")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_absence_proof(store, lease["lease_token"])
    barrier = Barrier(2)

    def reserve() -> dict[str, Any]:
        barrier.wait(timeout=5)
        return store.reserve_sidebar_create(
            lease_token=lease["lease_token"],
            recovery_key="hermes-session-bridge-create-v1:concurrent",
            reconciliation_proof_digest=proof["proof_digest"],
            reconciliation_generation=proof["reconciliation_generation"],
            now=110.0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: reserve(), range(2)))

    assert results[0] == results[1]
    assert (
        store.get_sidebar_create_reservation(candidate.source_session_id)
        == results[0]
    )


def test_sidebar_reconciliation_proof_is_durable_append_only_and_replay_safe(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("proof-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="reconciliation-proof")
    queued = store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    evidence = SidebarReconciliationEvidence.create(
        state=SidebarReconciliationState.ABSENCE_PROVEN,
        generation="scan:1",
        completed_at=100.0,
        expires_at=130.0,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=0,
        recovered_thread_id=None,
        fixed_reason=None,
    )

    proof = store.record_sidebar_reconciliation_proof(
        lease_token=lease["lease_token"],
        evidence=evidence,
        marker_digest="1" * 64,
        placement_generation=1,
        delivery_generation=1,
        now=100.0,
    )
    replay = store.record_sidebar_reconciliation_proof(
        lease_token=lease["lease_token"],
        evidence=evidence,
        marker_digest="1" * 64,
        placement_generation=1,
        delivery_generation=1,
        now=101.0,
    )

    assert replay == proof
    assert proof["job_id"] == queued["id"]
    assert proof["state"] == "absence_proven"
    assert proof["match_count"] == 0
    assert store.get_sidebar_reconciliation_proof(
        lease_token=lease["lease_token"]
    ) == proof
    persisted = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert persisted["reconciliation_proof_digest"] == proof["proof_digest"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE session_sidebar_reconciliation_proofs SET state='blocked'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute("DELETE FROM session_sidebar_reconciliation_proofs")


def test_sidebar_status_reports_sanitized_reconciliation_health(db) -> None:
    store = SessionBridgeStore(db)
    proof_states = (
        ("recovered", None, "codex-recovered", "sidebar_visible", None, 130.0),
        ("absence_proven", None, None, "sidebar_visible", None, 130.0),
        ("absence_proven", None, None, "sidebar_pending", None, 99.0),
        ("blocked", "marker_conflict", None, "sidebar_failed", "marker_conflict", 130.0),
        (
            "blocked",
            "bridge_temporarily_unavailable",
            None,
            "sidebar_failed",
            "native_create_ambiguous",
            130.0,
        ),
        (
            "blocked",
            "bridge_temporarily_unavailable",
            None,
            "sidebar_failed",
            "native_create_ambiguous",
            130.0,
        ),
    )
    for index, (state, reason, thread_id, job_state, error_code, expires_at) in enumerate(
        proof_states
    ):
        candidate = _sidebar_candidate(
            db,
            native_id=f"reconciliation-status-{index}",
            eligible_at=60.0,
        )
        job = store.enqueue_sidebar_job(candidate)
        proof_digest = hashlib.sha256(f"proof-{index}".encode()).hexdigest()
        db._conn.execute(
            """INSERT INTO session_sidebar_reconciliation_proofs (
                   proof_digest, job_id, source_session_id, bridge_id,
                   marker_digest, placement_generation, delivery_generation,
                   reconciliation_generation, completed_at, expires_at,
                   inventory_digest, state, match_count, recovered_thread_id,
                   fixed_reason, created_at
               ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, 90, ?, ?, ?, ?, ?, ?, 90)""",
            (
                proof_digest,
                job["id"],
                candidate.source_session_id,
                candidate.bridge_id,
                hashlib.sha256(f"marker-{index}".encode()).hexdigest(),
                f"scan:private:{index}",
                expires_at,
                hashlib.sha256(f"inventory-{index}".encode()).hexdigest(),
                state,
                1 if state == "recovered" else (2 if state == "blocked" else 0),
                thread_id,
                reason,
            ),
        )
        visible = job_state == "sidebar_visible"
        db._conn.execute(
            """UPDATE session_sidebar_jobs
               SET state = ?, codex_thread_id = ?, error_code = ?,
                   visible_at = ?, completion_digest = ?,
                   reconciliation_proof_digest = ?
               WHERE id = ?""",
            (
                job_state,
                f"codex-visible-{index}" if visible else None,
                error_code,
                95.0 if visible else None,
                hashlib.sha256(f"complete-{index}".encode()).hexdigest()
                if visible
                else None,
                proof_digest,
                job["id"],
            ),
        )

    status = store.sidebar_delivery_status(now=100.0)

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
    assert status["recovered_existing_total"] == 1
    assert status["created_new_total"] == 1
    encoded = json.dumps(status)
    assert "scan:private" not in encoded
    assert "proof_digest" not in encoded
    assert "marker_digest" not in encoded


@pytest.mark.parametrize(
    ("state", "match_count", "thread_id", "reason"),
    [
        ("recovered", 0, "codex-thread", None),
        ("absence_proven", 0, "codex-thread", None),
        ("blocked", 0, None, None),
    ],
)
def test_sidebar_reconciliation_proof_schema_rejects_invalid_state_shape(
    db,
    state: str,
    match_count: int,
    thread_id: str | None,
    reason: str | None,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"proof-shape-{state}"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"proof-shape-{state}")
    job = store.enqueue_sidebar_job(candidate)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db._conn.execute(
            """INSERT INTO session_sidebar_reconciliation_proofs (
                   proof_digest, job_id, source_session_id, bridge_id,
                   marker_digest, placement_generation, delivery_generation,
                   reconciliation_generation, completed_at, expires_at,
                   inventory_digest, state, match_count, recovered_thread_id,
                   fixed_reason, created_at
               ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, 100, 130, ?, ?, ?, ?, ?, 100)""",
            (
                hashlib.sha256(state.encode()).hexdigest(),
                job["id"],
                candidate.source_session_id,
                candidate.bridge_id,
                "1" * 64,
                "scan:invalid",
                "2" * 64,
                state,
                match_count,
                thread_id,
                reason,
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "stale_digest",
        "expired",
        "recovered",
        "placement_generation",
        "delivery_generation",
        "reconciliation_generation",
    ],
)
def test_reserve_sidebar_create_rejects_stale_or_changed_proof(
    db,
    mutation: str,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"proof-reserve-{mutation}"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"proof-reserve-{mutation}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    state = (
        SidebarReconciliationState.RECOVERED
        if mutation == "recovered"
        else SidebarReconciliationState.ABSENCE_PROVEN
    )
    generation = (
        "scan:new" if mutation == "reconciliation_generation" else "scan:1"
    )
    evidence = SidebarReconciliationEvidence.create(
        state=state,
        generation=generation,
        completed_at=100.0,
        expires_at=105.0 if mutation == "expired" else 130.0,
        inventory_digest="2" * 64,
        marker_digest="1" * 64,
        match_count=1 if state is SidebarReconciliationState.RECOVERED else 0,
        recovered_thread_id=(
            "codex-proof-reserve" if state is SidebarReconciliationState.RECOVERED else None
        ),
        fixed_reason=None,
    )
    proof = store.record_sidebar_reconciliation_proof(
        lease_token=lease["lease_token"],
        evidence=evidence,
        marker_digest=evidence.marker_digest,
        placement_generation=2 if mutation == "placement_generation" else 1,
        delivery_generation=2 if mutation == "delivery_generation" else 1,
        now=100.0,
    )
    supplied_digest = (
        "f" * 64 if mutation == "stale_digest" else proof["proof_digest"]
    )
    supplied_generation = (
        "scan:1"
        if mutation == "reconciliation_generation"
        else proof["reconciliation_generation"]
    )

    with pytest.raises(ValueError, match="reconciliation proof"):
        store.reserve_sidebar_create(
            lease_token=lease["lease_token"],
            recovery_key="hermes-session-bridge-create-v1:proof-boundary",
            reconciliation_proof_digest=supplied_digest,
            reconciliation_generation=supplied_generation,
            now=110.0,
        )

    assert store.get_sidebar_create_reservation(candidate.source_session_id) is None


def test_sidebar_failure_atomically_retains_known_thread_after_bind_ambiguity(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("bind-loss", "bind-loss-retry"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bind-loss")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    retried = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="sqlite_busy",
        codex_thread_id="codex-bind-loss-thread",
        now=120.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["codex_thread_id"] == "codex-bind-loss-thread"
    reclaimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert reclaimed["codex_thread_id"] == "codex-bind-loss-thread"


def test_expired_sidebar_bind_retains_exact_thread_across_restart(tmp_path) -> None:
    path = tmp_path / "expired-sidebar-bind.db"
    first_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("expired-bind-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        candidate = _sidebar_candidate(first_db, native_id="expired-bind-retention")
        first.enqueue_sidebar_job(candidate)
        lease = first.claim_sidebar_jobs(now=100.0, limit=1)[0]

        with pytest.raises(ValueError, match="sidebar lease has expired"):
            first.bind_sidebar_thread(
                lease_token=lease["lease_token"],
                codex_thread_id="codex-expired-bind-thread",
                now=400.0,
            )
    finally:
        first_db.close()

    reopened_db = SessionDB(path)
    try:
        reopened = SessionBridgeStore(reopened_db)
        persisted = reopened.get_sidebar_job_for_source(candidate.source_session_id)
        assert persisted["state"] == SidebarJobState.RETRY.value
        assert persisted["codex_thread_id"] == "codex-expired-bind-thread"
    finally:
        reopened_db.close()


def test_sidebar_create_reservation_cutover_quarantines_expired_legacy_lease_once(
    db,
) -> None:
    marker_secret = b"cutover-marker-secret" * 2
    store = SessionBridgeStore(
        db,
        clock=lambda: 100.0,
        sidebar_token_factory=_token_factory("cutover-expired-token"),
    )
    legacy = _sidebar_candidate(
        db,
        native_id="cutover-expired",
        eligible_at=90.0,
    )
    pristine = _sidebar_candidate(
        db,
        native_id="cutover-pristine",
        eligible_at=80.0,
    )
    legacy_job = store.enqueue_sidebar_job(legacy)
    pristine_job = store.enqueue_sidebar_job(pristine)
    store.claim_sidebar_jobs(now=100.0, limit=1)

    applied = store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=400.0,
    )

    assert applied == {
        "version": 1,
        "applied_at": 400.0,
        "quarantined": 1,
        "replayed": False,
    }
    assert store.get_state("session-bridge:sidebar:create-reservation-cutover:v1") == {
        "version": 1,
        "applied_at": 400.0,
        "quarantined_job_ids": [legacy_job["id"]],
    }
    reservation = store.get_sidebar_create_reservation(legacy.source_session_id)
    assert reservation is not None
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=legacy.bridge_id,
            source_session_id=legacy.source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_secret,
    )
    assert reservation["recovery_key"] == sidebar_create_recovery_key(
        marker,
        marker_secret,
    )
    assert store.get_sidebar_create_reservation(pristine.source_session_id) is None
    assert reservation["recovery_key"] not in json.dumps(applied)
    assert marker_secret.hex() not in json.dumps(applied)

    replay = store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=500.0,
    )
    assert replay == {
        "version": 1,
        "applied_at": 400.0,
        "quarantined": 1,
        "replayed": True,
    }

    def _post_cutover_retry(conn):
        conn.execute(
            """UPDATE session_sidebar_jobs
                  SET state = ?, attempts = 1, error_code = ?,
                      next_attempt_at = 600.0, updated_at = 500.0
                WHERE id = ?""",
            (
                SidebarJobState.RETRY.value,
                "sqlite_busy",
                pristine_job["id"],
            ),
        )

    db._execute_write(_post_cutover_retry)
    store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=550.0,
    )
    assert store.get_sidebar_create_reservation(pristine.source_session_id) is None


def test_sidebar_create_reservation_cutover_refuses_an_active_lease(db) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: 100.0,
        sidebar_token_factory=_token_factory("cutover-active-token"),
    )
    candidate = _sidebar_candidate(db, native_id="cutover-active")
    store.enqueue_sidebar_job(candidate)
    store.claim_sidebar_jobs(now=100.0, limit=1)

    with pytest.raises(ValueError, match="active sidebar lease"):
        store.apply_sidebar_create_reservation_cutover(
            marker_secret=b"active-cutover-secret" * 2,
            now=200.0,
        )

    assert (
        store.get_state("session-bridge:sidebar:create-reservation-cutover:v1") is None
    )
    assert store.get_sidebar_create_reservation(candidate.source_session_id) is None


def test_sidebar_create_reservation_cutover_replay_fails_on_reservation_drift(
    db,
) -> None:
    marker_secret = b"replay-cutover-secret" * 2
    store = SessionBridgeStore(
        db,
        clock=lambda: 100.0,
        sidebar_token_factory=_token_factory("cutover-replay-token"),
    )
    candidate = _sidebar_candidate(db, native_id="cutover-replay")
    store.enqueue_sidebar_job(candidate)
    store.claim_sidebar_jobs(now=100.0, limit=1)
    store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=400.0,
    )
    reservation_key = (
        "session-bridge:sidebar-create:"
        + hashlib.sha256(candidate.source_session_id.encode("utf-8")).hexdigest()
    )
    reservation = store.get_state(reservation_key)
    assert reservation is not None
    reservation["recovery_key"] = "hermes-session-bridge-create-v1:tampered"
    store.set_state(reservation_key, reservation)

    with pytest.raises(ValueError, match="cutover.*reservation"):
        store.apply_sidebar_create_reservation_cutover(
            marker_secret=marker_secret,
            now=500.0,
        )


def test_expired_sidebar_fail_retains_exact_thread_across_restart(tmp_path) -> None:
    path = tmp_path / "expired-sidebar-fail.db"
    first_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("expired-fail-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        candidate = _sidebar_candidate(first_db, native_id="expired-fail-retention")
        first.enqueue_sidebar_job(candidate)
        lease = first.claim_sidebar_jobs(now=100.0, limit=1)[0]

        with pytest.raises(ValueError, match="sidebar lease has expired"):
            first.fail_sidebar_job(
                lease_token=lease["lease_token"],
                error_code="bridge_temporarily_unavailable",
                codex_thread_id="codex-expired-fail-thread",
                now=400.0,
            )
    finally:
        first_db.close()

    reopened_db = SessionDB(path)
    try:
        reopened = SessionBridgeStore(reopened_db)
        persisted = reopened.get_sidebar_job_for_source(candidate.source_session_id)
        assert persisted["state"] == SidebarJobState.RETRY.value
        assert persisted["codex_thread_id"] == "codex-expired-fail-thread"
    finally:
        reopened_db.close()


def test_sidebar_execution_blockers_isolate_a_known_failed_row(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("hard-stop-failed"),
    )
    store.enqueue_sidebar_job(_sidebar_candidate(db, native_id="hard-stop-failed"))
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="source_identity_mismatch",
        now=110.0,
    )

    assert store.sidebar_execution_blockers() == ()


def test_precreate_cutover_resolution_is_append_only_and_unblocks_without_native_id(
    db,
) -> None:
    marker_secret = b"precreate-cutover-terminal-secret"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("precreate-terminal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="precreate-terminal")
    queued = store.enqueue_sidebar_job(candidate)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET state = ?, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (SidebarJobState.RETRY.value, 105.0, 105.0, queued["id"]),
        )
    )
    assert store.apply_sidebar_create_reservation_cutover(
        marker_secret=marker_secret,
        now=110.0,
    ) == {
        "version": 1,
        "applied_at": 110.0,
        "quarantined": 1,
        "replayed": False,
    }
    lease = store.claim_sidebar_jobs(now=120.0, limit=1)[0]
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=130.0,
    )
    assert failed["codex_thread_id"] is None
    assert failed["attempts"] == 0
    reservation = store.get_sidebar_create_reservation(candidate.source_session_id)
    assert reservation is not None
    cutover = store.get_state("session-bridge:sidebar:create-reservation-cutover:v1")
    assert cutover is not None
    reservation_key = (
        "session-bridge:sidebar-create:"
        + hashlib.sha256(candidate.source_session_id.encode("utf-8")).hexdigest()
    )
    forged_reservation = {
        **reservation,
        "recovery_key": "hermes-session-bridge-create-v1:forged-cutover-key",
    }
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_bridge_state SET value_json = ? WHERE key = ?",
            (
                json.dumps(
                    forged_reservation,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                reservation_key,
            ),
        )
    )
    forged_evidence = sidebar_precreate_terminal_evidence_digest(
        job=failed,
        reservation=forged_reservation,
        cutover=cutover,
        candidate=candidate,
    )
    with pytest.raises(ValueError, match="precreate resolution"):
        store.acknowledge_sidebar_precreate_resolution(
            job_id=failed["id"],
            expected_error_code=failed["error_code"],
            expected_attempts=failed["attempts"],
            expected_next_attempt_at=failed["next_attempt_at"],
            expected_updated_at=failed["updated_at"],
            evidence_digest=forged_evidence,
            marker_secret=marker_secret,
            now=135.0,
        )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_bridge_state SET value_json = ? WHERE key = ?",
            (
                json.dumps(
                    reservation,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                reservation_key,
            ),
        )
    )
    evidence = sidebar_precreate_terminal_evidence_digest(
        job=failed,
        reservation=reservation,
        cutover=cutover,
        candidate=candidate,
    )

    first = store.acknowledge_sidebar_precreate_resolution(
        job_id=failed["id"],
        expected_error_code=failed["error_code"],
        expected_attempts=failed["attempts"],
        expected_next_attempt_at=failed["next_attempt_at"],
        expected_updated_at=failed["updated_at"],
        evidence_digest=evidence,
        marker_secret=marker_secret,
        now=140.0,
    )
    replay = store.acknowledge_sidebar_precreate_resolution(
        job_id=failed["id"],
        expected_error_code=failed["error_code"],
        expected_attempts=failed["attempts"],
        expected_next_attempt_at=failed["next_attempt_at"],
        expected_updated_at=failed["updated_at"],
        evidence_digest=evidence,
        marker_secret=marker_secret,
        now=150.0,
    )

    assert first == {
        "job_id": failed["id"],
        "state": SidebarJobState.FAILED.value,
        "error_code": "native_create_ambiguous",
        "resolution_code": "precutover_create_unrecoverable",
        "created": True,
    }
    assert replay == {**first, "created": False}
    [audit] = _rows(db, "SELECT * FROM session_sidebar_precreate_resolutions")
    assert audit["job_id"] == failed["id"]
    assert audit["resolution_code"] == "precutover_create_unrecoverable"
    assert audit["evidence_digest"] == evidence
    assert store.sidebar_execution_blockers() == ()
    status = store.sidebar_delivery_status(now=160.0)
    assert status["blocking_failed_count"] == 0
    assert status["terminally_resolved_failed_count"] == 1
    assert status["terminal_resolutions"] == {
        "total": 1,
        "effective": 1,
        "ineffective": 0,
        "by_resolution_code": {
            "native_thread_unrecoverable": 0,
            "precutover_create_unrecoverable": 1,
        },
    }
    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="native_create_ambiguous",
            now=170.0,
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_sidebar_precreate_resolutions "
                "SET resolved_at = 180.0 WHERE job_id = ?",
                (failed["id"],),
            )
        )
    delivery_key = (
        "session-bridge:sidebar-delivery:"
        + hashlib.sha256(candidate.source_session_id.encode("utf-8")).hexdigest()
    )
    delivery_snapshot = store.get_state(delivery_key)
    assert delivery_snapshot is not None
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_bridge_state SET value_json = ? WHERE key = ?",
            (
                json.dumps(
                    {**delivery_snapshot, "cwd": "C:/workspace/drifted"},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                delivery_key,
            ),
        )
    )
    drifted = store.sidebar_delivery_status(now=190.0)
    assert drifted["blocking_failed_count"] == 1
    assert drifted["terminally_resolved_failed_count"] == 0
    assert drifted["ineffective_terminal_resolution_count"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "DELETE FROM session_sidebar_precreate_resolutions WHERE job_id = ?",
                (failed["id"],),
            )
        )


def test_unbound_create_resolution_is_append_only_after_exact_absence(db) -> None:
    marker_secret = b"unbound-create-terminal-secret"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "unbound-terminal-token-1",
            "unbound-terminal-token-2",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="unbound-terminal")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=candidate.bridge_id,
            source_session_id=candidate.source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_secret,
    )
    proof = _record_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key=sidebar_create_recovery_key(marker, marker_secret),
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=105.0,
    )
    retry = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="bridge_temporarily_unavailable",
        now=110.0,
    )
    lease = store.claim_sidebar_jobs(now=retry["next_attempt_at"], limit=1)[0]
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=retry["next_attempt_at"] + 1.0,
    )
    assert failed["state"] == SidebarJobState.FAILED.value
    assert failed["attempts"] > 0
    assert failed["codex_thread_id"] is None
    evidence = sidebar_unbound_terminal_evidence_digest(
        job=failed,
        reservation=reservation,
        candidate=candidate,
    )

    first = store.acknowledge_sidebar_unbound_resolution(
        job_id=failed["id"],
        expected_error_code="native_create_ambiguous",
        expected_attempts=failed["attempts"],
        expected_next_attempt_at=failed["next_attempt_at"],
        expected_updated_at=failed["updated_at"],
        evidence_digest=evidence,
        marker_secret=marker_secret,
        now=failed["updated_at"] + 1.0,
    )
    replay = store.acknowledge_sidebar_unbound_resolution(
        job_id=failed["id"],
        expected_error_code="native_create_ambiguous",
        expected_attempts=failed["attempts"],
        expected_next_attempt_at=failed["next_attempt_at"],
        expected_updated_at=failed["updated_at"],
        evidence_digest=evidence,
        marker_secret=marker_secret,
        now=failed["updated_at"] + 2.0,
    )

    assert first == {
        "job_id": failed["id"],
        "state": SidebarJobState.FAILED.value,
        "error_code": "native_create_ambiguous",
        "resolution_code": "native_create_unrecoverable",
        "created": True,
    }
    assert replay == {**first, "created": False}
    [audit] = _rows(db, "SELECT * FROM session_sidebar_unbound_resolutions")
    assert audit["failure_attempts"] == failed["attempts"]
    assert audit["reservation_reserved_at"] == reservation["reserved_at"]
    assert audit["evidence_digest"] == evidence
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "DELETE FROM session_sidebar_unbound_resolutions WHERE job_id = ?",
                (failed["id"],),
            )
        )
    status = store.sidebar_delivery_status(now=failed["updated_at"] + 3.0)
    assert status["blocking_failed_count"] == 0
    assert status["terminally_resolved_failed_count"] == 1
    assert status["terminal_resolutions"]["by_resolution_code"][
        "native_create_unrecoverable"
    ] == 1
    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="native_create_ambiguous",
            now=failed["updated_at"] + 4.0,
        )


def test_sidebar_terminal_resolution_is_append_only_and_unblocks_unrelated_work(
    db,
) -> None:
    store, candidate, failed, reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-resolution",
        token="terminal-resolution-token",
        thread_id="019f-terminal-resolution-thread",
    )
    before_job = store.get_sidebar_job_for_source(candidate.source_session_id)
    before_reservation = store.get_sidebar_create_reservation(
        candidate.source_session_id
    )
    pending = _sidebar_candidate(db, native_id="terminal-resolution-pending")
    store.enqueue_sidebar_job(pending)
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)

    resolved = _acknowledge_terminal_resolution(store, failed)

    assert resolved == {
        "job_id": failed["id"],
        "state": SidebarJobState.FAILED.value,
        "error_code": "native_create_ambiguous",
        "resolution_code": "native_thread_unrecoverable",
        "created": True,
    }
    assert store.get_sidebar_job_for_source(candidate.source_session_id) == before_job
    assert (
        store.get_sidebar_create_reservation(candidate.source_session_id)
        == before_reservation
        == reservation
    )
    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == [
        {
            "job_id": failed["id"],
            "idempotency_key": failed["idempotency_key"],
            "source_session_id": candidate.source_session_id,
            "bridge_id": candidate.bridge_id,
            "codex_thread_id": "019f-terminal-resolution-thread",
            "failure_state": SidebarJobState.FAILED.value,
            "failure_code": "native_create_ambiguous",
            "failure_attempts": failed["attempts"],
            "failure_next_attempt_at": failed["next_attempt_at"],
            "failure_updated_at": failed["updated_at"],
            "resolution_code": "native_thread_unrecoverable",
            "evidence_kind": ("codex_app_server_read_not_loaded_resume_no_rollout"),
            "evidence_version": 1,
            "evidence_digest": evidence_digest,
            "resolved_at": 200.0,
        }
    ]
    assert _rows(db, "SELECT * FROM session_sidebar_exclusions") == []
    assert (
        _rows(
            db,
            "SELECT * FROM session_links WHERE bridge_id = ?",
            (candidate.bridge_id,),
        )
        == []
    )
    assert (
        _rows(
            db,
            "SELECT * FROM external_sessions WHERE provider = 'codex' AND native_id = ?",
            (failed["codex_thread_id"],),
        )
        == []
    )
    assert store.sidebar_execution_blockers() == ()
    claimed = store.claim_sidebar_jobs(now=200.0, limit=1)
    assert [row["source_session_id"] for row in claimed] == [pending.source_session_id]
    assert store.get_sidebar_job_for_source(candidate.source_session_id) == before_job

    status = store.sidebar_delivery_status(now=200.0)
    assert status["counts"][SidebarJobState.FAILED.value] == 1
    assert status["blocking_failed_count"] == 0
    assert status["terminally_resolved_failed_count"] == 1
    assert status["terminal_resolutions"] == {
        "total": 1,
        "effective": 1,
        "ineffective": 0,
        "by_resolution_code": {"native_thread_unrecoverable": 1},
    }
    assert status["execution_blockers"] == []


def test_sidebar_terminal_resolution_replay_is_idempotent_and_conflicts_fail_closed(
    db,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-replay",
        token="terminal-replay-token",
        thread_id="019f-terminal-replay-thread",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)

    first = _acknowledge_terminal_resolution(store, failed, now=200.0)
    replay = _acknowledge_terminal_resolution(store, failed, now=300.0)

    assert replay == {**first, "created": False}
    assert _rows(
        db,
        "SELECT evidence_digest, resolved_at FROM session_sidebar_terminal_resolutions",
    ) == [{"evidence_digest": evidence_digest, "resolved_at": 200.0}]
    with pytest.raises(ValueError, match="terminal resolution"):
        _acknowledge_terminal_resolution(
            store,
            failed,
            evidence_digest="f" * 64,
            now=400.0,
        )


def test_sidebar_terminal_resolution_rejects_a_sha_shaped_fabricated_digest(
    db,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-fabricated-evidence",
        token="terminal-fabricated-evidence-token",
        thread_id="019f-terminal-fabricated-evidence",
    )

    with pytest.raises(ValueError, match="terminal resolution evidence"):
        _acknowledge_terminal_resolution(
            store,
            failed,
            evidence_digest="f" * 64,
        )

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []


@pytest.mark.parametrize(
    ("snapshot", "field"),
    (
        ("job", "eligible_at"),
        ("job", "created_at"),
        ("reservation", "recovery_key"),
        ("reservation", "reserved_at"),
    ),
)
def test_sidebar_terminal_resolution_rejects_digest_bound_snapshot_drift(
    db,
    snapshot: str,
    field: str,
) -> None:
    store, _candidate, failed, reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id=f"terminal-evidence-drift-{snapshot}-{field}",
        token=f"terminal-evidence-drift-{snapshot}-{field}-token",
        thread_id=f"019f-terminal-evidence-drift-{snapshot}-{field}",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)

    _drift_terminal_evidence_snapshot(
        db,
        failed,
        reservation,
        snapshot=snapshot,
        field=field,
    )

    with pytest.raises(ValueError, match="terminal resolution evidence"):
        _acknowledge_terminal_resolution(
            store,
            failed,
            evidence_digest=evidence_digest,
        )

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []


def test_sidebar_terminal_resolution_waiver_rejects_forged_legacy_evidence(
    db,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-forged-legacy-evidence",
        token="terminal-forged-legacy-evidence-token",
        thread_id="019f-terminal-forged-legacy-evidence",
    )
    _insert_terminal_resolution_directly(
        db,
        failed,
        evidence_digest="f" * 64,
        resolved_at=200.0,
    )

    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_mismatch",
    )


@pytest.mark.parametrize(
    ("snapshot", "field"),
    (
        ("job", "eligible_at"),
        ("job", "created_at"),
        ("reservation", "recovery_key"),
        ("reservation", "reserved_at"),
    ),
)
def test_sidebar_terminal_resolution_waiver_downgrades_on_evidence_snapshot_drift(
    db,
    snapshot: str,
    field: str,
) -> None:
    store, _candidate, failed, reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id=f"terminal-waiver-drift-{snapshot}-{field}",
        token=f"terminal-waiver-drift-{snapshot}-{field}-token",
        thread_id=f"019f-terminal-waiver-drift-{snapshot}-{field}",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)
    _acknowledge_terminal_resolution(
        store,
        failed,
        evidence_digest=evidence_digest,
    )

    _drift_terminal_evidence_snapshot(
        db,
        failed,
        reservation,
        snapshot=snapshot,
        field=field,
    )

    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_mismatch",
    )


def test_sidebar_terminal_resolution_rejects_resolution_before_failure_time(
    db,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-invalid-chronology",
        token="terminal-invalid-chronology-token",
        thread_id="019f-terminal-invalid-chronology",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)

    with pytest.raises(ValueError, match="terminal resolution time"):
        _acknowledge_terminal_resolution(
            store,
            failed,
            evidence_digest=evidence_digest,
            now=float(failed["updated_at"]) - 1.0,
        )

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []


def test_sidebar_terminal_resolution_schema_rejects_invalid_chronology(db) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-schema-chronology",
        token="terminal-schema-chronology-token",
        thread_id="019f-terminal-schema-chronology",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_terminal_resolution_directly(
            db,
            failed,
            evidence_digest=evidence_digest,
            resolved_at=float(failed["updated_at"]) - 1.0,
        )

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []


def test_sidebar_terminal_resolution_ledger_rows_cannot_be_changed_or_deleted(
    db,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-immutable",
        token="terminal-immutable-token",
        thread_id="019f-terminal-immutable-thread",
    )
    _acknowledge_terminal_resolution(store, failed)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_sidebar_terminal_resolutions SET resolved_at = 300"
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "DELETE FROM session_sidebar_terminal_resolutions"
            )
        )
    existing = _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions")[0]
    replacement = {**existing, "evidence_digest": "f" * 64, "resolved_at": 300.0}
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                """INSERT OR REPLACE INTO session_sidebar_terminal_resolutions (
                       job_id, idempotency_key, source_session_id, bridge_id,
                       codex_thread_id, failure_state, failure_code,
                       failure_attempts, failure_next_attempt_at,
                       failure_updated_at, resolution_code, evidence_kind,
                       evidence_version, evidence_digest, resolved_at
                   ) VALUES (
                       :job_id, :idempotency_key, :source_session_id, :bridge_id,
                       :codex_thread_id, :failure_state, :failure_code,
                       :failure_attempts, :failure_next_attempt_at,
                       :failure_updated_at, :resolution_code, :evidence_kind,
                       :evidence_version, :evidence_digest, :resolved_at
                   )""",
                replacement,
            )
        )
    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == [existing]


@pytest.mark.parametrize(
    ("parameter", "replacement"),
    (
        ("job_id", "sidebar-job:" + "0" * 64),
        ("codex_thread_id", "019f-wrong-terminal-thread"),
        ("expected_error_code", "marker_conflict"),
        ("expected_attempts", 1),
        ("expected_next_attempt_at", 151.0),
        ("expected_updated_at", 151.0),
        ("evidence_digest", "INVALID"),
    ),
)
def test_sidebar_terminal_resolution_cas_rejects_every_expected_snapshot_mismatch(
    db,
    parameter: str,
    replacement: object,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id=f"terminal-cas-{parameter}",
        token=f"terminal-cas-{parameter}-token",
        thread_id=f"019f-terminal-cas-{parameter}",
    )
    evidence_digest = _canonical_terminal_evidence_for_test(store, failed)
    arguments = {
        "job_id": failed["id"],
        "codex_thread_id": failed["codex_thread_id"],
        "expected_error_code": failed["error_code"],
        "expected_attempts": failed["attempts"],
        "expected_next_attempt_at": failed["next_attempt_at"],
        "expected_updated_at": failed["updated_at"],
        "evidence_digest": evidence_digest,
        "now": 200.0,
    }
    arguments[parameter] = replacement

    with pytest.raises(ValueError):
        store.acknowledge_sidebar_terminal_resolution(**arguments)

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []
    assert store.sidebar_execution_blockers() == ()


@pytest.mark.parametrize("materialization", ("external", "lineage"))
def test_sidebar_terminal_resolution_becomes_ineffective_after_materialization(
    db,
    materialization: str,
) -> None:
    store, candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id=f"terminal-materialized-{materialization}",
        token=f"terminal-materialized-{materialization}-token",
        thread_id=f"019f-terminal-materialized-{materialization}",
    )
    _acknowledge_terminal_resolution(store, failed)
    assert store.sidebar_execution_blockers() == ()

    if materialization == "external":
        _seed_sidebar_codex_target(
            store,
            candidate,
            failed["codex_thread_id"],
        )
    else:
        target_session_id = f"codex:terminal-lineage-{materialization}"
        db.ensure_session(target_session_id, source="codex")
        store.create_link(
            SessionLink(
                id=f"terminal-lineage-{materialization}",
                from_session_id=candidate.source_session_id,
                to_session_id=target_session_id,
                relation=Relation.MIRRORS,
                bridge_id=candidate.bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=210.0,
            )
        )

    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_mismatch",
    )
    status = store.sidebar_delivery_status(now=220.0)
    assert status["blocking_failed_count"] == 1
    assert status["counts"]["needs_attention"] == 1
    assert status["terminally_resolved_failed_count"] == 0
    assert status["terminal_resolutions"] == {
        "total": 1,
        "effective": 0,
        "ineffective": 1,
        "by_resolution_code": {"native_thread_unrecoverable": 0},
    }
    with pytest.raises(ValueError, match="terminal resolution"):
        _acknowledge_terminal_resolution(store, failed, now=230.0)


@pytest.mark.parametrize(
    "ledger_shape", ("missing", "malformed", "missing_immutable_trigger")
)
def test_sidebar_terminal_resolution_ledger_failure_is_fail_closed(
    db,
    ledger_shape: str,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id=f"terminal-ledger-{ledger_shape}",
        token=f"terminal-ledger-{ledger_shape}-token",
        thread_id=f"019f-terminal-ledger-{ledger_shape}",
    )
    _acknowledge_terminal_resolution(store, failed)

    def _break_ledger(conn: sqlite3.Connection) -> None:
        conn.execute("DROP TRIGGER trg_session_sidebar_terminal_resolutions_no_update")
        if ledger_shape == "missing_immutable_trigger":
            return
        conn.execute("DROP TRIGGER trg_session_sidebar_terminal_resolutions_no_delete")
        conn.execute("DROP TABLE session_sidebar_terminal_resolutions")
        if ledger_shape == "malformed":
            conn.execute(
                "CREATE TABLE session_sidebar_terminal_resolutions (job_id TEXT)"
            )
            conn.execute(
                "INSERT INTO session_sidebar_terminal_resolutions (job_id) VALUES (?)",
                (failed["id"],),
            )

    db._execute_write(_break_ledger)

    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_ledger_invalid",
    )
    assert store.claim_sidebar_jobs(now=300.0, limit=1) == []
    status = store.sidebar_delivery_status(now=300.0)
    assert status["blocking_failed_count"] == 1
    assert status["terminally_resolved_failed_count"] == 0
    assert status["terminal_resolutions"]["effective"] == 0
    assert status["terminal_resolution_ledger_valid"] is False


@pytest.mark.parametrize(
    ("trigger_name", "replacement_sql"),
    (
        (
            "trg_session_sidebar_precreate_resolutions_no_replacement",
            """CREATE TRIGGER trg_session_sidebar_precreate_resolutions_no_replacement
               BEFORE INSERT ON session_sidebar_precreate_resolutions
               WHEN EXISTS (SELECT 1 FROM session_sidebar_terminal_resolutions)
               BEGIN
                   SELECT RAISE(ABORT, 'sidebar precreate resolutions are immutable');
               END""",
        ),
        (
            "trg_session_sidebar_precreate_resolutions_no_update",
            """CREATE TRIGGER trg_session_sidebar_precreate_resolutions_no_update
               BEFORE UPDATE ON session_sidebar_precreate_resolutions
               WHEN 0
               BEGIN
                   SELECT RAISE(ABORT, 'sidebar precreate resolutions are immutable');
               END""",
        ),
        (
            "trg_session_sidebar_precreate_resolutions_no_delete",
            """CREATE TRIGGER trg_session_sidebar_precreate_resolutions_no_delete
               BEFORE DELETE ON session_sidebar_precreate_resolutions
               WHEN 0
               BEGIN
                   SELECT RAISE(ABORT, 'sidebar precreate resolutions are immutable');
               END""",
        ),
    ),
)
def test_sidebar_precreate_resolution_rejects_weakened_immutable_trigger(
    db,
    trigger_name: str,
    replacement_sql: str,
) -> None:
    store = SessionBridgeStore(db)

    def _weaken(conn: sqlite3.Connection) -> None:
        conn.execute(f'DROP TRIGGER "{trigger_name}"')
        conn.execute(replacement_sql)

    db._execute_write(_weaken)

    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_ledger_invalid",
    )
    status = store.sidebar_delivery_status(now=300.0)
    assert status["terminal_resolution_ledger_valid"] is False


def test_sidebar_terminal_resolution_refuses_an_unprotected_ledger(db) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-ledger-unprotected-write",
        token="terminal-ledger-unprotected-write-token",
        thread_id="019f-terminal-ledger-unprotected-write",
    )
    db._execute_write(
        lambda conn: conn.execute(
            "DROP TRIGGER trg_session_sidebar_terminal_resolutions_no_update"
        )
    )

    with pytest.raises(ValueError, match="terminal resolution ledger"):
        _acknowledge_terminal_resolution(store, failed)

    assert _rows(db, "SELECT * FROM session_sidebar_terminal_resolutions") == []
    assert store.sidebar_execution_blockers() == (
        "sidebar_terminal_resolution_ledger_invalid",
    )


def test_sidebar_terminal_resolution_concurrent_replay_converges_once(tmp_path) -> None:
    path = tmp_path / "terminal-concurrent.db"
    seed_db = SessionDB(path)
    seed_store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        seed_db,
        native_id="terminal-concurrent",
        token="terminal-concurrent-token",
        thread_id="019f-terminal-concurrent",
    )
    del seed_store
    seed_db.close()
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        stores = (SessionBridgeStore(first_db), SessionBridgeStore(second_db))
        barrier = Barrier(2)

        def resolve(store: SessionBridgeStore) -> dict[str, object]:
            barrier.wait()
            return _acknowledge_terminal_resolution(store, failed)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(resolve, stores))

        assert sorted(result["created"] for result in results) == [False, True]
        assert (
            len(_rows(first_db, "SELECT * FROM session_sidebar_terminal_resolutions"))
            == 1
        )
    finally:
        first_db.close()
        second_db.close()


def test_sidebar_retry_rejects_any_job_with_terminal_resolution_history(db) -> None:
    store, candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-retry-defense",
        token="terminal-retry-defense-token",
        thread_id="019f-terminal-retry-defense",
    )
    _acknowledge_terminal_resolution(store, failed)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET codex_thread_id = NULL WHERE id = ?",
            (failed["id"],),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="native_create_ambiguous",
            now=300.0,
        )

    row = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert row is not None
    assert row["state"] == SidebarJobState.FAILED.value
    assert row["error_code"] == "native_create_ambiguous"


@pytest.mark.parametrize(
    ("mutation_sql", "expected_blockers", "expected_blocking_failed"),
    (
        (
            "UPDATE session_sidebar_jobs SET state = 'sidebar_pending' WHERE id = ?",
            ("sidebar_terminal_resolution_mismatch",),
            0,
        ),
        (
            "UPDATE session_sidebar_jobs SET updated_at = updated_at + 1 WHERE id = ?",
            ("sidebar_terminal_resolution_mismatch",),
            1,
        ),
    ),
)
def test_sidebar_terminal_resolution_snapshot_drift_is_an_explicit_blocker(
    db,
    mutation_sql: str,
    expected_blockers: tuple[str, ...],
    expected_blocking_failed: int,
) -> None:
    store, _candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="terminal-snapshot-drift",
        token="terminal-snapshot-drift-token",
        thread_id="019f-terminal-snapshot-drift",
    )
    _acknowledge_terminal_resolution(store, failed)
    db._execute_write(lambda conn: conn.execute(mutation_sql, (failed["id"],)))

    assert store.sidebar_execution_blockers() == expected_blockers
    status = store.sidebar_delivery_status(now=300.0)
    assert status["blocking_failed_count"] == expected_blocking_failed
    assert status["terminally_resolved_failed_count"] == 0
    assert status["ineffective_terminal_resolution_count"] == 1
    assert status["terminal_resolutions"] == {
        "total": 1,
        "effective": 0,
        "ineffective": 1,
        "by_resolution_code": {"native_thread_unrecoverable": 0},
    }
    assert status["execution_blockers"] == list(expected_blockers)
    with pytest.raises(ValueError, match="terminal resolution"):
        _acknowledge_terminal_resolution(store, failed, now=310.0)


def test_sidebar_execution_blockers_report_unknown_retry_code(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("hard-stop-retry"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="hard-stop-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="sqlite_busy",
        now=110.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE source_session_id = ?",
            ("future_retry_code", candidate.source_session_id),
        )
    )

    assert store.sidebar_execution_blockers() == ("unknown_retry_code",)


def test_sidebar_claim_isolates_failed_row_and_claims_pending_work(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "failed-row-token",
            "pending-row-token",
        ),
    )
    failed = _sidebar_candidate(db, native_id="hard-stop-existing-failed")
    pending = _sidebar_candidate(db, native_id="hard-stop-still-pending")
    store.enqueue_sidebar_job(failed)
    failed_lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=failed_lease["lease_token"],
        error_code="source_identity_mismatch",
        now=110.0,
    )
    store.enqueue_sidebar_job(pending)

    claimed = store.claim_sidebar_jobs(now=200.0, limit=1)
    assert len(claimed) == 1
    assert claimed[0]["source_session_id"] == pending.source_session_id
    pending_row = store.get_sidebar_job_for_source(pending.source_session_id)
    assert pending_row is not None
    assert pending_row["state"] == SidebarJobState.LEASED.value


def test_sidebar_active_lease_probe_distinguishes_persisted_worker_ownership(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("active-probe"),
    )
    store.enqueue_sidebar_job(_sidebar_candidate(db, native_id="active-probe"))
    store.claim_sidebar_jobs(now=100.0, limit=1)

    assert store.sidebar_has_active_lease(now=399.999) is True
    assert store.sidebar_has_active_lease(now=400.0) is False


def test_sidebar_atomic_lineage_commit_and_exact_replay_are_idempotent(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="atomic")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = "44444444-4444-4444-8444-444444444444"
    target_id = _seed_sidebar_codex_target(store, candidate, thread_id)

    committed = store.commit_sidebar_job_with_lineage(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        placement_generation=1,
        now=200.0,
    )
    replay = store.commit_sidebar_job_with_lineage(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        placement_generation=1,
        now=201.0,
    )

    assert replay == committed
    assert committed["state"] == SidebarJobState.VISIBLE.value
    assert committed["placement_generation"] == 1
    assert committed["placement_verified_at"] == 200.0
    links = _rows(
        db, "SELECT * FROM session_links WHERE bridge_id = ?", (candidate.bridge_id,)
    )
    assert len(links) == 1
    assert links[0]["from_session_id"] == candidate.source_session_id
    assert links[0]["to_session_id"] == target_id
    assert links[0]["relation"] == Relation.MIRRORS.value


@pytest.mark.parametrize("failure", ["wrong_token", "expired", "wrong_target"])
def test_sidebar_atomic_lineage_commit_has_no_partial_state_on_validation_failure(
    db,
    failure: str,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-failure-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"atomic-{failure}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = f"atomic-{failure}-thread"
    _seed_sidebar_codex_target(
        store,
        candidate,
        thread_id,
        bridge_id=("sidebar:wrong" if failure == "wrong_target" else None),
    )

    with pytest.raises(ValueError):
        store.commit_sidebar_job_with_lineage(
            lease_token=(
                "wrong-token" if failure == "wrong_token" else lease["lease_token"]
            ),
            codex_thread_id=thread_id,
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            placement_generation=1,
            now=400.0 if failure == "expired" else 200.0,
        )

    assert (
        _rows(
            db,
            "SELECT * FROM session_links WHERE bridge_id = ?",
            (candidate.bridge_id,),
        )
        == []
    )
    job = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert job is not None
    assert job["state"] == (
        SidebarJobState.RETRY.value
        if failure == "expired"
        else SidebarJobState.LEASED.value
    )
    assert job["codex_thread_id"] is None


def test_sidebar_atomic_lineage_write_fault_rolls_back_job_and_link(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-link-fault-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="atomic-link-fault")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = "atomic-link-fault-thread"
    target_id = _seed_sidebar_codex_target(store, candidate, thread_id)
    collision_id = (
        "sidebar-link:"
        + hashlib.sha256(
            f"{candidate.bridge_id}\0{candidate.source_session_id}\0{target_id}".encode()
        ).hexdigest()
    )
    db.ensure_session("collision-source", source="cli")
    db.ensure_session("collision-target", source="cli")
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                collision_id,
                "collision-source",
                "collision-target",
                Relation.MIRRORS.value,
                "collision-bridge",
                1.0,
            ),
        )
    )

    with pytest.raises(ValueError, match="collision"):
        store.commit_sidebar_job_with_lineage(
            lease_token=lease["lease_token"],
            codex_thread_id=thread_id,
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            placement_generation=1,
            now=200.0,
        )

    assert (
        _rows(
            db,
            "SELECT * FROM session_links WHERE bridge_id = ?",
            (candidate.bridge_id,),
        )
        == []
    )
    job = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert job is not None
    assert job["state"] == SidebarJobState.LEASED.value
    assert job["codex_thread_id"] is None


def test_sidebar_completion_replay_requires_the_exact_placement_generation(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("placement-replay-token"),
    )
    candidate = _sidebar_candidate(db, native_id="placement-replay")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = "placement-replay-thread"
    _seed_sidebar_codex_target(store, candidate, thread_id)
    store.commit_sidebar_job_with_lineage(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        placement_generation=1,
        now=200.0,
    )

    with pytest.raises(ValueError, match="conflicting sidebar completion replay"):
        store.commit_sidebar_job_with_lineage(
            lease_token=lease["lease_token"],
            codex_thread_id=thread_id,
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            placement_generation=2,
            now=201.0,
        )


def test_sidebar_delivery_status_counts_only_verified_current_generation(
    db,
    tmp_path: Path,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("placement-status-token"),
    )
    verified = _sidebar_candidate(db, native_id="placement-verified")
    store.enqueue_sidebar_job(verified)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="placement-verified-thread",
        now=200.0,
    )
    with db._lock:
        assert db._conn is not None
        db._conn.execute(
            """UPDATE session_sidebar_jobs
               SET placement_generation = ?, placement_verified_at = ?
               WHERE source_session_id = ?""",
            (1, 200.0, verified.source_session_id),
        )
        db._conn.commit()

    legacy = _sidebar_candidate(db, native_id="placement-legacy")
    legacy_job = store.enqueue_sidebar_job(legacy)
    mismatch = _sidebar_candidate(db, native_id="placement-mismatch")
    mismatch_job = store.enqueue_sidebar_job(mismatch)
    with db._lock:
        assert db._conn is not None
        db._conn.execute(
            """UPDATE session_sidebar_jobs
               SET state = 'sidebar_visible', completion_digest = ?,
                   codex_thread_id = ?, visible_at = ?, updated_at = ?
               WHERE id = ?""",
            ("c" * 64, "placement-legacy-thread", 210.0, 210.0, legacy_job["id"]),
        )
        db._conn.execute(
            """UPDATE session_sidebar_jobs
               SET state = 'sidebar_failed', error_code = 'placement_mismatch',
                   updated_at = ? WHERE id = ?""",
            (220.0, mismatch_job["id"]),
        )
        db._conn.commit()
    store.record_sidebar_placement_canary(
        status="passed",
        placement_generation=1,
        verified_at=230.0,
        canary_identity="codex:private-canary-task",
    )

    status = store.sidebar_delivery_status(
        now=240.0,
        inbox_cwd=str(tmp_path),
        placement_generation=1,
    )

    assert status["placement"] == {
        "inbox_cwd": str(tmp_path),
        "generation": 1,
        "verified_visible": 1,
        "mismatch_count": 1,
        "canary": {"status": "passed", "verified_at": 230.0},
    }
    assert status["counts"]["projectless_legacy_count"] == 1
    raw_state = store.get_state("session-bridge:sidebar:placement-canary:v1")
    assert raw_state is not None
    assert set(raw_state) == {
        "version",
        "status",
        "placement_generation",
        "verified_at",
        "canary_identity_digest",
    }
    assert raw_state["canary_identity_digest"] != "codex:private-canary-task"
    assert "private-canary-task" not in json.dumps(status)


def test_sidebar_placement_canary_rejects_unknown_persisted_fields(db) -> None:
    store = SessionBridgeStore(db)
    store.set_state(
        "session-bridge:sidebar:placement-canary:v1",
        {
            "version": 1,
            "status": "passed",
            "placement_generation": 1,
            "verified_at": 200.0,
            "canary_identity_digest": "a" * 64,
            "task_id": "must-not-leak",
        },
    )

    with pytest.raises(ValueError, match="invalid sidebar placement canary state"):
        store.sidebar_delivery_status(
            now=240.0,
            inbox_cwd="C:\\Users\\diego\\.hermes",
            placement_generation=1,
        )


def test_sidebar_placement_canary_rejects_negative_verified_at_without_write(
    db,
) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(
        ValueError,
        match="sidebar placement canary verified_at",
    ):
        store.record_sidebar_placement_canary(
            status="passed",
            placement_generation=1,
            verified_at=-0.001,
            canary_identity="codex:negative-canary-task",
        )

    assert store.get_state("session-bridge:sidebar:placement-canary:v1") is None


def test_sidebar_placement_canary_rejects_negative_persisted_verified_at(db) -> None:
    store = SessionBridgeStore(db)
    store.set_state(
        "session-bridge:sidebar:placement-canary:v1",
        {
            "version": 1,
            "status": "passed",
            "placement_generation": 1,
            "verified_at": -0.001,
            "canary_identity_digest": "a" * 64,
        },
    )

    with pytest.raises(ValueError, match="invalid sidebar placement canary state"):
        store.sidebar_delivery_status(
            now=240.0,
            inbox_cwd="C:\\Users\\diego\\.hermes",
            placement_generation=1,
        )


def test_sidebar_delivery_latency_uses_fixed_recent_indexed_sample(db) -> None:
    sample_limit = 512
    row_count = sample_limit + 75

    def _seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            [
                (f"claude:latency-{index}", "claude", float(index))
                for index in range(row_count)
            ],
        )
        conn.executemany(
            """INSERT INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, completion_digest,
                   codex_thread_id, eligible_at, indexed_at, created_at,
                   updated_at, visible_at
               ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    f"latency-job-{index:04d}",
                    f"latency-key-{index:04d}",
                    f"claude:latency-{index}",
                    f"latency-bridge-{index:04d}",
                    SidebarJobState.VISIBLE.value,
                    float(index),
                    f"latency-digest-{index:04d}",
                    f"latency-thread-{index:04d}",
                    float(index - 990 if index < 75 else index),
                    float(index + 2),
                    float(index + 4),
                    float(index + 10),
                    float(index + 10),
                )
                for index in range(row_count)
            ],
        )

    db._execute_write(_seed)
    statements: list[str] = []
    with db._lock:
        db._conn.set_trace_callback(statements.append)
    try:
        status = SessionBridgeStore(db).sidebar_delivery_status(now=10_000.0)
    finally:
        with db._lock:
            db._conn.set_trace_callback(None)
    with db._lock:
        plan = db._conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT visible_at - eligible_at AS latency
                 FROM session_sidebar_jobs
                WHERE state = ? AND visible_at IS NOT NULL
                ORDER BY visible_at DESC, id DESC LIMIT ?""",
            (SidebarJobState.VISIBLE.value, sample_limit),
        ).fetchall()

    latency_queries = [
        " ".join(statement.upper().split())
        for statement in statements
        if "VISIBLE_AT - ELIGIBLE_AT AS LATENCY" in statement.upper()
    ]
    assert latency_queries
    assert all("LIMIT 512" in statement for statement in latency_queries)
    assert any(
        "USING INDEX IDX_SESSION_SIDEBAR_JOBS_VISIBLE_AT" in row[3].upper()
        for row in plan
    )
    assert status["delivery_latency_seconds"] == {
        "p50": 10.0,
        "p95": 10.0,
        "p99": 10.0,
    }
    assert status["stage_latency_seconds"] == {
        "source_to_index": {"p50": 2.0, "p95": 2.0},
        "index_to_queue": {"p50": 2.0, "p95": 2.0},
        "queue_to_visible": {"p50": 6.0, "p95": 6.0},
        "source_to_visible": {"p50": 10.0, "p95": 10.0},
    }


def test_sidebar_actionable_age_uses_fresh_lease_time_then_ages_while_leased(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: 0.0,
        sidebar_token_factory=_token_factory("leased-age-token"),
    )
    candidate = _sidebar_candidate(db, native_id="leased-age", eligible_at=0.0)
    store.enqueue_sidebar_job(candidate)

    store.claim_sidebar_jobs(now=100.0, limit=1)

    fresh = store.sidebar_delivery_status(now=100.0)
    stale = store.sidebar_delivery_status(now=281.0)
    assert fresh["counts"][SidebarJobState.LEASED.value] == 1
    assert fresh["oldest_pending_age_seconds"] == 0.0
    assert stale["oldest_pending_age_seconds"] == 181.0


def test_sidebar_broker_heartbeat_rejects_invalid_timestamps(db) -> None:
    store = SessionBridgeStore(db)

    for invalid in (True, float("nan"), float("inf"), "100", None):
        with pytest.raises((TypeError, ValueError)):
            store.record_sidebar_broker_heartbeat(now=invalid)  # type: ignore[arg-type]


def test_sidebar_broker_heartbeat_is_monotonic_across_overlapping_stores(db) -> None:
    """An older request finishing late must not overwrite a newer heartbeat."""

    older_store = SessionBridgeStore(db)
    newer_db = SessionDB(db.db_path)
    newer_store = SessionBridgeStore(newer_db)
    older_started = Event()
    newer_finished = Event()

    def finish_older_request_late() -> None:
        older_started.set()
        assert newer_finished.wait(timeout=5)
        older_store.record_sidebar_broker_heartbeat(now=100.0)

    def finish_newer_request_first() -> None:
        assert older_started.wait(timeout=5)
        newer_store.record_sidebar_broker_heartbeat(now=200.0)
        newer_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            older = executor.submit(finish_older_request_late)
            newer = executor.submit(finish_newer_request_first)
            older.result(timeout=5)
            newer.result(timeout=5)

        assert older_store.get_state("session-bridge:sidebar:broker-heartbeat") == {
            "at": 200.0
        }
    finally:
        newer_db.close()


def test_sidebar_broker_heartbeat_recovers_malformed_ephemeral_state(db) -> None:
    store = SessionBridgeStore(db)

    def seed_malformed(conn) -> None:
        conn.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)""",
            (
                "session-bridge:sidebar:broker-heartbeat",
                "not-json-sensitive-raw-value",
                1.0,
            ),
        )

    db._execute_write(seed_malformed)

    store.record_sidebar_broker_heartbeat(now=123.0)

    assert store.get_state("session-bridge:sidebar:broker-heartbeat") == {"at": 123.0}


def test_sidebar_delivery_status_counts_ambiguity_attention_and_projectless_legacy(
    db: SessionDB,
) -> None:
    store, candidate, failed, _reservation = _failed_bound_ambiguous_sidebar(
        db,
        native_id="status-ambiguous",
        token="status-ambiguous-token",
        thread_id="status-ambiguous-thread",
    )
    visible = _sidebar_candidate(db, native_id="status-projectless")
    store.enqueue_sidebar_job(visible)
    lease = store.claim_sidebar_jobs(now=200.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="status-projectless-thread",
        now=210.0,
    )

    status = store.sidebar_delivery_status(now=220.0)

    assert status["counts"]["ambiguous"] == 1
    assert status["counts"]["needs_attention"] == 1
    assert status["counts"]["projectless_legacy_count"] == 1

    _acknowledge_terminal_resolution(store, failed, now=230.0)
    _seed_sidebar_codex_target(store, visible, "status-projectless-thread")

    status = store.sidebar_delivery_status(now=240.0)
    assert status["counts"]["ambiguous"] == 1
    assert status["counts"]["needs_attention"] == 0
    assert status["counts"]["projectless_legacy_count"] == 1


def test_sidebar_lease_lookup_authenticates_active_and_completed_digest_minimally(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("lookup-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="lookup-authenticated")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    active = store.lookup_sidebar_job_by_lease(lease["lease_token"])
    assert active == {
        "id": lease["id"],
        "source_session_id": candidate.source_session_id,
        "bridge_id": candidate.bridge_id,
        "state": SidebarJobState.LEASED.value,
        "codex_thread_id": None,
    }
    assert "lease_digest" not in active
    assert "completion_digest" not in active
    assert "lease_token" not in active

    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-lookup-thread",
        now=200.0,
    )
    assert store.lookup_sidebar_job_by_lease(lease["lease_token"]) == {
        "id": lease["id"],
        "source_session_id": candidate.source_session_id,
        "bridge_id": candidate.bridge_id,
        "state": SidebarJobState.VISIBLE.value,
        "codex_thread_id": "codex-lookup-thread",
    }

    with pytest.raises(ValueError, match="lease token"):
        store.lookup_sidebar_job_by_lease("wrong-token")


def test_sidebar_digest_auth_uses_bounded_equality_candidates_for_commit_replay(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("indexed-commit-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="indexed-commit")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    token_digest = hashlib.sha256(lease["lease_token"].encode()).hexdigest()
    with db._lock:
        assert db._conn is not None
        lease_plan = db._conn.execute(
            """EXPLAIN QUERY PLAN SELECT * FROM session_sidebar_jobs
               WHERE lease_digest = ? LIMIT 2""",
            (token_digest,),
        ).fetchall()
    statements: list[str] = []
    with db._lock:
        assert db._conn is not None
        db._conn.set_trace_callback(statements.append)
    try:
        committed = store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-indexed-thread",
            now=200.0,
        )
        replay = store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-indexed-thread",
            now=201.0,
        )
        with db._lock:
            assert db._conn is not None
            completion_plan = db._conn.execute(
                """EXPLAIN QUERY PLAN SELECT * FROM session_sidebar_jobs
                   WHERE completion_digest = ? LIMIT 2""",
                (token_digest,),
            ).fetchall()
    finally:
        with db._lock:
            assert db._conn is not None
            db._conn.set_trace_callback(None)

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    digest_queries = [
        statement
        for statement in normalized
        if "SELECT * FROM SESSION_SIDEBAR_JOBS" in statement
        and ("LEASE_DIGEST =" in statement or "COMPLETION_DIGEST =" in statement)
    ]
    assert replay == committed
    assert any(
        "USING INDEX idx_session_sidebar_jobs_lease_digest" in row[3]
        for row in lease_plan
    )
    assert any(
        "USING INDEX idx_session_sidebar_jobs_completion_digest" in row[3]
        for row in completion_plan
    )
    assert digest_queries
    assert all("LIMIT 2" in statement for statement in digest_queries)
    assert not any(
        "LEASE_DIGEST IS NOT NULL OR COMPLETION_DIGEST IS NOT NULL" in statement
        for statement in normalized
    )


def test_sidebar_commit_rejects_expiry_and_recovers_the_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("expiring-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="expiring")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    with pytest.raises(ValueError, match="expired"):
        store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-too-late",
            now=400.0,
        )

    recovered = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert recovered is not None
    assert recovered["state"] == SidebarJobState.RETRY.value
    assert recovered["lease_digest"] is None


def test_sidebar_commit_fails_closed_for_different_task_or_token(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("exact-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="conflict")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-original",
        now=200.0,
    )

    with pytest.raises(ValueError, match="conflicting"):
        store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-different",
            now=201.0,
        )
    with pytest.raises(ValueError, match="lease token"):
        store.commit_sidebar_job(
            lease_token="different-token",
            codex_thread_id="codex-original",
            now=201.0,
        )
    persisted = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert persisted is not None
    assert persisted["codex_thread_id"] == "codex-original"


def test_sidebar_fail_rejects_non_allowlisted_error_without_releasing(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("error-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bad-error")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    with pytest.raises(ValueError, match="fixed allowlist"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="Traceback: secret detail",
            now=150.0,
        )

    row = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert row is not None
    assert row["state"] == SidebarJobState.LEASED.value
    assert row["error_code"] is None
    assert "error_detail" not in row


@pytest.mark.parametrize(
    "error_code",
    [None, True, False, [], {}, 1, 1.0, b"desktop_offline"],
)
def test_sidebar_fail_rejects_nonstring_error_codes_without_mutating_lease(
    db, error_code
) -> None:
    token = f"nonstring-error-{type(error_code).__name__}-{error_code!r}"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(token),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(
        db,
        native_id=f"nonstring-error-{type(error_code).__name__}",
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert before is not None

    with pytest.raises(ValueError, match="fixed allowlist"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code=error_code,
            now=150.0,
        )

    after = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert after == before
    assert after is not None
    assert after["state"] == SidebarJobState.LEASED.value
    assert after["lease_digest"] == hashlib.sha256(token.encode()).hexdigest()
    assert after["attempts"] == 0


def test_sidebar_retryable_error_allowlist_is_the_exact_fixed_contract() -> None:
    assert SIDEBAR_RETRYABLE_ERRORS == frozenset({
        "codex_tool_unavailable",
        "desktop_offline",
        "bridge_temporarily_unavailable",
        "sqlite_busy",
        "rename_failed",
        "project_lookup_failed",
        "native_task_not_indexed",
        "broker_time_budget",
        "inbox_unavailable",
    })


def test_sidebar_ambiguous_native_create_is_fatal_without_spending_retry_budget(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("ambiguous-create-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="ambiguous-native-create")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="native_create_ambiguous",
        now=150.0,
    )

    assert failed["state"] == SidebarJobState.FAILED.value
    assert failed["attempts"] == 0
    assert failed["error_code"] == "native_create_ambiguous"
    assert failed["codex_thread_id"] is None
    assert store.claim_sidebar_jobs(now=10_000.0, limit=1) == []


@pytest.mark.parametrize(
    "error_code",
    sorted(SIDEBAR_RETRYABLE_ERRORS - {"broker_time_budget"}),
)
def test_each_regular_retryable_sidebar_error_schedules_a_retry(
    db, error_code: str
) -> None:
    token = f"retryable-{error_code}"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(token),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"retryable-{error_code}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code=error_code,
        now=150.0,
    )

    assert failed["state"] == SidebarJobState.RETRY.value
    assert failed["attempts"] == 1
    assert failed["next_attempt_at"] == 210.0
    assert failed["error_code"] == error_code


@pytest.mark.parametrize("jitter_kind", ["negative", "above-bound"])
def test_sidebar_retry_rejects_out_of_range_jitter_and_rolls_back_lease(
    db, jitter_kind: str
) -> None:
    def _invalid_jitter(bound: float) -> float:
        return -0.01 if jitter_kind == "negative" else bound + 0.01

    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"jitter-{jitter_kind}"),
        sidebar_jitter=_invalid_jitter,
    )
    candidate = _sidebar_candidate(db, native_id=f"jitter-{jitter_kind}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert before is not None

    with pytest.raises(ValueError, match="outside its bound"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="desktop_offline",
            now=150.0,
        )

    after = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert after == before
    assert after is not None
    assert after["state"] == SidebarJobState.LEASED.value
    assert (
        after["lease_digest"]
        == hashlib.sha256(lease["lease_token"].encode()).hexdigest()
    )
    assert after["attempts"] == 0


def test_sidebar_retry_backoff_counts_failures_and_fails_on_attempt_five(db) -> None:
    jitter_bounds = []

    def _max_jitter(bound: float) -> float:
        jitter_bounds.append(bound)
        return bound

    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "retry-token-1",
            "retry-token-2",
            "retry-token-3",
            "retry-token-4",
            "retry-token-5",
        ),
        sidebar_jitter=_max_jitter,
    )
    candidate = _sidebar_candidate(db, native_id="retry")
    store.enqueue_sidebar_job(candidate)
    now = 100.0
    expected = (
        (60.0, 6.0),
        (120.0, 12.0),
        (240.0, 24.0),
        (480.0, 30.0),
        (900.0, 30.0),
    )

    for attempt, (base, jitter) in enumerate(expected, start=1):
        lease = store.claim_sidebar_jobs(now=now, limit=1)[0]
        failed = store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="desktop_offline",
            now=now,
        )
        assert failed["attempts"] == attempt
        assert failed["next_attempt_at"] == now + base + jitter
        expected_state = (
            SidebarJobState.FAILED.value
            if attempt == 5
            else SidebarJobState.RETRY.value
        )
        assert failed["state"] == expected_state
        now = failed["next_attempt_at"]

    assert jitter_bounds == [6.0, 12.0, 24.0, 30.0, 30.0]
    assert store.claim_sidebar_jobs(now=now, limit=1) == []


@pytest.mark.parametrize(
    "error_code",
    [
        "native_create_ambiguous",
        "marker_conflict",
        "source_identity_mismatch",
        "codex_thread_conflict",
        "provider_mismatch",
        "source_cwd_missing",
        "permission_preflight_failed",
        "retry_budget_exhausted",
        "placement_mismatch",
    ],
)
def test_sidebar_fatal_errors_fail_immediately_without_counting_attempt(
    db, error_code: str
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"fatal-{error_code}"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"fatal-{error_code}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code=error_code,
        now=150.0,
    )

    assert failed["state"] == SidebarJobState.FAILED.value
    assert failed["attempts"] == 0
    assert failed["error_code"] == error_code
    assert failed["lease_digest"] is None


def test_sidebar_fatal_error_allowlist_is_the_exact_fixed_contract() -> None:
    assert SIDEBAR_FATAL_ERRORS == frozenset({
        "native_create_ambiguous",
        "marker_conflict",
        "source_identity_mismatch",
        "codex_thread_conflict",
        "provider_mismatch",
        "source_cwd_missing",
        "permission_preflight_failed",
        "retry_budget_exhausted",
        "placement_mismatch",
    })


def test_sidebar_broker_budget_failure_releases_without_attempt_or_delay(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("budget-token", "budget-retry"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="budget")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    released = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="broker_time_budget",
        now=150.0,
    )

    assert released["state"] == SidebarJobState.PENDING.value
    assert released["attempts"] == 0
    assert released["next_attempt_at"] == 150.0
    assert released["error_code"] == "broker_time_budget"
    assert store.claim_sidebar_jobs(now=150.0, limit=1)[0]["lease_token"] == (
        "budget-retry"
    )


def test_sidebar_release_returns_an_active_lease_to_pending(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("release-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="release")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    released = store.release_sidebar_job(
        lease_token=lease["lease_token"],
        now=175.0,
    )

    assert released["state"] == SidebarJobState.PENDING.value
    assert released["attempts"] == 0
    assert released["next_attempt_at"] == 175.0
    assert released["lease_digest"] is None
    assert released["error_code"] is None


def test_bound_sidebar_operator_retry_preserves_exact_task_and_reservation(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "bound-retry-token-1",
            "bound-retry-token-2",
            "bound-retry-token-3",
            "bound-retry-token-4",
            "bound-retry-token-5",
            "bound-retry-token-recovered",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bound-operator-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key="hermes-session-bridge-create-v1:bound-operator-retry",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    thread_id = "019f-bound-retry-thread"
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )

    failed = None
    for _attempt in range(5):
        failed = store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="native_task_not_indexed",
            codex_thread_id=thread_id,
            now=150.0 if failed is None else failed["next_attempt_at"],
        )
        if failed["state"] == SidebarJobState.RETRY.value:
            lease = store.claim_sidebar_jobs(
                now=failed["next_attempt_at"],
                limit=1,
            )[0]

    assert failed is not None
    assert failed["state"] == SidebarJobState.FAILED.value
    assert failed["attempts"] == 5

    retried = store.retry_failed_bound_sidebar_job(
        job_id=failed["id"],
        source_session_id=candidate.source_session_id,
        codex_thread_id=thread_id,
        expected_error_code="native_task_not_indexed",
        confirmation="PRESERVE_EXACT_BOUND_TASK",
        now=1_000.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["next_attempt_at"] == 1_000.0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )
    claimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert claimed["codex_thread_id"] == thread_id
    assert claimed["lease_token"] == "bound-retry-token-recovered"


def test_bound_sidebar_operator_retry_accepts_exact_project_drift_conflict(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "bound-conflict-token",
            "bound-conflict-recovered",
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bound-project-drift")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    proof = _record_absence_proof(store, lease["lease_token"])
    reservation = store.reserve_sidebar_create(
        lease_token=lease["lease_token"],
        recovery_key="hermes-session-bridge-create-v1:bound-project-drift",
        reconciliation_proof_digest=proof["proof_digest"],
        reconciliation_generation=proof["reconciliation_generation"],
        now=110.0,
    )
    thread_id = "019f-bound-project-drift"
    store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        now=120.0,
    )
    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="codex_thread_conflict",
        codex_thread_id=thread_id,
        now=130.0,
    )

    retried = store.retry_failed_bound_sidebar_job(
        job_id=failed["id"],
        source_session_id=candidate.source_session_id,
        codex_thread_id=thread_id,
        expected_error_code="codex_thread_conflict",
        confirmation="PRESERVE_EXACT_BOUND_TASK",
        now=140.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )
    claimed = store.claim_sidebar_jobs(now=140.0, limit=1)[0]
    assert claimed["codex_thread_id"] == thread_id
    assert claimed["lease_token"] == "bound-conflict-recovered"


def test_bound_sidebar_operator_retry_accepts_exact_ambiguous_create(db) -> None:
    thread_id = "019f-bound-ambiguous-create"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id="bound-ambiguous-create",
        thread_id=thread_id,
    )
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("native_create_ambiguous", failed["id"]),
        )
    )

    retried = store.retry_failed_bound_sidebar_job(
        job_id=failed["id"],
        source_session_id=candidate.source_session_id,
        codex_thread_id=thread_id,
        expected_error_code="native_create_ambiguous",
        confirmation="PRESERVE_EXACT_BOUND_TASK",
        now=1_000.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )
    claimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert claimed["codex_thread_id"] == thread_id


def test_bound_sidebar_operator_retry_accepts_exact_idempotent_marker_replay(
    db,
) -> None:
    thread_id = "019f-bound-idempotent-marker-replay"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id="bound-idempotent-marker-replay",
        thread_id=thread_id,
    )
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("marker_conflict", failed["id"]),
        )
    )

    retried = store.retry_failed_bound_sidebar_job(
        job_id=failed["id"],
        source_session_id=candidate.source_session_id,
        codex_thread_id=thread_id,
        expected_error_code="marker_conflict",
        confirmation="PRESERVE_EXACT_BOUND_TASK",
        now=1_000.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )
    claimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert claimed["codex_thread_id"] == thread_id


def test_bound_sidebar_operator_retry_accepts_exact_transient_bridge_failure(
    db,
) -> None:
    thread_id = "019f-bound-transient-bridge-failure"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id="bound-transient-bridge-failure",
        thread_id=thread_id,
    )
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("bridge_temporarily_unavailable", failed["id"]),
        )
    )

    retried = store.retry_failed_bound_sidebar_job(
        job_id=failed["id"],
        source_session_id=candidate.source_session_id,
        codex_thread_id=thread_id,
        expected_error_code="bridge_temporarily_unavailable",
        confirmation="PRESERVE_EXACT_BOUND_TASK",
        now=1_000.0,
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )
    claimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert claimed["codex_thread_id"] == thread_id


def test_bound_sidebar_operator_retry_accepts_repaired_source_identity_only_with_narrow_authority(
    db,
) -> None:
    thread_id = "019f-bound-repaired-source-identity"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id="bound-repaired-source-identity",
        thread_id=thread_id,
    )
    store.db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_jobs SET error_code = ? WHERE id = ?",
            ("source_identity_mismatch", failed["id"]),
        )
    )
    arguments = {
        "job_id": failed["id"],
        "source_session_id": candidate.source_session_id,
        "codex_thread_id": thread_id,
        "expected_error_code": "source_identity_mismatch",
        "now": 1_000.0,
    }

    with pytest.raises(ValueError):
        store.retry_failed_bound_sidebar_job(
            **arguments,
            confirmation="PRESERVE_EXACT_BOUND_TASK",
        )

    retried = store.retry_failed_bound_sidebar_job(
        **arguments,
        confirmation="PRESERVE_EXACT_BOUND_TASK_AFTER_SOURCE_CWD_REPAIR",
    )

    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["attempts"] == 0
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] == thread_id
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )


@pytest.mark.parametrize(
    ("argument", "replacement"),
    (
        ("job_id", "sidebar-job:" + "f" * 64),
        ("source_session_id", "claude:different-bound-source"),
        ("codex_thread_id", "019f-different-bound-thread"),
        ("expected_error_code", "source_identity_mismatch"),
        ("confirmation", "REPLACE_BOUND_TASK"),
    ),
)
def test_bound_sidebar_operator_retry_rejects_stale_authority_without_mutation(
    db,
    argument: str,
    replacement: str,
) -> None:
    thread_id = "019f-bound-authority-thread"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id=f"bound-authority-{argument}",
        thread_id=thread_id,
    )
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    arguments = {
        "job_id": failed["id"],
        "source_session_id": candidate.source_session_id,
        "codex_thread_id": thread_id,
        "expected_error_code": "native_task_not_indexed",
        "confirmation": "PRESERVE_EXACT_BOUND_TASK",
        "now": 1_000.0,
    }
    arguments[argument] = replacement

    with pytest.raises(ValueError):
        store.retry_failed_bound_sidebar_job(**arguments)

    assert store.get_sidebar_job_for_source(candidate.source_session_id) == before
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )


@pytest.mark.parametrize(
    ("scenario", "mutation"),
    (
        (
            "missing-reservation",
            """DELETE FROM session_bridge_state
               WHERE key LIKE 'session-bridge:sidebar-create:%'""",
        ),
        (
            "completion-digest",
            """UPDATE session_sidebar_jobs
               SET completion_digest = 'stale-completion'
               WHERE source_session_id = ?""",
        ),
        (
            "visible-timestamp",
            """UPDATE session_sidebar_jobs
               SET visible_at = 999
               WHERE source_session_id = ?""",
        ),
    ),
)
def test_bound_sidebar_operator_retry_rejects_unsafe_persisted_state(
    db,
    scenario: str,
    mutation: str,
) -> None:
    thread_id = f"019f-bound-unsafe-{scenario}"
    store, candidate, failed, _reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id=f"bound-unsafe-{scenario}",
        thread_id=thread_id,
    )
    parameters = () if scenario == "missing-reservation" else (
        candidate.source_session_id,
    )
    db._execute_write(lambda conn: conn.execute(mutation, parameters))
    before = store.get_sidebar_job_for_source(candidate.source_session_id)

    with pytest.raises(ValueError):
        store.retry_failed_bound_sidebar_job(
            job_id=failed["id"],
            source_session_id=candidate.source_session_id,
            codex_thread_id=thread_id,
            expected_error_code="native_task_not_indexed",
            confirmation="PRESERVE_EXACT_BOUND_TASK",
            now=1_000.0,
        )

    assert store.get_sidebar_job_for_source(candidate.source_session_id) == before


def test_bound_sidebar_operator_retry_is_single_use(db) -> None:
    thread_id = "019f-bound-single-use-thread"
    store, candidate, failed, reservation = _failed_bound_not_indexed_sidebar(
        db,
        native_id="bound-single-use",
        thread_id=thread_id,
    )
    arguments = {
        "job_id": failed["id"],
        "source_session_id": candidate.source_session_id,
        "codex_thread_id": thread_id,
        "expected_error_code": "native_task_not_indexed",
        "confirmation": "PRESERVE_EXACT_BOUND_TASK",
        "now": 1_000.0,
    }

    first = store.retry_failed_bound_sidebar_job(**arguments)
    with pytest.raises(ValueError):
        store.retry_failed_bound_sidebar_job(**arguments)

    assert store.get_sidebar_job_for_source(candidate.source_session_id) == first
    assert store.get_sidebar_create_reservation(candidate.source_session_id) == (
        reservation
    )


def test_sidebar_operator_retry_requeues_only_the_expected_failed_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token", "recovered-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="source_identity_mismatch",
            now=175.0,
        )

    retried = store.retry_failed_sidebar_job(
        source_session_id=candidate.source_session_id,
        expected_error_code="marker_conflict",
        now=200.0,
    )

    assert retried["state"] == SidebarJobState.PENDING.value
    assert retried["attempts"] == 0
    assert retried["next_attempt_at"] == 200.0
    assert retried["lease_digest"] is None
    assert retried["lease_expires_at"] is None
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] is None
    assert store.claim_sidebar_jobs(now=200.0, limit=1)[0]["lease_token"] == (
        "recovered-token"
    )


def test_sidebar_operator_retry_rejects_a_completed_sibling_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-sibling")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_sidebar_jobs (
               id, idempotency_key, source_session_id, bridge_id, state,
               attempts, next_attempt_at, completion_digest, codex_thread_id,
               eligible_at, created_at, updated_at, visible_at
               ) VALUES (?, ?, ?, ?, 'sidebar_visible', 1, 1, ?, ?, 1, 1, 1, 1)""",
            (
                "sidebar-job:completed-sibling",
                "codex-sidebar:completed-sibling:v1",
                candidate.source_session_id,
                "sidebar:" + "f" * 64,
                "completed-digest",
                "codex:completed-sibling",
            ),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_sidebar_operator_retry_rejects_a_malformed_job_identity(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-malformed")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_sidebar_jobs
               SET id = ?, idempotency_key = ?, bridge_id = ?
               WHERE source_session_id = ?""",
            (
                "sidebar-job:malformed",
                "codex-sidebar:malformed:v1",
                "sidebar:" + "e" * 64,
                candidate.source_session_id,
            ),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_sidebar_operator_retry_rejects_a_stale_visibility_timestamp(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-visible-at")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_sidebar_jobs SET visible_at = ?
               WHERE source_session_id = ?""",
            (125.0, candidate.source_session_id),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_malformed_sidebar_provider_row_hard_stops_before_valid_provider(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("valid-provider-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    db.create_session("codex:malformed-source", source="codex")
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_sidebar_jobs (
               id, idempotency_key, source_session_id, bridge_id, state,
               attempts, next_attempt_at, eligible_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'sidebar_pending', 0, 1, 1, 1, 1)""",
            (
                "sidebar-job-malformed",
                "codex-sidebar:codex:malformed-source:v1",
                "codex:malformed-source",
                "sidebar:" + "f" * 64,
            ),
        )
    )
    candidate = _sidebar_candidate(db, native_id="valid-provider", eligible_at=2.0)
    store.enqueue_sidebar_job(candidate)

    claimed = store.claim_sidebar_jobs(now=100.0, limit=1)

    assert claimed == []
    malformed = _rows(
        db,
        "SELECT * FROM session_sidebar_jobs WHERE id = 'sidebar-job-malformed'",
    )[0]
    assert malformed["state"] == SidebarJobState.FAILED.value
    assert malformed["error_code"] == "provider_mismatch"
    valid = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert valid is not None
    assert valid["state"] == SidebarJobState.PENDING.value


def test_sidebar_claim_scans_one_bounded_page_then_hard_stops_malformed_rows(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("bounded-scan-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )

    def _seed_malformed_page(conn) -> None:
        for index in range(45):
            source_session_id = f"codex:malformed-page-{index:02d}"
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, 'codex', 1)",
                (source_session_id,),
            )
            conn.execute(
                """INSERT INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, eligible_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'sidebar_pending', 0, 1, ?, 1, 1)""",
                (
                    f"sidebar-malformed-page-{index:02d}",
                    f"codex-sidebar:{source_session_id}:v1",
                    source_session_id,
                    "sidebar:" + f"{index:064x}",
                    float(index),
                ),
            )

    db._execute_write(_seed_malformed_page)
    valid = _sidebar_candidate(db, native_id="after-malformed-page", eligible_at=100.0)
    store.enqueue_sidebar_job(valid)
    statements: list[str] = []
    with db._lock:
        assert db._conn is not None
        db._conn.set_trace_callback(statements.append)
    try:
        first = store.claim_sidebar_jobs(now=200.0, limit=1)
        after_first = store.sidebar_job_counts()
        second = store.claim_sidebar_jobs(now=200.0, limit=1)
        after_second = store.sidebar_job_counts()
        third = store.claim_sidebar_jobs(now=200.0, limit=1)
    finally:
        with db._lock:
            assert db._conn is not None
            db._conn.set_trace_callback(None)

    due_queries = [
        " ".join(statement.upper().split())
        for statement in statements
        if "SELECT * FROM SESSION_SIDEBAR_JOBS" in statement.upper()
        and "ORDER BY CASE WHEN STATE =" in " ".join(statement.upper().split())
        and "ELIGIBLE_AT, ID" in " ".join(statement.upper().split())
    ]
    assert first == []
    assert after_first[SidebarJobState.FAILED.value] == 40
    assert after_first[SidebarJobState.PENDING.value] == 6
    assert second == []
    assert after_second[SidebarJobState.FAILED.value] == 45
    assert after_second[SidebarJobState.PENDING.value] == 1
    assert len(third) == 1
    assert third[0]["source_session_id"] == valid.source_session_id
    assert len(due_queries) == 3
    assert all("LIMIT 40" in statement for statement in due_queries)
    valid_row = store.get_sidebar_job_for_source(valid.source_session_id)
    assert valid_row is not None
    assert valid_row["state"] == SidebarJobState.LEASED.value


def test_sidebar_counts_and_source_lookup_have_stable_public_shapes(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("lookup-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    pending = _sidebar_candidate(db, native_id="lookup-pending")
    leased = _sidebar_candidate(db, native_id="lookup-leased")
    store.enqueue_sidebar_job(pending)
    leased_row = store.enqueue_sidebar_job(leased)

    assert store.get_sidebar_job_for_source("missing") is None
    assert store.get_sidebar_job_for_source(leased.source_session_id) == {
        key: value for key, value in leased_row.items() if key != "created"
    }
    store.claim_sidebar_jobs(now=200.0, limit=1)
    assert store.sidebar_job_counts() == {
        "sidebar_pending": 1,
        "sidebar_leased": 1,
        "sidebar_visible": 0,
        "sidebar_retry": 0,
        "sidebar_failed": 0,
    }


def test_sidebar_delivery_status_reclassifies_expired_lease_as_retry(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("status-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="status-expired")
    store.enqueue_sidebar_job(candidate)
    store.claim_sidebar_jobs(now=100.0, limit=1)

    active = store.sidebar_delivery_status(now=399.999)
    expired = store.sidebar_delivery_status(now=400.0)

    assert active["counts"]["sidebar_leased"] == 1
    assert active["counts"]["sidebar_retry"] == 0
    assert expired["counts"]["sidebar_leased"] == 0
    assert expired["counts"]["sidebar_retry"] == 1


def test_claude_visibility_enqueue_claim_commit_and_idempotency(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    first = _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    duplicate = _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")

    assert first == duplicate
    assert first["state"] == "claude_pending"
    assert first["reserved_claude_uuid"] == identity.claude_uuid
    assert claim.status == "claimed"
    assert claim.job_id == identity.job_id
    assert claim.reserved_claude_uuid == identity.claude_uuid
    assert claim.lease_digest and len(claim.lease_digest) == 64
    assert claim.requires_exact_id_reconciliation is False
    visible = store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 120.0
    )
    assert visible["state"] == "claude_visible"
    assert visible["reserved_claude_uuid"] == identity.claude_uuid
    with pytest.raises(ValueError, match="active Claude visibility lease"):
        store.retry_claude_visibility_job(
            identity.job_id,
            claim.lease_digest,
            "native_transcript_not_indexed",
            130.0,
            "must not reopen visible work",
        )


@pytest.mark.parametrize(
    "collision",
    ["source_session_id", "bridge_id", "idempotency_key", "claude_uuid"],
)
def test_claude_visibility_enqueue_rejects_each_independent_identity_collision(
    db: SessionDB,
    collision: str,
) -> None:
    store = SessionBridgeStore(db)
    existing_candidate, existing_identity = _claude_visibility_identity("existing")
    candidate, identity = _claude_visibility_identity("new")
    _enqueue_claude_visibility_job(store, existing_candidate, existing_identity)
    if collision == "source_session_id":
        candidate = replace(
            candidate, source_session_id=existing_candidate.source_session_id
        )
    elif collision == "bridge_id":
        identity = replace(identity, bridge_id=existing_identity.bridge_id)
    elif collision == "idempotency_key":
        identity = replace(identity, idempotency_key=existing_identity.idempotency_key)
    else:
        identity = replace(identity, claude_uuid=existing_identity.claude_uuid)

    with pytest.raises(ValueError, match="Claude visibility"):
        _enqueue_claude_visibility_job(store, candidate, identity)


def test_claude_visibility_enqueue_rejects_mismatched_identity_in_empty_store(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db)
    candidate, _identity = _claude_visibility_identity("candidate")
    _other_candidate, other_identity = _claude_visibility_identity("other")

    with pytest.raises(ValueError, match="does not match candidate"):
        _enqueue_claude_visibility_job(store, candidate, other_identity)


def test_claude_visibility_enqueue_rejects_forged_marker_signature(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    candidate, identity = _claude_visibility_identity()
    replacement = "A" if identity.signed_marker[-1] != "A" else "B"
    forged = replace(identity, signed_marker=identity.signed_marker[:-1] + replacement)

    with pytest.raises(ValueError, match="signed marker"):
        _enqueue_claude_visibility_job(store, candidate, forged)

    assert _rows(db, "SELECT * FROM session_claude_visibility_jobs") == []


def test_claude_visibility_retry_restart_and_stale_lease_preserve_uuid(
    db: SessionDB,
) -> None:
    candidate, identity = _claude_visibility_identity()
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    _enqueue_claude_visibility_job(store, candidate, identity)
    first = store.claim_claude_visibility_job(100.0, 10, 25, "0.50", "0.02")
    with pytest.raises(ValueError, match="reconciliation lease"):
        store.record_claude_visibility_exact_id_absent(
            identity.job_id,
            first.lease_digest,
            identity.claude_uuid,
            first.attempt_ordinal,
            "a" * 64,
        )
    retried = store.retry_claude_visibility_job(
        identity.job_id,
        first.lease_digest,
        "creation_ambiguous",
        120.0,
        "launch result unknown",
    )
    assert retried["state"] == "claude_retry"
    assert retried["reserved_claude_uuid"] == identity.claude_uuid

    restarted = SessionBridgeStore(db, clock=lambda: 120.0, local_timezone=timezone.utc)
    reconciliation = restarted.claim_claude_visibility_job(
        120.0, 10, 25, "0.50", "0.02"
    )
    assert reconciliation.reserved_claude_uuid == identity.claude_uuid
    assert reconciliation.attempt_ordinal == 1
    assert reconciliation.prior_error_code == "creation_ambiguous"
    assert reconciliation.registration_reserved is False
    assert reconciliation.launch_permitted is False
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    with pytest.raises(ValueError, match="reconciliation lease"):
        restarted.record_claude_visibility_exact_id_absent(
            identity.job_id,
            "wrong",
            identity.claude_uuid,
            reconciliation.attempt_ordinal,
            "b" * 64,
        )
    with pytest.raises(ValueError, match="reconciliation lease"):
        restarted.record_claude_visibility_exact_id_absent(
            "forged-job-id",
            reconciliation.lease_digest,
            identity.claude_uuid,
            reconciliation.attempt_ordinal,
            "b" * 64,
        )
    with pytest.raises(ValueError, match="reconciliation lease"):
        restarted.record_claude_visibility_exact_id_absent(
            identity.job_id,
            reconciliation.lease_digest,
            "00000000-0000-4000-8000-000000000000",
            reconciliation.attempt_ordinal,
            "b" * 64,
        )
    with pytest.raises(ValueError, match="reconciliation lease"):
        restarted.record_claude_visibility_exact_id_absent(
            identity.job_id,
            reconciliation.lease_digest,
            identity.claude_uuid,
            reconciliation.attempt_ordinal + 1,
            "b" * 64,
        )
    absent = restarted.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "b" * 64,
    )
    assert absent["state"] == "claude_retry"
    assert absent["error_code"] == "creation_ambiguous"
    assert absent["reserved_claude_uuid"] == identity.claude_uuid

    after_restart = SessionBridgeStore(
        db, clock=lambda: 120.0, local_timezone=timezone.utc
    )
    second = after_restart.claim_claude_visibility_job(120.0, 10, 25, "0.50", "0.02")
    assert second.reserved_claude_uuid == identity.claude_uuid
    assert second.attempt_ordinal == 2
    assert second.prior_error_code == "creation_ambiguous"
    assert second.registration_reserved is True
    assert second.launch_permitted is True
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 2

    after_expiry = SessionBridgeStore(
        db, clock=lambda: 131.0, local_timezone=timezone.utc
    )
    stale = after_expiry.claim_claude_visibility_job(131.0, 10, 25, "0.50", "0.02")
    assert stale.status == "claimed"
    assert stale.reserved_claude_uuid == identity.claude_uuid
    assert stale.attempt_ordinal == 2
    assert stale.prior_error_code == "lease_expired"
    assert stale.registration_reserved is False
    assert stale.launch_permitted is False
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 2
    expired = SessionBridgeStore(db, clock=lambda: 142.0, local_timezone=timezone.utc)
    with pytest.raises(ValueError, match="reconciliation lease"):
        expired.record_claude_visibility_exact_id_absent(
            identity.job_id,
            stale.lease_digest,
            identity.claude_uuid,
            stale.attempt_ordinal,
            "e" * 64,
        )


@pytest.mark.parametrize(
    "other_state",
    ["claude_pending", "claude_leased", "claude_retry", "claude_failed"],
)
def test_claude_visibility_expected_job_claim_refuses_when_another_job_is_open(
    db: SessionDB, other_state: str
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    first_candidate, first_identity = _claude_visibility_identity("first-due")
    second_candidate, second_identity = _claude_visibility_identity("second-due")
    _enqueue_claude_visibility_job(store, first_candidate, first_identity)
    if other_state == "claude_leased":
        leased = store.claim_claude_visibility_job(
            100.0,
            60,
            25,
            "1.00",
            "0.02",
            expected_job_id=first_identity.job_id,
        )
        assert leased.lease_kind == "launch"
    else:
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE session_claude_visibility_jobs SET state = ? WHERE id = ?",
                (other_state, first_identity.job_id),
            )
        )
    _enqueue_claude_visibility_job(store, second_candidate, second_identity)

    claim = store.claim_claude_visibility_job(
        100.0,
        60,
        25,
        "1.00",
        "0.02",
        expected_job_id=second_identity.job_id,
    )

    assert claim.status == "not_sole_open_job"
    assert claim.job_id == second_identity.job_id
    rows = {
        row["id"]: row["state"]
        for row in _rows(db, "SELECT id, state FROM session_claude_visibility_jobs")
    }
    assert rows == {
        first_identity.job_id: other_state,
        second_identity.job_id: "claude_pending",
    }


def test_claude_visibility_expected_job_claim_reaps_its_expired_lease(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("expired-characterization")
    _enqueue_claude_visibility_job(store, candidate, identity)
    first = store.claim_claude_visibility_job(
        100.0,
        10,
        25,
        "1.00",
        "0.02",
        expected_job_id=identity.job_id,
    )
    assert first.lease_kind == "launch"

    clock[0] = 111.0
    recovered = store.claim_claude_visibility_job(
        111.0,
        10,
        25,
        "1.00",
        "0.02",
        expected_job_id=identity.job_id,
    )

    assert recovered.lease_kind == "reconciliation"
    assert recovered.prior_error_code == "lease_expired"
    assert recovered.reserved_claude_uuid == first.reserved_claude_uuid
    assert recovered.attempt_ordinal == first.attempt_ordinal == 1


def test_claude_auth_recovery_is_paid_once_and_releases_only_same_uuid(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-recovery")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )

    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000001",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    assert recovery["status"] == "claimed"
    assert recovery["reserved_claude_uuid"] == identity.claude_uuid
    assert recovery["attempt_ordinal"] == 2
    assert store.claude_visibility_status(100.0)["usage"]["attempts"] == 2

    store.begin_claude_auth_recovery(identity.job_id, recovery["lease_digest"])

    clock[0] = 111.0
    resumed = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000001",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=111.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    assert resumed["status"] == "claimed"
    assert resumed["attempt_ordinal"] == recovery["attempt_ordinal"] + 1
    assert resumed["reserved_claude_uuid"] == recovery["reserved_claude_uuid"]
    assert store.claude_visibility_status(111.0)["usage"]["attempts"] == 3
    store.begin_claude_auth_recovery(identity.job_id, resumed["lease_digest"])
    store.retry_claude_auth_recovery(
        identity.job_id,
        resumed["lease_digest"],
        "claude_authentication_unavailable",
        112.0,
    )
    clock[0] = 112.0
    repeated = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000001",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=112.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    assert repeated["attempt_ordinal"] == resumed["attempt_ordinal"] + 1
    assert store.claude_visibility_status(112.0)["usage"]["attempts"] == 4
    store.begin_claude_auth_recovery(identity.job_id, repeated["lease_digest"])
    committed = store.commit_claude_auth_recovery(
        job_id=identity.job_id,
        lease_digest=repeated["lease_digest"],
        reserved_claude_uuid=identity.claude_uuid,
        transcript_digest="c" * 64,
        visible_at=112.0,
    )
    assert committed["state"] == "claude_visible"
    recovery_row = _rows(
        db,
        "SELECT state, completed_at FROM session_claude_auth_recoveries WHERE job_id = ?",
        (identity.job_id,),
    )[0]
    assert recovery_row == {"state": "completed", "completed_at": 112.0}


def test_claude_auth_recovery_pre_call_crash_reuses_paid_reservation(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-pre-call-crash")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000003",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )

    clock[0] = 111.0
    resumed = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000003",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=111.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )

    assert resumed["attempt_ordinal"] == recovery["attempt_ordinal"]
    assert store.claude_visibility_status(111.0)["usage"]["attempts"] == 2


def test_claude_auth_recovery_accepts_exact_authentication_retry_state(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-retry-state")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id,
        launch.lease_digest,
        "claude_authentication_unavailable",
        101.0,
        "redacted",
    )

    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000004",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )

    assert recovery["status"] == "claimed"
    assert recovery["reserved_claude_uuid"] == identity.claude_uuid


def test_claude_auth_recovery_reconciles_completed_transcript_without_new_call(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-reconcile-crash")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    operation_id = "6ae1c4de-0000-4000-8000-000000000005"
    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id=operation_id,
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    store.begin_claude_auth_recovery(identity.job_id, recovery["lease_digest"])

    completed = store.reconcile_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id=operation_id,
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        transcript_digest="c" * 64,
        visible_at=101.0,
    )

    assert completed["state"] == "claude_visible"
    assert store.claude_visibility_status(101.0)["usage"]["attempts"] == 2

    repeated = store.reconcile_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id=operation_id,
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        transcript_digest="c" * 64,
        visible_at=102.0,
    )
    assert repeated["state"] == "claude_visible"

    with pytest.raises(ValueError, match="recovery authority"):
        store.reconcile_claude_auth_recovery(
            job_id=identity.job_id,
            reserved_claude_uuid=identity.claude_uuid,
            operation_id=operation_id,
            evidence_digest="a" * 64,
            prompt_digest="b" * 64,
            transcript_digest="d" * 64,
            visible_at=102.0,
        )


@pytest.mark.parametrize(
    ("boundary", "expected_label"),
    [
        ("commit_transcript", "recovered transcript digest"),
        ("reconcile_evidence", "authentication evidence digest"),
        ("reconcile_prompt", "authentication recovery prompt digest"),
        ("reconcile_transcript", "recovered transcript digest"),
    ],
)
def test_claude_auth_recovery_rejects_invalid_digest_before_mutation(
    db: SessionDB,
    boundary: str,
    expected_label: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(f"auth-invalid-{boundary}")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    operation_id = "6ae1c4de-0000-4000-8000-000000000025"
    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id=operation_id,
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    store.begin_claude_auth_recovery(identity.job_id, recovery["lease_digest"])
    before_job = _rows(
        db,
        "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
        (identity.job_id,),
    )[0]
    before_recovery = _rows(
        db,
        "SELECT * FROM session_claude_auth_recoveries WHERE job_id = ?",
        (identity.job_id,),
    )[0]

    with pytest.raises(ValueError, match=expected_label):
        if boundary == "commit_transcript":
            store.commit_claude_auth_recovery(
                job_id=identity.job_id,
                lease_digest=recovery["lease_digest"],
                reserved_claude_uuid=identity.claude_uuid,
                transcript_digest="not-a-sha256-digest",
                visible_at=101.0,
            )
        else:
            store.reconcile_claude_auth_recovery(
                job_id=identity.job_id,
                reserved_claude_uuid=identity.claude_uuid,
                operation_id=operation_id,
                evidence_digest=(
                    "not-a-sha256-digest"
                    if boundary == "reconcile_evidence"
                    else "a" * 64
                ),
                prompt_digest=(
                    "not-a-sha256-digest"
                    if boundary == "reconcile_prompt"
                    else "b" * 64
                ),
                transcript_digest=(
                    "not-a-sha256-digest"
                    if boundary == "reconcile_transcript"
                    else "c" * 64
                ),
                visible_at=101.0,
            )

    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
            (identity.job_id,),
        )[0]
        == before_job
    )
    assert (
        _rows(
            db,
            "SELECT * FROM session_claude_auth_recoveries WHERE job_id = ?",
            (identity.job_id,),
        )[0]
        == before_recovery
    )


@pytest.mark.parametrize("recovery_mode", ["leased_commit", "crash_reconcile"])
def test_claude_auth_recovery_finalizes_preindexed_target_lineage(
    db: SessionDB,
    recovery_mode: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(f"auth-lineage-{recovery_mode}")
    store.upsert_projection(
        _projection(
            _message("source-user", "meaningful request"),
            provider=Provider.CODEX,
            native_id=f"source-auth-lineage-{recovery_mode}",
        )
    )
    _enqueue_claude_visibility_job(store, candidate, identity)
    store.upsert_projection(
        _projection(
            _message("target-user", "signed registration"),
            native_id=identity.claude_uuid,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=identity.bridge_id,
        )
    )
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    operation_id = (
        "6ae1c4de-0000-4000-8000-000000000021"
        if recovery_mode == "leased_commit"
        else "6ae1c4de-0000-4000-8000-000000000022"
    )
    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id=operation_id,
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    store.begin_claude_auth_recovery(identity.job_id, recovery["lease_digest"])

    if recovery_mode == "leased_commit":
        completed = store.commit_claude_auth_recovery(
            job_id=identity.job_id,
            lease_digest=recovery["lease_digest"],
            reserved_claude_uuid=identity.claude_uuid,
            transcript_digest="c" * 64,
            visible_at=101.0,
        )
    else:
        completed = store.reconcile_claude_auth_recovery(
            job_id=identity.job_id,
            reserved_claude_uuid=identity.claude_uuid,
            operation_id=operation_id,
            evidence_digest="a" * 64,
            prompt_digest="b" * 64,
            transcript_digest="c" * 64,
            visible_at=101.0,
        )

    assert completed["state"] == "claude_visible"
    assert _rows(
        db,
        """SELECT from_session_id, to_session_id, relation
             FROM session_links WHERE bridge_id = ?""",
        (identity.bridge_id,),
    ) == [
        {
            "from_session_id": candidate.source_session_id,
            "to_session_id": f"claude:{identity.claude_uuid}",
            "relation": "mirrors",
        }
    ]
    if recovery_mode == "crash_reconcile":
        db._execute_write(
            lambda conn: conn.execute(
                "DELETE FROM session_links WHERE bridge_id = ?",
                (identity.bridge_id,),
            )
        )
        replay = store.reconcile_claude_auth_recovery(
            job_id=identity.job_id,
            reserved_claude_uuid=identity.claude_uuid,
            operation_id=operation_id,
            evidence_digest="a" * 64,
            prompt_digest="b" * 64,
            transcript_digest="c" * 64,
            visible_at=102.0,
        )
        assert replay["state"] == "claude_visible"
        assert _rows(
            db,
            "SELECT relation FROM session_links WHERE bridge_id = ?",
            (identity.bridge_id,),
        ) == [{"relation": "mirrors"}]


def test_claude_auth_recovery_refuses_when_any_other_open_job_exists(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-recovery-blocked")
    other_candidate, other_identity = _claude_visibility_identity("other-open")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    _enqueue_claude_visibility_job(store, other_candidate, other_identity)

    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000002",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    assert recovery == {"status": "not_sole_open_job", "job_id": identity.job_id}
    rows = _rows(db, "SELECT id, state FROM session_claude_visibility_jobs")
    assert {row["id"]: row["state"] for row in rows} == {
        identity.job_id: "claude_failed",
        other_identity.job_id: "claude_pending",
    }


def test_nonretryable_auth_recovery_output_terminalizes_immediately(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("auth-fatal-output")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 10, 25, "1.00", "0.02")
    store.fail_claude_visibility_job(
        identity.job_id, launch.lease_digest, "bridge_conflict", "redacted"
    )
    recovery = store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000010",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=100.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    )
    store.begin_claude_auth_recovery(identity.job_id, recovery["lease_digest"])

    result = store.retry_claude_auth_recovery(
        identity.job_id, recovery["lease_digest"], "bridge_conflict", 101.0
    )

    assert result == {"state": "failed", "error_code": "bridge_conflict"}
    row = _rows(
        db,
        """SELECT state, error_code, attempts
           FROM session_claude_visibility_jobs WHERE id = ?""",
        (identity.job_id,),
    )[0]
    assert row == {
        "state": "claude_failed",
        "error_code": "bridge_conflict",
        "attempts": 2,
    }
    assert store.claim_claude_auth_recovery(
        job_id=identity.job_id,
        reserved_claude_uuid=identity.claude_uuid,
        operation_id="6ae1c4de-0000-4000-8000-000000000010",
        evidence_digest="a" * 64,
        prompt_digest="b" * 64,
        now=101.0,
        lease_seconds=10,
        daily_limit=25,
        cost_limit="1.00",
        reserved_cost="0.02",
        max_attempts=5,
    ) == {"status": "completed", "job_id": identity.job_id}


def test_claude_visibility_max_attempts_allows_exact_match_reconciliation(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("max-match")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    launch = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    store.retry_claude_visibility_job(
        identity.job_id, launch.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    visible = store.commit_claude_visibility_job(
        identity.job_id, reconciliation.lease_digest, "a" * 64, 100.0
    )
    assert reconciliation.registration_reserved is False
    assert visible["state"] == "claude_visible"
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1


def test_claude_visibility_exact_absence_at_max_terminalizes_without_usage(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("max-absent")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    store.retry_claude_visibility_job(
        identity.job_id, launch.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "a" * 64,
    )
    clock[0] = 86_500.0
    exhausted = store.claim_claude_visibility_job(86_500.0, 60, 25, "1.00", "0.02", 1)
    row = _rows(
        db, "SELECT state, attempts, error_code FROM session_claude_visibility_jobs"
    )[0]
    assert exhausted.status == "max_attempts_exhausted"
    assert row == {
        "state": "claude_failed",
        "attempts": 1,
        "error_code": "max_attempts_exhausted",
    }
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1


def _corrupt_claude_visibility_counter(
    db: SessionDB,
    job_id: str,
    spent_ordinals: int,
    *,
    local_day: str = "2026-08-13",
) -> None:
    """Reproduce the 2026-08-13 hand-repair: re-queue the job row with a zeroed
    ``attempts`` counter while the append-only usage ledger keeps its spent
    ``attempt_ordinal`` rows. ``UNIQUE(job_id, attempt_ordinal)`` does not carry
    ``local_day``, so a new day grants no fresh ordinal namespace."""

    def _write(conn):
        conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_pending', attempts = 0, error_code = NULL,
                   error_detail = NULL, next_attempt_at = 0.0,
                   lease_digest = NULL, lease_expires_at = NULL,
                   lease_kind = NULL, updated_at = 100.0
               WHERE id = ?""",
            (job_id,),
        )
        conn.execute(
            "DELETE FROM session_claude_registration_usage WHERE job_id = ?",
            (job_id,),
        )
        for ordinal in range(1, spent_ordinals + 1):
            conn.execute(
                """INSERT INTO session_claude_registration_usage (
                       local_day, job_id, attempt_ordinal,
                       reserved_estimated_cost_usd, reserved_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (local_day, job_id, ordinal, "0.02", 100.0),
            )

    db._execute_write(_write)


def test_claude_visibility_reset_counter_with_spent_usage_terminalizes(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("reset-counter-exhausted")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _corrupt_claude_visibility_counter(db, identity.job_id, 7)

    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02", 7)

    assert claim.status == "max_attempts_exhausted"
    assert _rows(
        db,
        "SELECT state, attempts, error_code FROM session_claude_visibility_jobs",
    ) == [
        {
            "state": "claude_failed",
            "attempts": 7,
            "error_code": "max_attempts_exhausted",
        }
    ]
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 7
    assert store.claim_claude_visibility_job(
        100.0, 60, 25, "0.50", "0.02", 7
    ).status == "no_due_job"


def test_claude_visibility_reset_counter_below_max_resumes_after_spent_ordinals(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("reset-counter-partial")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _corrupt_claude_visibility_counter(db, identity.job_id, 3)

    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02", 7)

    assert claim.status == "claimed"
    assert claim.attempt_ordinal == 4
    assert sorted(
        row["attempt_ordinal"]
        for row in _rows(
            db, "SELECT attempt_ordinal FROM session_claude_registration_usage"
        )
    ) == [1, 2, 3, 4]
    assert _rows(
        db, "SELECT state, attempts FROM session_claude_visibility_jobs"
    ) == [{"state": "claude_leased", "attempts": 4}]


def test_exact_operator_recovery_preserves_exhausted_identity_and_attempt_history(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("max-operator-recovery")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    store.retry_claude_visibility_job(
        identity.job_id, launch.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "a" * 64,
    )
    clock[0] = 86_500.0
    exhausted = store.claim_claude_visibility_job(86_500.0, 60, 25, "1.00", "0.02", 1)
    assert exhausted.status == "max_attempts_exhausted"

    repaired = store.requeue_failed_claude_visibility_reconciliation(
        identity.job_id, identity.claude_uuid
    )
    assert repaired["state"] == "claude_retry"
    assert repaired["reserved_claude_uuid"] == identity.claude_uuid
    assert repaired["attempts"] == 1
    assert repaired["error_detail"] == "operator authorized exact UUID reconciliation"

    same_id_launch = store.claim_claude_visibility_job(
        86_500.0, 60, 25, "1.00", "0.02", 1
    )
    assert same_id_launch.status == "claimed"
    assert same_id_launch.lease_kind == "launch"
    assert same_id_launch.reserved_claude_uuid == identity.claude_uuid
    assert same_id_launch.attempt_ordinal == 2
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 2


def test_claude_visibility_concurrent_exhaustion_has_one_terminal_transition(
    tmp_path,
) -> None:
    path = tmp_path / "claude-max-race.db"
    seed_db = SessionDB(path)
    seed = SessionBridgeStore(seed_db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("max-race")
    _enqueue_claude_visibility_job(seed, candidate, identity)
    launch = seed.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    seed.retry_claude_visibility_job(
        identity.job_id, launch.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = seed.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 1)
    seed.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "a" * 64,
    )
    seed_db.close()
    databases = (SessionDB(path), SessionDB(path))
    try:
        stores = tuple(
            SessionBridgeStore(item, clock=lambda: 100.0, local_timezone=timezone.utc)
            for item in databases
        )
        barrier = Barrier(2)

        def claim(store):
            barrier.wait()
            return store.claim_claude_visibility_job(
                100.0, 60, 25, "1.00", "0.02", 1
            ).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, stores))

        assert sorted(results) == ["max_attempts_exhausted", "no_due_job"]
        assert _rows(
            databases[0],
            "SELECT state, error_code FROM session_claude_visibility_jobs",
        ) == [{"state": "claude_failed", "error_code": "max_attempts_exhausted"}]
        assert (
            len(_rows(databases[0], "SELECT * FROM session_claude_registration_usage"))
            == 1
        )
    finally:
        for item in databases:
            item.close()


def test_claude_visibility_cycle_status_is_durable_and_preserves_last_empty(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    first = store.claude_visibility_status(100.0)
    candidate, identity = _claude_visibility_identity("future-work")
    _enqueue_claude_visibility_job(store, candidate, identity)
    clock[0] = 200.0
    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    restarted = SessionBridgeStore(db, clock=lambda: 300.0, local_timezone=timezone.utc)
    second = restarted.claude_visibility_status(300.0)
    assert first["last_empty_cycle"] == {"tracked": True, "value": 100.0}
    assert second["last_empty_cycle"] == {"tracked": True, "value": 100.0}
    assert second["last_cycle"] == {
        "tracked": True,
        "value": {
            "at": 200.0,
            "sequence": 2,
            "status": "no_due_job",
            "error_code": None,
            "empty_verified": False,
        },
    }
    assert second["last_registrar_result"] == {"tracked": False, "value": None}


def test_legacy_v1_cycle_discards_unverified_empty_history_on_restart_and_write(
    db: SessionDB,
) -> None:
    clock = [100.0]
    seed = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    seed.set_state(
        "session-bridge:claude-visibility:cycle",
        {
            "version": 1,
            "sequence": 7,
            "last_cycle_at": 50.0,
            "last_result": {"status": "no_due_job", "error_code": None},
            "last_empty_cycle_at": 50.0,
        },
    )
    restarted = SessionBridgeStore(
        db, clock=lambda: clock[0], local_timezone=timezone.utc
    )

    legacy = restarted.claude_visibility_status(100.0)
    restarted.record_claude_visibility_cycle(
        status="daily_limit", error_code=None, registrar_result=False
    )
    nonempty = restarted.get_state("session-bridge:claude-visibility:cycle")
    clock[0] = 200.0
    restarted.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    genuine_empty = restarted.get_state("session-bridge:claude-visibility:cycle")

    assert legacy["last_cycle"] == {
        "tracked": True,
        "value": {
            "at": 50.0,
            "sequence": 7,
            "status": "no_due_job",
            "error_code": None,
            "empty_verified": False,
        },
    }
    assert legacy["last_empty_cycle"] == {"tracked": False, "value": None}
    assert nonempty is not None
    assert nonempty["version"] == 2
    assert nonempty["sequence"] == 8
    assert "last_empty_cycle_at" not in nonempty
    assert genuine_empty is not None
    assert genuine_empty["version"] == 2
    assert genuine_empty["last_result"]["empty_verified"] is True
    assert genuine_empty["last_empty_cycle_at"] == 200.0


def test_unversioned_cycle_discards_unverified_empty_history_on_next_write(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    store.set_state(
        "session-bridge:claude-visibility:cycle",
        {
            "sequence": 3,
            "last_cycle_at": 25.0,
            "last_result": {"status": "no_due_job", "error_code": None},
            "last_empty_cycle_at": 25.0,
        },
    )

    legacy = store.claude_visibility_status(100.0)
    store.record_claude_visibility_cycle(
        status="cost_limit", error_code=None, registrar_result=False
    )
    rewritten = store.get_state("session-bridge:claude-visibility:cycle")

    assert legacy["last_cycle"]["value"]["empty_verified"] is False
    assert legacy["last_empty_cycle"] == {"tracked": False, "value": None}
    assert rewritten is not None
    assert rewritten["version"] == 2
    assert rewritten["sequence"] == 4
    assert "last_empty_cycle_at" not in rewritten


def test_current_v2_cycle_preserves_only_verified_empty_history(db: SessionDB) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    candidate, identity = _claude_visibility_identity("v2-preserve")
    _enqueue_claude_visibility_job(store, candidate, identity)
    clock[0] = 200.0

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    current = store.get_state("session-bridge:claude-visibility:cycle")
    status = store.claude_visibility_status(200.0)
    assert current is not None
    assert current["version"] == 2
    assert current["last_result"]["empty_verified"] is False
    assert current["last_empty_cycle_at"] == 100.0
    assert status["last_empty_cycle"] == {"tracked": True, "value": 100.0}


@pytest.mark.parametrize("version", ("2", 2.0, True, 3))
def test_malformed_or_future_cycle_version_is_untracked(
    db: SessionDB,
    version: object,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    store.set_state(
        "session-bridge:claude-visibility:cycle",
        {
            "version": version,
            "sequence": 9,
            "last_cycle_at": 90.0,
            "last_result": {
                "status": "no_due_job",
                "error_code": None,
                "empty_verified": True,
            },
            "last_empty_cycle_at": 90.0,
        },
    )

    status = store.claude_visibility_status(100.0)

    assert status["last_cycle"] == {"tracked": False, "value": None}
    assert status["last_empty_cycle"] == {"tracked": False, "value": None}


def test_malformed_cycle_json_is_untracked_without_raw_error(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    with db._lock:
        assert db._conn is not None
        db._conn.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES ('session-bridge:claude-visibility:cycle', ?, 1)""",
            ('{"secret":',),
        )
        db._conn.commit()

    status = store.claude_visibility_status(100.0)

    assert status["last_cycle"] == {"tracked": False, "value": None}
    assert "secret" not in repr(status)


@pytest.mark.parametrize(
    "job_state",
    ("claude_pending", "claude_retry", "claude_leased", "claude_failed", "unknown"),
)
def test_no_due_cycle_does_not_advance_empty_with_open_or_fatal_work(
    db: SessionDB,
    job_state: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity(f"not-empty-{job_state}")
    _enqueue_claude_visibility_job(store, candidate, identity)
    if job_state == "claude_leased":
        store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 5)
    else:
        with db._lock:
            assert db._conn is not None
            if job_state == "unknown":
                db._conn.execute("PRAGMA ignore_check_constraints = ON")
                db._conn.execute(
                    "UPDATE session_claude_visibility_jobs SET state = 'future_state'"
                )
                db._conn.execute("PRAGMA ignore_check_constraints = OFF")
            elif job_state == "claude_retry":
                db._conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_retry', next_attempt_at = 999,
                           error_code = 'lease_expired'"""
                )
            elif job_state == "claude_failed":
                db._conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_failed', error_code = 'source_conflict'"""
                )
            db._conn.commit()

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    status = store.claude_visibility_status(100.0)
    assert status["last_cycle"]["value"]["empty_verified"] is False
    assert status["last_empty_cycle"] == {"tracked": False, "value": None}


@pytest.mark.parametrize("visible_only", (False, True))
def test_no_due_cycle_advances_empty_for_zero_or_visible_only_rows(
    db: SessionDB,
    visible_only: bool,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    if visible_only:
        candidate, identity = _claude_visibility_identity("visible-empty")
        _enqueue_claude_visibility_job(store, candidate, identity)
        _seed_claude_visibility_native_source(db, store, candidate)
        claim = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 5)
        store.commit_claude_visibility_job(
            identity.job_id, claim.lease_digest, "a" * 64, 100.0
        )

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    status = store.claude_visibility_status(100.0)
    assert status["last_cycle"]["value"]["empty_verified"] is True
    assert status["last_empty_cycle"] == {"tracked": True, "value": 100.0}


def test_no_due_cycle_advances_empty_after_multiple_operator_dismissals(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck_jobs = [
        _claude_visibility_identity("dismissed-empty-first"),
        _claude_visibility_identity("dismissed-empty-second"),
    ]
    for stuck in stuck_jobs:
        _enqueue_claude_visibility_job(store, *stuck)
        _fail_claude_visibility_job(db, stuck[1].job_id)
        store.dismiss_claude_visibility_job(
            job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
        )

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    status = store.claude_visibility_status(100.0)
    assert status["last_cycle"]["value"]["empty_verified"] is True
    assert status["last_empty_cycle"] == {"tracked": True, "value": 100.0}


def test_no_due_cycle_keeps_empty_untracked_for_noncleared_terminal_job(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("noncleared-not-empty")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id)

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    status = store.claude_visibility_status(100.0)
    assert status["last_cycle"]["value"]["empty_verified"] is False
    assert status["last_empty_cycle"] == {"tracked": False, "value": None}


def test_later_no_due_cycle_advances_empty_after_work_clears(db: SessionDB) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("cleared-work")
    _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02", 5)
    store.commit_claude_visibility_job(
        identity.job_id, claim.lease_digest, "a" * 64, 100.0
    )
    clock[0] = 200.0

    store.record_claude_visibility_cycle(
        status="no_due_job", error_code=None, registrar_result=False
    )

    assert store.claude_visibility_status(200.0)["last_empty_cycle"] == {
        "tracked": True,
        "value": 200.0,
    }


def test_cycle_empty_verification_serializes_after_concurrent_insert(tmp_path) -> None:
    path = tmp_path / "claude-empty-insert-race.db"
    seed = SessionDB(path)
    seed.close()
    insert_db, record_db = SessionDB(path), SessionDB(path)
    try:
        insert_store = SessionBridgeStore(
            insert_db, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        record_store = SessionBridgeStore(
            record_db, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        candidate, identity = _claude_visibility_identity("empty-race")
        inserted = Event()
        release = Event()

        def insert_while_locked(conn):
            insert_store._insert_claude_visibility_job(
                conn, candidate, identity, _CLAUDE_MARKER_SECRET, 100.0
            )
            inserted.set()
            assert release.wait(timeout=10)

        with ThreadPoolExecutor(max_workers=2) as executor:
            insertion = executor.submit(insert_db._execute_write, insert_while_locked)
            assert inserted.wait(timeout=10)
            recording = executor.submit(
                record_store.record_claude_visibility_cycle,
                status="no_due_job",
                error_code=None,
                registrar_result=False,
            )
            release.set()
            insertion.result(timeout=10)
            recording.result(timeout=10)

        status = record_store.claude_visibility_status(100.0)
        assert status["last_cycle"]["value"]["empty_verified"] is False
        assert status["last_empty_cycle"] == {"tracked": False, "value": None}
    finally:
        insert_db.close()
        record_db.close()


def test_claude_visibility_cycle_status_sanitizes_error_code(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    store.record_claude_visibility_cycle(
        status="retry",
        error_code="secret token / C:/private/path",
        registrar_result=True,
    )
    status = store.claude_visibility_status(100.0)
    assert status["last_cycle"]["value"]["error_code"] == "unknown_error_code"
    assert "secret" not in repr(status)


def test_claude_visibility_public_cycle_error_codes_round_trip(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    assert {
        "inventory_invalid",
        "enqueue_failed",
        "invalid_visibility_status",
        "unknown_retry_code",
    } <= CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES

    for error_code in sorted(CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES):
        store.record_claude_visibility_cycle(
            status="degraded", error_code=error_code, registrar_result=False
        )
        assert (
            store.claude_visibility_status(clock[0])["last_cycle"]["value"][
                "error_code"
            ]
            == error_code
        )
        clock[0] += 1


def test_claude_visibility_concurrent_cycles_use_transactional_sequence(
    tmp_path,
) -> None:
    path = tmp_path / "claude-cycle-race.db"
    seed = SessionDB(path)
    seed.close()
    databases = (SessionDB(path), SessionDB(path))
    try:
        stores = tuple(
            SessionBridgeStore(item, clock=lambda: 100.0, local_timezone=timezone.utc)
            for item in databases
        )
        barrier = Barrier(2)

        def record(args):
            store, status = args
            barrier.wait()
            store.record_claude_visibility_cycle(
                status=status, error_code=None, registrar_result=False
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(record, zip(stores, ("daily_limit", "cost_limit"))))

        cycle = stores[0].claude_visibility_status(100.0)["last_cycle"]
        assert cycle["tracked"] is True
        assert cycle["value"]["at"] == 100.0
        assert cycle["value"]["sequence"] == 2
        assert cycle["value"]["status"] in {"daily_limit", "cost_limit"}
    finally:
        for item in databases:
            item.close()


def test_claude_visibility_transitions_require_exact_active_lease(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")

    operations = (
        lambda: store.retry_claude_visibility_job(
            identity.job_id, "wrong", "creation_ambiguous", 120.0, "detail"
        ),
        lambda: store.commit_claude_visibility_job(
            identity.job_id, "wrong", "a" * 64, 120.0
        ),
        lambda: store.fail_claude_visibility_job(
            identity.job_id, "wrong", "uuid_conflict", "detail"
        ),
    )
    for operation in operations:
        with pytest.raises(ValueError, match="active Claude visibility lease"):
            operation()

    with pytest.raises(ValueError, match="requires a Claude reconciliation lease"):
        store.fail_claude_visibility_job(
            identity.job_id, claim.lease_digest, "uuid_conflict", "conflict"
        )

    failed = store.fail_claude_visibility_job(
        identity.job_id, claim.lease_digest, "source_conflict", "conflict"
    )
    assert failed["state"] == "claude_failed"
    assert failed["error_code"] == "source_conflict"


def test_claude_visibility_commit_cannot_backdate_an_expired_lease(
    db: SessionDB,
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 10, 25, "0.50", "0.02")
    clock[0] = 111.0

    with pytest.raises(ValueError, match="active Claude visibility lease"):
        store.commit_claude_visibility_job(
            identity.job_id,
            claim.lease_digest,
            "a" * 64,
            105.0,
        )


@pytest.mark.parametrize(
    "detail", ["registration response malformed", "exact transcript conflict"]
)
def test_failed_malformed_registration_can_only_requeue_exact_uuid_reconciliation(
    db: SessionDB, detail: str
) -> None:
    clock = [100.0]
    store = SessionBridgeStore(db, clock=lambda: clock[0], local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity("provider-limit")
    _enqueue_claude_visibility_job(store, candidate, identity)
    launch = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    failed = store.fail_claude_visibility_job(
        identity.job_id,
        launch.lease_digest,
        "bridge_conflict",
        detail,
    )
    assert failed["state"] == "claude_failed"

    with pytest.raises(ValueError, match="exact failed Claude visibility job"):
        store.requeue_failed_claude_visibility_reconciliation(
            identity.job_id, "00000000-0000-4000-8000-000000000000"
        )

    repaired = store.requeue_failed_claude_visibility_reconciliation(
        identity.job_id, identity.claude_uuid
    )
    assert repaired["state"] == "claude_retry"
    assert repaired["error_code"] == "creation_ambiguous"

    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert claim.lease_kind == "reconciliation"
    assert claim.requires_exact_id_reconciliation is True
    assert claim.launch_permitted is False
    assert claim.reserved_claude_uuid == identity.claude_uuid


@pytest.mark.parametrize(
    ("requested_code", "persisted_code"),
    [("source_conflict", "source_conflict"), ("invented_code", "unknown_error_code")],
)
def test_claude_visibility_retry_fatal_or_unknown_code_terminalizes(
    db: SessionDB,
    requested_code: str,
    persisted_code: str,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    result = store.retry_claude_visibility_job(
        identity.job_id, claim.lease_digest, requested_code, 120.0, "detail"
    )

    assert result["state"] == "claude_failed"
    assert result["error_code"] == persisted_code


def test_claude_visibility_source_lookup_is_read_only_and_covers_all_states(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()

    assert store.has_claude_visibility_source(candidate.source_session_id) is False

    _enqueue_claude_visibility_job(store, candidate, identity)

    assert store.has_claude_visibility_source(candidate.source_session_id) is True
    assert store.claude_visibility_status(100.0)["counts"]["claude_pending"] == 1


def test_claude_visibility_claims_at_most_one_due_job_and_status_is_exact(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    for suffix in range(5):
        candidate, identity = _claude_visibility_identity(str(suffix))
        _enqueue_claude_visibility_job(store, candidate, identity)
        _seed_claude_visibility_native_source(db, store, candidate)

    leased = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    retry_source = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        retry_source.job_id,
        retry_source.lease_digest,
        "native_transcript_not_indexed",
        200.0,
        "not indexed",
    )
    failed_source = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.fail_claude_visibility_job(
        failed_source.job_id, failed_source.lease_digest, "marker_conflict", "bad"
    )
    visible_source = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.commit_claude_visibility_job(
        visible_source.job_id,
        visible_source.lease_digest,
        "a" * 64,
        110.0,
    )
    status = store.claude_visibility_status(100.0)

    assert leased.status == "claimed"
    assert status["counts"] == {
        "claude_pending": 1,
        "claude_leased": 1,
        "claude_retry": 1,
        "claude_visible": 1,
        "claude_failed": 1,
    }
    assert status["retry_codes"] == {"native_transcript_not_indexed": 1}
    assert status["failed_codes"] == {"marker_conflict": 1}


def test_claude_visibility_status_reports_unknown_state_as_sanitized_fatal(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    with db._lock:
        assert db._conn is not None
        db._conn.execute("PRAGMA ignore_check_constraints = ON")
        db._conn.execute(
            "UPDATE session_claude_visibility_jobs SET state = ?, error_code = ?",
            ("future_state", "future-code"),
        )
        db._conn.commit()
        db._conn.execute("PRAGMA ignore_check_constraints = OFF")

    status = store.claude_visibility_status(100.0)

    assert status["fatal"] == [
        {
            "code": "unknown_job_state",
            "state": "future_state",
            "error_code": "future-code",
            "count": 1,
        }
    ]


def _fail_claude_visibility_job(
    db: SessionDB,
    job_id: str,
    *,
    attempts: int = 7,
    error_code: str = "max_attempts_exhausted",
) -> None:
    """Put one job in the terminal state the exhaustion path writes."""

    with db._lock:
        assert db._conn is not None
        db._conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_failed', attempts = ?, error_code = ?,
                   error_detail = 'maximum paid launch attempts exhausted'
               WHERE id = ?""",
            (attempts, error_code, job_id),
        )
        db._conn.commit()


def test_operator_dismissal_reopens_the_enqueue_gate(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("stuck")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id)

    blocked = store.enqueue_claude_visibility_batch_if_idle(
        [_claude_visibility_identity("before")], _CLAUDE_MARKER_SECRET
    )

    store.dismiss_claude_visibility_job(
        job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
    )
    reopened = store.enqueue_claude_visibility_batch_if_idle(
        [_claude_visibility_identity("after")], _CLAUDE_MARKER_SECRET
    )

    # The terminal job trips the FATAL arm, which precedes the open_work
    # check -- so this is the exact refusal the operator has to clear.
    assert blocked == {
        "status": "fatal",
        "inserted": 0,
        "duplicates": 0,
        "fatal_reasons": ["max_attempts_exhausted"],
    }
    assert reopened == {"status": "inserted", "inserted": 1, "duplicates": 0}


def test_operator_dismissal_clears_the_status_open_and_fatal_signals(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("stuck")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id)

    before = store.claude_visibility_status(100.0)
    store.dismiss_claude_visibility_job(
        job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
    )
    after = store.claude_visibility_status(100.0)

    assert before["counts"]["claude_failed"] == 1
    assert before["failed_codes"] == {"max_attempts_exhausted": 1}
    assert after["counts"]["claude_failed"] == 0
    assert after["failed_codes"] == {}


def test_operator_dismissal_never_touches_the_registration_usage_ledger(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("stuck")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id)
    with db._lock:
        assert db._conn is not None
        db._conn.executemany(
            """INSERT INTO session_claude_registration_usage
               (local_day, job_id, attempt_ordinal, reserved_estimated_cost_usd,
                reserved_at)
               VALUES (?, ?, ?, '0.020000', 100.0)""",
            [("2026-08-13", stuck[1].job_id, ordinal) for ordinal in range(1, 8)],
        )
        db._conn.commit()

    store.dismiss_claude_visibility_job(
        job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
    )

    ledger = _rows(
        db,
        """SELECT attempt_ordinal FROM session_claude_registration_usage
           WHERE job_id = ? ORDER BY attempt_ordinal""",
        (stuck[1].job_id,),
    )
    assert [row["attempt_ordinal"] for row in ledger] == [1, 2, 3, 4, 5, 6, 7]
    assert _rows(db, "SELECT state FROM session_claude_visibility_jobs")[0][
        "state"
    ] == "claude_failed"


def test_operator_dismissal_refuses_a_job_that_is_still_live(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    pending = _claude_visibility_identity("pending")
    _enqueue_claude_visibility_job(store, *pending)

    with pytest.raises(ValueError, match="terminally failed"):
        store.dismiss_claude_visibility_job(
            job_id=pending[1].job_id, expected_error_code="max_attempts_exhausted"
        )

    assert store.claude_visibility_status(100.0)["counts"]["claude_pending"] == 1


def test_operator_dismissal_refuses_a_mismatched_error_code(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("stuck")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id, error_code="uuid_conflict")

    with pytest.raises(ValueError, match="terminally failed"):
        store.dismiss_claude_visibility_job(
            job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
        )

    assert store.claude_visibility_status(100.0)["failed_codes"] == {"uuid_conflict": 1}


def test_operator_dismissal_refuses_a_second_time(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    stuck = _claude_visibility_identity("stuck")
    _enqueue_claude_visibility_job(store, *stuck)
    _fail_claude_visibility_job(db, stuck[1].job_id)
    store.dismiss_claude_visibility_job(
        job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
    )

    with pytest.raises(ValueError, match="terminally failed"):
        store.dismiss_claude_visibility_job(
            job_id=stuck[1].job_id, expected_error_code="max_attempts_exhausted"
        )


def test_atomic_idle_batch_rolls_back_all_rows_when_second_insert_fails(
    db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    items = [_claude_visibility_identity(str(index)) for index in range(2)]
    original = store._insert_claude_visibility_job
    calls = 0

    def fail_second(conn, candidate, identity, marker_secret, now):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected")
        return original(conn, candidate, identity, marker_secret, now)

    monkeypatch.setattr(store, "_insert_claude_visibility_job", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        store.enqueue_claude_visibility_batch_if_idle(items, _CLAUDE_MARKER_SECRET)

    assert _rows(db, "SELECT * FROM session_claude_visibility_jobs") == []


def test_atomic_idle_batch_reports_exact_duplicates_and_blocks_open_work(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    duplicate = _claude_visibility_identity("duplicate")
    _enqueue_claude_visibility_job(store, *duplicate)
    _seed_claude_visibility_native_source(db, store, duplicate[0])
    store.commit_claude_visibility_job(
        duplicate[1].job_id,
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").lease_digest,
        "a" * 64,
        100.0,
    )
    fresh = _claude_visibility_identity("fresh")

    result = store.enqueue_claude_visibility_batch_if_idle(
        [duplicate, fresh], _CLAUDE_MARKER_SECRET
    )
    blocked = store.enqueue_claude_visibility_batch_if_idle(
        [_claude_visibility_identity("later")], _CLAUDE_MARKER_SECRET
    )

    assert result == {"status": "inserted", "inserted": 1, "duplicates": 1}
    assert blocked == {"status": "open_work", "inserted": 0, "duplicates": 0}
    assert len(_rows(db, "SELECT * FROM session_claude_visibility_jobs")) == 2


def test_atomic_idle_batch_two_connections_allow_only_one_coordinator(
    tmp_path,
) -> None:
    path = tmp_path / "claude-idle-batch-race.db"
    seed = SessionDB(path)
    seed.close()
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        stores = (
            SessionBridgeStore(first_db, clock=lambda: 100.0),
            SessionBridgeStore(second_db, clock=lambda: 100.0),
        )
        batches = tuple(
            [_claude_visibility_identity(f"{owner}-{index}") for index in range(10)]
            for owner in range(2)
        )
        barrier = Barrier(2)

        def enqueue(args):
            store, batch = args
            barrier.wait()
            return store.enqueue_claude_visibility_batch_if_idle(
                batch, _CLAUDE_MARKER_SECRET
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enqueue, zip(stores, batches)))

        assert sorted(result["status"] for result in results) == [
            "inserted",
            "open_work",
        ]
        assert sorted(result["inserted"] for result in results) == [0, 10]
        assert (
            len(_rows(first_db, "SELECT * FROM session_claude_visibility_jobs")) == 10
        )
    finally:
        first_db.close()
        second_db.close()


def test_claude_visibility_daily_limit_blocks_twenty_sixth_attempt_atomically(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(
        db, clock=lambda: 1_768_608_000.0, local_timezone=timezone.utc
    )
    for suffix in range(26):
        candidate, identity = _claude_visibility_identity(str(suffix))
        _enqueue_claude_visibility_job(store, candidate, identity)

    for _ in range(25):
        claim = store.claim_claude_visibility_job(
            1_768_608_000.0, 60, 25, "100.00", "0.02"
        )
        assert claim.status == "claimed"
        store.fail_claude_visibility_job(
            claim.job_id, claim.lease_digest, "source_conflict", "bounded test"
        )

    blocked = store.claim_claude_visibility_job(
        1_768_608_000.0, 60, 25, "100.00", "0.02"
    )
    usage = _rows(db, "SELECT * FROM session_claude_registration_usage")

    assert blocked.status == "daily_limit"
    assert len(usage) == 25
    assert len({row["job_id"] for row in usage}) == 25


def test_claude_visibility_accounting_uses_authoritative_clock_and_local_day(
    db: SessionDB,
) -> None:
    authoritative = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc).timestamp()
    caller_future = datetime(2036, 1, 1, tzinfo=timezone.utc).timestamp()
    store = SessionBridgeStore(
        db,
        clock=lambda: authoritative,
        local_timezone=ZoneInfo("America/New_York"),
    )
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)

    claim = store.claim_claude_visibility_job(caller_future, 60, 25, "0.50", "0.02")

    assert claim.status == "claimed"
    assert _rows(
        db,
        """SELECT local_day, reserved_at, reserved_estimated_cost_usd
           FROM session_claude_registration_usage""",
    ) == [
        {
            "local_day": "2026-03-08",
            "reserved_at": authoritative,
            "reserved_estimated_cost_usd": "0.020000",
        }
    ]
    assert _rows(db, "SELECT lease_expires_at FROM session_claude_visibility_jobs") == [
        {"lease_expires_at": authoritative + 60}
    ]


@pytest.mark.parametrize(
    ("utc_time", "expected_day"),
    (
        (datetime(2026, 3, 8, 4, 59, tzinfo=timezone.utc), "2026-03-07"),
        (datetime(2026, 3, 8, 5, 1, tzinfo=timezone.utc), "2026-03-08"),
        (datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), "2026-03-08"),
        (datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc), "2026-03-08"),
    ),
)
def test_claude_visibility_local_day_handles_midnight_and_dst_transition(
    db: SessionDB,
    utc_time: datetime,
    expected_day: str,
) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: utc_time.timestamp(),
        local_timezone=ZoneInfo("America/New_York"),
    )
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)

    assert (
        store.claim_claude_visibility_job(
            9_999_999_999.0, 60, 25, "0.50", "0.02"
        ).status
        == "claimed"
    )
    assert _rows(db, "SELECT local_day FROM session_claude_registration_usage") == [
        {"local_day": expected_day}
    ]


def test_claude_visibility_money_is_exact_bounded_microdollars(db: SessionDB) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    for suffix in range(2):
        candidate, identity = _claude_visibility_identity(f"money-{suffix}")
        _enqueue_claude_visibility_job(store, candidate, identity)

    with pytest.raises(ValueError, match="at most 6 decimal places"):
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.0000001")
    with pytest.raises(ValueError, match="cannot exceed 1000000 USD"):
        store.claim_claude_visibility_job(100.0, 60, 25, "1e1000000", "0.02")

    with localcontext() as context:
        context.prec = 1
        for _ in range(2):
            claim = store.claim_claude_visibility_job(
                100.0, 60, 25, "0.040000", "0.020000"
            )
            assert claim.status == "claimed"
            store.fail_claude_visibility_job(
                claim.job_id, claim.lease_digest, "source_conflict", "bounded test"
            )
    assert {
        row["reserved_estimated_cost_usd"]
        for row in _rows(
            db,
            "SELECT reserved_estimated_cost_usd FROM session_claude_registration_usage",
        )
    } == {"0.020000"}


def test_claude_visibility_two_connections_cannot_exceed_daily_cap(tmp_path) -> None:
    path = tmp_path / "claude-cap-race.db"
    seed_db = SessionDB(path)
    seed_store = SessionBridgeStore(
        seed_db, clock=lambda: 100.0, local_timezone=timezone.utc
    )
    identities = []
    for suffix in range(26):
        candidate, identity = _claude_visibility_identity(f"race-{suffix}")
        identities.append(identity)
        _enqueue_claude_visibility_job(seed_store, candidate, identity)
    for identity in identities[:24]:
        seed_db._conn.execute(
            "UPDATE session_claude_visibility_jobs SET state = 'claude_failed' WHERE id = ?",
            (identity.job_id,),
        )
        seed_db._conn.execute(
            """INSERT INTO session_claude_registration_usage
               VALUES ('1970-01-01', ?, 1, '0.020000', 100)""",
            (identity.job_id,),
        )
    seed_db._conn.commit()
    seed_db.close()

    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        second = SessionBridgeStore(
            second_db, clock=lambda: 100.0, local_timezone=timezone.utc
        )
        barrier = Barrier(2)

        def claim(store: SessionBridgeStore):
            barrier.wait()
            return store.claim_claude_visibility_job(100.0, 60, 25, "1.00", "0.02")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (first, second)))

        assert sorted(result.status for result in results) == [
            "claimed",
            "daily_limit",
        ]
        assert (
            len(_rows(first_db, "SELECT * FROM session_claude_registration_usage"))
            == 25
        )
    finally:
        first_db.close()
        second_db.close()


def test_claude_visibility_store_rejects_daily_limit_above_hard_ceiling(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)

    with pytest.raises(ValueError, match="cannot exceed 25"):
        store.claim_claude_visibility_job(100.0, 60, 26, "0.50", "0.02")


def test_claude_visibility_emergency_cost_gate_is_independent(db: SessionDB) -> None:
    store = SessionBridgeStore(
        db, clock=lambda: 1_768_608_000.0, local_timezone=timezone.utc
    )
    for suffix in range(3):
        candidate, identity = _claude_visibility_identity(str(suffix))
        _enqueue_claude_visibility_job(store, candidate, identity)

    for _ in range(2):
        claim = store.claim_claude_visibility_job(
            1_768_608_000.0, 60, 25, "0.05", "0.02"
        )
        store.fail_claude_visibility_job(
            claim.job_id, claim.lease_digest, "source_conflict", "bounded test"
        )
    blocked = store.claim_claude_visibility_job(1_768_608_000.0, 60, 25, "0.05", "0.02")

    assert blocked.status == "cost_limit"
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 2


def test_claude_visibility_read_only_reconciliation_consumes_no_slot(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(
        db, clock=lambda: 1_768_608_000.0, local_timezone=timezone.utc
    )
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    claim = store.claim_claude_visibility_job(1_768_608_000.0, 60, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id,
        claim.lease_digest,
        "creation_ambiguous",
        1_768_608_100.0,
        "unknown launch result",
    )

    first = store.inspect_due_claude_visibility_reconciliation(1_768_608_100.0)
    second = store.inspect_due_claude_visibility_reconciliation(1_768_608_100.0)

    assert first == second
    assert first.status == "reconciliation_required"
    assert first.reserved_claude_uuid == identity.claude_uuid
    assert first.lease_digest is None
    assert first.requires_exact_id_reconciliation is True
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    assert _rows(
        db,
        "SELECT state, attempts, lease_digest FROM session_claude_visibility_jobs",
    ) == [{"state": "claude_retry", "attempts": 1, "lease_digest": None}]


def test_claude_visibility_reconciliation_api_does_not_divert_fresh_pending(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)

    inspected = store.inspect_due_claude_visibility_reconciliation(100.0)
    reconciliation = store.claim_claude_visibility_reconciliation(100.0, 60)

    assert inspected.status == "no_due_job"
    assert reconciliation.status == "no_due_job"
    paid = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert paid.job_id == identity.job_id
    assert paid.registration_reserved is True
    assert paid.launch_permitted is True


def test_claude_visibility_retry_with_no_usage_requires_absence_before_one_launch(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    db._conn.execute(
        """UPDATE session_claude_visibility_jobs
           SET state = 'claude_retry', error_code = 'exact_id_absent_reconciled'
           WHERE id = ?""",
        (identity.job_id,),
    )

    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert reconciliation.registration_reserved is False
    assert reconciliation.launch_permitted is False
    assert _rows(db, "SELECT * FROM session_claude_registration_usage") == []
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "d" * 64,
    )

    assert store.inspect_due_claude_visibility_reconciliation(100.0).status == (
        "no_due_job"
    )
    assert store.claim_claude_visibility_reconciliation(100.0, 60).status == (
        "no_due_job"
    )
    paid = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert paid.registration_reserved is True
    assert paid.launch_permitted is True
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    assert (
        store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02").status
        == "no_due_job"
    )


def test_claude_visibility_reconciliation_lease_can_commit_without_new_slot(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(
        db, clock=lambda: 1_768_608_100.0, local_timezone=timezone.utc
    )
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    _seed_claude_visibility_native_source(db, store, candidate)
    launch = store.claim_claude_visibility_job(1_768_608_000.0, 200, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id,
        launch.lease_digest,
        "creation_ambiguous",
        1_768_608_100.0,
        "unknown launch result",
    )

    reconciliation = store.claim_claude_visibility_reconciliation(1_768_608_100.0, 60)

    assert reconciliation.status == "claimed"
    assert reconciliation.registration_reserved is False
    assert reconciliation.launch_permitted is False
    assert reconciliation.requires_exact_id_reconciliation is True
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    visible = store.commit_claude_visibility_job(
        identity.job_id,
        reconciliation.lease_digest,
        "a" * 64,
        1_768_608_110.0,
    )
    assert visible["state"] == "claude_visible"
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    assert _rows(
        db,
        """SELECT reserved_claude_uuid, attempt_ordinal, outcome
           FROM session_claude_visibility_reconciliations""",
    ) == [
        {
            "reserved_claude_uuid": identity.claude_uuid,
            "attempt_ordinal": 1,
            "outcome": "exact_match",
        }
    ]


def test_claude_visibility_transient_reconciliation_retries_never_launch_or_spend(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    paid = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id, paid.lease_digest, "creation_ambiguous", 110.0, "unknown"
    )

    for due_at in (110.0, 120.0):
        reconciliation = store.claim_claude_visibility_job(
            due_at, 60, 25, "0.50", "0.02"
        )
        assert reconciliation.registration_reserved is False
        assert reconciliation.launch_permitted is False
        assert reconciliation.attempt_ordinal == 1
        store.retry_claude_visibility_job(
            identity.job_id,
            reconciliation.lease_digest,
            "native_transcript_not_indexed",
            due_at + 10,
            "lookup transient",
        )

    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1


def test_claude_visibility_reconciliation_lease_can_fail_conflict_without_slot(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    paid = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id, paid.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")

    failed = store.fail_claude_visibility_job(
        identity.job_id,
        reconciliation.lease_digest,
        "uuid_conflict",
        "exact reserved UUID belongs to another source",
    )

    assert failed["state"] == "claude_failed"
    assert failed["error_code"] == "uuid_conflict"
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 1
    assert _rows(
        db, "SELECT outcome FROM session_claude_visibility_reconciliations"
    ) == [{"outcome": "conflict"}]


def test_claude_visibility_new_ambiguity_invalidates_prior_absence(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0, local_timezone=timezone.utc)
    candidate, identity = _claude_visibility_identity()
    _enqueue_claude_visibility_job(store, candidate, identity)
    first = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.retry_claude_visibility_job(
        identity.job_id, first.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )
    reconciliation = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    store.record_claude_visibility_exact_id_absent(
        identity.job_id,
        reconciliation.lease_digest,
        identity.claude_uuid,
        reconciliation.attempt_ordinal,
        "c" * 64,
    )
    second = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert second.launch_permitted is True
    store.retry_claude_visibility_job(
        identity.job_id, second.lease_digest, "creation_ambiguous", 100.0, "unknown"
    )

    required_again = store.claim_claude_visibility_job(100.0, 60, 25, "0.50", "0.02")
    assert required_again.registration_reserved is False
    assert required_again.launch_permitted is False
    assert required_again.attempt_ordinal == 2
    assert len(_rows(db, "SELECT * FROM session_claude_registration_usage")) == 2


def test_sidebar_candidate_query_reads_its_page_from_the_ordered_index(db) -> None:
    """Pin the plan, not just the answer.

    A Claude row's ``last_active`` lives only inside ``session_bridge_state``
    JSON, so without an index SQLite computed it for every candidate and sorted
    the whole set before LIMIT could cut it -- the scan loop's largest frame.
    ``idx_session_bridge_state_activity_ordered`` stores that value DESC, key,
    which lets the Claude arm read its page straight off the index in final
    order and stop at LIMIT.

    SQLite only satisfies the ORDER BY from an index when the query repeats the
    indexed expression verbatim, so a reformat of either copy silently restores
    the full sort while every correctness test still passes.  Capture the SQL
    the method actually issues and assert the plan over it, rather than
    re-deriving a query the production path might no longer use.
    """
    _sidebar_candidate(db, native_id="planned", eligible_at=100.0)
    store = SessionBridgeStore(db)

    conn = db._conn
    assert conn is not None
    issued: list[str] = []
    conn.set_trace_callback(issued.append)
    try:
        store.list_sidebar_candidates(after=0.0, limit=10)
    finally:
        conn.set_trace_callback(None)

    # The trace callback hands back the statement with its parameters already
    # expanded, so it replays through EXPLAIN without rebinding.
    candidate_sql = [sql for sql in issued if "claude_candidate" in sql]
    assert len(candidate_sql) == 1, (
        f"expected one candidate query, got {len(candidate_sql)}"
    )
    with db._lock:
        plan = [
            (row["id"], row["parent"], row["detail"])
            for row in conn.execute("EXPLAIN QUERY PLAN " + candidate_sql[0])
        ]

    # Scope the assertion to the Claude arm's subtree.  The outer merge and the
    # Hermes arm each sort legitimately -- they handle a page-sized set -- so a
    # whole-plan "no sort" assertion would be false, and a whole-plan "uses the
    # index" assertion is too weak: INDEXED BY keeps the index in the plan even
    # when the expression stops matching, and SQLite just adds the sort back.
    arm = [node for node, _, detail in plan if "CO-ROUTINE claude_candidate" in detail]
    assert len(arm) == 1, f"could not locate the Claude arm: {plan}"
    subtree = set(arm)
    for node, parent, _ in plan:
        if parent in subtree:
            subtree.add(node)

    arm_steps = [detail for node, _, detail in plan if node in subtree]
    activity_steps = [step for step in arm_steps if "activity" in step]
    assert activity_steps, f"no step reads the activity table: {arm_steps}"
    assert all(
        "idx_session_bridge_state_activity_ordered" in step
        for step in activity_steps
    ), f"Claude arm is not driven by the ordered index: {activity_steps}"
    assert not [step for step in arm_steps if "TEMP B-TREE" in step], (
        "the Claude arm sorts instead of reading its page in index order; the "
        f"query expression no longer matches the index: {arm_steps}"
    )


def test_superseded_activity_key_index_is_dropped(db) -> None:
    """The first cut of this optimisation indexed (key, <expr>).

    The query no longer looks activity rows up by key, so leaving that index in
    place would cost every ``session_bridge_state`` write a second expression
    index maintained for nothing.  ``BRIDGE_SCHEMA_SQL`` drops it on open.
    """
    with db._lock:
        conn = db._conn
        assert conn is not None
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'session_bridge_state'"
            )
        }

    assert "idx_session_bridge_state_activity_ordered" in indexes
    assert "idx_session_bridge_state_activity" not in indexes


def test_activity_key_prefix_is_shared_by_writer_and_query(db) -> None:
    """The query recovers ``session_id`` by slicing the prefix off the key.

    That only works while the prefix the writer stamps on and the prefix the
    query slices are the same string.  If they ever drift, every Claude row
    silently stops matching and the sidebar goes empty with no error -- so
    assert the writer is built from the same constant the query is.
    """
    assert _external_activity_state_key("claude:abc") == (
        f"{_EXTERNAL_ACTIVITY_KEY_PREFIX}claude:abc"
    )

    candidate = _sidebar_candidate(db, native_id="prefixed", eligible_at=100.0)
    with db._lock:
        conn = db._conn
        assert conn is not None
        stored = conn.execute(
            "SELECT key FROM session_bridge_state WHERE key = ?",
            (_external_activity_state_key(candidate.source_session_id),),
        ).fetchone()
    assert stored is not None

    page = SessionBridgeStore(db).list_sidebar_candidates(after=0.0, limit=10)
    assert [row.source_session_id for row in page] == [candidate.source_session_id]


def test_session_bridge_state_still_accepts_non_json_values(db) -> None:
    """Arm the ``json_valid`` guard on the indexed expression.

    Indexing ``json_extract(value_json, ...)`` makes SQLite evaluate it on
    every write to ``session_bridge_state`` -- a table with ten unrelated
    writers.  Without the guard this INSERT fails with "malformed JSON",
    so an index added for the sidebar query would break callers that never
    touch the sidebar.  Drop the guard and this test fails.
    """
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.execute(
            "INSERT INTO session_bridge_state (key, value_json, updated_at) "
            "VALUES ('session-bridge:probe:not-json', 'plainly not json', 1.0)",
        )
        conn.commit()
        stored = conn.execute(
            "SELECT value_json FROM session_bridge_state "
            "WHERE key = 'session-bridge:probe:not-json'"
        ).fetchone()

    assert stored["value_json"] == "plainly not json"


def test_sidebar_candidate_with_malformed_activity_json_is_skipped(db) -> None:
    """A corrupt activity row drops that candidate instead of failing the page.

    The guarded expression yields NULL for unparseable state, which the
    ``last_active IS NOT NULL`` filter already discards -- the same treatment
    a Claude row with no activity record gets.  Unguarded, json_extract raises
    and blanks the entire enumeration.
    """
    healthy = _sidebar_candidate(db, native_id="healthy", eligible_at=200.0)
    corrupt = _sidebar_candidate(db, native_id="corrupt", eligible_at=100.0)
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE session_bridge_state SET value_json = 'not json at all' "
            "WHERE key = ?",
            (_external_activity_state_key(corrupt.source_session_id),),
        )
        conn.commit()

    page = SessionBridgeStore(db).list_sidebar_candidates(after=0.0, limit=10)

    assert [candidate.source_session_id for candidate in page] == [
        healthy.source_session_id
    ]
