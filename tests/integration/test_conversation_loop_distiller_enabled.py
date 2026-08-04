"""End-to-end tests for the Phase 3 principle distiller wiring — ENABLED path.

Exercises ``run_conversation`` (via ``AIAgent.run_conversation``) against an
in-process mock provider with ``HERMES_PRINCIPLE_DISTILLER=1``:

- a clean turn with a seeded principle store retrieves principles (W1),
  injects them into the wire message (api_content sidecar), stashes the hit
  dicts, and calls ``distill_from_turn`` at the end of the turn with the
  correct state; a non-empty returned ``text`` lands in ``final_response``
  and the record is persisted via ``PrincipleRepository.add`` (A2);
- a recording fake pins the exact call arguments (turn user message, stashed
  hit dicts, clean flags) (A2);
- a tool-error turn passes ``has_tool_error=True`` (A3);
- a two-turn session runs the reward hook (W3) at turn 2 start with turn 1's
  ids and applies the strong-correction delta (A4).

Store paths are patched to tmp_path so the tests never touch the real
~/.hermes/data/principles.
"""

from __future__ import annotations

import json
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
    # Set by the fixture before each request cycle.
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
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


def _tc_resp(name: str, args: str = "{}") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


SEED_TEXT = "重构前先明确范围，再逐步执行并验证中间结果"
TURN1_MSG = "帮我重构这个 Python 模块并补上测试"


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """Mock provider + isolated HERMES_HOME + isolated principle store.

    Yields (agent, handler, store_path). The distiller is ENABLED via the
    HERMES_PRINCIPLE_DISTILLER env var, so the loop's read-once stash
    (agent._principle_distiller_enabled) is True for every turn.
    """
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_pd_e2e_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")
    prev_env = os.environ.get("HERMES_PRINCIPLE_DISTILLER")
    os.environ["HERMES_PRINCIPLE_DISTILLER"] = "1"

    # Isolate the principle store: both the distiller's dedup corpus and the
    # repository's DEFAULT_PATH must point at tmp_path (the real modules are
    # anchored to Path.home()/.hermes, not HERMES_HOME).
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

    # Import fresh so the patched conversation_loop is exercised even when the
    # module was imported earlier in the same worker.
    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=10, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )
    agent.valid_tool_names = {"terminal", "read_file", "write_file", "execute_code", "session_search"}

    try:
        yield agent, _MockHandler, store_path
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home
        if prev_env is None:
            os.environ.pop("HERMES_PRINCIPLE_DISTILLER", None)
        else:
            os.environ["HERMES_PRINCIPLE_DISTILLER"] = prev_env


def _seed(store_path: str, text: str = SEED_TEXT, **kw) -> dict:
    """Seed one principle through the (path-patched) repository."""
    from auto.principle_repo import PrincipleRepository

    repo = PrincipleRepository()
    repo.load()
    return repo.add(text=text, tags=kw.get("tags", []), source=kw.get("source", "manual"),
                    score=kw.get("score", 0.5))


