"""Regression coverage for durable mid-turn ``/steer`` mutations.

Tool results are flushed to SQLite before a pending steer may append its marker
to the live message. The later flush must update that row rather than skip it
or insert a duplicate. The executor tests cover the mutation sites:
concurrent/sequential batch-end drains (the only in-batch delivery — upstream
deliberately keeps /steer pending until after aggregate budget enforcement)
and the pre-API drain.
"""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.prompt_builder import STEER_MARKER_OPEN
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.budget_config import BudgetConfig
from tools.tool_result_storage import enforce_turn_budget

STEER_TEXT = "please prefer the smaller fix"
SESSION_ID = "steer-db-test"


def _tool_call_data(call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "web_search", "arguments": "{}"},
    }


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent(session_db, hermes_home: Path, session_id=SESSION_ID):
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=session_id,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._ensure_db_session()
    return agent


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "t.db")
    try:
        yield session_db
    finally:
        session_db.close()


@pytest.fixture
def agent(db, tmp_path):
    return _make_agent(db, tmp_path / "hermes-home")


def _mock_tool_call(call_id="c1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="web_search", arguments="{}"),
    )


def _mock_response(content="", finish_reason="stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _tool_rows(db, session_id=SESSION_ID):
    return [
        row for row in db.get_messages(session_id) if row.get("role") == "tool"
    ]


def _assert_single_steered_row(db, live_tool_msg):
    rows = _tool_rows(db)
    assert len(rows) == 1
    assert STEER_MARKER_OPEN in rows[0]["content"]
    assert STEER_TEXT in rows[0]["content"]
    assert rows[0]["content"] == live_tool_msg["content"]


def _execute_tool_batch(agent, messages, mode, *, budget_hook=None):
    assistant = SimpleNamespace(
        content="",
        tool_calls=[_mock_tool_call()],
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "agent.tool_executor.maybe_persist_tool_result",
                side_effect=lambda **kwargs: kwargs["content"],
            )
        )
        if budget_hook is not None:
            stack.enter_context(
                patch(
                    "agent.tool_executor.enforce_turn_budget",
                    side_effect=budget_hook,
                )
            )
        if mode == "concurrent":
            stack.enter_context(
                patch.object(
                    agent,
                    "_invoke_tool",
                    side_effect=lambda *_args, **_kwargs: "search result",
                )
            )
            executor = agent._execute_tool_calls_concurrent
        else:
            stack.enter_context(
                patch(
                    "run_agent.handle_function_call",
                    side_effect=lambda *_args, **_kwargs: "search result",
                )
            )
            executor = agent._execute_tool_calls_sequential
        executor(assistant, messages, "task-1")


def _seed_tool_history(db, *tool_contents, final_answer=None):
    db.create_session(SESSION_ID, source="cli")
    db.append_message(
        SESSION_ID,
        "assistant",
        "",
        tool_calls=[_tool_call_data()],
    )
    for content in tool_contents:
        db.append_message(
            SESSION_ID,
            "tool",
            content,
            tool_name="web_search",
            tool_call_id="c1",
        )
    if final_answer is not None:
        db.append_message(SESSION_ID, "assistant", final_answer)


@pytest.mark.parametrize("mode", ["concurrent", "sequential"])
def test_pre_batch_steer_persists(db, agent, mode):
    """A steer queued before the batch is delivered at the batch-end drain
    (after aggregate budget enforcement) and persisted in place."""
    messages = []
    agent.steer(STEER_TEXT)

    _execute_tool_batch(agent, messages, mode)

    assert STEER_MARKER_OPEN in messages[-1]["content"]
    agent._flush_messages_to_session_db(messages)
    _assert_single_steered_row(db, messages[-1])


@pytest.mark.parametrize("mode", ["concurrent", "sequential"])
def test_batch_end_drain_persists_steer(db, agent, mode):
    messages = []

    def _set_steer_at_budget(*_args, **_kwargs):
        agent.steer(STEER_TEXT)

    _execute_tool_batch(agent, messages, mode, budget_hook=_set_steer_at_budget)

    assert STEER_MARKER_OPEN in messages[-1]["content"]
    assert agent._pending_steer is None
    agent._flush_messages_to_session_db(messages)
    _assert_single_steered_row(db, messages[-1])


