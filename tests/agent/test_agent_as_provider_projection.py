"""Agent-as-provider transcript projection + skill-nudge tick.

A provider that IS an agent (junie-acp today) executes its own tools inside its
own session. Those calls never come back as pending ``tool_calls``, so two Hermes
subsystems would otherwise be blind to them:

  * the self-improvement loop, which distils skills/memories from ``messages``;
  * the skill-review nudge, whose counter only moves on Hermes tool iterations.

``splice_provider_projection`` is what closes both gaps, and
``run_conversation`` calls it on every response.
"""

from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace

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


def test_conversation_loop_applies_the_projection():
    """Guard the wiring: the helper is useless if the loop stops calling it."""
    source = open(
        os.path.join(_REPO_ROOT, "agent", "conversation_loop.py"), encoding="utf-8"
    ).read()
    assert "from agent.provider_projection import splice_provider_projection" in source
    assert re.search(r"^\s+splice_provider_projection\(agent, response, messages\)", source, re.M)
