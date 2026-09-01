"""E2E tests: pre_llm_call runtime_override through the REAL conversation loop.

These drive ``AIAgent.run_conversation`` end-to-end against in-process fake
wire clients (no network), with hooks and middleware registered on the real
plugin manager.  They prove the three P1 blockers on the production path
instead of mirroring production ordering with stand-ins:

1. P1-1 — llm_request middleware and ``pre_api_request`` observe the SAME
   route that goes on the wire while a ``runtime_override`` is active.
2. P1-2 — ``override route -> failure -> configured fallback`` never re-enters
   the failed override; the retry is shaped for and sent to the fallback route.
3. P1-3 — an api_mode-changing override (chat_completions -> anthropic_messages)
   from an eagerly-warmed route invalidates/replaces the transport cache and
   derived client state, so no stale transport/client leaks after the turn.

Issue: #23739 / PR #92893.  Reviewer: the original e2e used FakeAgent/FakePluginCtx
mirroring production ordering and could not catch ordering drift.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── fake wire clients ───────────────────────────────────────────────────────


def _chat_response(content: str = "ok"):
    """A valid chat.completions response (the shape test_provider_projection uses)."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=content, tool_calls=[], reasoning=None
            ),
            finish_reason="stop",
        )],
        usage=None,
    )


class _WireRecorder:
    """Records every fake client construction and wire request.

    The OpenAI-wire factory routes on ``base_url`` so the test can make the
    override route fail and the fallback route succeed.  ``*`` is a catch-all
    handler.
    """

    def __init__(self) -> None:
        self.openai_clients: list = []      # (base_url, api_key) per construction
        self.openai_requests: list = []     # kwargs per chat.completions.create
        self.anthropic_builds: list = []    # (api_key, base_url) per anthropic build
        self.anthropic_requests: list = []  # kwargs per messages.create
        self._handlers = {}

    def route(self, base_url: str, handler):
        self._handlers[base_url] = handler
        return self

    def _handler_for(self, base_url: str):
        return self._handlers.get(base_url, self._handlers.get("*"))

    def make_openai(self, **kwargs):
        base_url = str(kwargs.get("base_url") or "")
        api_key = kwargs.get("api_key")
        self.openai_clients.append((base_url, api_key))
        handler = self._handler_for(base_url) or (lambda k: _chat_response())
        return _FakeChatClient(base_url, api_key, handler, self.openai_requests)

    def make_anthropic(self, api_key, base_url=None, **kwargs):
        self.anthropic_builds.append((api_key, base_url))
        return _FakeAnthropicClient(api_key, base_url, self.anthropic_requests)


class _FakeCompletions:
    def __init__(self, handler, sink) -> None:
        self._handler = handler
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return self._handler(kwargs)


class _FakeChatClient:
    def __init__(self, base_url, api_key, handler, sink) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.chat = SimpleNamespace(completions=_FakeCompletions(handler, sink))

    def close(self) -> None:
        pass


class _FakeAnthropicMessages:
    def __init__(self, sink) -> None:
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi from anthropic")],
            stop_reason="end_turn",
            usage=None,
        )


class _FakeAnthropicClient:
    def __init__(self, api_key, base_url, sink) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.messages = _FakeAnthropicMessages(sink)

    def close(self) -> None:
        pass


# ── plugin registration on the real manager ─────────────────────────────────


class _BundledManifest:
    name = "e2e-runtime-override"
    key = "e2e-runtime-override"
    source = "bundled"


def _register_plugin_hooks(manager, *, pre_llm_call=None, pre_api_request=None, llm_request=None):
    """Register callbacks through the real PluginContext path (bundled =>
    trusted => runtime_override survives the trust gate).  Returns disposers."""
    from hermes_cli.plugins import PluginContext

    ctx = object.__new__(PluginContext)
    ctx.manifest = _BundledManifest()
    ctx._manager = manager
    handles = []
    if pre_llm_call is not None:
        handles.append(ctx.register_hook("pre_llm_call", pre_llm_call))
    if pre_api_request is not None:
        handles.append(ctx.register_hook("pre_api_request", pre_api_request))
    if llm_request is not None:
        handles.append(ctx.register_middleware("llm_request", llm_request))
    return handles


# ── agent + turn helpers ────────────────────────────────────────────────────