@pytest.mark.parametrize("mode", ["concurrent", "sequential"])
def test_batch_end_steer_lands_after_aggregate_budget(db, agent, mode):
    """Upstream keeps /steer pending until after aggregate budget
    enforcement, so the budget rewrite and the steer append are two
    independent post-flush mutations. Both must reach the durable rows:
    the budget-externalized content via its pending flag, and the steer
    marker appended on top of the last (post-budget) tool result."""
    messages = []
    assistant = SimpleNamespace(
        content="",
        tool_calls=[_mock_tool_call("c1"), _mock_tool_call("c2")],
    )
    budget = BudgetConfig(
        # High per-tool threshold keeps Layer-2 per-tool persistence out of
        # the picture so only the Layer-3 aggregate budget mutates messages.
        default_result_size=100_000,
        turn_budget=1_500,
        preview_size=100,
    )
    agent.steer(STEER_TEXT)

    with ExitStack() as stack:
        stack.enter_context(
            patch("agent.tool_executor._budget_for_agent", return_value=budget)
        )
        stack.enter_context(
            patch("agent.tool_executor.get_active_env", return_value=None)
        )
        if mode == "concurrent":
            stack.enter_context(
                patch.object(agent, "_invoke_tool", return_value="x" * 900)
            )
            executor = agent._execute_tool_calls_concurrent
        else:
            stack.enter_context(
                patch("run_agent.handle_function_call", return_value="x" * 900)
            )
            executor = agent._execute_tool_calls_sequential
        executor(assistant, messages, "task-1")

    assert len(messages) == 2
    steered = next(msg for msg in messages if STEER_MARKER_OPEN in msg["content"])
    assert STEER_TEXT in steered["content"]
    # The steer marker is the final segment of the message — proof it was
    # appended AFTER the aggregate budget pass, not protected through it.
    assert STEER_TEXT in steered["content"].split(STEER_MARKER_OPEN)[-1]
    assert steered["_db_content_update_pending"] is True
    # Exactly one sibling was externalized by the aggregate budget AFTER the
    # incremental flush — the second post-flush mutation.
    budget_rewritten = [msg for msg in messages if "x" * 900 not in msg["content"]]
    assert len(budget_rewritten) == 1
    assert budget_rewritten[0]["_db_content_update_pending"] is True
    assert agent._pending_steer is None

    assert agent._flush_messages_to_session_db(messages) is True
    rows = _tool_rows(db)
    assert len(rows) == 2
    assert [row["content"] for row in rows] == [msg["content"] for msg in messages]
    assert any(STEER_MARKER_OPEN in row["content"] for row in rows)
    assert all(
        msg.get("_db_content_update_pending") is None for msg in messages
    )


def test_steer_update_reindexes_fts_search(db, agent):
    if not db._fts_enabled:
        pytest.skip("FTS5 unavailable in this sqlite build")

    tool_msg = {
        "role": "tool",
        "content": "tool result",
        "tool_call_id": "c1",
        "tool_name": "web_search",
    }
    messages = [tool_msg]
    agent._flush_messages_to_session_db(messages)
    assert db.search_messages("smaller", role_filter=["tool"]) == []

    agent.steer(STEER_TEXT)
    agent._apply_pending_steer_to_tool_results(messages, 1)
    agent._flush_messages_to_session_db(messages)

    hits = db.search_messages("smaller", role_filter=["tool"])
    assert len(hits) == 1
    assert hits[0]["session_id"] == SESSION_ID
    assert "smaller" in hits[0]["snippet"]
    _assert_single_steered_row(db, tool_msg)


def test_sequence_repair_does_not_rewrite_durable_rows(db, agent):
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    agent._flush_messages_to_session_db(messages)

    assert agent._repair_message_sequence(messages) == 1
    assert [message["content"] for message in messages] == ["first\n\nsecond"]
    agent._flush_messages_to_session_db(messages)

    assert [message["content"] for message in db.get_messages(SESSION_ID)] == [
        "first",
        "second",
    ]


def test_cold_resumed_pre_api_drain_updates_exact_tool_row(db, tmp_path):
    _seed_tool_history(db, "old result", final_answer="old final answer")
    loaded = db.get_messages_as_conversation(
        SESSION_ID,
        repair_alternation=True,
        include_row_ids=True,
    )
    agent = _make_agent(db, tmp_path / "hermes-home")
    requests = []

    def _create(**kwargs):
        requests.append(kwargs["messages"])
        if len(requests) == 1:
            agent.steer(STEER_TEXT)
            return _mock_response(
                content="<REASONING_SCRATCHPAD>still thinking",
                finish_reason="stop",
            )
        return _mock_response(content="done", finish_reason="stop")

    agent.client.chat.completions.create.side_effect = _create
    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_spawn_background_review"),
    ):
        result = agent.run_conversation(
            "new question",
            conversation_history=loaded,
        )

    historical_tool = next(
        message for message in loaded if message.get("role") == "tool"
    )
    assert result["final_response"] == "done"
    assert len(requests) == 2
    assert STEER_MARKER_OPEN in next(
        message["content"]
        for message in requests[1]
        if message.get("role") == "tool"
    )
    _assert_single_steered_row(db, historical_tool)
    assert historical_tool.get("_db_content_update_pending") is None
    assert isinstance(historical_tool.get("_row_id"), int)


