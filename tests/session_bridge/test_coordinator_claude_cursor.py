"""Incremental-cursor behaviour of the Claude scan paths.

The adapter has carried a working incremental cursor since it was written --
``_read_for_parse`` seeks to a stored offset, re-hashes the head sample and
falls back to a full read whenever the transcript was rewritten. The
coordinator never persisted that cursor, so every scan cycle called
``parse(path)`` with ``previous=None`` and re-read every changed transcript in
full. These tests pin the coordinator side of that contract.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
import session_bridge.claude_adapter as claude_adapter_module
import session_bridge.coordinator as coordinator_module
from session_bridge.claude_adapter import (
    ClaudeCursor,
    ClaudeParseResult,
    ClaudeSourceAdapter,
)
from session_bridge.config import BridgeConfig, MirrorsConfig
from session_bridge.coordinator import SessionBridgeCoordinator
from session_bridge.models import Provider, UpsertResult, canonical_session_id
from session_bridge.store import SessionBridgeStore


_MARKER_SECRET = b"synthetic-claude-cursor-secret"
_CLAUDE_CURSOR_KEY = "session-bridge:scan:claude:cursors"


def _record(
    native_id: str,
    *,
    event_id: str,
    content: str,
    timestamp: str,
    role: str = "user",
) -> bytes:
    return (
        json.dumps(
            {
                "type": role,
                "sessionId": native_id,
                "uuid": event_id,
                "timestamp": timestamp,
                "cwd": "C:/synthetic/claude",
                "gitBranch": "main",
                "isSidechain": False,
                "message": {"role": role, "content": content},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _padded_record(native_id: str, index: int, *, padding: int = 0) -> bytes:
    return _record(
        native_id,
        event_id=f"event-{native_id}-{index:04d}",
        content=f"message {index:04d} " + ("x" * padding),
        timestamp=f"2026-07-13T10:{index // 60:02d}:{index % 60:02d}Z",
    )


class _CountingReads:
    """Record how many transcript bytes each ``parse`` actually pulled in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.slices: list[int] = []
        original = claude_adapter_module._read_for_parse

        def _wrapped(path: Path, previous: object, **kwargs: Any) -> Any:
            result = original(path, previous, **kwargs)
            self.slices.append(len(result.data))
            return result

        monkeypatch.setattr(claude_adapter_module, "_read_for_parse", _wrapped)


class _RecordingAdapter(ClaudeSourceAdapter):
    def __init__(self, projects_root: Path) -> None:
        super().__init__(projects_root, marker_secret=_MARKER_SECRET)
        self.previous_cursors: list[ClaudeCursor | None] = []

    def parse(
        self, path: Path, previous: ClaudeCursor | None = None
    ) -> ClaudeParseResult:
        self.previous_cursors.append(previous)
        return super().parse(path, previous)


def _coordinator(
    store: Any,
    adapter: Any,
    *,
    config: BridgeConfig | None = None,
) -> SessionBridgeCoordinator:
    return SessionBridgeCoordinator(
        config=config or BridgeConfig(),
        store=store,
        adapters={Provider.CLAUDE: adapter},
    )


