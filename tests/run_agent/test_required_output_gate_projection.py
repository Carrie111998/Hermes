"""Regression coverage for Host-owned assistant output projection.

The required persist gate owns the only user-visible assistant body.  Internal
length-continuation turns and model text attached to a pre-authorization tool
call must therefore never enter the durable/user-visible transcript.
"""

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.tool_dispatch_helpers import make_tool_result_message
from hermes_state import SessionDB
from run_agent import AIAgent


def _tool_defs() -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": "terminal_gate",
            "description": "terminal Host gate",
            "parameters": {"type": "object", "properties": {}},
        },
    }]


def _agent() -> AIAgent:
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-gated-output-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
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
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _response(*, content: str, finish_reason: str, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _tool_call():
    return SimpleNamespace(
        id="close-1",
        type="function",
        function=SimpleNamespace(name="terminal_gate", arguments="{}"),
    )


def _attach_db(agent: AIAgent, db_path: Path, session_id: str) -> SessionDB:
    db = SessionDB(db_path=db_path)
    db.create_session(session_id=session_id, source="desktop", model="test/model")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    return db


def test_gate_hides_length_nudge_and_pre_authorization_tool_text(tmp_path):
    """One final authorized answer survives; both private drafts stay absent."""
    agent = _agent()
    db_path = tmp_path / "state.db"
    session_id = "gated-length-tool-turn"
    db = _attach_db(agent, db_path, session_id)
    agent.interim_assistant_callback = MagicMock()
    agent.client.chat.completions.create.side_effect = [
        _response(content="", finish_reason="length"),
        _response(
            content="untrusted answer before terminal authorization",
            finish_reason="tool_calls",
            tool_calls=[_tool_call()],
        ),
        _response(content="model terminal draft", finish_reason="stop"),
    ]

    receipts: list[dict] = []

    def _required_hook(name: str, **kwargs):
        if name == "assistant_persist_gate":
            return {
                "action": "ALLOW",
                "content": "authorized answer",
            }
        if name == "assistant_persist_receipt":
            receipts.append(kwargs)
            return {"action": "COMMITTED", "message_id": kwargs["message_id"]}
        return None

    def _execute(_assistant, messages, _task_id, _api_call_count=0):
        messages.append(
            make_tool_result_message("terminal_gate", "accepted", "close-1")
        )

    try:
        with (
            patch(
                "hermes_cli.lifecycle.has_applicable_required_hook",
                side_effect=lambda name, **_: name == "assistant_persist_gate",
            ),
            patch(
                "hermes_cli.lifecycle.invoke_required_hook",
                side_effect=_required_hook,
            ),
            patch.object(agent, "_execute_tool_calls", side_effect=_execute),
        ):
            result = agent.run_conversation("explain price earnings ratio")

        durable = db.get_messages_as_conversation(session_id)
    finally:
        db.close()

    durable_text = "\n".join(str(message.get("content") or "") for message in durable)
    assert "Your previous response was truncated" not in durable_text
    assert "untrusted answer before terminal authorization" not in durable_text
    assert "model terminal draft" not in durable_text

    tool_turns = [message for message in durable if message.get("tool_calls")]
    assert len(tool_turns) == 1
    assert (tool_turns[0].get("content") or "") == ""
    assert durable[-1]["role"] == "assistant"
    assert durable[-1]["content"] == "authorized answer"
    assert result["final_response"] == "authorized answer"
    assert receipts and receipts[-1]["persisted_content"] == "authorized answer"
    agent.interim_assistant_callback.assert_not_called()