def _build_agent(monkeypatch, recorder, *, fallback_chain=None):
    """Build a real AIAgent whose wire traffic lands on the fake clients."""
    monkeypatch.setattr("run_agent.OpenAI", recorder.make_openai)
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client", recorder.make_anthropic
    )
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *a, **k: [])
    # Fallback activation resolves the fallback's context length; pin it so no
    # live /models probe can fire in a hermetic test.
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *a, **k: 128000
    )
    # try_activate_fallback constructs the fallback client through the central
    # router; hand it a fake instead of a real SDK client.
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda provider, model=None, **kw: (
            SimpleNamespace(
                api_key="sk-fallback",
                base_url="https://fallback.example.com/v1",
            ),
            model,
        ),
    )

    from run_agent import AIAgent

    agent = AIAgent(
        model="base-model",
        api_key="sk-base",
        base_url="http://localhost:8080/v1",
        platform="cli",
        max_iterations=3,
        quiet_mode=True,
        skip_memory=True,
    )
    agent._disable_streaming = True
    if fallback_chain is not None:
        agent._fallback_chain = fallback_chain
    return agent


def _run_turn(agent):
    """One real ``run_conversation`` turn."""
    return agent.run_conversation("hello")


# ── P1-1: middleware / pre_api_request / wire see one route ────────────────


def test_override_route_is_authoritative_across_middleware_hook_and_wire(monkeypatch):
    """Middleware and pre_api_request observe the override route, and the wire
    call uses exactly that route — one canonical identity for the request."""
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    recorder = _WireRecorder()
    recorder.route(
        "https://override.example.com/v1",
        lambda kwargs: _chat_response("routed"),
    )
    seen_middleware = []
    seen_hook = []

    def pre_llm_call(**kw):
        return {"runtime_override": {
            "model": "override-model",
            "provider": "openai",
            "base_url": "https://override.example.com/v1",
            "api_key": "sk-override",
        }}

    def llm_request(**kw):
        seen_middleware.append((
            kw.get("model"), kw.get("provider"),
            kw.get("base_url"), kw.get("api_mode"),
        ))
        return None

    def pre_api_request(**kw):
        seen_hook.append((
            kw.get("model"), kw.get("provider"),
            kw.get("base_url"), kw.get("api_mode"),
        ))
        return None

    handles = _register_plugin_hooks(
        manager,
        pre_llm_call=pre_llm_call,
        llm_request=llm_request,
        pre_api_request=pre_api_request,
    )
    try:
        agent = _build_agent(monkeypatch, recorder)
        pre = (agent.model, agent.provider, agent.base_url, agent.api_mode)
        result = _run_turn(agent)

        assert "routed" in (result.get("final_response") or "")
        assert seen_middleware, "llm_request middleware never ran"
        assert seen_hook, "pre_api_request hook never ran"

        expected_route = (
            "override-model", "openai",
            "https://override.example.com/v1", "chat_completions",
        )
        # Middleware and pre_api_request were handed the OVERRIDDEN identity.
        assert seen_middleware[0] == expected_route, seen_middleware
        assert seen_hook[0] == expected_route, seen_hook

        # The wire was built from exactly that route.
        assert recorder.openai_requests, "no OpenAI-wire request recorded"
        wire_req = recorder.openai_requests[0]
        assert wire_req.get("model") == "override-model"
        assert ("https://override.example.com/v1", "sk-override") in recorder.openai_clients

        # The turn restored the pre-override identity.
        assert (agent.model, agent.provider, agent.base_url, agent.api_mode) == pre
        assert agent._client_kwargs["api_key"] == "sk-base"
        assert agent._client_kwargs["base_url"] == "http://localhost:8080/v1"
    finally:
        for h in handles:
            h.dispose()


# ── P1-2: fallback supersedes the failed override ──────────────────────────


def test_fallback_after_override_failure_never_reenters_the_override(monkeypatch):
    """override route -> invalid response -> configured fallback: the retry is
    sent to the fallback route (never the failed override), and the final wire
    target shapes the request."""
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    recorder = _WireRecorder()
    recorder.route(
        # The override route fails validation (empty choices) -> eager fallback.
        "https://override.example.com/v1",
        lambda kwargs: SimpleNamespace(choices=[], usage=None),
    )
    recorder.route(
        "https://fallback.example.com/v1",
        lambda kwargs: _chat_response("fallback answered"),
    )

    def pre_llm_call(**kw):
        return {"runtime_override": {
            "model": "override-model",
            "provider": "openai",
            "base_url": "https://override.example.com/v1",
            "api_key": "sk-override",
        }}

    handles = _register_plugin_hooks(manager, pre_llm_call=pre_llm_call)
    try:
        agent = _build_agent(
            monkeypatch,
            recorder,
            fallback_chain=[{
                "model": "gpt-4o-mini",
                "provider": "openai",
                "base_url": "https://fallback.example.com/v1",
                "api_key": "sk-fallback",
            }],
        )
        result = _run_turn(agent)

        assert "fallback answered" in (result.get("final_response") or ""), result

        # The failed override route was hit exactly once (the primary attempt).
        override_reqs = [
            req for req in recorder.openai_requests
            if req.get("model") == "override-model"
        ]
        assert len(override_reqs) == 1, override_reqs

        # The retry went to the fallback route: fallback client + fallback model.
        fallback_reqs = [
            req for req in recorder.openai_requests
            if req.get("model") == "gpt-4o-mini"
        ]
        assert fallback_reqs, "fallback route was never called"
        assert ("https://fallback.example.com/v1", "sk-fallback") in recorder.openai_clients

        # The override was consumed: nothing re-applies it for the rest of the
        # logical request, and the turn ends on the fallback route.
        assert agent._runtime_override == {}
        assert agent.model == "gpt-4o-mini"
        assert agent.provider == "openai"
        assert agent.base_url == "https://fallback.example.com/v1"
        assert agent._client_kwargs["base_url"] == "https://fallback.example.com/v1"
    finally:
        for h in handles:
            h.dispose()


