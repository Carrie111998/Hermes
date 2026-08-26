from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import BinaryIO

import pytest

from hermes_state import SessionDB
import session_bridge.claude_adapter as claude_adapter_module
from session_bridge.claude_adapter import ClaudeCursor, ClaudeSourceAdapter
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    canonical_session_id,
    encode_bridge_marker,
)
from session_bridge.store import SessionBridgeStore


FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SECRET = b"synthetic-marker-secret"
BASIC_SESSION_ID = "11111111-1111-4111-8111-111111111111"
TOOLS_SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _copy_fixture(root: Path, name: str, relative: str | None = None) -> Path:
    destination = root / (relative or name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / name, destination)
    return destination


def _json_line(record: dict, *, ending: bytes = b"\n") -> bytes:
    return (
        json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        + ending
    )


def _message_record(
    content: str,
    *,
    session_id: str = BASIC_SESSION_ID,
    event_id: str | None = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    timestamp: object = "2026-01-01T00:00:00Z",
    sidechain: bool = False,
) -> dict:
    record = {
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": "C:/synthetic/project",
        "gitBranch": "feature/synthetic",
        "isSidechain": sidechain,
        "message": {"role": "user", "content": content},
    }
    if event_id is not None:
        record["uuid"] = event_id
    return record


def _epoch(value: str) -> float:
    return (
        datetime
        .fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )


def _bridge_marker(bridge_id: str) -> str:
    return encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )


class _RecordingBinaryStream:
    def __init__(self, stream: BinaryIO, reads: list[tuple[int, int, int]]) -> None:
        self._stream = stream
        self._reads = reads

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._stream.__exit__(exc_type, exc_value, traceback)

    def read(self, size: int = -1) -> bytes:
        offset = self._stream.tell()
        value = self._stream.read(size)
        self._reads.append((offset, size, len(value)))
        return value

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()


def test_discover_recurses_and_is_deterministic(tmp_path):
    first = _copy_fixture(tmp_path, "basic.jsonl", "z/basic.jsonl")
    second = _copy_fixture(
        tmp_path, "tools-and-title.jsonl", "a/deep/tools-and-title.jsonl"
    )
    (tmp_path / "ignored.txt").write_text("not a transcript", encoding="utf-8")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)

    assert adapter.discover() == sorted([first, second], key=lambda path: str(path))


def test_fresh_exact_stem_lookup_observes_warm_absence_without_recursive_walk(
    tmp_path: Path, monkeypatch
) -> None:
    unrelated = tmp_path / "C--unrelated" / "unrelated.jsonl"
    unrelated.parent.mkdir()
    unrelated.write_text("{}\n", encoding="utf-8")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.discover() == [unrelated]

    expected = tmp_path / "C--target" / f"{BASIC_SESSION_ID}.jsonl"
    expected.parent.mkdir()
    expected.write_text("{}\n", encoding="utf-8")

    def forbidden_rglob(_self: Path, _pattern: str):
        raise AssertionError("fresh exact lookup recursively walked transcript inventory")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)

    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == [expected]
    assert adapter.discover() == sorted([unrelated, expected], key=lambda path: str(path))


def test_fresh_exact_stem_lookup_returns_every_new_duplicate_deterministically(
    tmp_path: Path,
) -> None:
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.discover() == []
    first = tmp_path / "C--first" / f"{BASIC_SESSION_ID}.jsonl"
    second = tmp_path / "D--second" / f"{BASIC_SESSION_ID}.jsonl"
    for path in (second, first):
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")

    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == [
        first,
        second,
    ]


def test_fresh_exact_stem_lookup_drops_removed_cached_match_only(
    tmp_path: Path,
) -> None:
    exact = tmp_path / "C--first" / f"{BASIC_SESSION_ID}.jsonl"
    unrelated = tmp_path / "D--second" / "unrelated.jsonl"
    for path in (exact, unrelated):
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.discover() == [exact, unrelated]
    exact.unlink()

    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == []
    assert adapter.discover() == [unrelated]


def test_fresh_exact_stem_lookup_treats_missing_root_as_absent(tmp_path: Path) -> None:
    adapter = ClaudeSourceAdapter(tmp_path / "absent", marker_secret=SECRET)

    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == []


def test_fresh_exact_stem_lookup_treats_file_root_as_absent(tmp_path: Path) -> None:
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("not a directory", encoding="utf-8")
    adapter = ClaudeSourceAdapter(root_file, marker_secret=SECRET)

    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == []


def test_fresh_exact_stem_lookup_does_not_extend_inventory_ttl(
    tmp_path: Path, monkeypatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(claude_adapter_module.time, "monotonic", lambda: now[0])
    initial = tmp_path / "C--first" / "initial.jsonl"
    initial.parent.mkdir()
    initial.write_text("{}\n", encoding="utf-8")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.discover() == [initial]

    now[0] = 59.0
    exact = tmp_path / "D--second" / f"{BASIC_SESSION_ID}.jsonl"
    exact.parent.mkdir()
    exact.write_text("{}\n", encoding="utf-8")
    assert adapter.find_native_sessions_by_stem_fresh(BASIC_SESSION_ID) == [exact]

    newcomer = tmp_path / "E--third" / "newcomer.jsonl"
    newcomer.parent.mkdir()
    newcomer.write_text("{}\n", encoding="utf-8")
    now[0] = 60.0
    assert adapter.discover() == [initial, exact, newcomer]


def test_parse_returns_canonical_metadata_and_native_activity(tmp_path):
    path = _copy_fixture(tmp_path, "basic.jsonl")

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    projection = result.projection
    assert projection.provider is Provider.CLAUDE
    assert projection.native_id == BASIC_SESSION_ID
    assert canonical_session_id(projection.provider, projection.native_id) == (
        f"claude:{BASIC_SESSION_ID}"
    )
    assert projection.title is None
    assert projection.cwd == "C:/synthetic/project"
    assert projection.git_branch == "feature/synthetic"
    assert projection.started_at == _epoch("2026-01-01T00:00:00Z")
    assert projection.last_active == 1767225602.5
    assert projection.native_path == str(path)
    assert projection.native_status == "active"
    assert projection.parser_version == 1
    assert projection.native_hash == result.cursor.head_hash
    assert projection.native_cursor is not None
    assert json.loads(projection.native_cursor) == {
        "head_hash": result.cursor.head_hash,
        "head_length": result.cursor.head_length,
        "offset": result.cursor.offset,
    }
    assert result.cursor.offset == path.stat().st_size
    assert result.cursor.head_length == min(path.stat().st_size, 65_536)
    assert result.rebuild is False
    assert result.malformed_lines == 0
    assert result.unknown_records == 0
    assert result.entrypoint is None


@pytest.mark.parametrize("entrypoint", ["cli", "sdk-cli"])
def test_parse_preserves_transcript_head_entrypoint(
    tmp_path: Path, entrypoint: str
) -> None:
    record = _message_record("entrypoint characterization")
    record["entrypoint"] = entrypoint
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(record))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.entrypoint == entrypoint


