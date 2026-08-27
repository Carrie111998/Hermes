from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent


def _prompt(*criteria: tuple[str, str, int]) -> str:
    payload = {
        "version": 1,
        "artifact_path": "deliverable.md",
        "criteria": [
            {"name": name, "text": text, "expected_count": expected_count}
            for name, text, expected_count in criteria
        ],
    }
    return (
        "Produce the requested artifact.\n"
        "<HERMES_ARTIFACT_CONTRACT>\n"
        f"{json.dumps(payload)}\n"
        "</HERMES_ARTIFACT_CONTRACT>"
    )


@pytest.mark.asyncio
async def test_acp_required_lane_rejects_missing_contract_without_agent_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "required")
    monkeypatch.setattr(HermesACPAgent, "__abstractmethods__", frozenset())

    class NeverRunAgent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("model path must not run without a contract")

    state = SimpleNamespace(agent=NeverRunAgent(), session_id="missing-contract", cwd=".")
    manager = SimpleNamespace(get_session=lambda _session_id: state)
    updates = []

    class Connection:
        async def session_update(self, session_id, update):
            updates.append((session_id, update))

    server = cast(Any, HermesACPAgent)(session_manager=cast(Any, manager))
    server.on_connect(cast(Any, Connection()))
    response = await server.prompt(
        prompt=[TextContentBlock(type="text", text="uncertified task")],
        session_id="missing-contract",
    )

    assert response.stop_reason == "refusal"
    assert len(updates) == 1
    assert updates[0][1].content.text.startswith("ARTIFACT CERTIFICATION CONTRACT FAIL")


