from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import base64
import json
import sys

from run_agent import AIAgent


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=f"{platform}-session",
            platform=platform,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    # Per-call deltas are enqueued for the SessionDB background writer
    # (queue_token_counts) rather than written inline on the turn thread.
    session_db.queue_token_counts.assert_called_once()
    assert session_db.queue_token_counts.call_args.args[0] == "telegram-session"


def test_run_conversation_attributes_codex_tokens_to_account():
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-real"}
    }).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")
    agent.provider = "openai-codex"
    agent.api_key = token

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    kwargs = session_db.queue_token_counts.call_args.kwargs
    from agent.account_token_usage import codex_account_identity
    identity = codex_account_identity(token)
    assert identity is not None
    assert kwargs["account_key"] == identity.account_key
    assert token not in repr(kwargs)





def test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one(monkeypatch):
    sentinel_db = object()
    captured = {}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = FakeSessionDB
    hermes_state.get_shared_session_db = lambda db_path=None: sentinel_db
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(None, platform="acp")
    result = json.loads(agent._invoke_tool(
        "session_search",
        {"query": "Hermes", "detail": "full"},
        "task-id",
    ))

    assert result["success"] is True
    assert captured["db"] is sentinel_db
    assert captured["query"] == "Hermes"
    assert captured["detail"] == "full"
    assert agent._session_db is sentinel_db


def test_sequential_session_search_forwards_detail(monkeypatch):
    session_db = MagicMock()
    captured = {}

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(session_db, platform="acp")
    tool_call = SimpleNamespace(
        id="search-1",
        function=SimpleNamespace(
            name="session_search",
            arguments=json.dumps({"query": "Hermes", "detail": "full"}),
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = []

    agent._execute_tool_calls_sequential(
        assistant_message,
        messages,
        "task-id",
    )

    assert captured["db"] is session_db
    assert captured["query"] == "Hermes"
    assert captured["detail"] == "full"