def test_parse_finds_entrypoint_after_leading_named_session_metadata(
    tmp_path: Path,
) -> None:
    first = {
        "type": "custom-title",
        "sessionId": BASIC_SESSION_ID,
        "customTitle": "Named registration",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    second = _message_record("interactive registration")
    second["entrypoint"] = "cli"
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(first) + _json_line(second))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.entrypoint == "cli"


def test_parse_finds_entrypoint_beyond_four_kibibytes_of_named_metadata(
    tmp_path: Path,
) -> None:
    first = {
        "type": "custom-title",
        "sessionId": BASIC_SESSION_ID,
        "customTitle": "x" * 5_500,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    second = _message_record("interactive registration")
    second["entrypoint"] = "cli"
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(first) + _json_line(second))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.entrypoint == "cli"


def test_cold_increment_probes_entrypoint_beyond_cursor_head(tmp_path: Path) -> None:
    first = {
        "type": "custom-title",
        "sessionId": BASIC_SESSION_ID,
        "customTitle": "x" * 5_500,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    second = _message_record("interactive registration")
    second["entrypoint"] = "cli"
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(first) + _json_line(second))
    cursor = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).cursor
    tail = _message_record(
        "later metadata-free event",
        event_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )
    with path.open("ab") as stream:
        stream.write(_json_line(tail))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, cursor)

    assert result.entrypoint == "cli"


def test_parse_accepts_claude_2_1_216_registration_metadata(tmp_path: Path) -> None:
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    records = [
        {
            "type": "agent-name",
            "sessionId": BASIC_SESSION_ID,
            "agentName": "session-bridge",
        },
        {
            "type": "permission-mode",
            "sessionId": BASIC_SESSION_ID,
            "permissionMode": "dontAsk",
        },
        {
            "type": "file-history-snapshot",
            "messageId": "message-1",
            "snapshot": {},
            "isSnapshotUpdate": False,
        },
        _message_record("Registered prompt"),
    ]
    path.write_bytes(b"".join(_json_line(record) for record in records))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.malformed_lines == 0
    assert result.unknown_records == 0
    assert [(message.role, message.content) for message in result.projection.messages] == [
        ("user", "Registered prompt")
    ]


def test_parse_preserves_first_entrypoint_when_desktop_mode_changes(
    tmp_path: Path,
) -> None:
    first = _message_record("first")
    first["entrypoint"] = "claude-desktop"
    second = _message_record(
        "second",
        event_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    second["entrypoint"] = "claude-desktop-3p"
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(first) + _json_line(second))

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.entrypoint == "claude-desktop"


def test_increment_preserves_launch_entrypoint_when_desktop_mode_changes(
    tmp_path: Path,
) -> None:
    first = _message_record("first")
    first["entrypoint"] = "claude-desktop"
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(first))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    initial = adapter.parse(path)
    second = _message_record(
        "second",
        event_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    second["entrypoint"] = "claude-desktop-3p"
    with path.open("ab") as stream:
        stream.write(_json_line(second))

    result = adapter.parse(path, initial.cursor)

    assert result.entrypoint == "claude-desktop"


def test_parse_prefers_record_session_id_and_extracts_custom_title(tmp_path):
    path = _copy_fixture(tmp_path, "tools-and-title.jsonl", "wrong-filename.jsonl")

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.native_id == TOOLS_SESSION_ID
    assert projection.title == "Synthetic tool session"
    assert projection.cwd == "C:/synthetic/tools"
    assert projection.git_branch == "feature/tools"
    assert projection.started_at == _epoch("2026-01-02T00:00:00Z")
    assert projection.last_active == _epoch("2026-01-02T00:00:01Z")


def test_subagent_transcript_uses_agent_filename_identity(tmp_path):
    parent_session_id = "ab77a170-ba02-4e93-844c-12a87e6e8fa1"
    agent_id = "ac9f05c39dc3cef8f"
    record = _message_record(
        "Synthetic delegated request",
        session_id=parent_session_id,
        timestamp="2026-07-12T22:47:48.886Z",
    )
    record["agentId"] = agent_id
    record["isSidechain"] = True
    path = tmp_path / f"agent-{agent_id}.jsonl"
    path.write_bytes(_json_line(record))

    parsed = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert parsed.projection.native_id == path.stem
    assert parsed.projection.messages == []
    database = SessionDB(tmp_path / "state.db")
    try:
        result = SessionBridgeStore(database).upsert_projection(parsed.projection)
        assert result.session_id == f"claude:{path.stem}"
    finally:
        database.close()


def test_full_parse_rejects_mixed_native_session_ids(tmp_path):
    path = tmp_path / "mixed-identities.jsonl"
    path.write_bytes(
        b"".join([
            _json_line(_message_record("first", session_id=BASIC_SESSION_ID)),
            _json_line(
                _message_record(
                    "second",
                    session_id=TOOLS_SESSION_ID,
                    event_id="30303030-3030-4303-8303-303030303030",
                )
            ),
        ])
    )

    with pytest.raises(ValueError, match="native identity"):
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)


