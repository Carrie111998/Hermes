"""Deterministic tests for the disabled-by-default Relay boundary."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import relay_tools


def setup_function():
    relay_tools.reset_claims_for_tests()


def test_disabled_path_is_passthrough_and_does_not_require_relay_runtime():
    args = {"path": "docs/a.txt", "content": "private"}
    called = []

    result, observed = relay_tools.execute(
        "write_file",
        args,
        lambda value: called.append(value) or "ok",
        session_id="s",
        task_id="t",
        tool_call_id="c",
        enabled=False,
    )

    assert result == "ok"
    assert observed == args
    assert called == [args]


def test_metadata_adapter_never_emits_raw_values():
    decision = relay_tools.pre_relay_egress(
        "write_file",
        {"path": "docs/a.txt", "content": "private-content"},
        enabled=True,
    )

    assert decision.allow is True
    encoded = json.dumps(decision.payload)
    assert "private-content" not in encoded
    assert "docs/a.txt" not in encoded
    assert decision.payload["argument_types"] == {"path": "string", "content": "string"}
    assert decision.payload["redacted"] == {"path": True, "content": True}
    assert len(encoded.encode()) <= relay_tools.MAX_METADATA_BYTES


@pytest.mark.parametrize(
    ("tool_name", "args", "reason"),
    [
        ("unknown_tool", {}, "tool_not_allowlisted"),
        ("write_file", {"path": "docs/a.txt", "unknown": "x"}, "unknown_argument_field"),
        ("write_file", {"path": "/home/cwliao/.ssh/id_ed25519", "content": "x"}, "sensitive_path"),
        ("write_file", {"path": "docs/a.txt", "content": ["raw"]}, "argument_type_mismatch"),
    ],
)
def test_egress_gate_denies_unclassified_payloads(tool_name, args, reason):
    decision = relay_tools.pre_relay_egress(tool_name, args, enabled=True)
    assert decision.allow is False
    assert decision.reason == reason


def test_enabled_runtime_unavailable_blocks_before_callback():
    called = []
    with pytest.raises(relay_tools.RelayBlockedError) as exc_info:
        relay_tools.execute(
            "web_search",
            {"query": "safe"},
            lambda value: called.append(value) or "should-not-run",
            session_id="s",
            task_id="t",
            tool_call_id="c",
            enabled=True,
            phase="prepare",
        )
    assert exc_info.value.reason == "relay_unavailable"
    assert called == []


def test_default_config_does_not_enable_relay_runtime():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["relay"]["tool_execution"]["enabled"] is False


def test_relay_boundary_preserves_prompt_cache_and_message_roles():
    from agent.tool_executor import _apply_tool_request_middleware_for_agent

    agent = SimpleNamespace(
        session_id="session",
        _current_turn_id="turn",
        _current_api_request_id="api",
        _relay_tool_execution_enabled=False,
        _cached_system_prompt="cached-system-prompt",
    )
    messages = [
        {"role": "system", "content": "cached-system-prompt"},
        {"role": "user", "content": "inspect"},
    ]
    before_messages = [dict(message) for message in messages]

    final_args, trace = _apply_tool_request_middleware_for_agent(
        agent,
        function_name="web_search",
        function_args={"query": "bounded"},
        effective_task_id="task",
        tool_call_id="call",
    )

    assert final_args == {"query": "bounded"}
    assert trace == []
    assert agent._cached_system_prompt == "cached-system-prompt"
    assert messages == before_messages
    assert [message["role"] for message in messages] == ["system", "user"]


def test_candidate_validation_rejects_tool_name_unknown_fields_and_oversize():
    original = {"query": "original"}
    with pytest.raises(relay_tools.RelayBlockedError, match="tool_name_changed"):
        relay_tools._validate_candidate("web_search", original, {"tool_name": "terminal", "args": {"query": "x"}})
    with pytest.raises(relay_tools.RelayBlockedError, match="candidate_unknown_field"):
        relay_tools._validate_candidate("web_search", original, {"query": "x", "extra": True})
    with pytest.raises(relay_tools.RelayBlockedError, match="candidate_oversized"):
        relay_tools._validate_candidate("web_search", original, {"query": "x" * (relay_tools.MAX_CANDIDATE_BYTES + 1)})


def test_claim_is_at_most_once_with_deterministic_two_worker_harness():
    barrier = threading.Barrier(2)
    events = []
    lock = threading.Lock()

    def worker(index):
        barrier.wait(timeout=2)
        try:
            claim = relay_tools.claim_execution("session", "task", "call")
        except relay_tools.RelayBlockedError as exc:
            with lock:
                events.append((index, "blocked", exc.reason))
            barrier.wait(timeout=2)
            return
        with lock:
            events.append((index, "claimed"))
        barrier.wait(timeout=2)
        relay_tools.release_execution(claim)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, index) for index in (0, 1)]
        for future in futures:
            future.result(timeout=3)

    assert sorted(event[1:] for event in events) == [("blocked", "duplicate_execution_claim"), ("claimed",)]


def test_dispatch_claim_releases_on_callback_exception():
    with pytest.raises(RuntimeError, match="boom"):
        relay_tools.execute(
            "web_search",
            {"query": "safe"},
            lambda _args: (_ for _ in ()).throw(RuntimeError("boom")),
            session_id="s",
            task_id="t",
            tool_call_id="c",
            enabled=True,
            prevalidated=True,
        )

    result, _ = relay_tools.execute(
        "web_search",
        {"query": "safe"},
        lambda _args: "ok",
        session_id="s",
        task_id="t",
        tool_call_id="c",
        enabled=True,
        prevalidated=True,
    )
    assert result == "ok"


def test_pre_tool_modify_returns_merged_final_args(monkeypatch):
    from hermes_cli import plugins

    monkeypatch.setattr(
        plugins,
        "invoke_hook",
        lambda *_args, **_kwargs: [
            {"action": "modify", "args": {"path": "docs/final.txt"}},
        ],
    )

    block_message, final_args = plugins._dispatch_pre_tool_call_hooks(
        "write_file",
        {"path": "docs/original.txt", "content": "bounded"},
        task_id="task",
        tool_call_id="call",
    )

    assert block_message is None
    assert final_args == {"path": "docs/final.txt", "content": "bounded"}
