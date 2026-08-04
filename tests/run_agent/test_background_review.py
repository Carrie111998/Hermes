"""Regression tests for background review agent cleanup."""

from __future__ import annotations

import types
import pytest
import run_agent as run_agent_module
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.model = "fake-model"
    agent.platform = "telegram"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = "test-session"
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent._memory_store = object()
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._cached_system_prompt = "test-cached-system-prompt"
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.background_review_callback = None
    agent.status_callback = None
    agent._safe_print = lambda *_args, **_kwargs: None
    agent._session_messages = []
    # Required by _run_agent_tool_execution_middleware used in dispatch-seam tests.
    agent.quiet_mode = True
    agent._current_tool = None
    agent._touch_activity = lambda *_a, **_kw: None
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent._memory_manager = None
    agent._turns_since_memory = 0
    agent._iters_since_skill = 0
    agent._should_emit_quiet_tool_messages = False
    agent._guardrail_block_result = lambda _d: '{"blocked": true}'
    agent._tool_guardrails = types.SimpleNamespace(
        before_call=lambda tn, a: types.SimpleNamespace(allows_execution=True)
    )
    return agent


class ImmediateThread:
    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _dispatch(parent_agent, tool_name, args):
    """Route one tool call through the production dispatch seam.

    Returns (blocked: bool, sentinel_called: bool). The sentinel stands in for
    the real tool handler; it must NOT be called when the guard blocks.
    """
    from agent import tool_executor as _te
    sentinel = []
    result = _te._run_agent_tool_execution_middleware(
        parent_agent,
        function_name=tool_name,
        function_args=args,
        effective_task_id="",
        tool_call_id="",
        execute=lambda final_args: sentinel.append(final_args) or '{"ok": true}',
    )
    return result.blocked, bool(sentinel)


def test_background_review_shuts_down_memory_provider_before_close(monkeypatch):
    events = []

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            events.append(("init", kwargs))
            self._session_messages = []

        def run_conversation(self, **kwargs):
            events.append(("run_conversation", kwargs))

        def shutdown_memory_provider(self):
            events.append(("shutdown_memory_provider", None))

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert [name for name, _payload in events] == [
        "init",
        "run_conversation",
        "shutdown_memory_provider",
        "close",
    ]


def test_background_review_fork_opts_out_of_session_finalization(monkeypatch):
    """The review fork shares the parent's live session_id, so it must set
    ``_end_session_on_close = False``. Otherwise close() (now finalizing owned
    session rows) would end the still-active parent session mid-conversation
    every time the review fires (~every 10 turns). Regression for #12029.
    """
    seen = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []
            # Default matches AIAgent.__init__ (agent_init.py): owns its row.
            self._end_session_on_close = True

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "_end_session_on_close":
                seen["end_session_on_close"] = value

        def run_conversation(self, **kwargs):
            # By the time the fork runs, the opt-out must already be applied.
            seen["at_run_time"] = self._end_session_on_close

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    assert seen.get("end_session_on_close") is False
    assert seen.get("at_run_time") is False










# ---------------------------------------------------------------------------
# memory_notifications mode: off | on | verbose
# ---------------------------------------------------------------------------

import json as _json

from agent.background_review import summarize_background_review_actions