def test_full_parse_recovers_mixed_ids_when_filename_is_an_exact_session_id(
    tmp_path,
):
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(
        b"".join([
            _json_line(_message_record("preserve this", session_id=BASIC_SESSION_ID)),
            _json_line(
                _message_record(
                    "belongs to the other transcript",
                    session_id=TOOLS_SESSION_ID,
                    event_id="30303030-3030-4303-8303-303030303030",
                )
            ),
        ])
    )

    parsed = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert parsed.projection.native_id == BASIC_SESSION_ID
    assert [
        message.content for message in parsed.projection.messages
    ] == ["preserve this"]


def test_warm_increment_rejects_changed_native_id_without_mutating_cache(tmp_path):
    path = tmp_path / "warm-identity.jsonl"
    original = _json_line(_message_record("first"))
    path.write_bytes(original)
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    bad_tail = _json_line(
        _message_record(
            "wrong identity",
            session_id=TOOLS_SESSION_ID,
            event_id="31313131-3131-4313-8313-313131313131",
        )
    )
    path.write_bytes(original + bad_tail)

    with pytest.raises(ValueError, match="native identity"):
        adapter.parse(path, first.cursor)

    good_tail = _json_line(
        _message_record(
            "right identity",
            event_id="32323232-3232-4323-8323-323232323232",
        )
    )
    path.write_bytes(original + good_tail)
    recovered = adapter.parse(path, first.cursor)
    assert recovered.projection.native_id == BASIC_SESSION_ID


def test_cold_increment_rejects_tail_with_different_native_id(tmp_path):
    path = tmp_path / "cold-mismatch.jsonl"
    path.write_bytes(_json_line(_message_record("first")))
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    with path.open("ab") as stream:
        stream.write(
            _json_line(
                _message_record(
                    "wrong identity",
                    session_id=TOOLS_SESSION_ID,
                    event_id="33333333-3333-4333-8333-333333333334",
                )
            )
        )

    with pytest.raises(ValueError, match="native identity"):
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)


def test_cold_increment_recovers_record_identity_when_stem_differs(tmp_path):
    path = tmp_path / "not-the-native-id.jsonl"
    path.write_bytes(_json_line(_message_record("first")))
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    tail_record = _message_record(
        "same identity",
        event_id="34343434-3434-4434-8434-343434343435",
    )
    tail_record.pop("sessionId")
    with path.open("ab") as stream:
        stream.write(_json_line(tail_record))

    cold = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)

    assert cold.projection.native_id == BASIC_SESSION_ID


def test_cold_identity_head_ignores_unknown_record_session_ids(tmp_path):
    path = tmp_path / "cold-unknown-record.jsonl"
    path.write_bytes(
        _json_line({
            "type": "synthetic-unknown",
            "sessionId": TOOLS_SESSION_ID,
        })
        + _json_line(_message_record("first"))
    )
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    tail_record = _message_record(
        "same identity",
        event_id="34343434-3434-4434-8434-343434343436",
    )
    tail_record.pop("sessionId")
    with path.open("ab") as stream:
        stream.write(_json_line(tail_record))

    cold = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)

    assert cold.projection.native_id == BASIC_SESSION_ID


def test_cold_identity_outside_head_falls_back_to_stem(tmp_path):
    path = tmp_path / "cold-prefix-identity.jsonl"
    initial = _message_record("first")
    initial = {
        "type": initial.pop("type"),
        "syntheticPadding": "x" * 70_000,
        **initial,
    }
    path.write_bytes(_json_line(initial))
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    tail_record = _message_record(
        "same identity",
        event_id="34343434-3434-4434-8434-343434343437",
    )
    tail_record.pop("sessionId")
    with path.open("ab") as stream:
        stream.write(_json_line(tail_record))

    cold = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)

    assert cold.projection.native_id == "cold-prefix-identity"


def test_cold_stem_fallback_does_not_reopen_or_reread_history(tmp_path, monkeypatch):
    path = tmp_path / "cold-stem-fallback.jsonl"
    initial = _message_record("first")
    initial.pop("sessionId")
    initial_bytes = _json_line(initial)
    path.write_bytes(initial_bytes)
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    tail_record = _message_record(
        "same identity",
        event_id="34343434-3434-4434-8434-343434343438",
    )
    tail_record.pop("sessionId")
    tail_bytes = _json_line(tail_record)
    with path.open("ab") as stream:
        stream.write(tail_bytes)

    reads: list[tuple[int, int, int]] = []
    opens: list[Path] = []
    original_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        if self == path and mode == "rb":
            opens.append(self)
            return _RecordingBinaryStream(stream, reads)
        return stream

    monkeypatch.setattr(Path, "open", recording_open)

    cold = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)

    assert cold.projection.native_id == "cold-stem-fallback"
    assert opens == [path]
    assert reads == [
        (0, len(initial_bytes), len(initial_bytes)),
        (len(initial_bytes) - 1, 1, 1),
        (len(initial_bytes), -1, len(tail_bytes)),
    ]


def test_cold_stem_baseline_rejects_a_different_delta_session_id(tmp_path):
    path = tmp_path / "cold-stem-baseline.jsonl"
    initial = _message_record("first")
    initial.pop("sessionId")
    path.write_bytes(_json_line(initial))
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    with path.open("ab") as stream:
        stream.write(
            _json_line(
                _message_record(
                    "different identity",
                    session_id=BASIC_SESSION_ID,
                    event_id="34343434-3434-4434-8434-343434343439",
                )
            )
        )

    with pytest.raises(ValueError, match="native identity"):
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)


def test_normalizes_user_assistant_text_and_tool_call_result(tmp_path):
    path = _copy_fixture(tmp_path, "tools-and-title.jsonl")

    messages = list(
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
        .parse(path)
        .projection.messages
    )

    assert [(message.role, message.content) for message in messages] == [
        ("assistant", "I will inspect the synthetic file."),
        ("assistant", None),
        ("tool", "Synthetic tool output"),
    ]
    assert [(message.native_event_id, message.ordinal) for message in messages] == [
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", 1),
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", 2),
        ("ffffffff-ffff-4fff-8fff-ffffffffffff", 0),
    ]
    assert messages[1].tool_name == "Read"
    assert messages[1].tool_calls == [
        {
            "id": "tool-1",
            "type": "function",
            "function": {
                "name": "Read",
                "arguments": '{"path":"synthetic.txt"}',
            },
        }
    ]
    assert messages[2].tool_call_id == "tool-1"