def _store_lines(store_path) -> list[dict]:
    if not os.path.exists(store_path):
        return []
    with open(store_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _wire_user_contents(handler) -> list[str]:
    """User-message contents captured by the mock provider (wire bytes)."""
    out = []
    for req in handler.captured_requests:
        for m in req.get("messages", []):
            if m.get("role") == "user":
                c = m.get("content", "")
                out.append(c if isinstance(c, str) else json.dumps(c))
    return out


class TestEnabledDistillation:
    def test_clean_turn_distills_appends_and_persists(self, agent_env):
        agent, handler, store_path = agent_env
        seed = _seed(store_path)
        handler.response_queue.append(_text_resp("好的，我来重构。"))

        result = agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="t1")

        # Distilled text landed in the final response (A2).
        assert isinstance(result["final_response"], str)
        assert "好的，我来重构。" in result["final_response"]
        distilled_texts = [
            line["text"] for line in _store_lines(store_path)
            if line.get("source") == "self-distilled"
        ]
        assert len(distilled_texts) == 1
        assert result["final_response"].endswith(distilled_texts[0])

        # Persisted via repo.add -> raw record (score_history/archived are
        # backfilled on the NEXT load, not at write time).
        rec = [line for line in _store_lines(store_path) if line.get("source") == "self-distilled"][0]
        assert rec.get("score") == 0.3
        assert not rec.get("score_history")
        assert not rec.get("archived", False)
        assert "princ_" in rec["id"]

        # W1 stashes the full hit dicts for the distill block (same-turn).
        assert [p["id"] for p in agent._prev_turn_principle_hits] == [seed["id"]]
        assert agent._prev_turn_principle_hits[0]["text"] == SEED_TEXT
        assert agent._prev_turn_principle_ids == [seed["id"]]
        # Gate stash read once at turn start.
        assert agent._principle_distiller_enabled is True

        # Injection reached the wire (A9: sidecar carries the principle block).
        assert any(SEED_TEXT in c for c in _wire_user_contents(handler))

    def test_recording_fake_receives_exact_turn_state(self, agent_env, monkeypatch):
        agent, handler, store_path = agent_env
        seed = _seed(store_path)
        handler.response_queue.append(_text_resp("完成。"))

        import auto.principle_distiller as _pd

        calls: list[dict] = []

        def _fake(user_message, hit_principles, has_tool_error=False, has_user_correction=False, dry_run=False):
            calls.append({
                "user_message": user_message,
                "hit_principles": hit_principles,
                "has_tool_error": has_tool_error,
                "has_user_correction": has_user_correction,
                "dry_run": dry_run,
            })
            return None

        monkeypatch.setattr(_pd, "distill_from_turn", _fake)
        result = agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="t2")

        # Called exactly once, at the end of the turn, with this turn's state.
        assert len(calls) == 1
        call = calls[0]
        assert call["user_message"] == TURN1_MSG
        assert [p["id"] for p in call["hit_principles"]] == [seed["id"]]
        assert call["has_tool_error"] is False
        assert call["has_user_correction"] is False
        assert call["dry_run"] is False
        # None return -> nothing appended, response unchanged.
        assert result["final_response"] == "完成。"

    def test_tool_error_turn_passes_has_tool_error(self, agent_env, monkeypatch):
        agent, handler, store_path = agent_env
        _seed(store_path)
        # Invalid tool name first, then a plain-text recovery.
        handler.response_queue.append(_tc_resp("nonsense_tool", "{}"))
        handler.response_queue.append(_text_resp("已修正。"))

        import auto.principle_distiller as _pd

        calls: list[dict] = []

        def _fake(user_message, hit_principles, has_tool_error=False, has_user_correction=False, dry_run=False):
            calls.append(has_tool_error)
            return None

        monkeypatch.setattr(_pd, "distill_from_turn", _fake)
        agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="t3")

        # A3: the tool-error turn is flagged dirty for the distiller.
        assert calls == [True]
        # The error tool-result is visible in the wire transcript too.
        assert any(
            "does not exist" in str(m.get("content", ""))
            for req in handler.captured_requests
            for m in req.get("messages", [])
            if m.get("role") == "tool"
        )

    def test_two_turn_reward_applies_strong_correction(self, agent_env):
        agent, handler, store_path = agent_env
        seed = _seed(store_path)
        handler.response_queue.append(_text_resp("第一轮完成。"))

        r1 = agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="t4")
        assert "self-distilled" in json.dumps(_store_lines(store_path))

        # Turn 1's ids are stashed for the next turn's reward slot.
        assert agent._prev_turn_principle_ids == [seed["id"]]

        # Turn 2: strong correction -> -0.2 on turn 1's principle.
        handler.response_queue.append(_text_resp("你说得对。"))
        r2 = agent.run_conversation("不对，你搞错了，重构前应该先备份", conversation_history=[], task_id="t5")
        assert isinstance(r2["final_response"], str)

        from auto.principle_repo import PrincipleRepository

        repo = PrincipleRepository()
        repo.load()
        by_id = {p["id"]: p for p in repo.principles}
        assert by_id[seed["id"]]["score"] == pytest.approx(0.3)  # 0.5 - 0.2 (strong correction)
        assert by_id[seed["id"]]["score_history"][-1]["reason"] == "strong_correction"
        assert by_id[seed["id"]]["hit_count"] == 1
        # Turn 1's ids were consumed; turn 2's fresh stash exists.
        assert isinstance(agent._prev_turn_principle_ids, list)
        assert all(isinstance(i, str) for i in agent._prev_turn_principle_ids)
        # Distillation ran on turn 2 as well (clean tool-wise).
        assert len(_store_lines(store_path)) >= 3