@pytest.mark.asyncio
async def test_claude_scan_reads_only_the_appended_bytes_on_the_second_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_id = "cursor-append"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    head = b"".join(_padded_record(native_id, index) for index in range(4))
    transcript.write_bytes(head)
    reads = _CountingReads(monkeypatch)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        adapter = _RecordingAdapter(root)
        coordinator = _coordinator(store, adapter)

        first = await coordinator.scan_once(Provider.CLAUDE)
        tail = _padded_record(native_id, 4)
        with transcript.open("ab") as stream:
            stream.write(tail)
        second = await coordinator.scan_once(Provider.CLAUDE)

        assert (first.indexed, second.indexed) == (1, 1)
        assert adapter.previous_cursors[0] is None
        assert adapter.previous_cursors[1] == ClaudeCursor(
            offset=len(head),
            head_length=len(head),
            head_hash=claude_adapter_module._sha256(head),
        )
        assert reads.slices == [len(head), len(tail)]
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(db.get_messages(session_id)) == 5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_scan_persists_the_cursor_across_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_id = "cursor-restart"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    head = b"".join(_padded_record(native_id, index) for index in range(3))
    transcript.write_bytes(head)
    db_path = tmp_path / "state.db"
    reads = _CountingReads(monkeypatch)

    db = SessionDB(db_path=db_path)
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        await _coordinator(store, _RecordingAdapter(root)).scan_once(Provider.CLAUDE)
        persisted = store.get_state(_CLAUDE_CURSOR_KEY)
    finally:
        db.close()

    assert persisted is not None
    assert persisted["version"] == 1
    assert persisted["sessions"][native_id] == {
        "head_hash": claude_adapter_module._sha256(head),
        "head_length": len(head),
        "offset": len(head),
    }

    tail = _padded_record(native_id, 3)
    with transcript.open("ab") as stream:
        stream.write(tail)

    restarted_db = SessionDB(db_path=db_path)
    try:
        restarted_store = SessionBridgeStore(restarted_db, clock=lambda: 1_001.0)
        adapter = _RecordingAdapter(root)
        summary = await _coordinator(restarted_store, adapter).scan_once(
            Provider.CLAUDE
        )

        assert summary.indexed == 1
        assert adapter.previous_cursors == [
            ClaudeCursor(
                offset=len(head),
                head_length=len(head),
                head_hash=claude_adapter_module._sha256(head),
            )
        ]
        assert reads.slices == [len(head), len(tail)]
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(restarted_db.get_messages(session_id)) == 4
    finally:
        restarted_db.close()