def test_exact_duplicate_tool_result_record_is_projected_once(tmp_path):
    event_id = "5bc142fe-c731-42b1-b34d-4fd7d0b57a75"
    record = {
        "type": "user",
        "sessionId": BASIC_SESSION_ID,
        "uuid": event_id,
        "timestamp": "2026-06-09T15:58:57.172Z",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-synthetic",
                    "content": "Synthetic tool output",
                    "is_error": False,
                }
            ],
        },
    }
    replayed = {**record, "slug": "synthetic-compaction-replay"}
    path = tmp_path / f"{BASIC_SESSION_ID}.jsonl"
    path.write_bytes(_json_line(record) + _json_line(replayed))

    parsed = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert [
        (message.native_event_id, message.ordinal, message.content)
        for message in parsed.projection.messages
    ] == [(event_id, 0, "Synthetic tool output")]
    database = SessionDB(tmp_path / "state.db")
    try:
        SessionBridgeStore(database).upsert_projection(parsed.projection)
    finally:
        database.close()


def test_mixed_media_tool_result_redacts_binary_payloads_through_store(tmp_path):
    secret_payload = "SYNTHETIC_BASE64_SECRET_" + "A" * 200
    record = {
        "type": "user",
        "sessionId": BASIC_SESSION_ID,
        "uuid": "35353535-3535-4535-8535-353535353535",
        "timestamp": "2026-01-02T00:00:01Z",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-media",
                    "content": [
                        {"type": "text", "text": "Visible synthetic result"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": secret_payload,
                            },
                        },
                        {
                            "type": "document",
                            "media_type": "application/pdf",
                            "data": secret_payload,
                        },
                        {
                            "type": "future_binary",
                            "media_type": "application/octet-stream",
                            "payload": secret_payload,
                        },
                    ],
                }
            ],
        },
    }
    path = tmp_path / "mixed-media.jsonl"
    path.write_bytes(_json_line(record))
    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )
    content = projection.messages[0].content

    assert content is not None
    assert "Visible synthetic result" in content
    assert "image" in content and "image/png" in content
    assert "document" in content and "application/pdf" in content
    assert "future_binary" in content and "application/octet-stream" in content
    assert "omitted" in content
    assert secret_payload not in content
    assert len(content) < 500

    database = SessionDB(tmp_path / "mixed-media.db")
    try:
        SessionBridgeStore(database, clock=lambda: 100.0).upsert_projection(projection)
        persisted = database.get_messages(f"claude:{BASIC_SESSION_ID}")[0]["content"]
        assert secret_payload not in persisted
        assert "Visible synthetic result" in persisted
    finally:
        database.close()


def test_hidden_thinking_is_excluded(tmp_path):
    path = _copy_fixture(tmp_path, "tools-and-title.jsonl")

    messages = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
        .parse(path)
        .projection.messages
    )

    assert all(message.reasoning is None for message in messages)
    assert all(
        "hidden synthetic thought" not in (message.content or "").lower()
        for message in messages
    )


def test_sidechain_records_are_excluded_from_main_projection(tmp_path):
    path = _copy_fixture(tmp_path, "malformed-and-unknown.jsonl")

    messages = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
        .parse(path)
        .projection.messages
    )

    assert [(message.role, message.content) for message in messages] == [
        ("user", "Visible main-thread text")
    ]


def test_sidechain_and_unknown_timestamps_do_not_drive_main_activity(tmp_path):
    path = _copy_fixture(tmp_path, "malformed-and-unknown.jsonl")

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    expected = _epoch("2026-01-03T00:00:00Z")
    assert projection.started_at == expected
    assert projection.last_active == expected


def test_malformed_unknown_and_recognized_metadata_counters(tmp_path):
    path = _copy_fixture(tmp_path, "malformed-and-unknown.jsonl")

    result = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)

    assert result.malformed_lines == 1
    assert result.unknown_records == 1
    assert result.projection.title == "Synthetic malformed session"

    basic = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(
        _copy_fixture(tmp_path, "basic.jsonl", "recognized/basic.jsonl")
    )
    assert basic.malformed_lines == 0
    assert basic.unknown_records == 0


def test_incremental_byte_cursor_returns_only_appended_messages(tmp_path):
    path = _copy_fixture(tmp_path, "basic.jsonl")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    appended = _message_record(
        "Appended synthetic prompt",
        event_id="56565656-5656-4656-8656-565656565656",
        timestamp="2026-01-01T00:00:03Z",
    )
    with path.open("ab") as stream:
        stream.write(_json_line(appended))

    second = adapter.parse(path, first.cursor)
    unchanged = adapter.parse(path, second.cursor)

    assert second.rebuild is False
    assert [
        (message.content, message.native_event_id)
        for message in second.projection.messages
    ] == [("Appended synthetic prompt", "56565656-5656-4656-8656-565656565656")]
    assert second.projection.started_at == _epoch("2026-01-01T00:00:00Z")
    assert second.projection.last_active == _epoch("2026-01-01T00:00:03Z")
    assert second.cursor.offset == path.stat().st_size
    assert second.cursor.head_length == first.cursor.head_length
    assert second.cursor.head_hash == first.cursor.head_hash
    assert unchanged.projection.messages == []
    assert unchanged.cursor == second.cursor


def test_incremental_parse_physically_reads_only_head_validation_and_tail(
    tmp_path, monkeypatch
):
    path = tmp_path / "large-increment.jsonl"
    path.write_bytes(_json_line(_message_record("x" * 6000)))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    appended = _json_line(
        _message_record(
            "Physical tail only",
            event_id="23232323-2323-4232-8232-232323232323",
            timestamp="2026-01-01T00:00:05Z",
        )
    )
    with path.open("ab") as stream:
        stream.write(appended)
    reads: list[tuple[int, int, int]] = []
    original_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        if self == path and mode == "rb":
            return _RecordingBinaryStream(stream, reads)
        return stream

    monkeypatch.setattr(Path, "open", recording_open)

    second = adapter.parse(path, first.cursor)

    assert [message.content for message in second.projection.messages] == [
        "Physical tail only"
    ]
    assert (0, first.cursor.head_length, first.cursor.head_length) in reads
    assert any(offset == first.cursor.offset for offset, _, _ in reads)
    assert not any(offset == 0 and requested == -1 for offset, requested, _ in reads)
    assert sum(length for _, _, length in reads) <= (
        first.cursor.head_length + 1 + len(appended)
    )


