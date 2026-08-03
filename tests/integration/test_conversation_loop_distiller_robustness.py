"""End-to-end tests for the Phase 3 principle distiller wiring — DISABLED path
and failure containment.

- config/env disabled -> the distiller path is never executed: no repo read,
  no stash writes, final response byte-identical to the model output (A5);
- the distiller raising -> logged at ERROR, loop completes normally (A7);
- malformed distiller returns (str/list/{}/missing/non-str text) -> ignored,
  never raises, never injected into the response;
- the reward hook raising -> logged, loop completes (A7);
- W1 injection failing -> empty stashes, turn completes (A7);
- the distiller module unavailable (``_principle_distiller`` is None) ->
  the distill block is skipped entirely even when enabled.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# Repo root = three levels up from tests/integration/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# The principle system lives in ~/.hermes/auto (outside the vendored repo).
_AUTO_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "auto")
_AUTO_HOME = os.path.dirname(_AUTO_DIR)
for _p in (_AUTO_DIR, _AUTO_HOME):
    if _p not in sys.path:
        sys.path.insert(0, _p)


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
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            content = msg.get("content") or ""
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a, **kw):  # silence the default stderr logging
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


SEED_TEXT = "重构前先明确范围，再逐步执行并验证中间结果"
TURN_MSG = "帮我重构这个 Python 模块并补上测试"
MODEL_TEXT = "这是模型的原始回答。"


def _make_env(enabled: bool):
    """Context-manager-lite factory used by the fixture."""

    class _Env:
        def __init__(self):
            self.srv = None
            self.thread = None
            self.test_home = None
            self.prev_home = os.environ.get("HERMES_HOME")
            self.prev_env = os.environ.get("HERMES_PRINCIPLE_DISTILLER")

        def __enter__(self):
            _MockHandler.captured_requests = []
            _MockHandler.response_queue = []
            self.srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
            self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
            self.thread.start()
            self.test_home = tempfile.mkdtemp(prefix="hermes_pd_rob_")
            os.makedirs(os.path.join(self.test_home, ".hermes"))
            os.environ["HERMES_HOME"] = os.path.join(self.test_home, ".hermes")
            if enabled:
                os.environ["HERMES_PRINCIPLE_DISTILLER"] = "1"
            elif self.prev_env is not None:
                os.environ.pop("HERMES_PRINCIPLE_DISTILLER", None)
            return self

        def __exit__(self, *exc):
            self.srv.shutdown()
            shutil.rmtree(self.test_home, ignore_errors=True)
            if self.prev_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self.prev_home
            if self.prev_env is None:
                os.environ.pop("HERMES_PRINCIPLE_DISTILLER", None)
            else:
                os.environ["HERMES_PRINCIPLE_DISTILLER"] = self.prev_env

    return _Env()


def _build_agent(env, tmp_path, monkeypatch):
    """Patch the principle-store paths, import fresh, build the stub agent."""
    store_dir = tmp_path / "principles"
    store_path = store_dir / "principles.jsonl"
    store_dir.mkdir(parents=True, exist_ok=True)
    import auto.principle_distiller as _pd
    import auto.principle_repo as _pr

    # The distiller/reward-hook import the repo with a BARE name
    # (`from principle_repo import ...`), which would create a second module
    # instance anchored at the REAL ~/.hermes store. Alias the patched
    # modules under the bare names so every internal import hits them.
    sys.modules["principle_distiller"] = _pd
    sys.modules["principle_repo"] = _pr

    monkeypatch.setattr(_pd, "DATA_DIR", store_dir)
    monkeypatch.setattr(_pd, "PRINCIPLES_PATH", store_path)
    monkeypatch.setattr(_pr, "DATA_DIR", store_dir)
    monkeypatch.setattr(_pr, "DEFAULT_PATH", store_path)

    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{env.srv.server_address[1]}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=10, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )
    agent.valid_tool_names = {"terminal", "read_file", "write_file", "execute_code", "session_search"}
    return agent, store_path


def _seed(store_path) -> dict:
    from auto.principle_repo import PrincipleRepository

    repo = PrincipleRepository()
    repo.load()
    return repo.add(text=SEED_TEXT, tags=[], source="manual", score=0.5)


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    env = _make_env(enabled=False)
    with env:
        agent, store_path = _build_agent(env, tmp_path, monkeypatch)
        yield agent, _MockHandler, store_path


@pytest.fixture()
def agent_env_enabled(tmp_path, monkeypatch):
    env = _make_env(enabled=True)
    with env:
        agent, store_path = _build_agent(env, tmp_path, monkeypatch)
        yield agent, _MockHandler, store_path


class TestTurnSliceHasToolError:
    """Pure-function coverage for the tool-error slice scan feeding the
    distiller's ``has_tool_error`` signal (t_d1048be1 advisory #1)."""

    def test_startswith_marker_detected(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "x", "content": "Error executing tool 'boom': timeout"},
        ]
        assert _turn_slice_has_tool_error(messages, 0) is True

    def test_scope_block_contains_marker_detected(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "tool_search",
             "content": "'memory_search' is not available in this session. Use tool_search to find tools you can call."},
        ]
        assert _turn_slice_has_tool_error(messages, 0) is True

    def test_contains_marker_buried_mid_content(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "mcp_x",
             "content": "<untrusted_tool_result>prefix 'foo' is not available in this session. Use tool_search to find tools you can call.</untrusted_tool_result>"},
        ]
        assert _turn_slice_has_tool_error(messages, 0) is True

    def test_normal_tool_content_not_flagged(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "x", "content": "all good here"},
        ]
        assert _turn_slice_has_tool_error(messages, 0) is False

    def test_prior_turn_messages_ignored(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "first"},
            {"role": "tool", "name": "x", "content": "Error executing tool 'boom'"},
            {"role": "user", "content": "second"},
            {"role": "tool", "name": "y", "content": "ok"},
        ]
        # start_idx=2 (second user message): only messages after index 2 scanned.
        assert _turn_slice_has_tool_error(messages, 2) is False

    def test_non_str_content_ignored(self):
        from agent.conversation_loop import _turn_slice_has_tool_error

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "x", "content": ["not", "a", "string"]},
        ]
        assert _turn_slice_has_tool_error(messages, 0) is False


