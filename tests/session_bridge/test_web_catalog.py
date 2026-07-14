from __future__ import annotations

from collections.abc import Sequence
import json
import logging
from pathlib import Path
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
)
from session_bridge.store import SessionBridgeStore


def _projection(provider: Provider, native_id: str, *, started_at: float) -> SessionProjection:
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
        db._execute_write(lambda conn: conn.execute(
            "UPDATE external_sessions SET sync_error = ? WHERE session_id = ?",
            (detail, session_id),
        ))
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
