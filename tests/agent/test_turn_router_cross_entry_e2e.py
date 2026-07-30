"""Real-import cross-entry evidence for the native user-turn router.

These tests deliberately keep the production AIAgent conversation loop and an
actual OpenAI-wire HTTP boundary.  Surface-only concerns (renderers,
notifications, and durable stores) may be disabled, but the turn-routing
request, lifecycle, provider submission, prompt/history construction, and
runtime restoration are never replaced by fake agents or fake model loops.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.turn_routing_runtime import TurnRoutingRequest
from run_agent import AIAgent


ROOT = Path(__file__).resolve().parents[2]
MODEL = "local-turn-router-model"
PROMPT = "Preserve this exact user prompt."
HISTORY = [
    {"role": "user", "content": "Earlier user turn."},
    {"role": "assistant", "content": "Earlier assistant turn."},
]


class _ProviderHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()
    chat_status = 200
    request_seen = threading.Event()
    block_release: threading.Event | None = None

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL, "object": "model"},
                        {"id": "openai/gpt-4o", "object": "model"},
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if "messages" not in payload:
            body = json.dumps({"tokens": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with type(self).lock:
            type(self).requests.append(payload)
        type(self).request_seen.set()
        release = type(self).block_release
        if release is not None:
            release.wait(timeout=10)
        if type(self).chat_status != 200:
            body = json.dumps(
                {
                    "error": {
                        "message": "deterministic local provider failure",
                        "type": "invalid_request_error",
                    }
                }
            ).encode()
            self.send_response(type(self).chat_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        response = {
                "id": f"chatcmpl-{len(type(self).requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": payload.get("model") or MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "local provider response",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            }
        if payload.get("stream") is True:
            chunks = [
                {
                    "id": response["id"],
                    "model": response["model"],
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }],
                },
                {
                    "id": response["id"],
                    "model": response["model"],
                    "choices": [{
                        "index": 0,
                        "delta": {"content": "local provider response"},
                        "finish_reason": None,
                    }],
                },
                {
                    "id": response["id"],
                    "model": response["model"],
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }],
                    "usage": response["usage"],
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def local_provider():
    _ProviderHandler.requests = []
    _ProviderHandler.chat_status = 200
    _ProviderHandler.request_seen.clear()
    _ProviderHandler.block_release = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", _ProviderHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _agent(base_url: str, *, session_id: str) -> AIAgent:
    return AIAgent(
        api_key="synthetic-test-key",
        base_url=base_url,
        provider="openai",
        model=MODEL,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id=session_id,
        max_iterations=2,
    )


def _config(
    *, routed_model: str = MODEL, routed_provider: str = "openai"
) -> dict[str, Any]:
    return {
        "mode": "off",
        "routes": {
            "local": {
                "kind": "model",
                "provider": routed_provider,
                "model": routed_model,
            }
        },
        "lanes": {"plain": "local"},
        "budget": {"grok_weekly_limit": 0},
    }


def _target(
    *, model: str = MODEL, provider: str = "openai"
) -> dict[str, str]:
    return {"kind": "model", "provider": provider, "model": model}


def _assert_wire_invariants(payload: dict[str, Any]) -> None:
    messages = payload["messages"]
    roles = [message["role"] for message in messages]
    assert roles[-3:] == ["user", "assistant", "user"]
    assert messages[-3:] == [*HISTORY, {"role": "user", "content": PROMPT}]
    assert all(left != right for left, right in zip(roles, roles[1:]))
    encoded_messages = json.dumps(messages, sort_keys=True)
    for forbidden in (
        "turn_routing_request",
        "explicit_turn_override",
        "reason_code",
        "route.decided",
        "turn_sequence",
    ):
        assert forbidden not in encoded_messages


def _assert_result_invariants(result: dict[str, Any]) -> None:
    messages = result["messages"]
    assert messages[-4:-1] == [*HISTORY, {"role": "user", "content": PROMPT}]
    assert messages[-1]["role"] == "assistant"
    roles = [message["role"] for message in messages if message["role"] != "system"]
    assert all(left != right for left, right in zip(roles, roles[1:]))


def _runtime_snapshot(agent: AIAgent) -> dict[str, Any]:
    return {
        "model": agent.model,
        "provider": agent.provider,
        "requested_provider": agent.requested_provider,
        "base_url": agent.base_url,
        "api_key": agent.api_key,
        "api_mode": agent.api_mode,
        "client": agent.client,
        "client_kwargs": deepcopy(agent._client_kwargs),
        "primary_runtime": deepcopy(agent._primary_runtime),
        "system_prompt": agent._cached_system_prompt,
    }


def _openrouter_route_request(
    agent: AIAgent,
    events: list[tuple[str, dict[str, Any]]],
    *,
    surface: str,
) -> TurnRoutingRequest:
    routed_model = "openai/gpt-4o"
    routed_provider = "openrouter"
    return TurnRoutingRequest(
        surface=surface,
        session_id=agent.session_id,
        user_text=PROMPT,
        explicit_turn_override=True,
        explicit_target=_target(model=routed_model, provider=routed_provider),
        config_loader=lambda: _config(
            routed_model=routed_model,
            routed_provider=routed_provider,
        ),
        emit=lambda event, payload: events.append((event, payload)),
    )


def _surface_request(surface: str, agent: AIAgent, monkeypatch) -> TurnRoutingRequest:
    import agent.turn_routing_runtime as runtime

    monkeypatch.setattr(runtime, "load_turn_routing_config", lambda: _config())
    monkeypatch.setattr(runtime, "load_turn_moa_config", lambda: None)

    if surface == "cli":
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.agent = agent
        cli.session_id = agent.session_id
        cli._pending_turn_route_target = _target()
        cli._session_model_pinned = False
        return cli._take_turn_routing_request(
            user_text=PROMPT,
            api_user_message=PROMPT,
            persist_user_message=PROMPT,
        )

    if surface == "gateway":
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._pending_turn_route_targets = {"gateway-key": _target()}
        runner._turn_routing_session_states = {}
        runner._session_model_overrides = {}
        runner._agent_cache = {}
        runner._evict_cached_agent = lambda _key: None
        return runner._take_gateway_turn_routing_request(
            session_key="gateway-key",
            agent=agent,
            user_text=PROMPT,
            api_user_message=PROMPT,
            persist_user_message=PROMPT,
        )

    raise AssertionError(surface)


@pytest.mark.parametrize("surface", ["cli", "gateway"])
def test_real_cli_and_gateway_adapters_preserve_wire_cache_domain(
    surface, monkeypatch, local_provider
):
    base_url, requests = local_provider
    agent = _agent(base_url, session_id=f"e2e-{surface}")
    baseline = TurnRoutingRequest(
        surface=f"{surface}-baseline",
        session_id=agent.session_id,
        user_text=PROMPT,
        config_loader=lambda: {"mode": "off"},
    )
    try:
        baseline_result = agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=baseline,
        )
        original_runtime = {
            "model": agent.model,
            "provider": agent.provider,
            "base_url": agent.base_url,
            "api_mode": agent.api_mode,
            "client": agent.client,
            "system_prompt": agent._cached_system_prompt,
        }
        routed_request = _surface_request(surface, agent, monkeypatch)
        routed_result = agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=routed_request,
        )

        assert len(requests) == 2
        _assert_wire_invariants(requests[0])
        _assert_wire_invariants(requests[1])
        assert requests[0]["messages"] == requests[1]["messages"]
        _assert_result_invariants(baseline_result)
        _assert_result_invariants(routed_result)
        assert {
            "model": agent.model,
            "provider": agent.provider,
            "base_url": agent.base_url,
            "api_mode": agent.api_mode,
            "client": agent.client,
            "system_prompt": agent._cached_system_prompt,
        } == original_runtime
        if surface == "cli":
            assert routed_request.explicit_target == _target()
        else:
            assert "gateway-key" not in getattr(
                routed_request, "_pending_turn_route_targets", {}
            )
    finally:
        agent.close()


def test_real_route_apply_uses_routed_wire_and_restores_exact_runtime(
    monkeypatch, local_provider
):
    base_url, requests = local_provider
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)
    agent = _agent(base_url, session_id="e2e-runtime-restore")
    events: list[tuple[str, dict[str, Any]]] = []
    routed_model = "openai/gpt-4o"
    routed_provider = "openrouter"
    request = TurnRoutingRequest(
        surface="e2e-runtime",
        session_id=agent.session_id,
        user_text=PROMPT,
        explicit_turn_override=True,
        explicit_target=_target(model=routed_model, provider=routed_provider),
        config_loader=lambda: _config(
            routed_model=routed_model,
            routed_provider=routed_provider,
        ),
        emit=lambda event, payload: events.append((event, payload)),
    )
    try:
        baseline_result = agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=TurnRoutingRequest(
                surface="e2e-runtime-baseline",
                session_id=agent.session_id,
                user_text=PROMPT,
                config_loader=lambda: {"mode": "off"},
            ),
        )
        original_runtime = {
            "model": agent.model,
            "provider": agent.provider,
            "requested_provider": agent.requested_provider,
            "base_url": agent.base_url,
            "api_key": agent.api_key,
            "api_mode": agent.api_mode,
            "client": agent.client,
            "client_kwargs": deepcopy(agent._client_kwargs),
            "primary_runtime": deepcopy(agent._primary_runtime),
            "system_prompt": agent._cached_system_prompt,
        }
        result = agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=request,
        )
        assert any(name == "route.applied" for name, _payload in events), events
        assert any(name == "route.completed" for name, _payload in events), events
        assert len(requests) == 2
        assert requests[1]["model"] == routed_model
        _assert_wire_invariants(requests[0])
        _assert_wire_invariants(requests[1])
        assert requests[0]["messages"] == requests[1]["messages"]
        _assert_result_invariants(baseline_result)
        _assert_result_invariants(result)

        assert {
            "model": agent.model,
            "provider": agent.provider,
            "requested_provider": agent.requested_provider,
            "base_url": agent.base_url,
            "api_key": agent.api_key,
            "api_mode": agent.api_mode,
            "client": agent.client,
            "client_kwargs": agent._client_kwargs,
            "primary_runtime": agent._primary_runtime,
            "system_prompt": agent._cached_system_prompt,
        } == original_runtime
    finally:
        agent.close()


def test_real_route_restores_exact_runtime_after_provider_error(
    monkeypatch, local_provider
):
    base_url, requests = local_provider
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)
    agent = _agent(base_url, session_id="e2e-runtime-error")
    events: list[tuple[str, dict[str, Any]]] = []
    try:
        agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=TurnRoutingRequest(
                surface="e2e-error-baseline",
                session_id=agent.session_id,
                user_text=PROMPT,
                config_loader=lambda: {"mode": "off"},
            ),
        )
        original_runtime = _runtime_snapshot(agent)
        _ProviderHandler.chat_status = 400
        outcome: dict[str, Any] | Exception
        try:
            outcome = agent.run_conversation(
                PROMPT,
                conversation_history=deepcopy(HISTORY),
                turn_routing_request=_openrouter_route_request(
                    agent,
                    events,
                    surface="e2e-provider-error",
                ),
            )
        except Exception as exc:  # provider adapters differ on terminal 4xx shape
            outcome = exc

        assert len(requests) == 2
        assert requests[1]["model"] == "openai/gpt-4o"
        assert any(name == "route.applied" for name, _payload in events), events
        assert events[-1][0] in {"route.completed", "route.degraded"}
        assert _runtime_snapshot(agent) == original_runtime
        if isinstance(outcome, dict):
            assert outcome.get("completed") is not True
        else:
            assert "deterministic local provider failure" in str(outcome)
    finally:
        _ProviderHandler.chat_status = 200
        agent.close()


def test_real_route_restores_exact_runtime_after_inflight_interrupt(
    monkeypatch, local_provider
):
    base_url, requests = local_provider
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)
    agent = _agent(base_url, session_id="e2e-runtime-interrupt")
    events: list[tuple[str, dict[str, Any]]] = []
    release = threading.Event()
    outcome: dict[str, Any] = {}

    try:
        agent.run_conversation(
            PROMPT,
            conversation_history=deepcopy(HISTORY),
            turn_routing_request=TurnRoutingRequest(
                surface="e2e-interrupt-baseline",
                session_id=agent.session_id,
                user_text=PROMPT,
                config_loader=lambda: {"mode": "off"},
            ),
        )
        original_runtime = _runtime_snapshot(agent)
        _ProviderHandler.request_seen.clear()
        _ProviderHandler.block_release = release

        def _run() -> None:
            try:
                outcome["result"] = agent.run_conversation(
                    PROMPT,
                    conversation_history=deepcopy(HISTORY),
                    turn_routing_request=_openrouter_route_request(
                        agent,
                        events,
                        surface="e2e-provider-interrupt",
                    ),
                )
            except Exception as exc:
                outcome["exception"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        assert _ProviderHandler.request_seen.wait(timeout=5)
        agent.interrupt()
        release.set()
        thread.join(timeout=10)

        assert not thread.is_alive()
        assert len(requests) == 2
        assert requests[1]["model"] == "openai/gpt-4o"
        assert any(name == "route.applied" for name, _payload in events), events
        assert events[-1][0] in {"route.completed", "route.degraded"}
        assert _runtime_snapshot(agent) == original_runtime
        if "result" in outcome:
            assert outcome["result"].get("interrupted") is True
        else:
            assert "exception" in outcome
    finally:
        release.set()
        _ProviderHandler.block_release = None
        agent.clear_interrupt()
        agent.close()


def test_real_oneshot_entry_reaches_provider_without_route_metadata(
    monkeypatch, local_provider
):
    base_url, requests = local_provider
    import agent.turn_routing_runtime as runtime
    import hermes_cli.config as config_module
    import hermes_cli.oneshot as oneshot
    import hermes_cli.runtime_provider as provider_module


    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "model": {"default": MODEL, "provider": "openai"},
            "routing": _config(),
        },
    )
    monkeypatch.setattr(
        provider_module,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "synthetic-test-key",
            "base_url": base_url,
            "provider": "openai",
            "requested_provider": "openai",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(runtime, "load_turn_routing_config", lambda: _config())
    monkeypatch.setattr(runtime, "load_turn_moa_config", lambda: None)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)

    response, result = oneshot._run_agent(
        PROMPT,
        model=MODEL,
        provider="openai",
        use_config_toolsets=False,
    )

    assert response == "local provider response"
    assert result["final_response"] == response
    assert len(requests) == 1
    assert requests[0]["messages"][-1] == {"role": "user", "content": PROMPT}
    assert "turn_routing_request" not in json.dumps(requests[0]["messages"])


def _configure_inline_tui(monkeypatch, tmp_path: Path, emitted: list[tuple]):
    from tui_gateway import server

    monkeypatch.setattr(server, "_emit", lambda *args, **_kwargs: emitted.append(args))
    monkeypatch.setattr(server, "make_stream_renderer", lambda _cols: None)
    monkeypatch.setattr(server, "render_message", lambda _raw, _cols: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(
        server, "_sync_session_key_after_compress", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_args: False)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_load_turn_routing_config", lambda: _config())
    monkeypatch.setattr(server, "_load_turn_moa_config", lambda: None)


def test_real_inline_tui_prompt_path_preserves_history_and_consumes_intent(
    monkeypatch, tmp_path, local_provider
):
    from tui_gateway import server

    base_url, requests = local_provider
    agent = _agent(base_url, session_id="e2e-inline-tui")
    emitted: list[tuple] = []
    _configure_inline_tui(monkeypatch, tmp_path, emitted)
    session = {
        "agent": agent,
        "session_key": agent.session_id,
        "history": deepcopy(HISTORY),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "one_turn_route_target": _target(),
    }
    server._sessions["e2e-inline-tui"] = session
    try:
        server._run_prompt_submit(
            "e2e-inline-request", "e2e-inline-tui", session, PROMPT
        )
        run_thread = session.get("_run_thread")
        assert run_thread is not None
        run_thread.join(timeout=15)
        assert not run_thread.is_alive()

        assert len(requests) == 1
        _assert_wire_invariants(requests[0])
        assert "one_turn_route_target" not in session
        assert session["history"][-4:-1] == [
            *HISTORY,
            {"role": "user", "content": PROMPT},
        ]
        assert any(args[0] == "route.decided" for args in emitted)
    finally:
        server._sessions.pop("e2e-inline-tui", None)
        agent.close()


def _read_json_frame(process: subprocess.Popen, timeout: float) -> dict[str, Any]:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready = selector.select(timeout=min(0.25, deadline - time.monotonic()))
            if not ready:
                if process.poll() is not None:
                    stderr = process.stderr.read() if process.stderr else ""
                    raise AssertionError(
                        f"compute host exited {process.returncode}: {stderr[-2000:]}"
                    )
                continue
            raw = process.stdout.readline()
            if raw:
                return json.loads(raw)
        raise TimeoutError("timed out waiting for compute-host frame")
    finally:
        selector.close()


def test_actual_compute_host_process_runs_real_routed_turn(
    tmp_path, local_provider
):
    base_url, requests = local_provider
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                f"  default: {MODEL}",
                "  provider: openrouter",
                "routing:",
                "  mode: off",
                "  routes:",
                "    local:",
                "      kind: model",
                "      provider: openrouter",
                f"      model: {MODEL}",
                "  lanes:",
                "    plain: local",
                "  budget:",
                "    grok_weekly_limit: 0",
                "tools:",
                "  enabled_toolsets: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(profile_home),
            "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0",
            "HERMES_IGNORE_RULES": "1",
            "OPENROUTER_API_KEY": "synthetic-test-key",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "tui_gateway.compute_host"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    frame = {
        "type": "turn.start",
        "sid": "e2e-compute-host",
        "request_id": "e2e-compute-turn",
        "session_key": "e2e-compute-session",
        "text": PROMPT,
        "history": deepcopy(HISTORY),
        "history_version": 0,
        "profile_home": str(profile_home),
        "cwd": str(tmp_path),
        "cols": 80,
        "source": "tui",
        "model_override": {
            "model": MODEL,
            "provider": "openrouter",
            "base_url": base_url,
            "api_mode": "chat_completions",
        },
        "one_turn_route_target": {
            "kind": "model",
            "provider": "openrouter",
            "model": MODEL,
        },
        "turn_routing_state": {"turn_sequence": 4},
    }
    try:
        process.stdin.write(json.dumps(frame) + "\n")
        process.stdin.flush()
        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            received = _read_json_frame(process, max(0.1, deadline - time.monotonic()))
            seen.append(received)
            if (
                received.get("type") == "turn.end"
                and received.get("request_id") == "e2e-compute-turn"
            ):
                break
            if received.get("type") == "turn.error":
                raise AssertionError(received)
        else:
            raise TimeoutError(f"turn.end not observed; frames={seen[-10:]}")

        assert len(requests) == 1
        _assert_wire_invariants(requests[0])
        ended = next(item for item in seen if item.get("type") == "turn.end")
        assert ended["message_count"] >= 4
        assert ended["turn_routing_state"]["turn_sequence"] == 5
        assert any(
            item.get("type") == "rpc"
            and ((item.get("message") or {}).get("params") or {}).get("type")
            == "route.decided"
            for item in seen
        )
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(
                    json.dumps(
                        {"type": "shutdown", "request_id": "e2e-shutdown"}
                    )
                    + "\n"
                )
                process.stdin.flush()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
