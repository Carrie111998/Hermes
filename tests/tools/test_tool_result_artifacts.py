from __future__ import annotations

import json


def test_small_result_stays_inline(monkeypatch, tmp_path):
    from tools import tool_result_artifacts as artifacts

    monkeypatch.setattr(artifacts, "_artifact_root", lambda: tmp_path)
    monkeypatch.setattr(artifacts, "_load_limits", lambda: (True, 100, 30))
    assert artifacts.externalize_large_tool_result(
        tool_name="small", result="ok"
    ) == "ok"


def test_large_result_becomes_preview_and_reference(monkeypatch, tmp_path):
    from tools import tool_result_artifacts as artifacts

    monkeypatch.setattr(artifacts, "_artifact_root", lambda: tmp_path)
    monkeypatch.setattr(artifacts, "_load_limits", lambda: (True, 100, 30))
    original = "abcdefghij" * 30

    compact = artifacts.externalize_large_tool_result(
        tool_name="mcp_list_customers",
        result=original,
        session_id="session/unsafe",
        tool_call_id="call:1",
    )
    payload = json.loads(compact)

    assert payload["externalized"] is True
    assert payload["originalChars"] == len(original)
    assert len(payload["preview"]) < len(original)
    assert open(payload["artifactRef"], encoding="utf-8").read() == original
    assert str(tmp_path) in payload["artifactRef"]


def test_error_result_is_never_externalized(monkeypatch, tmp_path):
    from tools import tool_result_artifacts as artifacts

    monkeypatch.setattr(artifacts, "_artifact_root", lambda: tmp_path)
    monkeypatch.setattr(artifacts, "_load_limits", lambda: (True, 20, 10))
    error = json.dumps({"success": False, "error": "x" * 100})

    assert artifacts.externalize_large_tool_result(
        tool_name="failing_tool", result=error
    ) == error
    assert list(tmp_path.rglob("*.txt")) == []
