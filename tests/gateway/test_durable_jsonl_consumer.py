import argparse
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from gateway import durable_jsonl_consumer as consumer


def _message(message_id: str, chat_id: str = "test-group@g.us") -> dict:
    return {
        "messageId": message_id,
        "chatId": chat_id,
        "senderId": "fixture-user",
        "senderName": "Fixture User",
        "chatName": "Fixture Chat",
        "isGroup": True,
        "body": "fixture message",
        "hasMedia": False,
        "mediaType": None,
        "mediaUrls": [],
        "timestamp": 100,
        "fromMe": False,
    }


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def test_stage_is_durable_before_cursor_and_idempotent(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    values = [_message("m1"), _message("m2")]
    _write_jsonl(source, values)
    first_line_end = len((json.dumps(values[0]) + "\n").encode())

    consumer.initialize_cursor(source, cursor_path, position="start")
    assert inbox.stage_from_source(source, cursor_path, max_records=1) == 1
    assert consumer.SourceCursor.from_path(cursor_path).offset == first_line_end
    assert inbox.counts() == {"pending": 1}

    assert inbox.stage_from_source(source, cursor_path, max_records=10) == 1
    assert inbox.counts() == {"pending": 2}

    # Simulate a crash after the DB commit but before cursor replacement by
    # rewinding the cursor. The unique message id absorbs the re-read.
    raw = json.loads(cursor_path.read_text())
    raw["offset"] = 0
    consumer._atomic_write_json(cursor_path, raw)
    assert inbox.stage_from_source(source, cursor_path, max_records=10) == 2
    assert inbox.counts() == {"pending": 2}
    assert consumer.SourceCursor.from_path(cursor_path).offset == source.stat().st_size


def test_partial_line_never_advances_cursor(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    source.write_text(json.dumps(_message("partial")), encoding="utf-8")
    consumer.initialize_cursor(source, cursor_path, position="start")

    assert inbox.stage_from_source(source, cursor_path) == 0
    assert consumer.SourceCursor.from_path(cursor_path).offset == 0
    with source.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert inbox.stage_from_source(source, cursor_path) == 1


def test_source_rotation_and_truncation_fail_closed(tmp_path):
    source = tmp_path / "events.jsonl"
    cursor_path = tmp_path / "cursor.json"
    inbox = consumer.DurableInbox(tmp_path / "inbox.db")
    _write_jsonl(source, [_message("m1")])
    consumer.initialize_cursor(source, cursor_path, position="end")

    replacement = tmp_path / "replacement.jsonl"
    _write_jsonl(replacement, [_message("m2")])
    replacement.replace(source)
    with pytest.raises(consumer.ConsumerError, match="inode changed"):
        inbox.stage_from_source(source, cursor_path)


def test_singleton_guard_rejects_second_holder(tmp_path):
    lock_path = tmp_path / "consumer.lock"
    with consumer.SingletonLock(lock_path):
        with pytest.raises(consumer.ConsumerError, match="singleton"):
            with consumer.SingletonLock(lock_path):
                pass


def test_disabled_once_does_not_require_or_open_source(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: false\n", encoding="utf-8")
    gate = tmp_path / "processing-gate.json"
    gate.write_text(
        json.dumps({"version": 1, "enabled": False, "generation": 0}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=str(config),
        source=str(tmp_path / "does-not-exist.jsonl"),
        cursor=str(tmp_path / "does-not-exist.cursor"),
        inbox=str(tmp_path / "inbox.db"),
        status_file=str(tmp_path / "status.json"),
        lock_file=str(tmp_path / "consumer.lock"),
        state_db=str(tmp_path / "state.db"),
        processing_gate=str(gate),
        once=True,
        poll_seconds=0.01,
        max_records=10,
    )

    assert asyncio.run(consumer.run_consumer(args)) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "standby"
    assert status["processing_enabled"] is False
    assert status["config_enabled"] is False
    assert status["gate_enabled"] is False
    assert status["source_opened"] is False
    assert status["cursor_advanced"] is False
    assert not Path(args.cursor).exists()


def test_root_gate_blocks_accidentally_enabled_config_without_opening_source(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    gate = tmp_path / "processing-gate.json"
    gate.write_text(
        json.dumps({"version": 1, "enabled": False, "generation": 0}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=str(config),
        source=str(tmp_path / "does-not-exist.jsonl"),
        cursor=str(tmp_path / "does-not-exist.cursor"),
        inbox=str(tmp_path / "inbox.db"),
        status_file=str(tmp_path / "status.json"),
        lock_file=str(tmp_path / "consumer.lock"),
        state_db=str(tmp_path / "state.db"),
        processing_gate=str(gate),
        once=True,
        poll_seconds=0.01,
        max_records=10,
    )

    assert asyncio.run(consumer.run_consumer(args)) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["processing_enabled"] is False
    assert status["config_enabled"] is True
    assert status["gate_enabled"] is False
    assert status["source_opened"] is False
    assert status["cursor_advanced"] is False
    assert not Path(args.cursor).exists()


def test_fixture_mode_uses_inbox_path_and_marks_completed(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "model": {
                "provider": "openai-direct-primary",
                "default": "gpt-5.4-mini",
            },
            "pa": {"enabled": True},
        }),
        encoding="utf-8",
    )
    source = tmp_path / "fixture.jsonl"
    _write_jsonl(source, [_message("fixture-1")])

    async def fake_process(records, **kwargs):
        assert [record.message_id for record in records] == ["fixture-1"]
        return {
            "turn_id": "pa-turn-fixture",
            "provider": "openai-direct-primary",
            "model": "gpt-5.4-mini",
            "processed": 1,
            "outbound_captured": 1,
            "blocked_commands": 0,
        }

    monkeypatch.setattr(consumer, "process_replay_records", fake_process)
    args = argparse.Namespace(
        test_root=str(tmp_path),
        source=str(source),
        cursor=str(tmp_path / "cursor.json"),
        inbox=str(tmp_path / "inbox.db"),
        config=str(config),
        state_db=str(tmp_path / "state.db"),
        report=str(tmp_path / "report.json"),
        run_id="fixture-run",
        max_records=10,
    )
    assert asyncio.run(consumer.run_fixture(args)) == 0
    assert consumer.DurableInbox(tmp_path / "inbox.db").counts() == {"completed": 1}
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["ok"] is True
    assert report["result"]["turn_id"] == "pa-turn-fixture"


def test_fixture_mode_rejects_path_outside_test_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = root / "config.yaml"
    config.write_text("pa:\n  enabled: true\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, [_message("m1")])
    args = argparse.Namespace(
        test_root=str(root),
        source=str(outside),
        cursor=str(root / "cursor.json"),
        inbox=str(root / "inbox.db"),
        config=str(config),
        state_db=str(root / "state.db"),
        report=str(root / "report.json"),
        run_id="fixture-run",
        max_records=10,
    )
    with pytest.raises(consumer.ConsumerError, match="escapes test root"):
        asyncio.run(consumer.run_fixture(args))


def test_bridge_wrapper_is_normalized_without_client_content_in_metadata():
    item = _message("wrapped")
    assert consumer._bridge_item({"event": item}) == item
    with pytest.raises(consumer.ConsumerError, match="messageId/chatId"):
        consumer._bridge_item({"event": {"type": "connection"}})