def test_cold_increment_uses_head_identity_and_mtime_without_full_read(
    tmp_path, monkeypatch
):
    path = tmp_path / "cold-native-id.jsonl"
    initial_bytes = (
        json.dumps(
            _message_record("x" * 6000),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    assert b'"sessionId"' in initial_bytes[:4096]
    path.write_bytes(initial_bytes)
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    os.utime(path, (321.0, 321.0))
    reads: list[tuple[int, int, int]] = []
    original_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        if self == path and mode == "rb":
            return _RecordingBinaryStream(stream, reads)
        return stream

    monkeypatch.setattr(Path, "open", recording_open)

    cold = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path, first.cursor)

    assert cold.rebuild is False
    assert cold.projection.native_id == BASIC_SESSION_ID
    assert cold.projection.messages == []
    assert cold.projection.started_at == 321.0
    assert cold.projection.last_active == 321.0
    assert not any(offset == 0 and requested == -1 for offset, requested, _ in reads)


@pytest.mark.parametrize(
    "invalid_cursor",
    [
        ClaudeCursor(offset=-1, head_length=1, head_hash="0" * 64),
        ClaudeCursor(offset=0, head_length=-1, head_hash="0" * 64),
        ClaudeCursor(offset=0, head_length=10**9, head_hash="0" * 64),
        ClaudeCursor(offset=True, head_length=1, head_hash="0" * 64),
        ClaudeCursor(offset=0, head_length=False, head_hash="0" * 64),
        ClaudeCursor(offset=0, head_length=1, head_hash="A" * 64),
        ClaudeCursor(offset=0, head_length=1, head_hash="not-a-hash"),
    ],
    ids=[
        "negative-offset",
        "negative-head",
        "huge-head",
        "bool-offset",
        "bool-head",
        "uppercase-hash",
        "short-hash",
    ],
)
def test_invalid_cursor_shape_rebuilds_before_bounded_reads(
    tmp_path, monkeypatch, invalid_cursor
):
    path = _copy_fixture(tmp_path, "basic.jsonl")
    reads: list[tuple[int, int, int]] = []
    original_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        if self == path and mode == "rb":
            return _RecordingBinaryStream(stream, reads)
        return stream

    monkeypatch.setattr(Path, "open", recording_open)

    rebuilt = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(
        path, invalid_cursor
    )

    assert rebuilt.rebuild is True
    assert [message.content for message in rebuilt.projection.messages] == [
        "First synthetic prompt",
        "Synthetic response",
    ]
    assert reads == [(0, -1, path.stat().st_size)]


def test_partial_trailing_multibyte_line_waits_for_newline_completion(tmp_path):
    path = tmp_path / "partial.jsonl"
    first_line = _json_line(_message_record("Complete line"))
    second_line = _json_line(
        _message_record(
            "Olá from a partial line",
            event_id="78787878-7878-4878-8878-787878787878",
            timestamp="1767225604.5",
        ),
        ending=b"\r\n",
    )
    split_at = second_line.index("á".encode("utf-8")) + 1
    path.write_bytes(first_line + second_line[:split_at])
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)

    first = adapter.parse(path)
    with path.open("ab") as stream:
        stream.write(second_line[split_at:])
    second = adapter.parse(path, first.cursor)

    assert [message.content for message in first.projection.messages] == [
        "Complete line"
    ]
    assert first.cursor.offset == len(first_line)
    assert [message.content for message in second.projection.messages] == [
        "Olá from a partial line"
    ]
    assert second.projection.last_active == 1767225604.5
    assert second.cursor.offset == len(first_line) + len(second_line)


def test_metadata_only_incremental_activity_never_decreases(tmp_path):
    path = tmp_path / "metadata-only.jsonl"
    path.write_bytes(
        _json_line({
            "type": "custom-title",
            "sessionId": BASIC_SESSION_ID,
            "customTitle": "Synthetic metadata only",
        })
    )
    os.utime(path, (200.0, 200.0))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    with path.open("ab") as stream:
        stream.write(
            _json_line(
                _message_record(
                    "First timestamped record",
                    event_id="89898989-8989-4989-8989-898989898989",
                    timestamp=100.0,
                )
            )
        )

    second = adapter.parse(path, first.cursor)

    assert second.rebuild is False
    assert second.projection.last_active >= first.projection.last_active


def test_nonfinite_numeric_timestamps_are_ignored(tmp_path):
    path = tmp_path / "nonfinite.jsonl"
    records = [
        _message_record("Invalid time", timestamp="NaN"),
        _message_record(
            "Valid time",
            event_id="67676767-6767-4767-8767-676767676767",
            timestamp=50.0,
        ),
    ]
    path.write_bytes(b"".join(_json_line(record) for record in records))

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.started_at == 50.0
    assert projection.last_active == 50.0


def test_truncation_forces_whole_single_session_rebuild(tmp_path):
    path = _copy_fixture(tmp_path, "basic.jsonl")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    first_line = path.read_bytes().splitlines(keepends=True)[0]
    path.write_bytes(first_line)

    rebuilt = adapter.parse(path, first.cursor)

    assert rebuilt.rebuild is True
    assert [message.content for message in rebuilt.projection.messages] == [
        "First synthetic prompt"
    ]
    assert rebuilt.projection.native_id == BASIC_SESSION_ID
    assert rebuilt.cursor.offset == len(first_line)


def test_head_replacement_rebuilds_only_requested_session(tmp_path):
    first_path = _copy_fixture(tmp_path, "basic.jsonl", "a/session.jsonl")
    _copy_fixture(tmp_path, "tools-and-title.jsonl", "b/other-session.jsonl")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(first_path)
    replacement_content = "Replacement session only " + "x" * first_path.stat().st_size
    replacement = _json_line(
        _message_record(
            replacement_content,
            event_id="90909090-9090-4090-8090-909090909090",
            timestamp="2026-01-04T00:00:00Z",
        )
    )
    assert len(replacement) >= first.cursor.offset
    first_path.write_bytes(replacement)

    rebuilt = adapter.parse(first_path, first.cursor)

    assert rebuilt.rebuild is True
    assert rebuilt.projection.native_id == BASIC_SESSION_ID
    assert [message.content for message in rebuilt.projection.messages] == [
        replacement_content
    ]
    assert all(
        message.native_event_id != "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        for message in rebuilt.projection.messages
    )