def test_duplicate_tool_id_updates_exact_repaired_row(db, agent):
    _seed_tool_history(db, "first result", "retry duplicate")
    loaded = db.get_messages_as_conversation(
        SESSION_ID,
        repair_alternation=True,
        include_row_ids=True,
    )
    live_tool = next(message for message in loaded if message.get("role") == "tool")
    assert live_tool["content"] == "first result"
    assert isinstance(live_tool.get("_row_id"), int)

    agent.steer(STEER_TEXT)
    agent._apply_pending_steer_to_tool_results(loaded, 1)
    assert agent._flush_messages_to_session_db(
        loaded,
        conversation_history=loaded,
    )

    durable_tools = _tool_rows(db)
    assert STEER_TEXT in durable_tools[0]["content"]
    assert durable_tools[1]["content"] == "retry duplicate"
    repaired_again = db.get_messages_as_conversation(
        SESSION_ID,
        repair_alternation=True,
    )
    assert STEER_TEXT in next(
        message["content"]
        for message in repaired_again
        if message.get("role") == "tool"
    )


def test_duplicate_tool_id_without_row_metadata_fails_closed(db, agent):
    _seed_tool_history(db, "first result", "retry duplicate")
    loaded = db.get_messages_as_conversation(SESSION_ID)
    assert agent._repair_message_sequence(loaded) == 1
    live_tool = next(message for message in loaded if message.get("role") == "tool")
    assert "_row_id" not in live_tool

    agent.steer(STEER_TEXT)
    agent._apply_pending_steer_to_tool_results(loaded, 1)
    assert not agent._flush_messages_to_session_db(
        loaded,
        conversation_history=loaded,
    )

    assert [row["content"] for row in _tool_rows(db)] == [
        "first result",
        "retry duplicate",
    ]
    assert live_tool["_db_content_update_pending"] is True


def test_rewritten_tool_row_falls_back_from_stale_row_id(db, agent):
    tool_msg = {
        "role": "tool",
        "content": "old result",
        "tool_call_id": "c1",
        "tool_name": "web_search",
    }
    messages = [tool_msg]
    agent._flush_messages_to_session_db(messages)
    old_row_id = tool_msg["_row_id"]
    db.replace_messages(SESSION_ID, messages)
    assert _tool_rows(db)[0]["id"] != old_row_id

    agent.steer(STEER_TEXT)
    agent._apply_pending_steer_to_tool_results(messages, 1)
    agent._flush_messages_to_session_db(messages)

    _assert_single_steered_row(db, tool_msg)
    assert tool_msg["_row_id"] == _tool_rows(db)[0]["id"]


def test_aggregate_budget_rewrite_updates_durable_row(db, agent):
    """Aggregate budget enforcement rewrites tool content AFTER the per-tool
    flush (agent/tool_executor.py flushes each result, then enforce_turn_budget
    externalizes oversized ones in place). The next flush must UPDATE the
    durable row rather than leave the pre-budget content behind."""
    original = "x" * 250_000
    tool_msg = {
        "role": "tool",
        "content": original,
        "tool_call_id": "c1",
        "tool_name": "web_search",
    }
    messages = [tool_msg]
    assert agent._flush_messages_to_session_db(messages) is True

    enforce_turn_budget(messages, env=None, config=BudgetConfig(turn_budget=200_000))
    assert tool_msg["content"] != original
    assert tool_msg["_db_content_update_pending"] is True

    assert agent._flush_messages_to_session_db(messages) is True
    rows = _tool_rows(db)
    assert len(rows) == 1
    assert rows[0]["content"] == tool_msg["content"]
    assert tool_msg.get("_db_content_update_pending") is None


def test_steer_update_respects_compression_lease_and_retries(db, agent):
    tool_msg = {
        "role": "tool",
        "content": "old result",
        "tool_call_id": "c1",
        "tool_name": "web_search",
    }
    messages = [tool_msg]
    agent._flush_messages_to_session_db(messages)
    agent.steer(STEER_TEXT)
    agent._apply_pending_steer_to_tool_results(messages, 1)

    assert db.try_acquire_compression_lock(
        SESSION_ID,
        "foreign-writer",
        ttl_seconds=60,
    )
    assert agent._flush_messages_to_session_db(messages) is False
    assert _tool_rows(db)[0]["content"] == "old result"
    assert tool_msg["_db_content_update_pending"] is True

    db.release_compression_lock(SESSION_ID, "foreign-writer")
    assert agent._flush_messages_to_session_db(messages) is True
    _assert_single_steered_row(db, tool_msg)
    assert tool_msg.get("_db_content_update_pending") is None
