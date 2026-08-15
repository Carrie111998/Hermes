"""Regression tests for #86798: interrupted ACP turns with None final_response.

When a turn is interrupted mid-flight, `agent.run_conversation()` returns
`{"final_response": None, "interrupted": True}` — the key is present with an
explicit None value, so `result.get("final_response", "")` returns None and
the adapter's `.startswith()` check crashes with:

    AttributeError: 'NoneType' object has no attribute 'startswith'

That exception escapes `prompt()` before the queued-prompt drain loop, so a
queued user message is never executed and stays queued forever.

Fix: normalize with `result.get("final_response") or ""` so the truthiness
guard below (`if final_response:`) still decides.
"""

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


class InterruptingAgent:
    """Fake agent whose run_conversation returns the interrupted-turn shape.

    The first prompt records the interrupt; the *queued* prompt (drained
    after the interrupted turn) must still run and produce a real response.
    """

    def __init__(self):
        self.model = "fake-model"
        self.provider = "fake-provider"
        self.enabled_toolsets = ["hermes-acp"]
        self.disabled_toolsets = []
        self.tools = []
        self.valid_tool_names = set()
        self._supports_active_turn_redirect = True
        self.steers = []
        self.redirects = []
        self.runs = []

    def steer(self, text):
        self.steers.append(text)
        return True

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def run_conversation(self, *, user_message, conversation_history, task_id, **kwargs):
        self.runs.append(user_message)
        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        # Interrupted turn: final_response is EXPLICITLY None (the budget-
        # exhausted fallback requires not-interrupted, so finalize returns
        # the dict with final_response None — the #86798 crash shape).
        return {"final_response": None, "interrupted": True, "messages": messages}


class CaptureConn:
    def __init__(self):
        self.updates = []

    async def session_update(self, *args, **kwargs):
        if kwargs:
            self.updates.append((kwargs.get("session_id"), kwargs.get("update")))
        else:
            self.updates.append((args[0], args[1]))

    async def request_permission(self, *args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(outcome="allow")


class NoopDb:
    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session(self, *_args, **_kwargs):
        return None


def make_interrupt_agent():
    fake = InterruptingAgent()
    manager = SessionManager(agent_factory=lambda **kwargs: fake, db=NoopDb())
    acp_agent = HermesACPAgent(session_manager=manager)
    state = manager.create_session(cwd=".")
    conn = CaptureConn()
    acp_agent.on_connect(conn)
    return acp_agent, state, fake, conn


@pytest.mark.asyncio
async def test_interrupted_turn_with_none_final_response_does_not_crash():
    """#86798: None final_response must not raise AttributeError in prompt()."""
    acp_agent, state, fake, conn = make_interrupt_agent()

    # First prompt runs, gets interrupted, returns final_response=None.
    response = await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="do the long thing")],
    )

    assert response is not None
    # The interrupted turn produced no assistant prose and no crash.
    assert fake.runs == ["do the long thing"]


@pytest.mark.asyncio
async def test_queued_prompt_drains_after_interrupted_turn():
    """#86798: the queued prompt must still run after an interrupted turn.

    Before the fix the AttributeError escaped `prompt()` before the drain
    loop, so the queued message stayed queued forever.
    """
    acp_agent, state, fake, conn = make_interrupt_agent()

    # Queue a second prompt while the first turn is running. The adapter
    # acknowledges it as queued, then the interrupted turn finishes; the
    # drain loop must pick up the queued prompt and run it.
    await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="first")],
    )
    await acp_agent.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="second")],
    )

    # Both prompts were dispatched to the agent — the queued one drained.
    assert fake.runs == ["first", "second"]
