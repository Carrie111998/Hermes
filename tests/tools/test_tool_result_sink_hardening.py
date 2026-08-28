"""Causal regressions for every non-model tool-result sink."""

import json
from unittest.mock import MagicMock, patch

from model_tools import _emit_post_tool_call_hook
from tools.tool_result_storage import (
    _write_to_spillover,
    maybe_persist_tool_result,
    sanitize_tool_result_for_sink,
    _write_to_sandbox,
)


OPAQUE = "opaque-r3-UNREUSABLE-9f7c2a"


def test_sink_sanitizer_handles_opaque_nested_and_url_values():
    value = {
        "message": OPAQUE,
        "config": {"apiKey": OPAQUE, "token": OPAQUE},
        "callback": f"https://user:{OPAQUE}@example.test/cb?token={OPAQUE}",
    }
    safe = sanitize_tool_result_for_sink(value)
    assert OPAQUE not in safe
    assert "redacted" in safe.lower()


def test_sink_sanitizer_handles_bytes_without_type_error():
    safe = sanitize_tool_result_for_sink(f"prefix={OPAQUE}".encode("utf-8"))
    assert isinstance(safe, str)
    assert OPAQUE not in safe


def test_direct_spillover_accepts_structured_and_binary_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    structured = {"apiKey": OPAQUE, "nested": [{"token": OPAQUE}]}
    path = _write_to_spillover(structured, "structured.txt")
    assert path is not None
    assert OPAQUE not in open(path, encoding="utf-8").read()
    path = _write_to_spillover(OPAQUE.encode(), "binary.txt")
    assert path is not None
    assert OPAQUE not in open(path, encoding="utf-8").read()


def test_remote_stdin_sink_receives_only_sanitized_text():
    remote = MagicMock()
    remote.execute.return_value = {"returncode": 0}
    assert _write_to_sandbox({"token": OPAQUE}, "/tmp/result.txt", remote)
    assert OPAQUE not in remote.execute.call_args.kwargs["stdin_data"]


def test_persistence_normalizes_structured_and_binary_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for suffix, value in (("json", {"apiKey": OPAQUE}), ("bytes", OPAQUE.encode())):
        result = maybe_persist_tool_result(
            content=value,
            tool_name="demo",
            tool_use_id=f"tc_{suffix}",
            threshold=0,
            env=None,
        )
        assert OPAQUE not in result


def test_post_tool_hook_receives_sanitized_result_payload():
    seen = []
    with patch("hermes_cli.lifecycle.has_hook", return_value=True), patch(
        "hermes_cli.lifecycle.invoke_hook",
        side_effect=lambda *args, **kwargs: seen.append(kwargs),
    ):
        _emit_post_tool_call_hook(
            function_name="demo",
            function_args={},
            result={"token": OPAQUE},
            task_id="task",
        )
    assert len(seen) == 1
    assert OPAQUE not in json.dumps(seen[0], default=str)


def test_post_tool_hook_sanitizes_error_message():
    seen = []
    with patch("hermes_cli.lifecycle.has_hook", return_value=True), patch(
        "hermes_cli.lifecycle.invoke_hook",
        side_effect=lambda *args, **kwargs: seen.append(kwargs),
    ):
        _emit_post_tool_call_hook(
            function_name="demo",
            function_args={},
            result="safe",
            error_message=OPAQUE,
            status="error",
        )
    assert seen[0]["error_message"] != OPAQUE