# ── P1-3: api_mode-changing override refreshes transport + client state ────


def test_api_mode_changing_override_invalidates_transport_and_client_state(monkeypatch):
    """chat_completions -> anthropic_messages override from an eagerly-warmed
    route: the transport cache is invalidated and the derived anthropic client
    state is replaced; nothing stale leaks after the turn."""
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    recorder = _WireRecorder()

    def pre_llm_call(**kw):
        return {"runtime_override": {
            "model": "claude-sonnet-4-6",
            "provider": "openai",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "sk-anthropic",
            "api_mode": "anthropic_messages",
        }}

    handles = _register_plugin_hooks(manager, pre_llm_call=pre_llm_call)
    try:
        agent = _build_agent(monkeypatch, recorder)
        # Eagerly warm the transport cache for the pre-override route (mirrors
        # agent_init's warm).
        agent._get_transport()
        assert set(agent._transport_cache) == {"chat_completions"}

        pre_state = {
            "model": agent.model,
            "provider": agent.provider,
            "base_url": agent.base_url,
            "api_mode": agent.api_mode,
            "api_key": agent.api_key,
            "requested_provider": agent.requested_provider,
            "request_overrides": dict(agent.request_overrides or {}),
            "client_kwargs": dict(agent._client_kwargs),
            "anthropic_api_key": getattr(agent, "_anthropic_api_key", None),
            "anthropic_base_url": getattr(agent, "_anthropic_base_url", None),
            "is_anthropic_oauth": getattr(agent, "_is_anthropic_oauth", None),
        }

        result = _run_turn(agent)

        # The turn completed through the ANTHROPIC wire (mode-changing override
        # is authoritative for the whole request lifecycle).
        assert "hi from anthropic" in (result.get("final_response") or ""), result
        assert recorder.anthropic_requests, "no anthropic wire request recorded"
        assert ("sk-anthropic", "https://api.anthropic.com/v1") in recorder.anthropic_builds

        # Transport cache: restored to the pre-override content — the anthropic
        # transport built under the override did not leak back into the cache.
        assert set(agent._transport_cache) == {"chat_completions"}, agent._transport_cache

        # Derived route state fully restored.
        assert agent.model == pre_state["model"]
        assert agent.provider == pre_state["provider"]
        assert agent.base_url == pre_state["base_url"]
        assert agent.api_mode == pre_state["api_mode"]
        assert agent.api_key == pre_state["api_key"]
        assert agent.requested_provider == pre_state["requested_provider"]
        assert dict(agent.request_overrides or {}) == pre_state["request_overrides"]
        assert agent._client_kwargs == pre_state["client_kwargs"]
        # The override created _anthropic_* on a route that had none; the
        # scope must have removed them again (no stale client state leaks).
        assert getattr(agent, "_anthropic_api_key", None) == pre_state["anthropic_api_key"]
        assert getattr(agent, "_anthropic_base_url", None) == pre_state["anthropic_base_url"]
        assert getattr(agent, "_is_anthropic_oauth", None) == pre_state["is_anthropic_oauth"]
    finally:
        for h in handles:
            h.dispose()


# ── BUG-1: turn-2 on fallback + all-invalid override + chain advance ────────