def test_tail_replacement_that_removes_newline_holds_partial_record(tmp_path):
    path = tmp_path / "tail-replacement.jsonl"
    first_line = _json_line(_message_record("x" * 5000))
    second_line = _json_line(
        _message_record(
            "Tail record",
            event_id="45454545-4545-4545-8545-454545454545",
        )
    )
    path.write_bytes(first_line + second_line)
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    assert first.cursor.head_length == min(path.stat().st_size, 65_536)
    path.write_bytes((first_line + second_line)[:-1] + b" ")

    rebuilt = adapter.parse(path, first.cursor)

    assert rebuilt.rebuild is True
    assert rebuilt.cursor.offset == len(first_line)
    assert [message.content for message in rebuilt.projection.messages] == ["x" * 5000]


def test_fallback_event_id_uses_byte_offset_and_complete_raw_line_hash(tmp_path):
    path = tmp_path / "fallback.jsonl"
    raw_line = _json_line(_message_record("No UUID", event_id=None))
    path.write_bytes(raw_line)

    message = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
        .parse(path)
        .projection.messages[0]
    )

    assert message.native_event_id == f"offset:0:{hashlib.sha256(raw_line).hexdigest()}"
    assert message.ordinal == 0


def test_valid_claude_signed_marker_without_later_user_is_placeholder(tmp_path):
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-synthetic",
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )
    path = tmp_path / "marker.jsonl"
    path.write_bytes(_json_line(_message_record(marker)))

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert projection.origin_bridge_id == "bridge-synthetic"


def test_valid_claude_signed_marker_with_later_user_is_continuation(tmp_path):
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-continued",
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )
    records = [
        _message_record(marker),
        _message_record(
            "A later synthetic user turn",
            event_id="24242424-2424-4242-8242-242424242424",
            timestamp="2026-01-01T00:00:01Z",
        ),
    ]
    path = tmp_path / "continued-marker.jsonl"
    path.write_bytes(b"".join(_json_line(record) for record in records))

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.origin_kind is OriginKind.BRIDGE_CONTINUATION
    assert projection.origin_bridge_id == "bridge-continued"


def test_distinct_valid_bridge_markers_in_one_parse_are_rejected(tmp_path):
    records = [
        _message_record(_bridge_marker("bridge-first")),
        _message_record(
            _bridge_marker("bridge-second"),
            event_id="36363636-3636-4636-8636-363636363636",
        ),
    ]
    path = tmp_path / "conflicting-markers.jsonl"
    path.write_bytes(b"".join(_json_line(record) for record in records))

    with pytest.raises(ValueError, match="bridge marker"):
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)


def test_repeated_same_marker_records_do_not_count_as_human_turns(tmp_path):
    marker = _bridge_marker("bridge-repeated")
    records = [
        _message_record(marker),
        _message_record(
            marker,
            event_id="37373737-3737-4737-8737-373737373737",
            timestamp="2026-01-01T00:00:01Z",
        ),
    ]
    path = tmp_path / "repeated-marker.jsonl"
    path.write_bytes(b"".join(_json_line(record) for record in records))

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert projection.origin_bridge_id == "bridge-repeated"


def test_warm_increment_rejects_a_different_valid_bridge_marker(tmp_path):
    path = tmp_path / "warm-marker-conflict.jsonl"
    original = _json_line(_message_record(_bridge_marker("bridge-original")))
    path.write_bytes(original)
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    path.write_bytes(
        original
        + _json_line(
            _message_record(
                _bridge_marker("bridge-conflict"),
                event_id="38383838-3838-4838-8838-383838383838",
            )
        )
    )

    with pytest.raises(ValueError, match="bridge marker"):
        adapter.parse(path, first.cursor)


def test_meta_local_command_is_excluded_from_delta_activity_and_provenance(tmp_path):
    path = tmp_path / "meta-local-command.jsonl"
    path.write_bytes(_json_line(_message_record(_bridge_marker("bridge-original"))))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    meta_record = _message_record(
        f"/synthetic-local-command {_bridge_marker('bridge-meta-ignored')}",
        event_id="39393939-3939-4939-8939-393939393939",
        timestamp="2026-01-01T00:00:10Z",
    )
    meta_record["isMeta"] = True
    with path.open("ab") as stream:
        stream.write(_json_line(meta_record))

    second = adapter.parse(path, first.cursor)

    assert second.projection.messages == []
    assert second.projection.last_active == first.projection.last_active
    assert second.projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    assert second.projection.origin_bridge_id == "bridge-original"


def test_appended_user_turn_promotes_cached_placeholder_to_continuation(tmp_path):
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-promoted",
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )
    path = tmp_path / "promoted-marker.jsonl"
    path.write_bytes(_json_line(_message_record(marker)))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    first = adapter.parse(path)
    assert first.projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    with path.open("ab") as stream:
        stream.write(
            _json_line(
                _message_record(
                    "A later appended user turn",
                    event_id="25252525-2525-4252-8252-252525252525",
                    timestamp="2026-01-01T00:00:02Z",
                )
            )
        )

    second = adapter.parse(path, first.cursor)

    assert second.projection.origin_kind is OriginKind.BRIDGE_CONTINUATION
    assert second.projection.origin_bridge_id == "bridge-promoted"