@pytest.mark.asyncio
async def test_claude_scan_keeps_every_message_when_a_rewrite_shrinks_past_the_head(
    tmp_path: Path,
) -> None:
    """A rewritten transcript must rebuild from a FULL read, never from a delta.

    ``upsert_projection``'s rebuild branch DELETEs every message mapped to the
    session and re-INSERTs only what the projection carries. A cursor-driven
    delta projection carries just the appended tail, so pairing rebuild with a
    delta would wipe the session down to that tail.
    """

    native_id = "cursor-shrink"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    # Larger than the 64 KiB head sample so the head hash stays stable across
    # the rewrite -- otherwise the adapter's own head check would mask the bug.
    complete = b"".join(
        _padded_record(native_id, index, padding=2_048) for index in range(40)
    )
    assert len(complete) > claude_adapter_module._HEAD_SAMPLE_BYTES
    partial = b'{"type":"user","sessionId":"' + native_id.encode() + b'"'
    transcript.write_bytes(complete + partial)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        adapter = _RecordingAdapter(root)
        coordinator = _coordinator(store, adapter)

        first = await coordinator.scan_once(Provider.CLAUDE)
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert first.indexed == 1
        assert len(db.get_messages(session_id)) == 40

        # The dangling fragment is dropped: the file SHRINKS, yet every byte
        # the cursor already consumed is still there and still newline-ended.
        transcript.write_bytes(complete)
        second = await coordinator.scan_once(Provider.CLAUDE)

        assert second.indexed == 1
        assert second.rebuilt == 1
        assert len(db.get_messages(session_id)) == 40
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_scan_rebuilds_a_rewritten_transcript_without_stale_messages(
    tmp_path: Path,
) -> None:
    native_id = "cursor-rewrite"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(
        b"".join(_padded_record(native_id, index) for index in range(4))
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        coordinator = _coordinator(store, _RecordingAdapter(root))

        await coordinator.scan_once(Provider.CLAUDE)
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(db.get_messages(session_id)) == 4

        transcript.write_bytes(
            b"".join(
                _record(
                    native_id,
                    event_id=f"compacted-{index}",
                    content=f"compacted {index}",
                    timestamp=f"2026-07-13T11:00:0{index}Z",
                )
                for index in range(2)
            )
        )
        summary = await coordinator.scan_once(Provider.CLAUDE)

        assert (summary.indexed, summary.rebuilt) == (1, 1)
        contents = [row["content"] for row in db.get_messages(session_id)]
        assert contents == ["compacted 0", "compacted 1"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_scan_keeps_the_old_cursor_when_the_upsert_fails(
    tmp_path: Path,
) -> None:
    native_id = "cursor-failure"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    head = b"".join(_padded_record(native_id, index) for index in range(2))
    transcript.write_bytes(head)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        adapter = _RecordingAdapter(root)
        coordinator = _coordinator(store, adapter)
        await coordinator.scan_once(Provider.CLAUDE)
        committed = store.get_state(_CLAUDE_CURSOR_KEY)["sessions"][native_id]

        failures = {"remaining": 1}
        original_upsert = store.upsert_projection

        def _flaky(projection: Any, *, rebuild: bool = False) -> Any:
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise RuntimeError("synthetic upsert failure")
            return original_upsert(projection, rebuild=rebuild)

        store.upsert_projection = _flaky  # type: ignore[method-assign]
        with transcript.open("ab") as stream:
            stream.write(_padded_record(native_id, 2))
        failed = await coordinator.scan_once(Provider.CLAUDE)

        assert failed.failed == 1
        assert store.get_state(_CLAUDE_CURSOR_KEY)["sessions"][native_id] == committed

        store.upsert_projection = original_upsert  # type: ignore[method-assign]
        retried = await coordinator.scan_once(Provider.CLAUDE)

        assert retried.indexed == 1
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(db.get_messages(session_id)) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_scan_reads_in_full_when_automatic_mirror_creation_is_on(
    tmp_path: Path,
) -> None:
    """Mirror eligibility is decided from the projection's own message list.

    ``classify_mirror_eligibility`` needs the session's FIRST meaningful user
    message to clear the debounce window; a delta projection cannot carry it.
    So the incremental cursor stands down while automatic mirroring is on.
    """

    native_id = "cursor-mirroring"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(_padded_record(native_id, 0))
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        adapter = _RecordingAdapter(root)
        config = replace(
            BridgeConfig(),
            mirrors=MirrorsConfig(automatic_creation=True),
        )
        coordinator = _coordinator(store, adapter, config=config)

        await coordinator.scan_once(Provider.CLAUDE)
        with transcript.open("ab") as stream:
            stream.write(_padded_record(native_id, 1))
        await coordinator.scan_once(Provider.CLAUDE)

        assert adapter.previous_cursors == [None, None]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_scan_ignores_a_persisted_cursor_the_adapter_cannot_accept(
    tmp_path: Path,
) -> None:
    """Legacy adapters expose ``parse(path)``; the scan must not break on them."""

    native_id = "cursor-legacy"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(_padded_record(native_id, 0))

    class _LegacyAdapter(ClaudeSourceAdapter):
        def __init__(self) -> None:
            super().__init__(root, marker_secret=_MARKER_SECRET)
            self.parse_calls = 0

        def parse(self, path: Path) -> ClaudeParseResult:  # type: ignore[override]
            self.parse_calls += 1
            return ClaudeSourceAdapter.parse(self, path)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        adapter = _LegacyAdapter()
        coordinator = _coordinator(store, adapter)

        await coordinator.scan_once(Provider.CLAUDE)
        with transcript.open("ab") as stream:
            stream.write(_padded_record(native_id, 1))
        summary = await coordinator.scan_once(Provider.CLAUDE)

        assert (summary.indexed, summary.failed) == (1, 0)
        assert adapter.parse_calls == 2
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(db.get_messages(session_id)) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_claude_immediate_scan_reuses_the_in_process_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stores without scan state still get the in-process cursor."""

    native_id = "cursor-immediate"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    head = b"".join(_padded_record(native_id, index) for index in range(3))
    transcript.write_bytes(head)
    reads = _CountingReads(monkeypatch)

    class _StatelessStore:
        def __init__(self) -> None:
            self.upserts: list[tuple[str, bool]] = []

        def upsert_projection(self, projection: Any, *, rebuild: bool = False) -> Any:
            self.upserts.append((projection.native_id, rebuild))
            return UpsertResult(
                session_id=canonical_session_id(
                    Provider.CLAUDE, projection.native_id
                ),
                inserted_messages=len(projection.messages),
                rebuilt=rebuild,
                first_seen=len(self.upserts) == 1,
            )

    store = _StatelessStore()
    adapter = _RecordingAdapter(root)
    coordinator = _coordinator(store, adapter)

    await coordinator.scan_once(Provider.CLAUDE)
    tail = _padded_record(native_id, 3)
    with transcript.open("ab") as stream:
        stream.write(tail)
    await coordinator.scan_once(Provider.CLAUDE)

    assert adapter.previous_cursors[0] is None
    assert adapter.previous_cursors[1] is not None
    assert reads.slices == [len(head), len(tail)]


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param({"head_hash": "nope", "offset": -1}, id="missing-head-length"),
        pytest.param(
            {"head_hash": "nope", "head_length": 8, "offset": 4},
            id="head-hash-is-not-a-digest",
        ),
        pytest.param(
            {"head_hash": "a" * 64, "head_length": 8, "offset": -4},
            id="negative-offset",
        ),
    ],
)
@pytest.mark.asyncio
async def test_claude_scan_re_reads_in_full_when_a_persisted_cursor_is_corrupt(
    tmp_path: Path,
    corrupt: dict[str, object],
) -> None:
    """One unreadable entry costs a full read, not a wedged scan.

    The entry must never reach the adapter either: ``_read_for_parse`` would
    also reject it, but only after the coordinator had already treated a
    meaningless offset as a resumable position.
    """

    native_id = "cursor-corrupt"
    root = tmp_path / "projects"
    transcript = root / "project" / f"{native_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(
        b"".join(_padded_record(native_id, index) for index in range(2))
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        store.set_state(
            _CLAUDE_CURSOR_KEY,
            {
                "version": 1,
                "sessions": {native_id: corrupt},
            },
        )
        adapter = _RecordingAdapter(root)
        summary = await _coordinator(store, adapter).scan_once(Provider.CLAUDE)

        assert (summary.indexed, summary.failed) == (1, 0)
        assert adapter.previous_cursors == [None]
        session_id = canonical_session_id(Provider.CLAUDE, native_id)
        assert len(db.get_messages(session_id)) == 2
        assert store.get_state(_CLAUDE_CURSOR_KEY)["sessions"][native_id][
            "offset"
        ] == transcript.stat().st_size
    finally:
        db.close()


@pytest.mark.parametrize(
    "state",
    [
        {"version": 2, "sessions": {}},
        {"version": 1, "sessions": []},
        {"version": 1, "sessions": {"": {}}},
        ["not", "a", "mapping"],
    ],
)
def test_decode_claude_cursors_rejects_a_malformed_envelope(state: object) -> None:
    with pytest.raises(RuntimeError, match="invalid Claude cursor state"):
        coordinator_module._decode_claude_cursors(state)


def test_decode_claude_cursors_round_trips_an_encoded_cursor() -> None:
    cursor = ClaudeCursor(offset=12, head_length=8, head_hash="b" * 64)
    encoded = claude_adapter_module.encode_claude_cursor(cursor)

    assert encoded == {"head_hash": "b" * 64, "head_length": 8, "offset": 12}
    assert coordinator_module._decode_claude_cursors(
        {"version": 1, "sessions": {"session": encoded}}
    ) == {"session": cursor}


@pytest.mark.parametrize(
    "cursor",
    [
        ClaudeCursor(offset=-1, head_length=0, head_hash="c" * 64),
        ClaudeCursor(offset=0, head_length=-1, head_hash="c" * 64),
        ClaudeCursor(offset=0, head_length=0, head_hash="not-a-sha"),
        ClaudeCursor(offset=True, head_length=0, head_hash="c" * 64),
        "not a cursor",
        None,
    ],
)
def test_encode_claude_cursor_refuses_anything_a_read_would_reject(
    cursor: object,
) -> None:
    assert claude_adapter_module.encode_claude_cursor(cursor) is None
