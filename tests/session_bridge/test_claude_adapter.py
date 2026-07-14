from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import BinaryIO

import pytest

from session_bridge.claude_adapter import ClaudeCursor, ClaudeSourceAdapter
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    canonical_session_id,
    encode_bridge_marker,
)


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
    assert result.cursor.head_length == min(path.stat().st_size, 4096)
    assert result.rebuild is False
    assert result.malformed_lines == 0
    assert result.unknown_records == 0


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


def test_cold_increment_uses_stem_and_mtime_without_full_read(tmp_path, monkeypatch):
    path = tmp_path / "cold-native-id.jsonl"
    path.write_bytes(_json_line(_message_record("x" * 6000)))
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
    assert cold.projection.native_id == "cold-native-id"
    assert cold.projection.messages == []
    assert cold.projection.started_at == 321.0
    assert cold.projection.last_active == 321.0
    assert not any(offset == 0 and requested == -1 for offset, requested, _ in reads)


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
    assert first.cursor.head_length == 4096
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