def test_bug1_turn2_on_fallback_all_invalid_override_chain_advance(monkeypatch):
    """BUG-1 regression: the agent is ALREADY on fallback-1 when turn 2 opens
    (rate-limit cooldown keeps the previous turn's fallback active) and the
    plugin returns an override whose keys are all stripped by validation.  A
    mid-scope failure advances the chain to fallback-2; the restore must never
    clobber the fallback-2 route (the old _fallback_activated/route-tuple
    inference could false-negative here; the explicit supersede handoff cannot).
    """
    import time

    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    recorder = _WireRecorder()
    fallback_calls = {"count": 0}
    # Primary route: always fails (invalid empty response) -> drives turn 1
    # onto the fallback chain.
    recorder.route(
        "http://localhost:8080/v1",
        lambda kwargs: SimpleNamespace(choices=[], usage=None),
    )

    def fallback_handler(kwargs):
        # Call 1 (turn 1, fallback-1): valid -> turn 1 ends on fallback-1.
        # Call 2 (turn 2, still fallback-1): invalid -> chain advances.
        # Call 3 (turn 2, fallback-2): valid.
        fallback_calls["count"] += 1
        if fallback_calls["count"] == 2:
            return SimpleNamespace(choices=[], usage=None)
        return _chat_response(f"fallback call {fallback_calls['count']}")

    recorder.route("https://fallback.example.com/v1", fallback_handler)

    armed = {"on": False}

    def pre_llm_call(**kw):
        if armed["on"]:
            # Every key is invalid: bogus api_mode + non-string model are both
            # stripped by validate_runtime_override -> _runtime_override={}.
            return {"runtime_override": {"api_mode": "bogus_wire", "model": 12345}}
        return None

    handles = _register_plugin_hooks(manager, pre_llm_call=pre_llm_call)
    try:
        agent = _build_agent(
            monkeypatch,
            recorder,
            fallback_chain=[
                {
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "base_url": "https://fallback.example.com/v1",
                    "api_key": "sk-fb1",
                },
                {
                    "model": "gpt-4o",
                    "provider": "openai",
                    "base_url": "https://fallback.example.com/v1",
                    "api_key": "sk-fb2",
                },
            ],
        )
        # Turn 1: primary fails -> fallback-1 activates and answers.
        result1 = _run_turn(agent)
        assert "fallback call 1" in (result1.get("final_response") or ""), result1
        assert agent.model == "gpt-4o-mini"
        assert agent._fallback_activated is True

        # Keep the agent on fallback-1 into turn 2 (rate-limit cooldown gates
        # restore_primary_runtime), then arm the all-invalid override.
        agent._rate_limited_until = time.monotonic() + 3600
        armed["on"] = True

        # Turn 2: still on fallback-1; the all-invalid override leaves no
        # scope; the mid-scope failure advances the chain to fallback-2.
        result2 = _run_turn(agent)
        assert "fallback call 3" in (result2.get("final_response") or ""), result2

        # fallback-2 stands — NOT clobbered back to fallback-1 or the primary.
        assert agent.model == "gpt-4o"
        assert agent._fallback_index == 2
        assert agent._runtime_override == {}
    finally:
        for h in handles:
            h.dispose()


# ── LEAK-2: _is_anthropic_oauth restored after a superseded scope ──────────


def test_leak2_oauth_flag_restored_after_static_key_override_superseded(monkeypatch):
    """LEAK-2 regression: an OAuth agent overrides to a static key for one
    turn and the fallback supersedes the override mid-scope.  The static-key
    override unconditionally forced _is_anthropic_oauth=False and the chat
    fallback does not re-derive it, so the superseded scope must restore the
    pre-override OAuth state instead of leaving it permanently False."""
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    recorder = _WireRecorder()
    # The override route fails validation (empty choices) -> eager fallback.
    recorder.route(
        "https://override.example.com/v1",
        lambda kwargs: SimpleNamespace(choices=[], usage=None),
    )
    recorder.route(
        "https://fallback.example.com/v1",
        lambda kwargs: _chat_response("fallback answered"),
    )

    def pre_llm_call(**kw):
        return {"runtime_override": {
            "base_url": "https://override.example.com/v1",
            "api_key": "sk-static",
        }}

    handles = _register_plugin_hooks(manager, pre_llm_call=pre_llm_call)
    try:
        agent = _build_agent(
            monkeypatch,
            recorder,
            fallback_chain=[{
                "model": "gpt-4o-mini",
                "provider": "openai",
                "base_url": "https://fallback.example.com/v1",
                "api_key": "sk-fallback",
            }],
        )
        # Simulate an OAuth anthropic primary: the flag the override clobbers.
        agent._is_anthropic_oauth = True

        result = _run_turn(agent)
        assert "fallback answered" in (result.get("final_response") or ""), result

        # The fallback superseded the scope; the OAuth flag must be restored
        # to its pre-override value, never left permanently False.
        assert agent._is_anthropic_oauth is True
    finally:
        for h in handles:
            h.dispose()
