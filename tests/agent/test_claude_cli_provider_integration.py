from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LOCALAPPDATA", os.environ.get("TEMP", r"C:\Windows\Temp"))

import pytest

from agent.agent_init import init_agent
from agent.auxiliary_client import resolve_provider_client
from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
from agent.claude_cli_client import ClaudeCLIClient
from hermes_state import SessionDB
from run_agent import AIAgent


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_claude_cli.py"


def test_agent_init_selects_claude_cli_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "success")
    db = SessionDB(tmp_path / "state.db")
    db.create_session("integration-1", "cli")
    agent = AIAgent.__new__(AIAgent)

    init_agent(
        agent,
        model="opus",
        provider="claude-cli",
        api_key="claude-cli-process",
        base_url="claude-cli://local",
        acp_command=sys.executable,
        acp_args=[str(FIXTURE)],
        session_id="integration-1",
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    assert agent.api_mode == "chat_completions"
    assert isinstance(agent.client, ClaudeCLIClient)
    assert agent.client.session_id == "integration-1"


def test_dispatch_uses_facade_without_constructing_openai_request_client():
    class Completions:
        @staticmethod
        def create(**kwargs):
            return {"messages": kwargs["messages"]}

    agent = type(
        "Agent",
        (),
        {
            "api_mode": "chat_completions",
            "provider": "claude-cli",
            "client": type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})(),
        },
    )()

    result = _dispatch_nonstreaming_api_request(
        agent,
        {"messages": [{"role": "user", "content": "hi"}]},
        make_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OpenAI client must not be constructed")
        ),
    )

    assert result == {"messages": [{"role": "user", "content": "hi"}]}


def test_auxiliary_router_returns_claude_facade_not_anthropic(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "success")
    db = SessionDB(tmp_path / "state.db")
    db.create_session("aux-1", "cli")

    client, model = resolve_provider_client(
        "claude-cli",
        model="opus",
        main_runtime={
            "command": sys.executable,
            "args": [str(FIXTURE)],
            "session_db": db,
            "session_id": "aux-1",
        },
        task="compression",
    )

    assert isinstance(client, ClaudeCLIClient)
    assert model == "opus"
    assert client.base_url == "claude-cli://local"


@pytest.mark.asyncio
async def test_auxiliary_router_supports_async_claude_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "success")
    db = SessionDB(tmp_path / "state.db")
    db.create_session("aux-async", "cli")
    client, _ = resolve_provider_client(
        "claude-cli",
        model="opus",
        async_mode=True,
        main_runtime={
            "command": sys.executable,
            "args": [str(FIXTURE)],
            "session_db": db,
            "session_id": "aux-async",
        },
        task="compression",
    )

    response = await client.chat.completions.create(
        model="opus",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )

    assert response.choices[0].message.content == "ok"
