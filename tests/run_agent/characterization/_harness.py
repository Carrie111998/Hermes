"""Shared helpers for the agent-loop characterization (pinning) harness.

These tests are a BEHAVIOR-EQUIVALENCE ORACLE.  They pin the *current*
``AIAgent.run_conversation`` behavior (Hermes 0.11.0, run_agent.py monolith)
so that an upcoming upstream refactor — which splits run_agent.py into
``agent/*`` modules — can be proven behavior-preserving by re-running them
green.  Passing against the current code is what makes them a valid oracle.

The model-call seam is the SDK client constructed from the module-level
``run_agent.OpenAI`` symbol.  We monkeypatch that symbol to return a single
shared :class:`ScriptedClient` instance whose ``.chat.completions.create``
pops successive scripted responses and records every received ``kwargs``.

Why a *single shared* instance (not ``lambda **k: NewClient()``)?  The agent
builds a primary client at construction AND may build per-request clients
inside ``_interruptible_api_call`` / ``_handle_max_iterations``.  A shared
instance keeps one response cursor across all of them, so multi-turn scripts
advance deterministically regardless of which client object the loop reaches
for.

All filesystem / persistence side-effects are neutralized via
``patch.object`` on ``_persist_session`` / ``_save_trajectory`` /
``_cleanup_task_resources`` (the pattern proven in
``tests/run_agent/test_413_compression.py``).
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Scripted model client (the OpenAI-symbol seam)
# ---------------------------------------------------------------------------


def build_response(fixture: Dict[str, Any]) -> SimpleNamespace:
    """Convert a JSON-ish fixture into the SimpleNamespace response shape that
    ``ChatCompletionsTransport.normalize_response`` expects.

    fixture keys (all optional except as noted):
        content: str | None        — assistant text
        reasoning: str | None       — structured reasoning field
        finish_reason: str          — e.g. "stop", "tool_calls", "length"
        tool_calls: list[dict]       — each {id, name, arguments}
            * ``arguments`` may be a dict (auto-serialized) or a JSON string.
        usage: dict | None           — {prompt_tokens, completion_tokens, total_tokens}

    The shape mirrors an OpenAI ChatCompletion:
        response.choices[0].message.{content, reasoning, tool_calls}
        response.choices[0].finish_reason
        response.usage
    """
    raw_tool_calls = fixture.get("tool_calls") or []
    tool_calls = None
    if raw_tool_calls:
        tool_calls = []
        for tc in raw_tool_calls:
            args = tc.get("arguments", "{}")
            if isinstance(args, (dict, list)):
                args = json.dumps(args)
            tool_calls.append(
                SimpleNamespace(
                    id=tc["id"],
                    type="function",
                    function=SimpleNamespace(name=tc["name"], arguments=args),
                )
            )

    message = SimpleNamespace(
        content=fixture.get("content"),
        reasoning=fixture.get("reasoning"),
        # reasoning_content is read via getattr by normalize_response; default None.
        reasoning_content=fixture.get("reasoning_content"),
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(
        message=message,
        finish_reason=fixture.get("finish_reason", "stop"),
    )

    usage = None
    usage_fixture = fixture.get("usage")
    if usage_fixture:
        usage = SimpleNamespace(**usage_fixture)

    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model=fixture.get("model", "characterization/model"),
    )


# ---------------------------------------------------------------------------
# Provider-branch response shapes (the SECOND mock seam)
# ---------------------------------------------------------------------------
#
# api_mode={anthropic_messages,codex_responses} do NOT route through the
# ``run_agent.OpenAI`` chat.completions seam — they dispatch through the
# instance methods ``_anthropic_messages_create`` / ``_run_codex_stream``
# inside ``_interruptible_api_call`` (run_agent.py).  Because the harness
# forces ``_disable_streaming=True``, the loop takes the non-streaming
# ``_interruptible_api_call`` path for every api_mode, so patching those two
# methods is sufficient to characterize the whole provider loop branch with
# no real client call.  The same fixture vocabulary (content / finish_reason /
# tool_calls) feeds all three seams; these builders translate one fixture into
# the *provider-native* response shape each transport's ``normalize_response``
# consumes (shapes verified against test_pure_seams.py::TestAnthropicNormalize
# and ::TestCodexNormalize).


# Reverse of ``AnthropicTransport._STOP_REASON_MAP``: the fixture carries the
# OpenAI-style normalized ``finish_reason``; Anthropic responses carry a raw
# ``stop_reason`` that the transport maps back.  Build the raw value so the
# round-trip through ``map_finish_reason`` reproduces the fixture's intent.
_ANTHROPIC_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def build_anthropic_response(fixture: Dict[str, Any]) -> SimpleNamespace:
    """Convert a fixture into the Anthropic Messages-API response shape.

    ``AnthropicTransport.normalize_response`` iterates ``response.content``
    (a list of ``text`` / ``thinking`` / ``tool_use`` blocks) and reads
    ``response.stop_reason``.  Tool-call ``arguments`` (a JSON string or dict
    in the fixture) become the block's ``input`` *dict*, mirroring the wire
    format.  ``usage`` is present-but-None (the transport reads it via getattr).
    """
    blocks: List[SimpleNamespace] = []
    content = fixture.get("content")
    if content:
        blocks.append(SimpleNamespace(type="text", text=content))
    for tc in fixture.get("tool_calls") or []:
        args = tc.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=tc["id"],
                name=tc["name"],
                input=args,
            )
        )
    finish_reason = fixture.get("finish_reason", "stop")
    stop_reason = _ANTHROPIC_STOP_REASON.get(finish_reason, "end_turn")
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=None)


def build_codex_response(fixture: Dict[str, Any]) -> SimpleNamespace:
    """Convert a fixture into the Codex Responses-API response shape.

    ``_normalize_codex_response`` iterates ``response.output`` (a list of
    ``message`` / ``function_call`` items) and reads ``response.status``.  A
    fixture ``finish_reason == "incomplete"`` becomes ``status="incomplete"``
    (with a non-empty ``output`` so ``validate_response`` passes), which
    ``_normalize_codex_response`` maps to ``finish_reason="incomplete"`` and
    drives the loop's codex continuation branch (run_agent.py:11088).
    ``incomplete_details`` is None so the loop's raw-status check
    (run_agent.py:9664) does NOT divert into the length/truncation branch.
    """
    output: List[SimpleNamespace] = []
    content = fixture.get("content")
    if content:
        output.append(
            SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[SimpleNamespace(type="output_text", text=content)],
            )
        )
    for tc in fixture.get("tool_calls") or []:
        args = tc.get("arguments", "{}")
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        output.append(
            SimpleNamespace(
                type="function_call",
                id=tc["id"],
                call_id=tc["id"],
                name=tc["name"],
                arguments=args,
                status="completed",
            )
        )
    finish_reason = fixture.get("finish_reason", "stop")
    status = "incomplete" if finish_reason == "incomplete" else "completed"
    return SimpleNamespace(output=output, status=status, incomplete_details=None)


def _summarize_provider_call(api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """4-key call summary for provider seams, identical in shape to the OpenAI
    seam's so the golden's ``create_call_kwargs_summary`` stays uniform.

    Anthropic kwargs carry ``messages``; Codex Responses kwargs carry ``input``
    — count whichever is present.
    """
    msgs = api_kwargs.get("messages")
    if msgs is None:
        msgs = api_kwargs.get("input")
    msgs = msgs if isinstance(msgs, list) else []
    tools = api_kwargs.get("tools")
    tools = tools if isinstance(tools, list) else []
    return {
        "model": api_kwargs.get("model"),
        "message_count": len(msgs),
        "tool_count": len(tools),
        "has_tools": bool(tools),
    }


class _ScriptCursor:
    """Shared response cursor + call log across every provider seam.

    A single cursor backs the OpenAI ``chat.completions`` seam AND the
    Anthropic / Codex provider seams, so a multi-turn script advances
    deterministically no matter which seam the loop dispatches to, and every
    model call the loop issues is recorded exactly once in ``calls`` (the
    basis for the golden's ``create_call_kwargs_summary``).  An exhausted
    script raises rather than silently looping — that means the loop made more
    calls than the scenario anticipated, which is a test bug.
    """

    def __init__(self, script: List[Dict[str, Any]]):
        self._script = list(script)
        self._idx = 0
        self.calls: List[Dict[str, Any]] = []

    def take(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(summary)
        if self._idx >= len(self._script):
            raise AssertionError(
                f"Scripted seam exhausted: loop requested response "
                f"#{self._idx + 1} but only {len(self._script)} were scripted. "
                f"(summary={summary})"
            )
        fixture = self._script[self._idx]
        self._idx += 1
        return fixture

    @property
    def call_count(self) -> int:
        return self._idx


class _ScriptedCompletions:
    """``client.chat.completions`` stand-in.

    ``create(**kwargs)`` records a compact kwargs summary and pops the next
    scripted response via the shared cursor.
    """

    def __init__(self, cursor: _ScriptCursor):
        self._cursor = cursor

    def create(self, **kwargs):  # noqa: D401 — SDK-compatible signature
        messages = kwargs.get("messages") or []
        tools = kwargs.get("tools") or []
        fixture = self._cursor.take(
            {
                "model": kwargs.get("model"),
                "message_count": len(messages),
                "tool_count": len(tools),
                "has_tools": bool(tools),
            }
        )
        return build_response(fixture)

    @property
    def call_count(self) -> int:
        return self._cursor.call_count


class ScriptedClient:
    """Minimal OpenAI-client stand-in returned by the patched ``run_agent.OpenAI``.

    Exposes ``.chat.completions.create``.  Accepts and ignores arbitrary
    construction kwargs (api_key, base_url, http_client, ...).  Has no
    ``is_closed`` / ``_client`` attribute, so ``_is_openai_client_closed``
    always reports it as live and the agent never tries to rebuild it.

    ``.calls`` aliases the shared cursor's call log, so provider seams that
    advance the same cursor are reflected in ``summarize_create_calls`` even
    though they never touch ``chat.completions``.
    """

    def __init__(self, script: List[Dict[str, Any]]):
        self._cursor = _ScriptCursor(script)
        self.calls: List[Dict[str, Any]] = self._cursor.calls
        self._completions = _ScriptedCompletions(self._cursor)
        self.chat = SimpleNamespace(completions=self._completions)

    # The agent calls .close() on per-request clients; make it a no-op that
    # does NOT invalidate the shared script cursor.
    def close(self) -> None:  # noqa: D401
        return None

    @property
    def call_count(self) -> int:
        return self._cursor.call_count


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def make_agent(
    script: List[Dict[str, Any]],
    stack: ExitStack,
    *,
    tool_names: Optional[List[str]] = None,
    tool_result: Any = None,
    max_iterations: int = 4,
    model: str = "characterization-model",
    api_mode: Optional[str] = None,
    task_id: str = "characterization-task",
    **overrides: Any,
) -> "object":
    """Construct an ``AIAgent`` wired to a shared :class:`ScriptedClient`.

    Safe defaults: ``quiet_mode=True``, ``skip_memory=True``,
    ``skip_context_files=True``, ``max_iterations=4``, ``platform="cli"``.
    ``_disable_streaming`` is forced on and ``_cached_system_prompt`` is set so
    no prompt build / disk read happens.  ``get_tool_definitions`` and
    ``handle_function_call`` are monkeypatched so ``valid_tool_names`` and tool
    results are fully controlled and hermetic.

    The caller owns ``stack`` (an ``ExitStack``); all patches that must stay
    live for the duration of ``run_conversation`` are entered on it.  Returns
    the constructed agent.  The shared client is reachable as
    ``agent._characterization_client`` for kwargs assertions.

    ``task_id`` is fixed (not a uuid) so message/identity snapshots are stable.
    """
    import run_agent
    from run_agent import AIAgent

    names = tool_names if tool_names is not None else ["read_file"]
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]

    if tool_result is None:
        tool_result = json.dumps({"ok": True})

    def _fake_handle_function_call(name, args, task_id=None, **kwargs):
        if callable(tool_result):
            return tool_result(name, args)
        if isinstance(tool_result, dict):
            return json.dumps(tool_result)
        return tool_result

    shared_client = ScriptedClient(script)

    # Patches that must remain active across construction AND run_conversation.
    stack.enter_context(
        patch.object(run_agent, "OpenAI", lambda **kwargs: shared_client)
    )
    stack.enter_context(
        patch.object(run_agent, "get_tool_definitions", lambda *a, **k: tool_defs)
    )
    stack.enter_context(
        patch.object(run_agent, "handle_function_call", _fake_handle_function_call)
    )
    # v0.15.1 catch-up: upstream's extracted agent_init / ContextCompressor query
    # get_model_context_length during construction, which makes REAL network calls
    # (Ollama /api/show POST + local-server-detect GET to base_url/api/v1/models)
    # for models not in static metadata — that hangs the hermetic harness and
    # trips the new 30s pytest-timeout. Patch the two network helpers inside
    # model_metadata (called via the module's own namespace, so this covers every
    # get_model_context_length caller incl. ContextCompressor) so resolution falls
    # back to static/default with no I/O. Init-time context length is not the loop
    # behavior these characterization tests pin.
    # detect_local_server_type does the GET and is the common chokepoint for
    # both _query_local_context_length and query_ollama_num_ctx; _query_ollama_api_show
    # does the separate POST. Patching both eliminates all init-time network I/O.
    stack.enter_context(
        patch("agent.model_metadata.detect_local_server_type", lambda *a, **k: None)
    )
    stack.enter_context(
        patch("agent.model_metadata._query_ollama_api_show", lambda *a, **k: None)
    )
    stack.enter_context(
        patch("agent.model_metadata._query_local_context_length", lambda *a, **k: None)
    )

    init_kwargs: Dict[str, Any] = dict(
        model=model,
        api_key="characterization-key-0000000000",
        base_url="http://localhost:8080/v1",
        platform="cli",
        max_iterations=max_iterations,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    if api_mode is not None:
        init_kwargs["api_mode"] = api_mode
    init_kwargs.update(overrides)

    agent = AIAgent(**init_kwargs)
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are a helpful assistant."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._characterization_client = shared_client
    agent._characterization_task_id = task_id

    # Second mock seam: provider api_modes bypass the OpenAI chat.completions
    # symbol and dispatch through these instance methods (run_agent.py).  Patch
    # them onto the agent so they pop from the SAME shared cursor as the OpenAI
    # seam — one script, one call log, regardless of which seam the loop reaches.
    # (Patched as instance attributes => called unbound: the signatures below
    # match the loop's call sites, no leading ``self``.)
    cursor = shared_client._cursor
    if api_mode == "anthropic_messages":
        def _scripted_anthropic_messages_create(api_kwargs, client=None):
            # 0.19.0: chat_completion_helpers passes the per-request client
            # (agent/chat_completion_helpers.py: `client=request_client`).
            # The scripted stub ignores it — fixtures fully determine the reply.
            fixture = cursor.take(_summarize_provider_call(api_kwargs))
            return build_anthropic_response(fixture)

        stack.enter_context(
            patch.object(
                agent, "_anthropic_messages_create",
                _scripted_anthropic_messages_create,
            )
        )
    elif api_mode == "codex_responses":
        def _scripted_run_codex_stream(api_kwargs, client=None, on_first_delta=None):
            fixture = cursor.take(_summarize_provider_call(api_kwargs))
            return build_codex_response(fixture)

        stack.enter_context(
            patch.object(
                agent, "_run_codex_stream",
                _scripted_run_codex_stream,
            )
        )

    # Neutralize filesystem / persistence side-effects for the whole turn.
    stack.enter_context(patch.object(agent, "_persist_session"))
    stack.enter_context(patch.object(agent, "_save_trajectory"))
    stack.enter_context(patch.object(agent, "_cleanup_task_resources"))
    stack.enter_context(patch.object(agent, "_save_session_log"))

    return agent


def run_scenario(agent, user_message: str = "do the thing") -> Dict[str, Any]:
    """Drive one turn through ``run_conversation`` with the fixed task_id."""
    return agent.run_conversation(
        user_message,
        task_id=getattr(agent, "_characterization_task_id", "characterization-task"),
    )


# ---------------------------------------------------------------------------
# Message-fingerprint normalization (strip volatile fields)
# ---------------------------------------------------------------------------


def _short_content(content: Any) -> Any:
    """Stable, low-entropy view of a message's content."""
    if content is None:
        return None
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return ""
        # Keep short content verbatim (it's load-bearing for the snapshot);
        # collapse long content to a length marker so reasoning blobs / large
        # tool results don't make snapshots brittle.
        if len(stripped) <= 80:
            return stripped
        return f"<len={len(stripped)}>"
    if isinstance(content, list):
        return f"<list:{len(content)}>"
    if isinstance(content, dict):
        return "<dict>"
    return f"<{type(content).__name__}>"


def normalize_messages_for_snapshot(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reduce each message to a stable fingerprint.

    Strips volatile fields (timestamps, uuids, reasoning blobs, opaque ids).
    Each fingerprint:
        {role, has_tool_calls, tool_names (sorted), tool_call_ref (bool),
         content}
    where ``content`` is the short/verbatim view from :func:`_short_content`.
    Tool-call *ids* are reduced to a boolean (present or not) because they may
    be deterministic-hashed or provider-shaped — only their presence and the
    tool *names* are behavior.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append({"role": "<non-dict>", "type": type(msg).__name__})
            continue
        role = msg.get("role")
        tool_calls = msg.get("tool_calls") or []
        tool_names = sorted(
            (tc.get("function", {}) or {}).get("name", "")
            for tc in tool_calls
            if isinstance(tc, dict)
        )
        fp: Dict[str, Any] = {
            "role": role,
            "has_tool_calls": bool(tool_calls),
            "tool_names": tool_names,
            "content": _short_content(msg.get("content")),
        }
        if role == "tool":
            # Pin only that a tool result references *some* call id, not the id.
            fp["tool_call_ref"] = bool(msg.get("tool_call_id"))
        out.append(fp)
    return out


def summarize_create_calls(client: ScriptedClient) -> List[Dict[str, Any]]:
    """Stable summary of every create() the loop issued."""
    return list(client.calls)


def result_fingerprint(result: Dict[str, Any], client: ScriptedClient) -> Dict[str, Any]:
    """Assemble the full scenario fingerprint pinned by the golden file."""
    return {
        "final_response": result.get("final_response"),
        "completed": result.get("completed"),
        "partial": result.get("partial"),
        "api_calls": result.get("api_calls"),
        "error": result.get("error"),
        "messages_fingerprint": normalize_messages_for_snapshot(
            result.get("messages") or []
        ),
        "create_call_kwargs_summary": summarize_create_calls(client),
    }


# ---------------------------------------------------------------------------
# Golden comparison
# ---------------------------------------------------------------------------

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _golden_path(scenario_name: str) -> str:
    return os.path.join(_FIXTURES_DIR, scenario_name, "expected_golden.json")


def golden_compare(actual: Dict[str, Any], scenario_name: str) -> None:
    """Write the golden on first run; assert equality thereafter.

    Set ``HERMES_CHAR_REGEN=1`` to force-regenerate every golden (use sparingly
    — only when an intentional behavior change is being re-pinned).
    """
    path = _golden_path(scenario_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    actual_json = json.dumps(actual, indent=2, sort_keys=True)

    regen = os.environ.get("HERMES_CHAR_REGEN") == "1"
    if regen or not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(actual_json + "\n")
        # First-run write is allowed; on regen we still continue (no assert).
        return

    with open(path, "r", encoding="utf-8") as fh:
        expected_json = fh.read().strip()

    assert actual_json.strip() == expected_json, (
        f"Characterization drift for scenario '{scenario_name}'.\n"
        f"The current agent-loop behavior no longer matches the pinned golden "
        f"at {path}.\n"
        f"If this change is intentional, re-pin with HERMES_CHAR_REGEN=1.\n\n"
        f"--- expected ---\n{expected_json}\n\n--- actual ---\n{actual_json}"
    )