def _memory_add_review():
    """A minimal review transcript: one memory add (assistant call + tool result)."""
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_mem1",
                    "function": {
                        "name": "memory",
                        "arguments": _json.dumps(
                            {
                                "action": "add",
                                "target": "memory",
                                "content": "User prefers terse replies",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_mem1",
            "content": _json.dumps(
                {"success": True, "message": "Entry added.", "target": "memory"}
            ),
        },
    ]


def _skill_patch_review():
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_skill1",
                    "function": {
                        "name": "skill_manage",
                        "arguments": _json.dumps(
                            {"action": "patch", "name": "demo", "old_string": "a", "new_string": "b"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_skill1",
            "content": _json.dumps(
                {
                    "success": True,
                    "message": "Patched SKILL.md in skill 'demo' (1 replacement).",
                    "_change": {"old": "a", "new": "b"},
                }
            ),
        },
    ]


def test_memory_notifications_off_returns_nothing():
    actions = summarize_background_review_actions(
        _memory_add_review(), [], notification_mode="off"
    )
    assert actions == []








def test_skill_patch_off_silent_verbose_shows_diff():
    assert (
        summarize_background_review_actions(
            _skill_patch_review(), [], notification_mode="off"
        )
        == []
    )
    verbose = summarize_background_review_actions(
        _skill_patch_review(), [], notification_mode="verbose"
    )
    assert len(verbose) == 1
    assert "demo" in verbose[0] and "→" in verbose[0]


def test_background_review_allows_memory_write_when_parent_state_matches(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            blocked, sentinel_called = _dispatch(agent, "memory", {"action": "add"})
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = [{"content": "hello", "role": "user"}]

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is False
    assert observed["sentinel_called"]


def test_background_review_blocks_memory_and_skill_writes_when_parent_state_changes(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            agent._session_messages.append(
                {"role": "assistant", "content": "new foreground reply"}
            )
            observed["memory"] = _dispatch(agent, "memory", {"action": "add"})
            observed["skill"] = _dispatch(agent, "skill_manage", {"action": "patch"})
            observed["skill_view"] = _dispatch(agent, "skill_view", {"name": "python"})
            observed["skills_list"] = _dispatch(agent, "skills_list", {})

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    memory_blocked, memory_sentinel = observed["memory"]
    assert memory_blocked is True
    assert not memory_sentinel

    skill_blocked, skill_sentinel = observed["skill"]
    assert skill_blocked is True
    assert not skill_sentinel

    skill_view_blocked, skill_view_sentinel = observed["skill_view"]
    assert skill_view_blocked is False
    assert skill_view_sentinel

    skills_list_blocked, skills_list_sentinel = observed["skills_list"]
    assert skills_list_blocked is False
    assert skills_list_sentinel


@pytest.mark.parametrize("action", [
    "create", "patch", "edit", "delete", "write_file", "remove_file",
])
def test_background_review_blocks_all_skill_manage_write_actions_when_parent_state_changes(
    monkeypatch, action
):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            agent._session_messages.append(
                {"role": "assistant", "content": "foreground advanced"}
            )
            blocked, sentinel_called = _dispatch(
                agent, "skill_manage", {"action": action, "name": "demo"}
            )
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is True, f"action={action} was not blocked"
    assert not observed["sentinel_called"], f"action={action}: sentinel must not fire on block"


@pytest.mark.parametrize("operations", [
    [{"action": "add", "key": "x", "value": "y"}],
    [{"action": "replace", "key": "x", "value": "z"}],
    [{"action": "remove", "key": "x"}],
    [{"action": "add", "key": "a"}, {"action": "remove", "key": "b"}],
])
def test_background_review_blocks_memory_batch_write_when_parent_state_changes(
    monkeypatch, operations
):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            agent._session_messages.append(
                {"role": "assistant", "content": "foreground advanced"}
            )
            blocked, sentinel_called = _dispatch(agent, "memory", {"operations": operations})
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is True
    assert not observed["sentinel_called"]


def test_background_review_allows_memory_batch_write_when_parent_state_matches(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            blocked, sentinel_called = _dispatch(
                agent,
                "memory",
                {"operations": [{"action": "add", "key": "x", "value": "y"}]},
            )
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = [{"content": "hello", "role": "user"}]

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is False
    assert observed["sentinel_called"]


@pytest.mark.parametrize("bad_state,label", [
    (None, "none"),
    ({}, "dict"),
    ("not a list", "string"),
    (["not a dict"], "list_with_non_dict"),
])
def test_background_review_blocks_write_when_parent_state_invalid(
    monkeypatch, bad_state, label
):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            agent._session_messages = bad_state
            blocked_mem, _ = _dispatch(
                agent,
                "memory",
                {"operations": [{"action": "add", "key": "x", "value": "y"}]},
            )
            blocked_skill, _ = _dispatch(agent, "skill_manage", {"action": "edit"})
            observed["memory_blocked"] = blocked_mem
            observed["skill_blocked"] = blocked_skill

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["memory_blocked"] is True, label
    assert observed["skill_blocked"] is True, label


def test_background_review_blocks_write_when_session_messages_absent(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            if hasattr(agent, "_session_messages"):
                del agent._session_messages
            blocked, sentinel_called = _dispatch(agent, "memory", {"action": "add"})
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is True
    assert not observed["sentinel_called"]


def test_background_review_allows_write_for_empty_matching_parent(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            # Both snapshot and live state are valid empty lists; tokens match.
            blocked, sentinel_called = _dispatch(agent, "memory", {"action": "add"})
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot: list = []
    agent = _bare_agent()
    agent._session_messages = []

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is False
    assert observed["sentinel_called"]


def test_background_review_blocks_write_on_tool_calls_field_drift(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            # Parent's message gains a tool_calls field after the snapshot was taken.
            agent._session_messages[-1]["tool_calls"] = [
                {"id": "c1", "function": {"name": "memory", "arguments": "{}"}}
            ]
            blocked, sentinel_called = _dispatch(agent, "memory", {"action": "add"})
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = [{"role": "user", "content": "hello"}]

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is True
    assert not observed["sentinel_called"]


def test_background_review_whitelist_denies_before_callback_for_non_whitelisted_tool(monkeypatch):
    from hermes_cli import plugins as _plugins

    observed: dict = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            # write_file is not in the review whitelist. Parent state MATCHES,
            # so the callback would allow — but the whitelist fires first and denies.
            blocked, sentinel_called = _dispatch(
                agent, "write_file", {"path": "x.txt", "content": "y"}
            )
            observed["blocked"] = blocked
            observed["sentinel_called"] = sentinel_called

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(_plugins, "invoke_hook", lambda hook_name, **kwargs: [])

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    assert observed["blocked"] is True
    assert not observed["sentinel_called"]


def test_background_review_clears_block_callback_on_completion(monkeypatch):
    from hermes_cli import plugins as _plugins

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            # Callback must be callable while the review is executing.
            assert callable(
                getattr(_plugins._thread_tool_whitelist, "block_callback", None)
            ), "block_callback should be callable during review"

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    messages_snapshot = [{"role": "user", "content": "hello"}]
    agent = _bare_agent()
    agent._session_messages = list(messages_snapshot)

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=messages_snapshot,
        review_memory=True,
    )

    post_callback = getattr(_plugins._thread_tool_whitelist, "block_callback", None)
    assert post_callback is None, "block_callback must be cleared after review completes"