class TestDisabledPath:
    def test_disabled_never_executes_distiller_path(self, agent_env):
        agent, handler, store_path = agent_env
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="d1")

        # Gate stash read once at turn start, and it is False.
        assert agent._principle_distiller_enabled is False
        # A5: final response byte-identical to the model output.
        assert result["final_response"] == MODEL_TEXT
        # No stashes written, no repo interaction via the loop.
        assert not hasattr(agent, "_prev_turn_principle_hits")
        assert not hasattr(agent, "_prev_turn_principle_ids")
        assert not hasattr(agent, "_turn_had_tool_error")
        # Store untouched: still exactly the seeded record.
        with open(store_path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 1
        # The seed principle never reached the wire.
        assert not any(
            SEED_TEXT in str(m.get("content", ""))
            for req in handler.captured_requests
            for m in req.get("messages", [])
            if m.get("role") == "user"
        )


class TestFailureContainment:
    def test_distiller_exception_contained(self, agent_env_enabled, monkeypatch, caplog):
        agent, handler, store_path = agent_env_enabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        import auto.principle_distiller as _pd

        def _boom(*a, **kw):
            raise RuntimeError("distiller exploded")

        monkeypatch.setattr(_pd, "distill_from_turn", _boom)
        with caplog.at_level(logging.ERROR, logger="agent.conversation_loop"):
            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="r1")

        assert result["final_response"] == MODEL_TEXT
        assert "Principle distillation failed" in caplog.text

    @pytest.mark.parametrize("malformed", [
        "not-a-dict",
        ["list", "of", "stuff"],
        {},
        {"text": 123},
        {"text": ""},
        {"text": "   "},
    ])
    def test_malformed_distiller_output_ignored(self, agent_env_enabled, monkeypatch, malformed):
        agent, handler, store_path = agent_env_enabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        import auto.principle_distiller as _pd

        monkeypatch.setattr(_pd, "distill_from_turn", lambda *a, **kw: malformed)
        result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="r2")

        # Never raises, never injects garbage: response unchanged, store not
        # grown by a bogus record.
        assert result["final_response"] == MODEL_TEXT
        with open(store_path, encoding="utf-8") as f:
            assert len([l for l in f if l.strip()]) == 1

    def test_reward_exception_contained(self, agent_env_enabled, monkeypatch, caplog):
        agent, handler, store_path = agent_env_enabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        import auto.principle_reward_hook as _rh

        def _boom(agent, user_message):
            raise RuntimeError("reward exploded")

        monkeypatch.setattr(_rh, "apply_principle_rewards", _boom)
        # Keep the distiller out of the picture so the response stays
        # byte-identical to the model output (isolates the reward failure).
        import auto.principle_distiller as _pd

        monkeypatch.setattr(_pd, "distill_from_turn", lambda *a, **kw: None)
        with caplog.at_level(logging.ERROR, logger="agent.conversation_loop"):
            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="r3")

        assert result["final_response"] == MODEL_TEXT
        assert "Principle reward failed" in caplog.text

    def test_injection_failure_contained(self, agent_env_enabled, monkeypatch, caplog):
        agent, handler, store_path = agent_env_enabled
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        import auto.principle_repo as _pr

        class _BoomRepo:
            def load(self):
                raise RuntimeError("store corrupted")

            def retrieve(self, *a, **kw):
                raise AssertionError("retrieve must not run")

        monkeypatch.setattr(_pr, "PrincipleRepository", lambda *a, **kw: _BoomRepo())
        with caplog.at_level(logging.WARNING, logger="agent.turn_context"):
            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="r4")

        assert result["final_response"] == MODEL_TEXT
        assert "Principle injection failed" in caplog.text
        # Degraded to empty stashes -> the distill block sees no hits.
        assert agent._prev_turn_principle_hits == []
        assert agent._prev_turn_principle_ids == []
        # No distill record was persisted.
        assert not os.path.exists(store_path) or os.path.getsize(store_path) == 0

    def test_distiller_module_unavailable_skips_block(self, agent_env_enabled, monkeypatch):
        """Even with the switch on, a None distiller module skips the block."""
        agent, handler, store_path = agent_env_enabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        import agent.conversation_loop as _cl

        monkeypatch.setattr(_cl, "_principle_distiller", None)
        result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="r5")

        assert result["final_response"] == MODEL_TEXT
        with open(store_path, encoding="utf-8") as f:
            assert len([l for l in f if l.strip()]) == 1
