import asyncio
import sys
import types
from types import SimpleNamespace

import pytest


sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import pytest

import cron.scheduler as cron_scheduler
import gateway.run as gateway_run
import run_agent
from gateway.config import Platform
from gateway.session import SessionSource

# ── Cold-import budget ─────────────────────────────────────────────────────
#
# The live-I/O that used to dominate these tests is gone — see the
# ``_no_live_host_probes`` fixture in tests/cron/conftest.py, which removed a
# 9.60s host toolchain probe and a 2.48s authenticated GET to chatgpt.com.
# Measured effect on ``pytest tests/cron``, same commit, same machine:
# test_cron_run_job_codex_path_handles_internal_401_refresh 33.93s -> 12.98s.
#
# What remains is cold third-party import cost, not live I/O. Building the
# agent's OpenAI client reaches ``_build_keepalive_http_client``, which is the
# first thing in a run to import httpx/h11, and ``init_agent`` similarly first
# imports mcp and loads the certifi CA bundle. Whichever test triggers those
# pays for them, inside its own call phase where pytest-timeout can see it.
# Run order therefore decides the number: 12.98s when an earlier cron file had
# already imported httpx, but >30s when this file runs first in the process.
#
# It is also NOT the platform-SDK import tax that ``_iter_home_target_platforms``
# used to pay (fixed separately): an import profile of this file shows the
# second test spending 15.78s while importing just SIX modules, and imports on
# non-main threads totalling 0.127s. So this mark declares a real budget rather
# than papering over a removable cost.
#
# The 30s ``addopts`` cap is documented in pyproject.toml as the fallback
# inside each per-file subprocess of scripts/run_tests_parallel.py, where the
# import lands at collection time and pytest-timeout never sees it. This mark
# gives the same budget to a monolithic ``pytest tests/events tests/cron``
# run. Not an xfail — every assertion below is unchanged and still enforced.
pytestmark = pytest.mark.timeout(180)


def _patch_agent_bootstrap(monkeypatch):
    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})


def _codex_message_response(text: str):
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8),
        status="completed",
        model="gpt-5-codex",
    )


class _UnauthorizedError(RuntimeError):
    def __init__(self):
        super().__init__("Error code: 401 - unauthorized")
        self.status_code = 401


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def close(self):
        return None


class _Codex401ThenSuccessAgent(run_agent.AIAgent):
    refresh_attempts = 0
    last_init = {}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("skip_context_files", True)
        kwargs.setdefault("skip_memory", True)
        kwargs.setdefault("max_iterations", 4)
        type(self).last_init = dict(kwargs)
        super().__init__(*args, **kwargs)
        self._cleanup_task_resources = lambda task_id: None
        self._persist_session = lambda messages, history=None: None
        self._save_trajectory = lambda messages, user_message, completed: None

    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:
        type(self).refresh_attempts += 1
        return True

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        calls = {"api": 0}

        def _fake_api_call(api_kwargs):
            calls["api"] += 1
            if calls["api"] == 1:
                raise _UnauthorizedError()
            return _codex_message_response("Recovered via refresh")

        self._interruptible_api_call = _fake_api_call
        return super().run_conversation(user_message, conversation_history=conversation_history, task_id=task_id)


class TestNoLiveHostProbes:
    """Guards the ``_no_live_host_probes`` fixture in ``tests/cron/conftest.py``.

    Both assertions fail if that autouse fixture is removed, which is the
    regression that made this file's 401-refresh tests overrun the 30s
    ``addopts`` timeout and hard-exit the whole pytest process.
    """

    def test_codex_context_resolution_does_not_reach_the_network(self, monkeypatch):
        import agent.model_metadata as mm

        # Record rather than raise: _fetch_codex_oauth_context_lengths wraps
        # the request in `except Exception`, so a raising tripwire is
        # swallowed and the test would pass even while calling out.
        calls = []

        def _record(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("url"))
            raise RuntimeError("blocked by test")

        monkeypatch.setattr(mm.requests, "get", _record)
        # An hour-long module cache would also suppress the request; clear it
        # so this asserts the stub, not a warm cache.
        monkeypatch.setattr(mm, "_codex_oauth_context_cache", {})
        monkeypatch.setattr(mm, "_codex_oauth_context_cache_time", 0.0)

        # Falls back to _CODEX_OAUTH_CONTEXT_FALLBACK, exactly as the live
        # probe does when it fails.
        assert mm._resolve_codex_oauth_context_length(
            "gpt-5.3-codex", "codex-token"
        ) == 272_000
        assert calls == [], f"live outbound request(s) during a unit test: {calls}"

    def test_env_probe_never_spawns_a_host_probe_thread(self):
        from tools import env_probe

        assert env_probe.get_environment_probe_line() == ""
        assert env_probe._PROBE_THREAD is None, (
            "env_probe started its worker thread — it shells out to "
            "python3/pip on the developer's host and blocks system-prompt "
            "construction for its full 10s wait timeout"
        )


def test_cron_run_job_codex_path_handles_internal_401_refresh(monkeypatch):
    _patch_agent_bootstrap(monkeypatch)
    monkeypatch.setattr(run_agent, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(run_agent, "AIAgent", _Codex401ThenSuccessAgent)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None, **kwargs: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "codex-token",
        },
    )
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    _Codex401ThenSuccessAgent.refresh_attempts = 0
    _Codex401ThenSuccessAgent.last_init = {}

    success, output, final_response, error = cron_scheduler.run_job(
        {"id": "job-1", "name": "Codex Refresh Test", "prompt": "ping", "model": "gpt-5.3-codex"}
    )

    assert success is True
    assert error is None
    assert final_response == "Recovered via refresh"
    assert "Recovered via refresh" in output
    assert _Codex401ThenSuccessAgent.refresh_attempts == 1
    assert _Codex401ThenSuccessAgent.last_init["provider"] == "openai-codex"
    assert _Codex401ThenSuccessAgent.last_init["api_mode"] == "codex_responses"


def test_gateway_run_agent_codex_path_handles_internal_401_refresh(monkeypatch):
    _patch_agent_bootstrap(monkeypatch)
    monkeypatch.setattr(run_agent, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(run_agent, "AIAgent", _Codex401ThenSuccessAgent)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "codex-token",
        },
    )
    monkeypatch.setenv("HERMES_TOOL_PROGRESS", "false")
    monkeypatch.setenv("HERMES_MODEL", "gpt-5.3-codex")

    _Codex401ThenSuccessAgent.refresh_attempts = 0
    _Codex401ThenSuccessAgent.last_init = {}

    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    from unittest.mock import MagicMock, AsyncMock
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    # Ensure model resolution returns the codex model even if xdist
    # leaked env vars cleared HERMES_MODEL.
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_resolve_turn_agent_config",
        lambda self, msg, model, runtime: {
            "model": model or "gpt-5.3-codex",
            "runtime": runtime,
        },
    )

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:local:dm",
        )
    )

    assert result["final_response"] == "Recovered via refresh"
    assert _Codex401ThenSuccessAgent.refresh_attempts == 1
    assert _Codex401ThenSuccessAgent.last_init["provider"] == "openai-codex"
    assert _Codex401ThenSuccessAgent.last_init["api_mode"] == "codex_responses"