def test_cold_restart_store_promotes_placeholder_from_new_human_tail(tmp_path):
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-cold-restart",
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )
    path = tmp_path / "cold-restart.jsonl"
    path.write_bytes(_json_line(_message_record(marker)))
    first = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path)
    database = SessionDB(tmp_path / "state.db")
    try:
        store = SessionBridgeStore(database, clock=lambda: 100.0)
        store.upsert_projection(first.projection)
        assert first.projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        with path.open("ab") as stream:
            stream.write(
                _json_line(
                    _message_record(
                        "A human turn after restart",
                        event_id="29292929-2929-4292-8292-292929292929",
                        timestamp="2026-01-01T00:00:03Z",
                    )
                )
            )

        second = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(
            path, first.cursor
        )
        assert second.projection.origin_kind is OriginKind.NATIVE
        assert [message.role for message in second.projection.messages] == ["user"]
        store.upsert_projection(second.projection)

        external = store.get_external_session(f"claude:{BASIC_SESSION_ID}")
        assert external is not None
        assert external["origin_kind"] == "bridge_continuation"
        assert external["origin_bridge_id"] == "bridge-cold-restart"
    finally:
        database.close()


def test_tool_result_only_user_record_does_not_promote_placeholder(tmp_path):
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-tool-result",
            source_session_id="codex:synthetic-source",
            target_provider=Provider.CLAUDE,
            policy_generation=4,
        ),
        SECRET,
    )
    records = [
        _message_record(marker),
        {
            "type": "assistant",
            "sessionId": BASIC_SESSION_ID,
            "uuid": "26262626-2626-4262-8262-262626262626",
            "timestamp": "2026-01-01T00:00:01Z",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-synthetic",
                        "name": "Read",
                        "input": {"path": "synthetic.txt"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": BASIC_SESSION_ID,
            "uuid": "27272727-2727-4272-8272-272727272727",
            "timestamp": "2026-01-01T00:00:02Z",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-synthetic",
                        "content": "Synthetic tool output",
                    }
                ],
            },
        },
    ]
    path = tmp_path / "tool-result-placeholder.jsonl"
    path.write_bytes(b"".join(_json_line(record) for record in records))
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)

    first = adapter.parse(path)

    assert first.projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
    with path.open("ab") as stream:
        stream.write(
            _json_line(
                _message_record(
                    "A genuine later human turn",
                    event_id="28282828-2828-4282-8282-282828282828",
                    timestamp="2026-01-01T00:00:03Z",
                )
            )
        )
    second = adapter.parse(path, first.cursor)
    assert second.projection.origin_kind is OriginKind.BRIDGE_CONTINUATION


def test_invalid_other_target_embedded_and_title_markers_do_not_set_origin(tmp_path):
    claude_marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-embedded",
            source_session_id="codex:source",
            target_provider=Provider.CLAUDE,
            policy_generation=1,
        ),
        SECRET,
    )
    codex_marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id="bridge-wrong-target",
            source_session_id="claude:source",
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        SECRET,
    )
    invalid_marker = claude_marker[:-1] + ("A" if claude_marker[-1] != "A" else "B")
    records = [
        {
            "type": "custom-title",
            "sessionId": BASIC_SESSION_ID,
            "customTitle": "HERMES_SESSION_BRIDGE_V1 title only",
        },
        _message_record(f"prefix{claude_marker}suffix"),
        _message_record(
            f"{invalid_marker} {codex_marker}",
            event_id="abababab-abab-4bab-8bab-abababababab",
        ),
    ]
    path = tmp_path / "non-markers.jsonl"
    path.write_bytes(b"".join(_json_line(record) for record in records))

    projection = (
        ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).parse(path).projection
    )

    assert projection.title == "HERMES_SESSION_BRIDGE_V1 title only"
    assert projection.origin_kind is OriginKind.NATIVE
    assert projection.origin_bridge_id is None


def test_find_native_session_uses_record_id_without_writes(tmp_path):
    expected = _copy_fixture(tmp_path, "basic.jsonl", "nested/not-the-id.jsonl")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)

    assert adapter.find_native_session(BASIC_SESSION_ID) == expected
    assert adapter.find_native_session("missing-synthetic-id") is None


def test_find_native_sessions_returns_every_exact_uuid_match_across_projects(
    tmp_path: Path,
) -> None:
    first = tmp_path / "C--first" / f"{BASIC_SESSION_ID}.jsonl"
    second = tmp_path / "D--second" / f"{BASIC_SESSION_ID}.jsonl"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("{}\n", encoding="utf-8")
    second.write_text("{}\n", encoding="utf-8")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.find_native_sessions(BASIC_SESSION_ID) == [first, second]


def test_find_native_sessions_by_stem_does_not_probe_unrelated_transcripts(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "C--first" / f"{BASIC_SESSION_ID}.jsonl"
    second = tmp_path / "D--second" / f"{BASIC_SESSION_ID}.jsonl"
    unrelated = tmp_path / "E--third" / "unrelated.jsonl"
    for path in (first, second, unrelated):
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")

    def forbidden_probe(path: Path) -> str | None:
        raise AssertionError(f"unexpected transcript probe: {path}")

    monkeypatch.setattr(claude_adapter_module, "_probe_native_id", forbidden_probe)

    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    assert adapter.find_native_sessions_by_stem(BASIC_SESSION_ID) == [first, second]


def test_find_native_session_skips_corrupt_probe_before_valid_target(tmp_path):
    corrupt = tmp_path / "a-corrupt-mixed-id.jsonl"
    corrupt.write_bytes(
        _json_line(_message_record("first", session_id=BASIC_SESSION_ID))
        + _json_line(
            _message_record(
                "second",
                session_id=TOOLS_SESSION_ID,
                event_id="52525252-5252-4252-8252-525252525252",
            )
        )
    )
    target_native_id = "53535353-5353-4353-8353-535353535353"
    target = tmp_path / "z-valid-mismatched-stem.jsonl"
    target.write_bytes(
        _json_line(_message_record("target", session_id=target_native_id))
    )

    found = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET).find_native_session(
        target_native_id
    )

    assert found == target


