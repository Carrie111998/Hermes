import json
from types import SimpleNamespace

import pytest

from devflow_delegation.agent_policy import CeilingExceeded
from devflow_delegation.agent_runner import build_messages, dispatch_tool, run_agent
from devflow_delegation.allowlist import TargetConfig


def _target(**over):
    values = dict(
        repo="fixture", checkout_path="/unused",
        allowed_globs=("src/**",), denied_globs=("**/.env",),
        test_commands=(("python", "-c", "print('tests passed')"),),
        agent_model="test/model", agent_max_iterations=5,
        agent_max_tokens=10_000, agent_max_files=3, agent_timeout_seconds=60,
    )
    values.update(over)
    return TargetConfig(**values)


def _request():
    return {
        "request_id": "dwr_test",
        "request": {
            "title": "Fix the greeting",
            "problem_statement": "greet() returns the wrong string.",
            "acceptance_criteria": ["greet() returns 'hello'"],
        },
    }


@pytest.fixture
def worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def greet():\n    return 'bye'\n", encoding="utf-8")
    return tmp_path


def _message(content=None, tool_calls=None, total_tokens=10):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(total_tokens=total_tokens))


def _call(call_id, name, args):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_build_messages_marks_the_request_as_untrusted_data(worktree):
    messages = build_messages(_request(), _target())
    system = messages[0]["content"]
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    # The request is free text from a producer; it must never be read as instructions.
    assert "untrusted" in system.lower()
    assert "instructions" in system.lower()
    assert "src/**" in system


def test_build_messages_includes_the_problem_and_acceptance_criteria():
    body = build_messages(_request(), _target())[1]["content"]
    assert "greet() returns the wrong string." in body
    assert "greet() returns 'hello'" in body


def test_dispatch_tool_returns_a_tool_error_as_text_not_an_exception(worktree):
    # A refusal must go back to the model so it can correct, not kill the run.
    result = dispatch_tool("write_file", {"path": "tools/evil.py", "content": "x"},
                           worktree=worktree, target=_target())
    assert "allowed scope" in result
    assert not (worktree / "tools" / "evil.py").exists()


def test_dispatch_tool_rejects_an_unknown_tool(worktree):
    assert "unknown tool" in dispatch_tool("rm_rf", {}, worktree=worktree, target=_target()).lower()


def test_run_agent_applies_a_write_and_stops_when_the_model_finishes(worktree):
    responses = [
        _message(tool_calls=[_call("1", "write_file",
                                   {"path": "src/app.py", "content": "def greet():\n    return 'hello'\n"})]),
        _message(content="Fixed the greeting."),
    ]

    def provider_call(**kwargs):
        return responses.pop(0)

    result = run_agent(worktree=worktree, target=_target(), request=_request(),
                       provider_call=provider_call)

    assert result["stopped"] == "model-finished"
    assert result["iterations"] == 2
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "def greet():\n    return 'hello'\n"


def test_run_agent_trips_the_iteration_ceiling(worktree):
    def provider_call(**kwargs):
        # A model that never finishes must not loop forever.
        return _message(tool_calls=[_call("1", "list_files", {"pattern": "**/*"})])

    with pytest.raises(CeilingExceeded, match="iterations"):
        run_agent(worktree=worktree, target=_target(agent_max_iterations=3),
                  request=_request(), provider_call=provider_call)


def test_run_agent_passes_the_configured_model_and_tools(worktree):
    seen = {}

    def provider_call(**kwargs):
        seen.update(kwargs)
        return _message(content="done")

    run_agent(worktree=worktree, target=_target(), request=_request(), provider_call=provider_call)

    assert seen["model"] == "test/model"
    assert {s["function"]["name"] for s in seen["tools"]} == {
        "read_file", "list_files", "write_file", "run_tests"}
