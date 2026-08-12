"""Regression tests for parallel image-generation tool batches."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import run_agent
from agent import tool_executor


def _tool_call(name: str, args: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args),
        ),
    )


def test_image_generate_batch_routes_to_concurrent_executor():
    agent = SimpleNamespace()
    agent._execute_tool_calls = run_agent.AIAgent._execute_tool_calls.__get__(agent)
    agent._execute_tool_calls_concurrent = MagicMock()
    agent._execute_tool_calls_sequential = MagicMock()
    assistant_message = SimpleNamespace(
        tool_calls=[
            _tool_call("image_generate", {"prompt": "variation one"}, "img_1"),
            _tool_call("image_generate", {"prompt": "variation two"}, "img_2"),
        ],
    )

    agent._execute_tool_calls(assistant_message, [], "task-image-batch")

    agent._execute_tool_calls_concurrent.assert_called_once()
    agent._execute_tool_calls_sequential.assert_not_called()


def test_image_generate_parallel_worker_cap_defaults_to_four():
    runnable_calls = [
        (
            0,
            _tool_call("image_generate", {"prompt": "one"}, "img_1"),
            "image_generate",
            {},
        ),
        (
            1,
            _tool_call("image_generate", {"prompt": "two"}, "img_2"),
            "image_generate",
            {},
        ),
        (
            2,
            _tool_call("image_generate", {"prompt": "three"}, "img_3"),
            "image_generate",
            {},
        ),
        (
            3,
            _tool_call("image_generate", {"prompt": "four"}, "img_4"),
            "image_generate",
            {},
        ),
        (
            4,
            _tool_call("image_generate", {"prompt": "five"}, "img_5"),
            "image_generate",
            {},
        ),
    ]

    with patch("hermes_cli.config.load_config", return_value={}):
        assert tool_executor._max_workers_for_tool_batch(runnable_calls) == 4


def test_image_generate_parallel_worker_cap_can_be_configured_lower():
    runnable_calls = [
        (
            0,
            _tool_call("image_generate", {"prompt": "one"}, "img_1"),
            "image_generate",
            {},
        ),
        (
            1,
            _tool_call("image_generate", {"prompt": "two"}, "img_2"),
            "image_generate",
            {},
        ),
    ]

    with patch(
        "hermes_cli.config.load_config",
        return_value={"image_gen": {"max_parallel_requests": 1}},
    ):
        assert tool_executor._max_workers_for_tool_batch(runnable_calls) == 1


def _non_image_calls(n):
    return [
        (i, _tool_call("web_search", {"q": "x"}, f"c{i}"), "web_search", {})
        for i in range(n)
    ]


def test_general_tool_worker_cap_defaults_to_eight():
    calls = _non_image_calls(10)
    with patch("hermes_cli.config.load_config", return_value={}):
        assert tool_executor._max_workers_for_tool_batch(calls) == 8


def test_general_tool_worker_cap_can_be_configured_lower():
    calls = _non_image_calls(10)
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 2}},
    ):
        assert tool_executor._max_workers_for_tool_batch(calls) == 2


def test_general_tool_worker_cap_clamps_invalid_and_high_values():
    calls = _non_image_calls(10)
    # Above the ceiling → clamped to _MAX_TOOL_WORKERS.
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 999}},
    ):
        assert tool_executor._max_workers_for_tool_batch(calls) == 8
    # Zero → floored to 1.
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 0}},
    ):
        assert tool_executor._max_workers_for_tool_batch(calls) == 1
    # Non-numeric → default.
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": "abc"}},
    ):
        assert tool_executor._max_workers_for_tool_batch(calls) == 8
