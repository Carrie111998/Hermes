from __future__ import annotations

from collections.abc import Sequence
import json
import logging
import math
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import web_server
from hermes_state import SessionDB
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    SidebarJobState,
)
from session_bridge.sidebar import SidebarCandidate, sidebar_bridge_id
from session_bridge.store import SessionBridgeStore


def _projection(
    provider: Provider, native_id: str, *, started_at: float
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} preserved title",
        cwd=f"C:/workspace/{provider.value}",
        started_at=started_at,
        last_active=started_at + 10.0,
        messages=(
            ProjectedMessage(
                native_event_id=f"event-{native_id}",
                ordinal=0,
                role="user",
                content=f"preserved {provider.value} preview",
                timestamp=started_at + 10.0,
            ),
        ),
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=f"cursor-{native_id}",
        native_hash=f"hash-{native_id}",
        origin_kind=OriginKind.NATIVE,
    )


def _seed_profile(db_path: Path, suffix: str) -> tuple[str, ...]:
    now = time.time()
    db = SessionDB(db_path=db_path)
    try:
        hermes_id = f"hermes-{suffix}"
        claude_id = f"claude:claude-{suffix}"
        codex_id = f"codex:codex-{suffix}"
        db.create_session(
            hermes_id,
            "cli",
            model="preserved-hermes-model",
            cwd="C:/workspace/hermes",
        )
        store = SessionBridgeStore(db)
        store.upsert_projection(
            _projection(Provider.CLAUDE, f"claude-{suffix}", started_at=now - 30.0)
        )
        store.upsert_projection(
            _projection(Provider.CODEX, f"codex-{suffix}", started_at=now - 20.0)
        )
        return hermes_id, claude_id, codex_id
    finally:
        db.close()


def _baseline_rows(db_path: Path) -> dict[str, dict[str, object]]:
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        return {
            row["id"]: row
            for row in db.list_sessions_rich(limit=20, order_by_last_active=False)
        }
    finally:
        db.close()


def _set_sync_error(db_path: Path, session_id: str, detail: str) -> None:
    db = SessionDB(db_path=db_path)
    try:
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE external_sessions SET sync_error = ? WHERE session_id = ?",
                (detail, session_id),
            )
        )
    finally:
        db.close()


