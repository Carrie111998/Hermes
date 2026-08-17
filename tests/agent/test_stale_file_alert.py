"""The Dirac-style stale-file alert: the edited files' mtimes are recorded
after the tool execution; the next turn's prompt flags externally changed
files before editing."""

import os

import pytest

from run_agent import AIAgent


def _agent(monkeypatch, tmp_path):
    agent = AIAgent.__new__(AIAgent)
    agent._edited_files_mtimes = {}
    agent._executing_tools = False
    agent._interrupt_requested = False
    agent._buffer_vprint = lambda *a, **k: None
    # the system-prompt builder's minimal surface
    agent.load_soul_identity = False
    agent.skip_context_files = True
    agent.skip_memory = True
    agent.model = "test-model"
    agent.quiet_mode = True
    agent.provider = "test"
    agent.base_url = ""
    agent.api_mode = "chat_completions"
    agent._system_prompt_extra = ""
    agent.valid_tool_names = set()
    agent.platform = "cli"
    agent._memory_store = None
    agent._memory_manager = None
    agent.pass_session_id = ""
    return agent


def test_records_edited_file_mtime(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("old")
    agent = _agent(None, tmp_path)
    call = type("TC", (), {"function": type("F", (), {
        "name": "patch", "arguments": '{"path": "%s"}' % f})})()
    agent._record_edited_files([call])
    assert str(f) in agent._edited_files_mtimes


def test_detects_external_change(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("old")
    agent = _agent(None, tmp_path)
    call = type("TC", (), {"function": type("F", (), {
        "name": "write_file", "arguments": '{"path": "%s"}' % f})})()
    agent._record_edited_files([call])
    assert agent._stale_edited_files() == []
    f.write_text("externally changed")  # the mtime bumps
    stale = agent._stale_edited_files()
    assert str(f) in stale


def test_alert_emitted_in_the_system_prompt(monkeypatch, tmp_path):
    from agent.system_prompt import build_system_prompt_parts
    f = tmp_path / "y.py"
    f.write_text("old")
    agent = _agent(monkeypatch, tmp_path)
    call = type("TC", (), {"function": type("F", (), {
        "name": "patch", "arguments": '{"path": "%s"}' % f})})()
    agent._record_edited_files([call])
    agent.quiet_mode = True
    agent.model = "test"
    parts = build_system_prompt_parts(agent)
    assert "CRITICAL FILE STATE ALERT" not in parts["volatile"]
    f.write_text("externally changed")
    parts = build_system_prompt_parts(agent)
    assert "CRITICAL FILE STATE ALERT" in parts["volatile"]


def test_deleted_edited_file_is_flagged(tmp_path):
    import json as _json, os
    agent = _agent(None, tmp_path)
    p = tmp_path / "gone.py"
    p.write_text("a\n")
    call = type("TC", (), {"function": type("F", (), {
        "name": "patch", "arguments": '{"path": "%s"}' % p})})()
    agent._record_edited_files([call])
    p.unlink()
    assert str(p) in agent._stale_edited_files()
