"""Tests for per-message token_count persistence (epic #1, ticket #2).

The ``messages.token_count`` column exists in the schema but no code path
ever populated it — every assistant message row was written with NULL.
This suite pins the fix contract:

1. ``build_assistant_message`` stamps ``token_count`` on the assistant
   message dict from the normalized response usage (output tokens).
2. The flush path (``_flush_messages_to_session_db``) passes the stamped
   value through to the batched session-DB write.
3. The session DB persists it on assistant rows; user/tool rows stay NULL.
4. The stamped key never reaches the API wire on the next turn's replay
   (strict providers reject unknown message fields with HTTP 400/422).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Seam 1 — build_assistant_message stamps token_count from usage
# ---------------------------------------------------------------------------

def _make_agent() -> "object":
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.provider = "openai-compat"
    agent.model = "test-model"
    agent.base_url = "https://example.com/v1"
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    return agent


def _sdk_message(*, content: str = "hello", usage: dict | None = None):
    return SimpleNamespace(
        content=content,
        tool_calls=None,
        usage=SimpleNamespace(**usage) if usage else None,
    )


class TestBuilderStampsTokenCount:
    def test_stamps_output_tokens_from_usage(self):
        from agent.chat_completion_helpers import build_assistant_message

        agent = _make_agent()
        msg = build_assistant_message(
            agent,
            _sdk_message(content="answer", usage={"completion_tokens": 42}),
            "stop",
        )
        assert msg["token_count"] == 42

    def test_no_key_when_usage_absent(self):
        from agent.chat_completion_helpers import build_assistant_message

        agent = _make_agent()
        msg = build_assistant_message(agent, _sdk_message(content="answer"), "stop")
        assert "token_count" not in msg


# ---------------------------------------------------------------------------
# Seam 1b — handle_max_iterations stamps the summary assistant row
# ---------------------------------------------------------------------------

def _make_summary_agent():
    """Real AIAgent (constructor) with mocked client + transport for
    handle_max_iterations — mirrors the ``agent`` fixture in test_run_agent."""
    from run_agent import AIAgent

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
        )
    agent.client = MagicMock()
    agent.transport = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    return agent


class TestSummaryPathStampsTokenCount:
    def test_summary_row_carries_token_count(self):
        from agent.chat_completion_helpers import handle_max_iterations

        agent = _make_summary_agent()
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Summary of work.", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(completion_tokens=17, prompt_tokens=10, total_tokens=27),
        )
        agent.client.chat.completions.create.return_value = resp
        agent.transport.normalize_response.return_value = SimpleNamespace(
            content="Summary of work.",
            tool_calls=None,
            finish_reason="stop",
            usage=SimpleNamespace(completion_tokens=17, prompt_tokens=10, total_tokens=27),
        )

        messages = [{"role": "user", "content": "do stuff"}]
        with patch("agent.relay_llm.complete_logical_call"):
            result = handle_max_iterations(agent, messages, 5)

        assert result == "Summary of work."
        assistant_rows = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["token_count"] == 17

    def test_summary_row_no_key_when_usage_absent(self):
        from agent.chat_completion_helpers import handle_max_iterations

        agent = _make_summary_agent()
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Summary.", tool_calls=None),
                finish_reason="stop",
            )],
            usage=None,
        )
        agent.client.chat.completions.create.return_value = resp
        agent.transport.normalize_response.return_value = SimpleNamespace(
            content="Summary.",
            tool_calls=None,
            finish_reason="stop",
            usage=None,
        )

        messages = [{"role": "user", "content": "do stuff"}]
        with patch("agent.relay_llm.complete_logical_call"):
            handle_max_iterations(agent, messages, 5)

        assistant_rows = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistant_rows) == 1
        assert "token_count" not in assistant_rows[0]


# ---------------------------------------------------------------------------
# Seam 2 — flush path passes token_count through to the batch rows
# ---------------------------------------------------------------------------

def _make_flush_agent(session_db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
        )


class TestFlushPassesTokenCount:
    def test_assistant_row_carries_token_count(self):
        session_db = MagicMock()
        agent = _make_flush_agent(session_db)

        messages = [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "answer",
                "finish_reason": "stop",
                "token_count": 42,
            },
        ]
        agent._flush_messages_to_session_db(messages)

        assert session_db.append_messages_batch.call_count == 1
        batch = session_db.append_messages_batch.call_args.kwargs["messages"]
        assistant_rows = [m for m in batch if m.get("role") == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["token_count"] == 42

    def test_user_row_has_no_token_count(self):
        session_db = MagicMock()
        agent = _make_flush_agent(session_db)

        messages = [{"role": "user", "content": "question"}]
        agent._flush_messages_to_session_db(messages)

        batch = session_db.append_messages_batch.call_args.kwargs["messages"]
        user_rows = [m for m in batch if m.get("role") == "user"]
        assert len(user_rows) == 1
        # The flush path stamps the key on every row (None when the message
        # carries no usage); the DB contract — proven by Seam 3 — is that
        # user/tool rows persist NULL. Assert the value, not dict key shape.
        assert user_rows[0]["token_count"] is None


# ---------------------------------------------------------------------------
# Seam 3 — session DB persists token_count on assistant rows only
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session("sess-tc", source="cli")
    yield d
    d.close()


class TestSessionDbPersistsTokenCount:
    def test_assistant_row_persisted_user_tool_null(self, db):
        db.append_messages_batch(
            "sess-tc",
            [
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "finish_reason": "stop",
                    "token_count": 42,
                },
                {
                    "role": "tool",
                    "content": "tool output",
                    "tool_name": "terminal",
                    "tool_call_id": "call_1",
                },
            ],
        )
        rows = db._conn.execute(
            "SELECT role, token_count FROM messages ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("user", None),
            ("assistant", 42),
            ("tool", None),
        ]


# ---------------------------------------------------------------------------
# Seam 4 — token_count never reaches the API wire on the next turn
# ---------------------------------------------------------------------------

class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = {
                "id": "m",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "DONE"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        # The openai-compat path streams SSE (stream_options.include_usage),
        # so a plain-JSON body yields an empty stream and the agent retries
        # into EmptyStreamError — no assistant row is ever persisted. Serve
        # real SSE chunks (content → finish → usage → [DONE]) for streaming
        # requests, JSON for everything else (context-length probes etc.).
        if req.get("stream"):
            chunks = [
                {
                    "id": "chatcmpl_1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "DONE"},
                        "finish_reason": None,
                    }],
                },
                {
                    "id": "chatcmpl_1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "chatcmpl_1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model",
                    "choices": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            body = "".join(
                f"data: {json.dumps(c)}\n\n" for c in chunks
            ) + "data: [DONE]\n\n"
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def wire_env():
    """Mock provider + isolated HERMES_HOME + a shared SessionDB.

    Yields (make_agent, handler, db, sid): ``make_agent()`` builds a fresh
    AIAgent bound to the shared DB/session, so a second call models a
    process-restart turn N+1 that reloads history from the store.
    """
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_token_count_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    from run_agent import AIAgent

    from pathlib import Path

    db = SessionDB(db_path=Path(test_home) / "state.db")
    sid = "sess-wire"

    def make_agent():
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=10, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
            session_db=db, session_id=sid,
        )
        agent.valid_tool_names = {"read_file"}
        return agent

    try:
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            yield make_agent, _MockHandler, db, sid
    finally:
        srv.shutdown()
        db.close()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _chat_requests(handler) -> list:
    return [r for r in handler.captured_requests if "messages" in r]


class TestWireNoTokenCountLeak:
    def test_second_turn_request_has_no_token_count(self, wire_env):
        make_agent, handler, db, sid = wire_env

        agent = make_agent()
        agent.run_conversation("first turn")
        agent.run_conversation("second turn")

        requests = _chat_requests(handler)
        assert len(requests) >= 2
        # The second turn replays the first turn's assistant message; the
        # stamped token_count must be stripped before the wire.
        for req in requests:
            for msg in req.get("messages", []):
                assert "token_count" not in msg, (
                    f"token_count leaked to wire: {msg}"
                )

    def test_persisted_assistant_row_has_token_count(self, wire_env):
        make_agent, handler, db, sid = wire_env

        agent = make_agent()
        agent.run_conversation("first turn")

        rows = db._conn.execute(
            "SELECT role, token_count FROM messages WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()
        assistant_rows = [r for r in rows if r[0] == "assistant"]
        assert assistant_rows, "expected at least one assistant row"
        assert all(r[1] is not None for r in assistant_rows), (
            f"assistant rows missing token_count: {assistant_rows}"
        )
