"""Regression tests for the R3-CAND-001 persistence and callback sinks."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import convert_to_trajectory_format
from agent.codex_runtime import make_codex_app_server_event_bridge
from agent.trajectory import save_trajectory
from run_agent import AIAgent


MARKER = "opaque-r3-candidate-fix-SECRET-123456"


def test_session_snapshot_sanitizes_below_threshold_tool_content(tmp_path):
    """JSON snapshots must not retain raw ordinary/nested/URL-like tool bytes."""
    agent = AIAgent.__new__(AIAgent)
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path
    agent.session_id = "r3-snapshot"
    agent.model = "test-model"
    agent.base_url = "https://example.test/v1"
    agent.platform = "test"
    agent.session_start = datetime.now()
    agent._cached_system_prompt = ""
    agent.tools = []
    agent.verbose_logging = False
    payload = json.dumps(
        {
            "ordinary": MARKER,
            "nested": {"value": MARKER},
            "url": f"https://example.test/callback?state={MARKER}",
        }
    )

    with patch("agent.redact._REDACT_ENABLED", True):
        agent._save_session_log(
            [
                {"role": "user", "content": "hello"},
                {"role": "tool", "content": payload, "api_content": payload},
            ]
        )

    snapshot = (tmp_path / "session_r3-snapshot.json").read_bytes()
    assert MARKER.encode() not in snapshot
    assert b"redacted" in snapshot.lower()


def test_trajectory_jsonl_sanitizes_tool_response_before_write(tmp_path):
    """Trajectory conversion and JSONL persistence must share the sink boundary."""
    agent = SimpleNamespace(_format_tools_for_system_message=lambda: "")
    payload = json.dumps(
        {
            "ordinary": MARKER,
            "nested": [{"value": MARKER}],
            "url": f"https://example.test/callback?token={MARKER}",
        }
    )
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "demo", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": payload},
    ]
    trajectory = convert_to_trajectory_format(agent, messages, "hello", True)
    output = tmp_path / "trajectory.jsonl"
    save_trajectory(trajectory, "test-model", True, str(output))

    written = output.read_bytes()
    assert MARKER.encode() not in written
    assert b"redacted" in written.lower()
    entry = json.loads(written.decode("utf-8"))
    assert [row["from"] for row in entry["conversations"]] == [
        "system",
        "human",
        "gpt",
        "tool",
    ]


def test_codex_no_start_completion_callback_sanitizes_name_args_and_result():
    """A completion without item/started must not bypass callback sanitization."""
    agent = SimpleNamespace(
        tool_progress_callback=MagicMock(name="tool_progress_callback"),
        tool_complete_callback=MagicMock(name="tool_complete_callback"),
        tool_start_callback=None,
    )
    bridge = make_codex_app_server_event_bridge(agent)
    bridge(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "dynamicToolCall",
                    "id": "dynamic-r3-no-start",
                    "tool": f"tool-{MARKER}",
                    "arguments": {"opaque": MARKER},
                    "contentItems": [{"text": MARKER}],
                    "success": True,
                }
            },
        }
    )

    call = agent.tool_complete_callback.call_args
    assert call is not None
    assert MARKER not in repr(call)
    assert "redacted" in repr(call).lower()
    progress = agent.tool_progress_callback.call_args
    assert MARKER not in repr(progress)
