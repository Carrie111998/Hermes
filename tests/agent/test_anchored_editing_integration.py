"""The anchored editing END-TO-END (the real flow, no mocks).

The only scenario worth duplicating beyond the unit tests: the full
read -> model-copies-anchors -> edit -> file-changed -> next-turn alert
flow. The ns-level scenarios (the flock serialization, the atomic rename's
no-partial-observability, the drift window) are NOT duplicated here — their
semantics are pinned by the unit tests and the integration cannot control
nanosecond timing."""

import json


def test_read_edit_round_trip_and_stale_alert(tmp_path):
    from tools.anchors import render_anchored_lines
    from tools.file_tools import anchored_edit_tool, read_file_tool

    p = tmp_path / "t.py"
    p.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")

    # 1) the model reads with include_anchors
    read = json.loads(read_file_tool(str(p), include_anchors=True))
    assert "ANCHOR" in read["content"]

    # 2) the model copies the anchored lines verbatim and edits the b-function
    anchored_lines = [l for l in read["content"].splitlines() if l.startswith("ANCHOR")]
    target = next(l for l in anchored_lines if l.endswith("    return 2"))
    res = json.loads(anchored_edit_tool(str(p), [{"anchor": target, "text": "    return 42"}]))
    assert res["ok"] is True and res["applied"] == 1
    assert "return 42" in p.read_text() and "return 2" not in p.read_text()

    # 3) the stale-alert records the anchored edit (the file's mtime tracked)
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent._edited_files_mtimes = {}
    call = type("TC", (), {"function": type("F", (), {
        "name": "anchored_edit",
        "arguments": json.dumps({"path": str(p), "edits": []})})})()
    agent._record_edited_files([call])
    assert str(p) in agent._edited_files_mtimes

    # 4) an external change trips the stale alert
    p.write_text("externally rewritten\n")
    from agent.system_prompt import build_system_prompt_parts
    for attr in ("load_soul_identity", "skip_context_files", "skip_memory", "quiet_mode",
                 "_system_prompt_extra", "platform", "_memory_store", "_memory_manager",
                 "pass_session_id"):
        setattr(agent, attr, "" if "pass" in attr else (False if attr in
                ("load_soul_identity", "skip_context_files", "skip_memory", "quiet_mode")
                else (None if "memory" in attr else "cli")))
    agent.model = "test-model"
    agent.provider = "test"
    agent.base_url = ""
    agent.api_mode = "chat_completions"
    agent.valid_tool_names = set()
    parts = build_system_prompt_parts(agent)
    assert "CRITICAL FILE STATE ALERT" in parts["volatile"]