def test_find_native_session_uses_stem_fast_path_and_bounded_prefix_probe(
    tmp_path, monkeypatch
):
    probed_root = tmp_path / "probed"
    probed_root.mkdir()
    native_id = "51515151-5151-4151-8151-515151515151"
    probed = probed_root / "different-stem.jsonl"
    probe_record = _message_record("x" * 200_000, session_id=native_id)
    probed.write_bytes(
        json.dumps(probe_record, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    reads: list[tuple[int, int, int]] = []
    original_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        if mode == "rb":
            return _RecordingBinaryStream(stream, reads)
        return stream

    monkeypatch.setattr(Path, "open", recording_open)

    assert (
        ClaudeSourceAdapter(probed_root, marker_secret=SECRET).find_native_session(
            native_id
        )
        == probed
    )
    assert reads
    assert all(requested != -1 and requested <= 65_536 for _, requested, _ in reads)

    stem_root = tmp_path / "stem"
    stem_root.mkdir()
    stem_match = stem_root / "native-by-stem.jsonl"
    stem_match.write_bytes(b"not opened\n")
    reads.clear()
    assert (
        ClaudeSourceAdapter(stem_root, marker_secret=SECRET).find_native_session(
            "native-by-stem"
        )
        == stem_match
    )
    assert reads == []


def test_adapter_uses_only_binary_read_streams_and_never_path_writes(
    tmp_path, monkeypatch
):
    path = _copy_fixture(tmp_path, "basic.jsonl", "nested/basic.jsonl")
    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=SECRET)
    original_open = Path.open
    modes: list[str] = []

    def forbidden_write(*args, **kwargs):
        raise AssertionError("Claude adapter attempted a Path write API")

    def guarded_open(self, mode="r", *args, **kwargs):
        modes.append(mode)
        if mode != "rb":
            raise AssertionError(f"Claude adapter opened a transcript with {mode!r}")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    monkeypatch.setattr(Path, "open", guarded_open)

    assert adapter.discover() == [path]
    assert adapter.parse(path).projection.native_id == BASIC_SESSION_ID
    assert adapter.find_native_session(BASIC_SESSION_ID) == path
    assert modes and set(modes) == {"rb"}


def test_cursor_records_are_frozen():
    cursor = ClaudeCursor(offset=1, head_length=1, head_hash="hash")

    with pytest.raises(AttributeError):
        setattr(cursor, "offset", 2)


def test_parse_orders_messages_by_time_not_file_position(tmp_path):
    """A reply flushed to the file before its prompt must still project second.

    Claude Code does not guarantee that the user record is written before the
    assistant records it caused.  Measured 2026-08-25 on a live registration:
    the prompt carried the EARLIER timestamp but was appended LAST, so the
    projection put the assistant first and _validate_projection rejected an
    otherwise perfect registration as a fatal bridge_conflict.
    """

    project = tmp_path / "C--Users-diego--hermes"
    project.mkdir()
    transcript = project / "d8ae024c-57a0-5e7c-9b72-55991ecdd908.jsonl"
    session = "d8ae024c-57a0-5e7c-9b72-55991ecdd908"
    records = [
        {
            "type": "assistant",
            "uuid": "4f022a89-0000-4000-8000-000000000001",
            "parentUuid": "32261ccb-0000-4000-8000-000000000000",
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:44.434Z",
            "cwd": "C:\\Users\\diego\\.hermes",
            "message": {
                "id": "msg_shared",
                "role": "assistant",
                "content": [{"type": "text", "text": "REGISTERED"}],
            },
        },
        {
            "type": "system",
            "entrypoint": "cli",
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:44.500Z",
            "cwd": "C:\\Users\\diego\\.hermes",
        },
        {
            "type": "user",
            "uuid": "32261ccb-0000-4000-8000-000000000000",
            "parentUuid": None,
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:41.377Z",
            "cwd": "C:\\Users\\diego\\.hermes",
            "message": {"role": "user", "content": "the registration prompt"},
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=b"secret")
    messages = adapter.parse(transcript).projection.messages

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "the registration prompt"
    assert messages[1].content == "REGISTERED"


def test_parse_keeps_file_order_when_a_timestamp_is_missing(tmp_path):
    """Never reorder on incomplete evidence.

    An unparseable timestamp projects as 0.0, so sorting on it would hoist that
    record ahead of everything and place a stray message before the prompt --
    the same fatal shape this ordering fix exists to prevent.
    """

    project = tmp_path / "C--Users-diego--hermes"
    project.mkdir()
    transcript = project / "aaaaaaaa-0000-4000-8000-00000000000a.jsonl"
    session = "aaaaaaaa-0000-4000-8000-00000000000a"
    records = [
        {
            "type": "user",
            "uuid": "11111111-0000-4000-8000-000000000001",
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:41.377Z",
            "cwd": "C:\\Users\\diego\\.hermes",
            "message": {"role": "user", "content": "first"},
        },
        {
            "type": "assistant",
            "uuid": "22222222-0000-4000-8000-000000000002",
            "sessionId": session,
            "message": {
                "id": "msg_untimed",
                "role": "assistant",
                "content": [{"type": "text", "text": "second"}],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=b"secret")
    messages = adapter.parse(transcript).projection.messages

    # The untimed record must stay where the file put it, not jump to index 0.
    assert [message.content for message in messages] == ["first", "second"]


def test_parse_keeps_block_order_within_one_timestamped_record(tmp_path):
    """Blocks of one record share a timestamp; the stable sort must not shuffle."""

    project = tmp_path / "C--Users-diego--hermes"
    project.mkdir()
    transcript = project / "bbbbbbbb-0000-4000-8000-00000000000b.jsonl"
    session = "bbbbbbbb-0000-4000-8000-00000000000b"
    records = [
        {
            "type": "user",
            "uuid": "33333333-0000-4000-8000-000000000003",
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:41.000Z",
            "cwd": "C:/Users/diego/.hermes",
            "message": {"role": "user", "content": "ask"},
        },
        {
            "type": "assistant",
            "uuid": "44444444-0000-4000-8000-000000000004",
            "sessionId": session,
            "timestamp": "2026-08-25T18:17:44.000Z",
            "cwd": "C:/Users/diego/.hermes",
            "message": {
                "id": "msg_blocks",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "beta"},
                    {"type": "text", "text": "gamma"},
                ],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeSourceAdapter(tmp_path, marker_secret=b"secret")
    messages = adapter.parse(transcript).projection.messages

    assert [message.content for message in messages] == [
        "ask",
        "alpha",
        "beta",
        "gamma",
    ]
    assert [message.ordinal for message in messages[1:]] == [0, 1, 2]
