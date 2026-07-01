"""Tests for LLM/project-facts JSON-RPC methods (tui_gateway/server.py)."""

from __future__ import annotations

import agent.coding_context as coding_context
import agent.oneshot as oneshot
import tui_gateway.server as srv


def _call(method: str, params: dict) -> dict:
    """Invoke a registered RPC method and return a compact ok/error result."""
    envelope = srv._methods[method](1, params)
    if "error" in envelope:
        return {"ok": False, "error": envelope["error"]["code"], "message": envelope["error"]["message"]}
    return {"ok": True, **envelope["result"]}


# ---------------------------------------------------------------------------
# project.facts
# ---------------------------------------------------------------------------


def test_project_facts_returns_facts_when_available(monkeypatch):
    facts = {
        "cwd": "/repo",
        "package_manager": "pytest",
        "verify_commands": ["python -m pytest"],
    }
    monkeypatch.setattr(coding_context, "project_facts_for", lambda cwd: facts)

    res = _call("project.facts", {"cwd": "/repo"})

    assert res["ok"] is True
    assert res["facts"] == facts


def test_project_facts_cwd_none_passthrough(monkeypatch):
    seen = {}

    def _project_facts_for(cwd):
        seen["cwd"] = cwd
        return {"ok": "facts"}

    monkeypatch.setattr(coding_context, "project_facts_for", _project_facts_for)

    res = _call("project.facts", {})

    assert res["ok"] is True
    assert seen["cwd"] is None


def test_project_facts_returns_null_on_non_code_workspace(monkeypatch):
    monkeypatch.setattr(coding_context, "project_facts_for", lambda cwd: None)

    res = _call("project.facts", {"cwd": "/tmp"})

    assert res["ok"] is True
    assert res["facts"] is None


def test_project_facts_fails_open_on_exception(monkeypatch):
    def _boom(cwd):
        raise RuntimeError("cannot inspect workspace")

    monkeypatch.setattr(coding_context, "project_facts_for", _boom)

    res = _call("project.facts", {"cwd": "/broken"})

    assert res["ok"] is True
    assert res["facts"] is None


def test_project_facts_method_is_registered():
    assert "project.facts" in srv._methods


# ---------------------------------------------------------------------------
# llm.oneshot
# ---------------------------------------------------------------------------


def test_llm_oneshot_returns_text_from_template(monkeypatch):
    monkeypatch.setattr(oneshot, "run_oneshot", lambda **kwargs: "feat: add widget")

    res = _call("llm.oneshot", {"template": "commit_message", "variables": {"diff": "+widget"}})

    assert res["ok"] is True
    assert res["text"] == "feat: add widget"


def test_llm_oneshot_branch_name_happy_path(monkeypatch):
    monkeypatch.setattr(oneshot, "run_oneshot", lambda **kwargs: "feat-relay-retry-limit")

    res = _call(
        "llm.oneshot",
        {
            "template": "branch_name",
            "variables": {"description": "add a retry limit to the relay watchdog"},
        },
    )

    assert res["ok"] is True
    assert res["text"] == "feat-relay-retry-limit"


def test_llm_oneshot_branch_name_missing_description_returns_4032():
    res = _call("llm.oneshot", {"template": "branch_name", "variables": {}})

    assert res["ok"] is False
    assert res["error"] == 4032


def test_llm_oneshot_returns_text_from_instructions(monkeypatch):
    seen = {}

    def _run_oneshot(**kwargs):
        seen.update(kwargs)
        return "generated text"

    monkeypatch.setattr(oneshot, "run_oneshot", _run_oneshot)

    res = _call("llm.oneshot", {"instructions": "Summarize", "input": "hello world"})

    assert res["ok"] is True
    assert res["text"] == "generated text"
    assert seen["instructions"] == "Summarize"
    assert seen["user_input"] == "hello world"


def test_llm_oneshot_missing_template_and_instructions_returns_4030():
    res = _call("llm.oneshot", {})

    assert res["ok"] is False
    assert res["error"] == 4030


def test_llm_oneshot_unknown_template_returns_4031(monkeypatch):
    def _unknown_template(**kwargs):
        raise KeyError("no-such-template")

    monkeypatch.setattr(oneshot, "run_oneshot", _unknown_template)

    res = _call("llm.oneshot", {"template": "no-such-template"})

    assert res["ok"] is False
    assert res["error"] == 4031


def test_llm_oneshot_invalid_template_vars_returns_4032(monkeypatch):
    def _bad_variables(**kwargs):
        raise ValueError("bad variables")

    monkeypatch.setattr(oneshot, "run_oneshot", _bad_variables)

    res = _call("llm.oneshot", {"template": "commit_message", "variables": {"bad": True}})

    assert res["ok"] is False
    assert res["error"] == 4032


def test_llm_oneshot_provider_error_returns_5030(monkeypatch):
    def _provider_error(**kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(oneshot, "run_oneshot", _provider_error)

    res = _call("llm.oneshot", {"instructions": "Generate", "input": "text"})

    assert res["ok"] is False
    assert res["error"] == 5030


def test_llm_oneshot_method_is_registered():
    assert "llm.oneshot" in srv._methods


def test_llm_oneshot_branch_name_registration_is_idempotent():
    assert "branch_name" in oneshot.PROMPT_TEMPLATES
