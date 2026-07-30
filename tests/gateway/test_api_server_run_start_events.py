import asyncio
import threading
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools.registry import registry


TOOL_NAME = "test_projected_run_start_event"


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True))


@pytest.fixture
def projected_tool():
    registry.register(
        name=TOOL_NAME,
        toolset="test",
        schema={
            "name": TOOL_NAME,
            "description": "Report intent",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: "ok",
        run_start_event={"event": "agent.intent", "fields": ("text", "speech")},
    )
    try:
        yield TOOL_NAME
    finally:
        registry.deregister(TOOL_NAME)


@pytest.mark.asyncio
async def test_projected_tool_emits_only_declared_redacted_string_fields(
    adapter, projected_tool
):
    run_id = "run_projected_tool"
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    callback = adapter._make_run_event_callback(run_id, loop)
    secret = "sk-proj-abcdef1234567890abcdef1234567890abcdef12"

    callback(
        "tool.started",
        projected_tool,
        f"generic preview leaks {secret}",
        {
            "text": (
                f"Use {secret} and "
                "https://user:password@example.test/run?token=private " + "x" * 1_000
            ),
            "speech": "brief",
            "extra": "must not cross the boundary",
            "nested": {"private": True},
        },
    )
    callback(
        "tool.completed",
        projected_tool,
        result="private result",
        duration=1.0,
    )

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["event"] == "agent.intent"
    assert event["run_id"] == run_id
    assert event["sequence"] == 1
    assert event["speech"] == "brief"
    assert secret not in event["text"]
    assert "password" not in event["text"]
    assert "token=private" not in event["text"]
    assert len(event["text"]) <= 160
    assert set(event) == {
        "event",
        "run_id",
        "sequence",
        "timestamp",
        "text",
        "speech",
    }
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.01)


@pytest.mark.asyncio
async def test_projected_tool_ignores_missing_or_non_string_fields(
    adapter, projected_tool
):
    run_id = "run_projected_invalid_args"
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    callback = adapter._make_run_event_callback(run_id, loop)

    callback(
        "tool.started",
        projected_tool,
        args={"text": {"nested": "private"}, "speech": "brief"},
    )

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.01)


@pytest.mark.asyncio
async def test_projected_sequences_match_concurrent_enqueue_order(
    adapter, projected_tool
):
    run_id = "run_projected_concurrent"
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    first_status_entered = threading.Event()
    second_status_entered = threading.Event()
    original_set_status = adapter._set_run_status

    def delayed_set_status(*args, **kwargs):
        if threading.current_thread().name == "first-intent":
            first_status_entered.set()
            second_status_entered.wait(timeout=0.2)
        else:
            second_status_entered.set()
        return original_set_status(*args, **kwargs)

    callback = adapter._make_run_event_callback(run_id, loop)
    with patch.object(adapter, "_set_run_status", side_effect=delayed_set_status):
        first = threading.Thread(
            name="first-intent",
            target=callback,
            args=(
                "tool.started",
                projected_tool,
                None,
                {"text": "first", "speech": "brief"},
            ),
        )
        second = threading.Thread(
            name="second-intent",
            target=callback,
            args=(
                "tool.started",
                projected_tool,
                None,
                {"text": "second", "speech": "brief"},
            ),
        )
        first.start()
        assert first_status_entered.wait(timeout=1.0)
        second.start()
        first.join(timeout=1.0)
        second.join(timeout=1.0)
        assert not first.is_alive()
        assert not second.is_alive()

    events = [
        await asyncio.wait_for(queue.get(), timeout=1.0),
        await asyncio.wait_for(queue.get(), timeout=1.0),
    ]
    assert [(event["text"], event["sequence"]) for event in events] == [
        ("first", 1),
        ("second", 2),
    ]


@pytest.mark.asyncio
async def test_projected_sequence_restarts_for_each_run(adapter, projected_tool):
    loop = asyncio.get_running_loop()
    events = []
    for run_id in ("run-a", "run-b"):
        queue = asyncio.Queue()
        adapter._run_streams[run_id] = queue
        callback = adapter._make_run_event_callback(run_id, loop)
        callback(
            "tool.started",
            projected_tool,
            args={"text": run_id, "speech": "brief"},
        )
        events.append(await asyncio.wait_for(queue.get(), timeout=1.0))

    assert [(event["run_id"], event["sequence"]) for event in events] == [
        ("run-a", 1),
        ("run-b", 1),
    ]


@pytest.mark.asyncio
async def test_ordinary_tool_lifecycle_is_unchanged(adapter):
    run_id = "run_ordinary_tool"
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    callback = adapter._make_run_event_callback(run_id, loop)

    callback("tool.started", "ordinary", "safe preview", {"value": "safe"})
    callback("tool.completed", "ordinary", duration=1.25, is_error=False)

    started = await asyncio.wait_for(queue.get(), timeout=1.0)
    completed = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert started["event"] == "tool.started"
    assert started["preview"] == "safe preview"
    assert completed["event"] == "tool.completed"
    assert completed["duration"] == 1.25
