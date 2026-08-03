"""End-to-end tests for the principle-distiller config SWITCH driven by the
``principle_distiller.enabled`` CONFIG KEY (config.yaml), not the env var.

The sibling files cover the env-var-driven paths (``HERMES_PRINCIPLE_DISTILLER``
set/unset). This file proves the same integration point — the read-once stash
``agent._principle_distiller_enabled`` in conversation_loop, the W1 injection
gate in turn_context, the end-of-turn distill block — honors the config-layer
switch exactly as documented (PRINCIPLE_INTEGRATION_DESIGN.md §5):

- unset (no ``principle_distiller`` section, no config.yaml) preserves the
  pre-feature behavior: distiller path never runs, response byte-identical;
- ``enabled: false`` (and the YAML 1.1 ``off`` spelling) keeps it disabled;
- ``enabled: true`` makes a real parsed boolean reach the integration point:
  W1 stash populated, distill block executes, record persisted, principles
  injected into the wire message;
- invalid values (``enabled: "yes"`` string, non-mapping section) degrade to
  False exactly like the config layer's validation warns: never raise, never
  enable, loop completes normally.

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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
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

CONFIG_TRUE = "principle_distiller:\n  enabled: true\n"
CONFIG_FALSE = "principle_distiller:\n  enabled: false\n"
CONFIG_OFF = "principle_distiller:\n  enabled: off\n"
# Invalid values — the config layer's validation warns about these and the
# reader must degrade to False, never raise and never enable (A6).
CONFIG_STRING_ENABLED = 'principle_distiller:\n  enabled: "yes"\n'
CONFIG_NON_MAPPING = "principle_distiller: garbage\n"


def _make_env(config_text):
    """Context-manager-lite factory used by the fixture.

    Sets up the mock provider + an isolated HERMES_HOME whose config.yaml
    carries exactly *config_text* (None -> no config.yaml at all). The
    HERMES_PRINCIPLE_DISTILLER env var is NEVER set, so the config key is the
    only thing driving the switch.
    """

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
            self.test_home = tempfile.mkdtemp(prefix="hermes_pd_cfg_")
            os.makedirs(os.path.join(self.test_home, ".hermes"))
            os.environ["HERMES_HOME"] = os.path.join(self.test_home, ".hermes")
            # The env override must not leak in from the outer environment:
            # this file is about the config key, so the switch must be decided
            # by config.yaml alone.
            os.environ.pop("HERMES_PRINCIPLE_DISTILLER", None)
            if config_text:
                cfg_path = os.path.join(self.test_home, ".hermes", "config.yaml")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(config_text)
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


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """Factory: ``agent_env(config_text)`` -> context manager yielding
    ``(agent, _MockHandler, store_path)`` with config.yaml set to
    *config_text* (None -> no config.yaml)."""

    def _factory(config_text):
        env = _make_env(config_text)

        class _Ctx:
            def __enter__(self):
                env.__enter__()
                try:
                    agent, store_path = _build_agent(env, tmp_path, monkeypatch)
                except BaseException:
                    env.__exit__(None, None, None)
                    raise
                return agent, _MockHandler, store_path

            def __exit__(self, *exc):
                return env.__exit__(*exc)

        return _Ctx()

    return _factory


def _seed(store_path) -> dict:
    from auto.principle_repo import PrincipleRepository

    repo = PrincipleRepository()
    repo.load()
    return repo.add(text=SEED_TEXT, tags=[], source="manual", score=0.5)


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


def _assert_distiller_never_ran(agent, handler, store_path):
    """The A5 contract: switch off -> the distiller path is never executed."""
    assert agent._principle_distiller_enabled is False
    # No stashes; store untouched (only the seed); no principle ever reached
    # the wire.
    with open(store_path, encoding="utf-8") as f:
        assert len([l for l in f if l.strip()]) == 1
    assert not any(
        SEED_TEXT in str(m.get("content", ""))
        for req in handler.captured_requests
        for m in req.get("messages", [])
        if m.get("role") == "user"
    )
    assert not hasattr(agent, "_prev_turn_principle_hits")
    assert not hasattr(agent, "_prev_turn_principle_ids")
    assert not hasattr(agent, "_turn_had_tool_error")


class TestConfigUnset:
    def test_unset_preserves_prior_behavior(self, agent_env):
        """No config.yaml at all -> the pre-feature behavior: distiller off."""
        with agent_env(None) as (agent, handler, store_path):
            _seed(store_path)
            handler.response_queue.append(_text_resp(MODEL_TEXT))

            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="u1")

            assert result["final_response"] == MODEL_TEXT
            _assert_distiller_never_ran(agent, handler, store_path)


class TestConfigDisabled:
    @pytest.mark.parametrize(
        "config_text",
        [CONFIG_FALSE, CONFIG_OFF],
        ids=["enabled=false", "enabled=off"],
    )
    def test_false_and_off_keep_distiller_disabled(self, agent_env, config_text):
        with agent_env(config_text) as (agent, handler, store_path):
            _seed(store_path)
            handler.response_queue.append(_text_resp(MODEL_TEXT))

            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="f1")

            assert result["final_response"] == MODEL_TEXT
            _assert_distiller_never_ran(agent, handler, store_path)


class TestConfigEnabled:
    def test_true_makes_parsed_boolean_reach_integration_point(self, agent_env):
        """enabled: true -> the loop stashes a real bool and the full
        distiller path runs: W1 injection, distill block, persistence."""
        with agent_env(CONFIG_TRUE) as (agent, handler, store_path):
            seed = _seed(store_path)
            handler.response_queue.append(_text_resp(MODEL_TEXT))

            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="t1")

            # The gate stash is a real parsed boolean (read once at turn start).
            assert agent._principle_distiller_enabled is True

            # Distilled text landed in the final response (A2).
            assert isinstance(result["final_response"], str)
            assert MODEL_TEXT in result["final_response"]
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

            # W1 stashed the full hit dicts for the distill block (same-turn).
            assert [p["id"] for p in agent._prev_turn_principle_hits] == [seed["id"]]
            assert agent._prev_turn_principle_hits[0]["text"] == SEED_TEXT
            assert agent._prev_turn_principle_ids == [seed["id"]]

            # Injection reached the wire (A9: sidecar carries the principle block).
            assert any(SEED_TEXT in c for c in _wire_user_contents(handler))

    def test_recording_fake_sees_exact_turn_state(self, agent_env, monkeypatch):
        """The config-key switch feeds the same call contract as the env-var
        path: one distill_from_turn call at end of turn with this turn's state."""
        with agent_env(CONFIG_TRUE) as (agent, handler, store_path):
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
            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="t2")

            assert len(calls) == 1
            call = calls[0]
            assert call["user_message"] == TURN_MSG
            assert [p["id"] for p in call["hit_principles"]] == [seed["id"]]
            assert call["has_tool_error"] is False
            assert call["has_user_correction"] is False
            assert call["dry_run"] is False
            # None return -> nothing appended, response unchanged.
            assert result["final_response"] == "完成。"


class TestConfigInvalid:
    @pytest.mark.parametrize(
        "config_text",
        [CONFIG_STRING_ENABLED, CONFIG_NON_MAPPING],
        ids=["enabled-string-yes", "non-mapping-section"],
    )
    def test_invalid_values_degrade_consistent_with_config_validation(self, agent_env, config_text):
        """validate_config_structure warns about these; the reader must turn
        them into a clean False — no crash, no enable, loop completes."""
        with agent_env(config_text) as (agent, handler, store_path):
            _seed(store_path)
            handler.response_queue.append(_text_resp(MODEL_TEXT))

            result = agent.run_conversation(TURN_MSG, conversation_history=[], task_id="i1")

            assert result["final_response"] == MODEL_TEXT
            _assert_distiller_never_ran(agent, handler, store_path)