def _seed_sidebar_catalog_jobs(db_path: Path) -> dict[str, str]:
    now = time.time()
    states = {
        "sidebar-pending": SidebarJobState.PENDING,
        "sidebar-leased": SidebarJobState.LEASED,
        "sidebar-retry": SidebarJobState.RETRY,
        "sidebar-visible": SidebarJobState.VISIBLE,
        "sidebar-failed": SidebarJobState.FAILED,
    }
    thread_id = "33333333-3333-4333-8333-333333333333"
    secret_error = "C:/private/native/sidebar.jsonl bearer-secret"
    secret_lease_digest = "lease-digest-must-not-leak"
    db = SessionDB(db_path=db_path)
    try:
        store = SessionBridgeStore(db, clock=lambda: now)
        for session_id, state in states.items():
            db.create_session(
                session_id,
                "cli",
                model="preserved-hermes-model",
                cwd=f"C:/workspace/{session_id}",
            )
            candidate = SidebarCandidate(
                source_session_id=session_id,
                provider=Provider.HERMES,
                bridge_id=sidebar_bridge_id(session_id),
                title=f"[Hermes] {session_id}",
                cwd=f"C:/workspace/{session_id}",
                git_root=None,
                git_branch=None,
                git_head=None,
                worktree_id=None,
                eligible_at=now - 120.0,
            )
            store.enqueue_sidebar_job(candidate)

            def set_state(
                conn, *, source_id: str = session_id, job_state=state
            ) -> None:
                values = {
                    "state": job_state.value,
                    "lease_digest": (
                        secret_lease_digest
                        if job_state is SidebarJobState.LEASED
                        else None
                    ),
                    "lease_expires_at": (
                        now - 1.0 if job_state is SidebarJobState.LEASED else None
                    ),
                    "completion_digest": (
                        "completion-digest"
                        if job_state is SidebarJobState.VISIBLE
                        else None
                    ),
                    "codex_thread_id": (
                        thread_id if job_state is SidebarJobState.VISIBLE else None
                    ),
                    "error_code": (
                        secret_error
                        if job_state in (SidebarJobState.RETRY, SidebarJobState.FAILED)
                        else None
                    ),
                    "visible_at": (
                        now if job_state is SidebarJobState.VISIBLE else None
                    ),
                }
                conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = :state,
                           lease_digest = :lease_digest,
                           lease_expires_at = :lease_expires_at,
                           completion_digest = :completion_digest,
                           codex_thread_id = :codex_thread_id,
                           error_code = :error_code,
                           visible_at = :visible_at
                       WHERE source_session_id = :source_id""",
                    {**values, "source_id": source_id},
                )

            db._execute_write(set_state)
    finally:
        db.close()
    return {
        "secret_error": secret_error,
        "secret_lease_digest": secret_lease_digest,
        "thread_id": thread_id,
    }


def _set_sidebar_catalog_field(
    db_path: Path,
    session_id: str,
    field: str,
    value: object,
    *,
    ignore_checks: bool = False,
) -> None:
    assert field in {"codex_thread_id", "error_code", "lease_expires_at"}
    db = SessionDB(db_path=db_path)
    try:

        def update(conn) -> None:
            if ignore_checks:
                conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                f"UPDATE session_sidebar_jobs SET {field} = ? WHERE source_session_id = ?",
                (value, session_id),
            )

        db._execute_write(update)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sessions_api_preserves_rows_and_batches_bridge_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    expected_ids = _seed_profile(db_path, "default")
    secret_sync_error = "C:/private/native/transcript.jsonl bearer-secret"
    _set_sync_error(db_path, expected_ids[1], secret_sync_error)
    baseline = _baseline_rows(db_path)
    calls: list[tuple[str, ...]] = []
    original = SessionBridgeStore.get_bridge_summaries

    def recording_summaries(
        store: SessionBridgeStore, session_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        calls.append(tuple(session_ids))
        return original(store, session_ids)

    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )
    monkeypatch.setattr(
        SessionBridgeStore,
        "get_bridge_summaries",
        recording_summaries,
    )

    response = await web_server.get_sessions(limit=20)

    assert len(calls) == 1
    assert set(calls[0]) == set(expected_ids)
    response_json = json.dumps(response)
    assert '"bridge_links"' not in response_json
    assert '"source_cursor"' not in response_json
    assert '"source_hash"' not in response_json
    assert secret_sync_error not in response_json
    rows = {row["id"]: row for row in response["sessions"]}
    assert set(rows) == set(expected_ids)
    for session_id, expected in baseline.items():
        for field, value in expected.items():
            expected_value = bool(value) if field == "archived" else value
            assert rows[session_id][field] == expected_value
        assert isinstance(rows[session_id]["is_active"], bool)

    assert rows[expected_ids[0]]["bridge_provider"] == "hermes"
    assert rows[expected_ids[0]]["bridge_mirror_state"] is None
    assert rows[expected_ids[1]]["bridge_provider"] == "claude"
    assert rows[expected_ids[1]]["bridge_native_id"] == "claude-default"
    assert rows[expected_ids[1]]["bridge_origin_kind"] == "native"
    assert rows[expected_ids[1]]["bridge_mirror_state"] == "catalog_only"
    assert rows[expected_ids[1]]["bridge_sync_error"] == "sync_degraded"
    assert rows[expected_ids[2]]["bridge_provider"] == "codex"
    assert rows[expected_ids[2]]["bridge_native_id"] == "codex-default"
    assert rows[expected_ids[2]]["bridge_origin_kind"] == "native"
    assert rows[expected_ids[2]]["bridge_mirror_state"] == "catalog_only"


@pytest.mark.asyncio
async def test_sessions_api_exposes_only_sanitized_batched_sidebar_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_profile(db_path, "default")
    secrets = _seed_sidebar_catalog_jobs(db_path)
    sidebar_queries: list[str] = []
    original = SessionBridgeStore.get_bridge_summaries

    def recording_summaries(
        store: SessionBridgeStore, session_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        assert store.db._conn is not None

        def record(statement: str) -> None:
            if "from session_sidebar_jobs" in statement.casefold():
                sidebar_queries.append(statement)

        store.db._conn.set_trace_callback(record)
        try:
            return original(store, session_ids)
        finally:
            store.db._conn.set_trace_callback(None)

    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )
    monkeypatch.setattr(
        SessionBridgeStore,
        "get_bridge_summaries",
        recording_summaries,
    )

    response = await web_server.get_sessions(limit=20)

    assert len(sidebar_queries) == 1
    rows = {row["id"]: row for row in response["sessions"]}
    expected = {
        "sidebar-pending": ("pending", None, None, False),
        "sidebar-leased": ("pending", None, None, True),
        "sidebar-retry": ("retrying", None, "delivery_degraded", False),
        "sidebar-visible": ("visible", secrets["thread_id"], None, False),
        "sidebar-failed": ("failed", None, "delivery_degraded", False),
    }
    for session_id, values in expected.items():
        row = rows[session_id]
        assert (
            row["bridge_sidebar_state"],
            row["bridge_sidebar_codex_thread_id"],
            row["bridge_sidebar_error"],
            row["bridge_sidebar_stale"],
        ) == values
        assert {key for key in row if key.startswith("bridge_sidebar_")} == {
            "bridge_sidebar_state",
            "bridge_sidebar_codex_thread_id",
            "bridge_sidebar_error",
            "bridge_sidebar_stale",
        }

    serialized = json.dumps(response, sort_keys=True)
    for forbidden in (
        "lease_digest",
        "lease_token",
        "signed_marker",
        "registration_prompt",
        secrets["secret_error"],
        secrets["secret_lease_digest"],
        "C:/claude/claude-default.jsonl",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "expected_thread_id"),
    [
        (
            "44444444-4444-4444-8444-444444444444",
            "44444444-4444-4444-8444-444444444444",
        ),
        ("thread_01JABCDEF0123456789ABCDE", "thread_01JABCDEF0123456789ABCDE"),
        ("C:/private/native/sidebar.jsonl bearer-secret", None),
        (sqlite3.Binary(b"binary-thread-secret"), None),
        ("thread with spaces", None),
        ("x" * 513, None),
    ],
    ids=("uuid", "opaque", "path-secret", "blob", "spaces", "overlong"),
)
async def test_sessions_api_validates_visible_codex_thread_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_id: object,
    expected_thread_id: str | None,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_sidebar_catalog_jobs(db_path)
    _set_sidebar_catalog_field(
        db_path,
        "sidebar-visible",
        "codex_thread_id",
        thread_id,
    )
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )

    response = await web_server.get_sessions(limit=20)

    row = next(
        session
        for session in response["sessions"]
        if session["id"] == "sidebar-visible"
    )
    expected_state = "visible" if expected_thread_id is not None else "failed"
    expected_error = None if expected_thread_id is not None else "delivery_degraded"
    assert (
        row["bridge_sidebar_state"],
        row["bridge_sidebar_codex_thread_id"],
        row["bridge_sidebar_error"],
        row["bridge_sidebar_stale"],
    ) == (expected_state, expected_thread_id, expected_error, False)
    serialized = json.dumps(response, sort_keys=True)
    assert "C:/private/native/sidebar.jsonl bearer-secret" not in serialized
    assert "binary-thread-secret" not in serialized
    assert "thread with spaces" not in serialized
    assert "x" * 513 not in serialized


@pytest.mark.asyncio
async def test_sessions_api_exposes_thread_identity_only_for_visible_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_sidebar_catalog_jobs(db_path)
    secret = "thread_01JSHOULDNOTBEPUBLIC"
    _set_sidebar_catalog_field(
        db_path,
        "sidebar-pending",
        "codex_thread_id",
        secret,
    )
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )

    response = await web_server.get_sessions(limit=20)

    row = next(
        session
        for session in response["sessions"]
        if session["id"] == "sidebar-pending"
    )
    assert row["bridge_sidebar_state"] == "pending"
    assert row["bridge_sidebar_codex_thread_id"] is None
    assert secret not in json.dumps(response, sort_keys=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_expires_at", "ignore_checks"),
    [
        ("C:/private/lease bearer-secret", False),
        (None, True),
        (math.nan, True),
        (math.inf, False),
        (-math.inf, False),
    ],
    ids=("text-secret", "null", "nan", "positive-inf", "negative-inf"),
)
async def test_sessions_api_fails_closed_for_invalid_leased_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lease_expires_at: object,
    ignore_checks: bool,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_sidebar_catalog_jobs(db_path)
    _set_sidebar_catalog_field(
        db_path,
        "sidebar-leased",
        "lease_expires_at",
        lease_expires_at,
        ignore_checks=ignore_checks,
    )
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )

    response = await web_server.get_sessions(limit=20)

    row = next(
        session for session in response["sessions"] if session["id"] == "sidebar-leased"
    )
    assert (
        row["bridge_sidebar_state"],
        row["bridge_sidebar_codex_thread_id"],
        row["bridge_sidebar_error"],
        row["bridge_sidebar_stale"],
    ) == ("failed", None, "delivery_degraded", True)
    assert "C:/private/lease bearer-secret" not in json.dumps(
        response,
        sort_keys=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "persisted_error", "expected_stale"),
    [
        ("sidebar-visible", "C:/private/visible bearer-secret", False),
        ("sidebar-visible", "", False),
        ("sidebar-visible", sqlite3.Binary(b"visible-binary-secret"), False),
        ("sidebar-leased", "C:/private/leased bearer-secret", True),
        ("sidebar-leased", "", True),
        ("sidebar-leased", sqlite3.Binary(b"leased-binary-secret"), True),
    ],
    ids=(
        "visible-text",
        "visible-empty",
        "visible-blob",
        "leased-text",
        "leased-empty",
        "leased-blob",
    ),
)
async def test_sessions_api_fails_closed_for_errors_on_active_sidebar_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    persisted_error: object,
    expected_stale: bool,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_sidebar_catalog_jobs(db_path)
    _set_sidebar_catalog_field(
        db_path,
        session_id,
        "error_code",
        persisted_error,
    )
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )

    response = await web_server.get_sessions(limit=20)

    row = next(
        session for session in response["sessions"] if session["id"] == session_id
    )
    assert (
        row["bridge_sidebar_state"],
        row["bridge_sidebar_codex_thread_id"],
        row["bridge_sidebar_error"],
        row["bridge_sidebar_stale"],
    ) == ("failed", None, "delivery_degraded", expected_stale)
    serialized = json.dumps(response, sort_keys=True)
    for forbidden in (
        "C:/private/visible bearer-secret",
        "visible-binary-secret",
        "C:/private/leased bearer-secret",
        "leased-binary-secret",
    ):
        assert forbidden not in serialized


def test_profiles_sessions_api_preserves_rows_and_batches_once_per_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import profiles as profiles_mod

    profiles: list[SimpleNamespace] = []
    expected_by_profile: dict[str, tuple[str, ...]] = {}
    baseline_by_profile: dict[str, dict[str, dict[str, object]]] = {}
    for name in ("default", "work"):
        home = tmp_path / name
        home.mkdir()
        db_path = home / "state.db"
        expected_by_profile[name] = _seed_profile(db_path, name)
        baseline_by_profile[name] = _baseline_rows(db_path)
        profiles.append(SimpleNamespace(name=name, path=home))

    calls: list[tuple[str, ...]] = []
    original = SessionBridgeStore.get_bridge_summaries

    def recording_summaries(
        store: SessionBridgeStore, session_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        calls.append(tuple(session_ids))
        return original(store, session_ids)

    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: profiles)
    monkeypatch.setattr(
        SessionBridgeStore,
        "get_bridge_summaries",
        recording_summaries,
    )

    response = web_server.get_profiles_sessions(limit=20, profile="all")

    assert len(calls) == 2
    assert {frozenset(call) for call in calls} == {
        frozenset(expected_by_profile["default"]),
        frozenset(expected_by_profile["work"]),
    }
    response_json = json.dumps(response)
    assert '"bridge_links"' not in response_json
    assert '"source_cursor"' not in response_json
    assert '"source_hash"' not in response_json
    rows = {(row["profile"], row["id"]): row for row in response["sessions"]}
    assert len(rows) == 6
    for profile_name, baseline in baseline_by_profile.items():
        for session_id, expected in baseline.items():
            actual = rows[(profile_name, session_id)]
            for field, value in expected.items():
                expected_value = bool(value) if field == "archived" else value
                assert actual[field] == expected_value
            assert actual["is_default_profile"] is (profile_name == "default")
            assert isinstance(actual["is_active"], bool)
        hermes_id, claude_id, codex_id = expected_by_profile[profile_name]
        assert rows[(profile_name, hermes_id)]["bridge_provider"] == "hermes"
        assert rows[(profile_name, hermes_id)]["bridge_mirror_state"] is None
        assert rows[(profile_name, claude_id)]["bridge_provider"] == "claude"
        assert rows[(profile_name, codex_id)]["bridge_provider"] == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ("sessions", "profiles"))
async def test_bridge_metadata_failure_is_sanitized_and_preserves_original_rows(
    endpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from hermes_cli import profiles as profiles_mod

    secret = "C:/private/native/transcript.jsonl bearer-secret"
    home = tmp_path / "default"
    home.mkdir()
    db_path = home / "state.db"
    expected_ids = _seed_profile(db_path, "default")
    baseline = _baseline_rows(db_path)
    monkeypatch.setattr(
        web_server,
        "_open_session_db_for_profile",
        lambda _profile: SessionDB(db_path=db_path),
    )
    monkeypatch.setattr(
        profiles_mod,
        "list_profiles",
        lambda: [SimpleNamespace(name="default", path=home)],
    )

    def fail_summaries(
        _store: SessionBridgeStore, _session_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        raise RuntimeError(secret)

    monkeypatch.setattr(
        SessionBridgeStore,
        "get_bridge_summaries",
        fail_summaries,
    )

    with caplog.at_level(logging.WARNING, logger=web_server.__name__):
        if endpoint == "sessions":
            response = await web_server.get_sessions(limit=20)
        else:
            response = web_server.get_profiles_sessions(limit=20, profile="all")

    rows = {row["id"]: row for row in response["sessions"]}
    assert set(rows) == set(expected_ids)
    for session_id, expected in baseline.items():
        for field, value in expected.items():
            expected_value = bool(value) if field == "archived" else value
            assert rows[session_id][field] == expected_value
        assert not any(key.startswith("bridge_") for key in rows[session_id])
    assert "bridge metadata unavailable" in caplog.text.casefold()
    assert secret not in caplog.text
