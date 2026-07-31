"""Agent-as-provider transcript projection + skill-nudge tick.

A provider that IS an agent (junie-acp today) executes its own tools inside its
own session. Those calls never come back as pending ``tool_calls``, so two Hermes
subsystems would otherwise be blind to them:

  * the self-improvement loop, which distils skills/memories from ``messages``;
  * the skill-review nudge, whose counter only moves on Hermes tool iterations.

``splice_provider_projection`` is what closes both gaps. The helper is unit
tested here, and the wiring is exercised for real: the last three tests drive a
whole ``AIAgent.run_conversation`` turn against an in-process fake ACP client and
assert on the resulting transcript and counters, so they fail if the loop ever
stops applying the projection.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.provider_projection import splice_provider_projection  # noqa: E402

_PROJECTED = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "junie_edit", "arguments": '{"path": "main.py"}'},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "t1",
        "name": "junie_edit",
        "content": "1 file changed",
    },
]


def _agent(iters: int = 0) -> SimpleNamespace:
    return SimpleNamespace(provider="junie-acp", _iters_since_skill=iters)


def _response(**attrs):
    return SimpleNamespace(**attrs)


def test_projected_rows_are_appended_and_the_nudge_ticks():
    agent = _agent()
    messages = [{"role": "user", "content": "edit main.py"}]
    spliced = splice_provider_projection(
        agent,
        _response(hermes_projected_messages=_PROJECTED, hermes_provider_tool_iterations=1),
        messages,
    )
    assert spliced == 2
    assert messages[1]["tool_calls"][0]["function"]["name"] == "junie_edit"
    assert messages[2]["content"] == "1 file changed"
    assert agent._iters_since_skill == 1


def test_iterations_accumulate_across_calls():
    agent = _agent(iters=2)
    splice_provider_projection(
        agent, _response(hermes_provider_tool_iterations=3), []
    )
    assert agent._iters_since_skill == 5


def test_ordinary_provider_response_is_a_no_op():
    agent = _agent(iters=1)
    messages = [{"role": "user", "content": "hi"}]
    # A normal OpenAI completion carries neither attribute.
    assert splice_provider_projection(agent, SimpleNamespace(choices=[]), messages) == 0
    assert messages == [{"role": "user", "content": "hi"}]
    assert agent._iters_since_skill == 1


def test_garbage_attributes_cannot_break_the_turn():
    agent = _agent()
    messages: list = []
    assert splice_provider_projection(
        agent,
        _response(
            hermes_projected_messages="not-a-list",
            hermes_provider_tool_iterations="lots",
        ),
        messages,
    ) == 0
    assert messages == []
    assert agent._iters_since_skill == 0

    # A list with non-dict entries keeps only the usable rows.
    assert splice_provider_projection(
        agent,
        _response(hermes_projected_messages=[{"role": "tool", "content": "ok"}, "junk", None]),
        messages,
    ) == 1
    assert messages == [{"role": "tool", "content": "ok"}]


class _FakeACPClient:
    """An agent-as-provider client: one canned completion, no subprocess.

    Shaped exactly like what ``JunieACPClient._create_chat_completion`` returns,
    including the two provider-projection attributes the loop consumes.
    """

    SUPPORTS_HERMES_TOOL_CALLS = True

    def __init__(self, completion):
        self.api_key = "junie-acp"
        self.base_url = "acp://junie"
        self.is_closed = False
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kw: completion)
        )

    def close(self):
        self.is_closed = True


def _completion(*, projected, iterations):
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    message = SimpleNamespace(
        content="Edited main.py.",
        tool_calls=[],
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
        model="junie-acp",
        hermes_projected_messages=projected,
        hermes_provider_tool_iterations=iterations,
    )


def _run_turn(request, monkeypatch, tmp_path, *, projected, iterations):
    """Drive one real ``run_conversation`` turn against the fake ACP client."""
    # Isolate config/session state, and restore it after the test — a leaked
    # HERMES_HOME would silently reconfigure every later test in the session.
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    # Building an agent publishes process-global "runtime main" state
    # (agent/auxiliary_client.py). Restore it after the turn or every later test
    # in the session resolves junie-acp as its main provider.
    from agent.auxiliary_client import clear_runtime_main

    request.addfinalizer(clear_runtime_main)

    client = _FakeACPClient(_completion(projected=projected, iterations=iterations))
    with patch("agent.junie_acp_client.JunieACPClient", return_value=client):
        agent = AIAgent(
            api_key="junie-acp",
            base_url="acp://junie",
            provider="junie-acp",
            acp_command="junie",
            acp_args=["--acp=true"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        # The client is built lazily on first use, so keep the fake in place for
        # the whole turn (otherwise a real subprocess spawn is attempted).
        agent.client = client
        try:
            result = agent.run_conversation("edit main.py")
        finally:
            with patch.object(agent, "_spawn_background_review", lambda *a, **k: None):
                agent.close()
    return agent, result


def test_provider_work_lands_in_the_transcript_through_the_real_loop(request, monkeypatch, tmp_path):
    """The loop must splice the provider's projection — exercised, not asserted
    against source text."""
    agent, result = _run_turn(request, monkeypatch, tmp_path, projected=_PROJECTED, iterations=1)

    roles = [(m.get("role"), m.get("name")) for m in result["messages"] if isinstance(m, dict)]
    assert ("tool", "junie_edit") in roles, roles
    tool_rows = [
        m for m in result["messages"]
        if isinstance(m, dict) and m.get("name") == "junie_edit" and m.get("role") == "tool"
    ]
    assert "1 file changed" in tool_rows[0]["content"]

    # The projected call precedes its result, and both precede the final answer.
    idx_call = next(
        i for i, m in enumerate(result["messages"])
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    )
    idx_result = result["messages"].index(tool_rows[0])
    assert idx_call < idx_result < len(result["messages"]) - 1


def test_provider_iterations_tick_the_skill_nudge_through_the_real_loop(request, monkeypatch, tmp_path):
    """Isolate our contribution from the loop's own per-iteration bump by
    running the same turn with and without provider iterations."""
    agent_with, _ = _run_turn(request, monkeypatch, tmp_path / "a", projected=_PROJECTED, iterations=1)
    agent_without, _ = _run_turn(request, monkeypatch, tmp_path / "b", projected=[], iterations=0)
    assert agent_with._iters_since_skill - agent_without._iters_since_skill == 1


def test_ordinary_provider_turn_is_unchanged(request, monkeypatch, tmp_path):
    """A completion without the attributes must not gain rows or counter ticks."""
    _agent, result = _run_turn(request, monkeypatch, tmp_path, projected=[], iterations=0)
    assert not [
        m for m in result["messages"]
        if isinstance(m, dict) and str(m.get("name") or "").startswith("junie_")
    ]