@pytest.mark.asyncio
async def test_acp_buffers_hostile_draft_and_emits_runtime_failure_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acp_adapter.multica_artifact_dispatch as dispatch
    import agent.title_generator as title_generator

    monkeypatch.setenv("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "required")
    monkeypatch.setattr(HermesACPAgent, "__abstractmethods__", frozenset())
    monkeypatch.setattr(dispatch, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(title_generator, "maybe_auto_title", lambda *_args, **_kwargs: None)

    leaked_tool_completions = []

    def preexisting_tool_complete_callback(*args):
        leaked_tool_completions.append(args)

    class HostileAgent:
        model = "hostile-test"
        provider = "test"
        base_url = ""
        api_key = ""
        api_mode = ""
        session_id = "internal-hostile"
        context_compressor = None
        tools = []
        _cached_system_prompt = ""
        stream_delta_callback = None
        reasoning_callback = None
        tool_complete_callback = staticmethod(preexisting_tool_complete_callback)
        _persist_disabled = False
        compression_enabled = True
        persisted = []

        def run_conversation(self, **_kwargs):
            assert self.stream_delta_callback is None
            assert self.reasoning_callback is None
            assert self.tool_complete_callback is None
            assert self.compression_enabled is False
            from acp_adapter.edit_approval import get_edit_approval_requester
            from tools import terminal_tool

            assert terminal_tool._get_approval_callback() is None
            assert get_edit_approval_requester() is None
            draft = "PASS. AF-004 appears twice: AF-004"
            (tmp_path / "deliverable.md").write_text(draft, encoding="utf-8")
            messages = [
                {"role": "user", "content": "raw user"},
                {"role": "assistant", "content": draft, "reasoning_content": "SECRET THOUGHT"},
            ]
            self._persist_session(messages, [])
            return {
                "final_response": draft,
                "messages": messages,
            }

        def _persist_session(self, messages, _history):
            if not getattr(self, "_certification_persistence_deferred", False):
                self.persisted.append([dict(message) for message in messages])

    state = SimpleNamespace(
        agent=HostileAgent(),
        session_id="hostile-acp",
        cwd=str(tmp_path),
        history=[],
        cancel_event=threading.Event(),
        runtime_lock=threading.Lock(),
        is_running=False,
        current_prompt_text="",
        interrupted_prompt_text="",
        queued_prompts=[],
    )

    class Manager:
        def get_session(self, _session_id):
            return state

        def save_session(self, _session_id):
            return None

    updates = []

    class Connection:
        async def session_update(self, session_id, update):
            updates.append((session_id, update))

        async def request_permission(self, **_kwargs):
            raise AssertionError("no permission request expected")

    server = cast(Any, HermesACPAgent)(session_manager=cast(Any, Manager()))
    server.on_connect(cast(Any, Connection()))
    response = await server.prompt(
        prompt=[
            TextContentBlock(
                type="text",
                text=_prompt(
                    ("required heading", "## Required", 1),
                    ("single marker", "AF-004", 1),
                ),
            )
        ],
        session_id="hostile-acp",
    )

    assert response.stop_reason == "end_turn"
    emitted = [update.content.text for _sid, update in updates if hasattr(update, "content")]
    assert len(emitted) == 1
    assert emitted[0].startswith(
        "ARTIFACT CERTIFICATION FAIL\n"
        "Runtime-owned verification rejected the agent draft. required heading: expected 1, actual 0; "
        "single marker: expected 1, actual 2\n"
        "Certification run: multica:hostile-acp:"
    )
    assert state.history[-1]["content"].startswith("ARTIFACT CERTIFICATION FAIL")
    assert len(state.agent.persisted) == 1
    durable_text = json.dumps(state.agent.persisted[0])
    assert "PASS. AF-004 appears twice" not in durable_text
    assert "SECRET THOUGHT" not in durable_text
    assert leaked_tool_completions == []
    assert state.agent.tool_complete_callback is preexisting_tool_complete_callback


@pytest.mark.asyncio
async def test_certified_turn_cancellation_restores_persistence_and_running_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acp_adapter.multica_artifact_dispatch as dispatch

    monkeypatch.setenv("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "required")
    monkeypatch.setattr(HermesACPAgent, "__abstractmethods__", frozenset())
    monkeypatch.setattr(dispatch, "get_hermes_home", lambda: tmp_path)
    started = threading.Event()
    release = threading.Event()

    class Agent:
        model = provider = "test"
        base_url = api_key = api_mode = ""
        session_id = "internal-cancel"
        context_compressor = None
        tools = []
        _cached_system_prompt = ""
        _persist_disabled = False
        compression_enabled = True
        persisted = []
        calls = 0
        _user_turn_count = 7
        _turns_since_memory = 3
        _iters_since_skill = 5
        _tool_policy_messages = [{"role": "user", "content": "safe policy input"}]
        _session_messages = [{"role": "assistant", "content": {"nested": "safe"}}]

        def run_conversation(self, **_kwargs):
            self.calls += 1
            self._user_turn_count += 1
            self._turns_since_memory = 0
            self._iters_since_skill = 0
            self._tool_policy_messages[0]["content"] = "RAW DRAFT POLICY INPUT"
            self._session_messages[0]["content"]["nested"] = "mutated"
            state.history[0]["content"]["nested"] = "mutated"
            started.set()
            assert release.wait(timeout=5)
            self._persist_session(
                [{"role": "assistant", "content": "LATE RAW DRAFT"}], []
            )
            return {"final_response": "MARKER", "messages": []}

        def _persist_session(self, messages, _history):
            if not getattr(self, "_certification_persistence_deferred", False):
                self.persisted.append(messages)

    state = SimpleNamespace(
        agent=Agent(), session_id="cancel-acp", cwd=str(tmp_path),
        history=[{"role": "user", "content": {"nested": "safe"}}],
        cancel_event=threading.Event(), runtime_lock=threading.Lock(),
        is_running=False, current_prompt_text="", interrupted_prompt_text="",
        queued_prompts=[],
    )
    manager = SimpleNamespace(get_session=lambda _sid: state)
    server = cast(Any, HermesACPAgent)(session_manager=cast(Any, manager))
    task = asyncio.create_task(server.prompt(
        prompt=[TextContentBlock(type="text", text=_prompt(("marker", "MARKER", 1)))],
        session_id="cancel-acp",
    ))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert state.agent._certification_persistence_deferred is True
    assert state.is_running is True

    queued = await server.prompt(
        prompt=[TextContentBlock(type="text", text=_prompt(("next", "NEXT", 1)))],
        session_id="cancel-acp",
    )
    assert queued.stop_reason == "end_turn"
    assert state.is_running is True
    assert state.agent.calls == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.agent._certification_persistence_deferred is False
    assert state.agent.persisted == []
    assert state.agent._user_turn_count == 7
    assert state.agent._turns_since_memory == 3
    assert state.agent._iters_since_skill == 5
    assert state.agent._tool_policy_messages == [
        {"role": "user", "content": "safe policy input"}
    ]
    assert state.agent._session_messages == [
        {"role": "assistant", "content": {"nested": "safe"}}
    ]
    assert state.history == [{"role": "user", "content": {"nested": "safe"}}]
    assert state.is_running is False
    assert state.current_prompt_text == ""


@pytest.mark.asyncio
async def test_certified_persistence_failure_restores_defer_and_running_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acp_adapter.multica_artifact_dispatch as dispatch

    monkeypatch.setenv("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "required")
    monkeypatch.setattr(HermesACPAgent, "__abstractmethods__", frozenset())
    monkeypatch.setattr(dispatch, "get_hermes_home", lambda: tmp_path)

    class Agent:
        model = provider = "test"
        base_url = api_key = api_mode = ""
        session_id = "internal-persist-fail"
        context_compressor = None
        tools = []
        _cached_system_prompt = ""
        _persist_disabled = False

        def run_conversation(self, **_kwargs):
            (tmp_path / "deliverable.md").write_text("MARKER", encoding="utf-8")
            return {"final_response": "MARKER", "messages": []}

        def _persist_session(self, *_args):
            if not self._certification_persistence_deferred:
                raise OSError("simulated certified persistence failure")

    state = SimpleNamespace(
        agent=Agent(), session_id="persist-fail-acp", cwd=str(tmp_path), history=[],
        cancel_event=threading.Event(), runtime_lock=threading.Lock(),
        is_running=False, current_prompt_text="", interrupted_prompt_text="",
        queued_prompts=[],
    )
    manager = SimpleNamespace(get_session=lambda _sid: state)
    server = cast(Any, HermesACPAgent)(session_manager=cast(Any, manager))

    with pytest.raises(OSError, match="simulated certified persistence failure"):
        await server.prompt(
            prompt=[TextContentBlock(type="text", text=_prompt(("marker", "MARKER", 1)))],
            session_id="persist-fail-acp",
        )

    assert state.agent._certification_persistence_deferred is False
    assert state.is_running is False
    assert state.current_prompt_text == ""


@pytest.mark.asyncio
async def test_certified_turns_are_unique_and_overlapping_prompts_never_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acp_adapter.multica_artifact_dispatch as dispatch
    import agent.title_generator as title_generator

    monkeypatch.setenv("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "required")
    monkeypatch.setattr(HermesACPAgent, "__abstractmethods__", frozenset())
    monkeypatch.setattr(dispatch, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(title_generator, "maybe_auto_title", lambda *_args, **_kwargs: None)

    first_started = threading.Event()
    release_first = threading.Event()

    class Agent:
        model = "test"
        provider = "test"
        base_url = ""
        api_key = ""
        api_mode = ""
        session_id = "internal-overlap"
        context_compressor = None
        tools = []
        _cached_system_prompt = ""
        _supports_active_turn_redirect = True
        _persist_disabled = False
        calls = []

        def redirect(self, _text):
            raise AssertionError("certified prompts must never redirect into an active contract")

        def run_conversation(self, *, user_message, **_kwargs):
            self.calls.append(user_message)
            if "FIRST_MARKER" in user_message:
                first_started.set()
                assert release_first.wait(timeout=5)
                draft = "FIRST_MARKER"
            else:
                draft = "SECOND_MARKER"
            (tmp_path / "deliverable.md").write_text(draft, encoding="utf-8")
            return {
                "final_response": draft,
                "messages": [{"role": "assistant", "content": draft}],
            }

    state = SimpleNamespace(
        agent=Agent(),
        session_id="overlap-acp",
        cwd=str(tmp_path),
        history=[],
        cancel_event=threading.Event(),
        runtime_lock=threading.Lock(),
        is_running=False,
        current_prompt_text="",
        interrupted_prompt_text="",
        queued_prompts=[],
    )

    class Manager:
        def get_session(self, _session_id):
            return state

        def save_session(self, _session_id):
            return None

    updates = []

    class Connection:
        async def session_update(self, session_id, update):
            updates.append((session_id, update))

        async def request_permission(self, **_kwargs):
            raise AssertionError("no permission request expected")

    server = cast(Any, HermesACPAgent)(session_manager=cast(Any, Manager()))
    server.on_connect(cast(Any, Connection()))
    first = asyncio.create_task(
        server.prompt(
            prompt=[TextContentBlock(type="text", text=_prompt(("first", "FIRST_MARKER", 1)))],
            session_id="overlap-acp",
        )
    )
    assert await asyncio.to_thread(first_started.wait, 5)
    second = await server.prompt(
        prompt=[TextContentBlock(type="text", text=_prompt(("second", "SECOND_MARKER", 1)))],
        session_id="overlap-acp",
    )
    release_first.set()
    first_response = await first

    assert second.stop_reason == "end_turn"
    assert first_response.stop_reason == "end_turn"
    assert len(state.agent.calls) == 2
    assert "FIRST_MARKER" in state.agent.calls[0]
    assert "SECOND_MARKER" not in state.agent.calls[0]
    assert "SECOND_MARKER" in state.agent.calls[1]

    ledger = tmp_path / "state" / "artifact_certifications.db"
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute(
            "SELECT run_id, status, contract_hash FROM artifact_certifications ORDER BY recorded_at"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert all(row[0].startswith("multica:overlap-acp:") for row in rows)
    assert [row[1] for row in rows] == ["PASS", "PASS"]
    assert rows[0][2] != rows[1][2]
