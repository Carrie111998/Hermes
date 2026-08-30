"""Tests for the `log` tool_progress mode (salvage of #3459 / #3458).

`display.tool_progress: log` keeps the chat silent and emits tool-call lines
through Hermes' structured container log stream. These tests exercise the
mode's building blocks without spinning up a full gateway run.
"""

import asyncio
import queue
from datetime import datetime

import pytest


def _log_branch(log_queue, progress_queue, event_type, tool_name, preview=None):
    """Replica of the log-mode branch in gateway/run.py progress_callback."""
    if log_queue is not None:
        if event_type == "tool.started" and tool_name and tool_name != "_thinking":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            preview_str = f' "{preview}"' if preview else ""
            log_queue.put(f"{ts}  {tool_name}:{preview_str}".rstrip())
        if not progress_queue:
            return "returned"
    return "fell-through"


class TestLogBranchSemantics:
    def test_tool_started_enqueued(self):
        q = queue.Queue()
        assert _log_branch(q, None, "tool.started", "terminal", "ls -la") == "returned"
        line = q.get_nowait()
        assert "terminal" in line and "ls -la" in line


    def test_thinking_not_enqueued(self):
        q = queue.Queue()
        _log_branch(q, None, "tool.started", "_thinking", "pondering")
        assert q.empty()


@pytest.mark.asyncio
async def test_write_tool_log_uses_structured_stream():
    """Tool progress log records are emitted as Cloud Logging JSON."""
    log_queue: queue.Queue = queue.Queue()
    log_queue.put("2026-07-02 10:00:00  terminal: \"echo hi\"")
    log_queue.put("2026-07-02 10:00:01  read_file: \"foo.py\"")

    import logging
    import io
    import json
    from hermes_logging import GCPStructuredLogHandler

    stream = io.StringIO()
    handler = GCPStructuredLogHandler(stream)
    tool_logger = logging.getLogger(f"hermes.tool_calls.test.{id(log_queue)}")
    tool_logger.setLevel(logging.INFO)
    tool_logger.propagate = False
    tool_logger.addHandler(handler)
    try:
        while True:
            try:
                tool_logger.info("%s", log_queue.get_nowait())
            except queue.Empty:
                break
    finally:
        tool_logger.removeHandler(handler)
        handler.flush()
        handler.close()

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 2
    assert "terminal" in records[0]["message"]
    assert "read_file" in records[1]["message"]
    assert all(record["severity"] == "INFO" for record in records)
    await asyncio.sleep(0)  # keep the asyncio marker honest
