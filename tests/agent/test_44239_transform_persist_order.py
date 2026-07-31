"""Regression tests for #44239 — transform_llm_output vs. persistence order.

The ``transform_llm_output`` hook fires in ``finalize_turn``. Before the
fix, ``_persist_session`` ran *before* the hook and the transformed text
was never written back into the assistant message, so the user saw the
transformed response while ``result["messages"]``, the JSON log, and the
SQLite session DB all kept the raw model output — which was then replayed
on resume / next turn.

These tests drive ``finalize_turn`` directly with a stub agent and a
patched ``hermes_cli.plugins.invoke_hook`` and assert INVARIANTS:

* the transformed text is synced into the turn's last assistant message
  before persistence (delivered text == persisted text);
* persistence still runs after ``transform_llm_output`` and before
  ``post_llm_call`` (observability plugins may read the session store
  expecting the turn to be there);
* an untransformed response persists the raw model output unchanged;
* the sync never crosses the turn boundary or rewrites a tool-call /
  non-text assistant message.

The final test (``test_real_persist_lands_transformed_text_in_sqlite``)
drives the REAL ``AIAgent._persist_session`` (JSON log + SQLite SessionDB)
against a temporary ``HERMES_HOME`` to verify that the storage level — not
just the in-memory snapshot — contains the transformed text.
"""

import copy
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import hermes_cli.plugins as plugins_mod
from agent.turn_finalizer import (
    _sync_final_response_to_last_assistant,
    finalize_turn,
)


class _StubAgent:
    """Bare-minimum agent surface that finalize_turn touches on the happy
    path (final_response present, not interrupted, budget remaining, no
    footer/explainer, no skill/memory review)."""

    def __init__(self):
        self.max_iterations = 100
        self.iteration_budget = SimpleNamespace(remaining=99, used=1, max_total=100)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.platform = "cli"
        self.session_id = "sess-44239"
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "ok"
        self.session_cost_source = "none"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self._tool_guardrail_halt_decision = None
        self._turn_failed_file_mutations = None
        self._interrupt_message = None
        self._stream_callback = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = set()
        self.events = []          # ordered record of persist + hook firings
        self.persisted_messages = None  # deep copy taken at persist time

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("budget-exhaustion path not expected in these tests")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, messages, user_message, completed):
        pass

    def _cleanup_task_resources(self, task_id):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.events.append("persist")
        self.persisted_messages = copy.deepcopy(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **kwargs):
        pass


def _run_finalize(monkeypatch, transform_result, messages):
    agent = _StubAgent()

    def fake_invoke_hook(hook_name, **kwargs):
        agent.events.append(hook_name)
        if hook_name == "transform_llm_output":
            return [transform_result] if transform_result is not None else []
        return []

    monkeypatch.setattr(plugins_mod, "invoke_hook", fake_invoke_hook)

    result = finalize_turn(
        agent,
        final_response=messages[-1]["content"],
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )
    return agent, result


def test_transformed_response_is_persisted(monkeypatch):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw model output"},
    ]
    agent, result = _run_finalize(
        monkeypatch, "[RENDERED] raw model output", messages,
    )

    assert result["final_response"] == "[RENDERED] raw model output"
    assert result["response_transformed"] is True
    # In-memory history handed back to the caller matches what was shown.
    assert result["messages"][-1]["content"] == "[RENDERED] raw model output"
    # ...and so does what hit the session store.
    assert agent.persisted_messages[-1]["content"] == "[RENDERED] raw model output"


def test_hook_persist_ordering(monkeypatch):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw model output"},
    ]
    agent, _ = _run_finalize(monkeypatch, "[RENDERED] x", messages)

    # Transform must precede persist (so the transform is durable);
    # persist must precede post_llm_call (observability plugins may read
    # the session store inside that hook).
    assert agent.events.index("transform_llm_output") < agent.events.index("persist")
    assert agent.events.index("persist") < agent.events.index("post_llm_call")


def test_untransformed_response_persists_raw(monkeypatch):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw model output"},
    ]
    agent, result = _run_finalize(monkeypatch, None, messages)

    assert result["response_transformed"] is False
    assert agent.persisted_messages[-1]["content"] == "raw model output"


def test_sync_skips_tool_call_assistant_message():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
    ]
    assert _sync_final_response_to_last_assistant(messages, "new") is False
    assert messages[1]["content"] == ""


def test_sync_does_not_cross_turn_boundary():
    messages = [
        {"role": "assistant", "content": "prior turn answer"},
        {"role": "user", "content": "this turn"},
        {"role": "tool", "content": "{}", "tool_call_id": "c1"},
    ]
    assert _sync_final_response_to_last_assistant(messages, "new") is False
    assert messages[0]["content"] == "prior turn answer"


def test_sync_skips_non_text_content():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "multimodal"}]},
    ]
    assert _sync_final_response_to_last_assistant(messages, "new") is False


def test_sync_updates_last_assistant_text_message():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw"},
        {"role": "tool", "content": "{}", "tool_call_id": "c1"},
    ]
    assert _sync_final_response_to_last_assistant(messages, "new") is True
    assert messages[1]["content"] == "new"


def test_sync_clears_db_persisted_marker():
    """A row already flushed to SQLite must be re-written after the sync.

    The incremental tool-call persist stamps ``_db_persisted`` on rows it has
    written, and ``_persist_session`` skips any row carrying that marker. If
    the sync edits content in place without clearing it, the transformed text
    never reaches the durable store — the #44239 symptom, just relocated from
    "persisted too early" to "skipped as already persisted".
    """
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw", "_db_persisted": True},
    ]
    assert _sync_final_response_to_last_assistant(messages, "new") is True
    assert messages[1]["content"] == "new"
    assert "_db_persisted" not in messages[1]


def test_real_persist_lands_transformed_text_in_sqlite(tmp_path, monkeypatch):
    """Storage-level regression: real AIAgent._persist_session against a
    temporary HERMES_HOME so the JSON/SQLite resume path is exercised.

    The stub-based tests above verify ordering and the in-memory snapshot.
    This test drives the REAL AIAgent's _persist_session (which writes to
    both the JSON session log and the SQLite SessionDB) against a temp
    HERMES_HOME, then queries the DB to confirm the transformed text —
    not the raw model output — is what was persisted. If the sync-before-
    persist ordering regresses, the DB will contain the raw output and
    the resume path will replay stale text.
    """
    from run_agent import AIAgent
    from hermes_state import SessionDB

    # Isolated HERMES_HOME so we never touch the user's real session store.
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-persist-test-"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Real SessionDB with a pre-created session.
    db_path = hermes_home / "state.db"
    db = SessionDB(db_path=db_path)
    session_id = "sess-44239-storage"
    db.create_session(session_id=session_id, source="test", model="test-model")

    # Build a bare real AIAgent with the real _persist_session,
    # _drop_trailing_empty_response_scaffolding, _save_session_log,
    # and _flush_messages_to_session_db — the methods that write to
    # the durable store. Stub only the methods that either do real
    # I/O we don't need (cleanup) or throw without a full agent init.
    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id

    # Attributes that finalize_turn reads from the agent.
    agent.max_iterations = 100
    agent.iteration_budget = SimpleNamespace(remaining=99, used=1, max_total=100)
    agent.quiet_mode = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.platform = "cli"
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "ok"
    agent.session_cost_source = "none"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent._tool_guardrail_halt_decision = None
    agent._turn_failed_file_mutations = None
    agent._interrupt_message = None
    agent._stream_callback = None
    agent._response_was_previewed = False
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()

    # Attributes that _persist_session and children need.
    agent.save_trajectories = False
    agent._persist_disabled = False
    agent._session_messages = None
    agent._session_json_enabled = False
    agent._session_init_model_config = None
    agent._parent_session_id = None
    agent._cached_system_prompt = None
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    # commit_memory_session runs heavy machinery not exercised here.
    agent.commit_memory_session = lambda *a, **k: None

    # Stub _cleanup_task_resources: the real method tries
    # _ra().cleanup_vm / _ra().cleanup_browser which aren't available
    # in a bare agent.
    agent._cleanup_task_resources = lambda task_id: None
    # Stub clear_interrupt: the real method references thread/interrupt
    # machinery (_execution_thread_id, _active_children_lock) not present
    # on a bare agent.
    agent.clear_interrupt = lambda: None
    # Stub _sync_external_memory_for_turn: the real method references
    # _memory_manager which isn't set on a bare agent.
    agent._sync_external_memory_for_turn = lambda **kwargs: None

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "raw model output"},
    ]

    events = []

    def fake_invoke_hook(hook_name, **kwargs):
        events.append(hook_name)
        if hook_name == "transform_llm_output":
            return ["[STORED VIA REAL PERSIST] raw model output"]
        return []

    monkeypatch.setattr(plugins_mod, "invoke_hook", fake_invoke_hook)

    result = finalize_turn(
        agent,
        final_response="raw model output",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    # 1. Hook ordering invariant (same as test_hook_persist_ordering).
    assert events.index("transform_llm_output") < events.index("post_llm_call"), (
        f"Expected transform before post_llm_call, got: {events}"
    )

    # 2. In-memory result carries the transformed text.
    assert result["final_response"] == "[STORED VIA REAL PERSIST] raw model output"
    assert result["messages"][-1]["content"] == "[STORED VIA REAL PERSIST] raw model output"

    # 3. SQLite SessionDB has the transformed text — this is the key
    #    regression assertion that stub-based tests cannot make.
    saved = db.get_messages_as_conversation(session_id)
    assistant_contents = [
        m.get("content", "") for m in saved if m.get("role") == "assistant"
    ]
    assert any("[STORED VIA REAL PERSIST]" in (c or "") for c in assistant_contents), (
        f"Transformed text not found in SQLite DB. "
        f"Assistant contents: {assistant_contents}"
    )
    assert not any(
        c and "raw model output" in c
        and "[STORED VIA REAL PERSIST]" not in c
        for c in assistant_contents
    ), (
        f"Raw model output leaked into SQLite DB (untransformed). "
        f"Assistant contents: {assistant_contents}"
    )


# Disjoint sentinels: the transformed marker does not contain the raw
# marker, so "transformed is present" and "raw is absent" are independent
# assertions with no substring overlap to reason around.
_RAW_MARKER = "RAW_44239_UNTRANSFORMED"
_XFORM_MARKER = "XFORM_44239_DELIVERED"


def test_resume_reads_back_transformed_text_from_disk(tmp_path, monkeypatch):
    """Storage-level regression for #44239 — what the user saw is what a
    resume reads back off disk.

    Everything above this test either stubs ``_persist_session`` or reads
    the DB through the same handle that wrote it. This one closes the loop
    on the contract the PR exists to protect:

    * an isolated ``HERMES_HOME`` under ``tmp_path`` (``get_hermes_home()``
      reads the env var at call time, so nothing can touch the real store);
    * the REAL ``AIAgent._persist_session`` — both durable writers, the
      SQLite ``SessionDB`` flush *and* the JSON session snapshot
      (``sessions.write_json_snapshots``), which the earlier storage test
      leaves disabled;
    * read back through a FRESH ``SessionDB`` handle via
      ``get_messages_as_conversation`` — the same call ``/resume`` uses in
      ``hermes_cli/cli_agent_setup_mixin.py`` — plus the JSON snapshot file.

    Deliberately asserts on DISK ONLY, and before any in-memory check, so a
    regression in the sync-before-persist ordering fails *here* on the
    storage contract rather than being masked by an earlier in-memory
    assertion. Pre-fix, ``_persist_session`` ran before
    ``transform_llm_output``, so the raw text was flushed and stamped with
    the dedup marker — the transformed text never reached the store and
    resume replayed something the user was never shown.
    """
    import json

    from hermes_state import SessionDB
    from run_agent import AIAgent

    hermes_home = tmp_path / "hermes-home"
    logs_dir = hermes_home / "sessions"
    logs_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db_path = hermes_home / "state.db"
    session_id = "sess-44239-resume"
    writer_db = SessionDB(db_path=db_path)
    writer_db.create_session(session_id=session_id, source="test", model="test-model")

    agent = object.__new__(AIAgent)
    agent._session_db = writer_db
    agent._session_db_created = True
    agent.session_id = session_id

    # finalize_turn reads these off the agent.
    agent.max_iterations = 100
    agent.iteration_budget = SimpleNamespace(remaining=99, used=1, max_total=100)
    agent.quiet_mode = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.platform = "cli"
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "ok"
    agent.session_cost_source = "none"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent._tool_guardrail_halt_decision = None
    agent._turn_failed_file_mutations = None
    agent._interrupt_message = None
    agent._stream_callback = None
    agent._response_was_previewed = False
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()

    # _persist_session and its two writers. Both durable paths are ON.
    agent.save_trajectories = False
    agent._persist_disabled = False
    agent._session_messages = None
    agent._session_json_enabled = True
    agent.logs_dir = logs_dir
    agent.session_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    agent.tools = []
    agent.verbose_logging = False
    agent._session_init_model_config = None
    agent._parent_session_id = None
    agent._cached_system_prompt = None
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent.commit_memory_session = lambda *a, **k: None

    # Stubs for machinery a bare (never-__init__'d) agent cannot provide;
    # none of them sit on the persistence path under test.
    agent._cleanup_task_resources = lambda task_id: None
    agent.clear_interrupt = lambda: None
    agent._sync_external_memory_for_turn = lambda **kwargs: None

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": _RAW_MARKER},
    ]

    def fake_invoke_hook(hook_name, **kwargs):
        if hook_name == "transform_llm_output":
            return [_XFORM_MARKER]
        return []

    monkeypatch.setattr(plugins_mod, "invoke_hook", fake_invoke_hook)

    result = finalize_turn(
        agent,
        final_response=_RAW_MARKER,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    # ── Disk assertions first — this is the point of the test ───────────

    # 1. SQLite, re-opened as a resuming process would: the transformed
    #    text is there and the raw text is nowhere in the transcript.
    restored = SessionDB(db_path=db_path).get_messages_as_conversation(session_id)
    restored_contents = [
        m.get("content") or "" for m in restored if m.get("role") == "assistant"
    ]
    assert any(_XFORM_MARKER in c for c in restored_contents), (
        f"Resume read back no transformed text from SQLite. "
        f"Assistant contents: {restored_contents}"
    )
    assert not any(_RAW_MARKER in c for c in restored_contents), (
        f"Raw pre-transform text survived into SQLite and would be replayed "
        f"on resume. Assistant contents: {restored_contents}"
    )

    # 2. JSON session snapshot — the other durable writer _persist_session
    #    drives, for tooling that reads sessions/*.json directly.
    snapshot_path = logs_dir / f"session_{session_id}.json"
    assert snapshot_path.exists(), (
        f"JSON session snapshot was never written to {snapshot_path}"
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_contents = [
        m.get("content") or ""
        for m in snapshot.get("messages", [])
        if m.get("role") == "assistant"
    ]
    assert any(_XFORM_MARKER in c for c in snapshot_contents), (
        f"Transformed text missing from the JSON session snapshot. "
        f"Assistant contents: {snapshot_contents}"
    )
    assert not any(_RAW_MARKER in c for c in snapshot_contents), (
        f"Raw pre-transform text survived into the JSON session snapshot. "
        f"Assistant contents: {snapshot_contents}"
    )

    # 3. Only now the in-memory sanity check: the store agrees with what
    #    the caller (and therefore the user) was handed.
    assert result["final_response"] == _XFORM_MARKER


def test_pre_flushed_assistant_row_still_receives_transformed_text(tmp_path, monkeypatch):
    """Storage-level regression for the already-flushed row (#44239).

    A turn that called tools flushes its assistant row to SQLite mid-turn via
    the incremental persist, which stamps ``_DB_PERSISTED_MARKER``.
    ``_persist_session`` skips any row carrying that marker, so moving the
    persist call after ``transform_llm_output`` is not sufficient on its own:
    the sync must also clear the marker, or the transformed text is silently
    dropped at the storage layer while every in-memory assertion still passes.

    This drives the real ``_persist_session`` twice against an isolated
    ``HERMES_HOME`` — once to flush and stamp the raw row, once through
    ``finalize_turn`` — and asserts the durable store ends up with the
    transformed text.
    """
    from hermes_state import SessionDB
    from run_agent import AIAgent

    hermes_home = tmp_path / "hermes-home-preflushed"
    logs_dir = hermes_home / "sessions"
    logs_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db_path = hermes_home / "state.db"
    session_id = "sess-44239-preflushed"
    writer_db = SessionDB(db_path=db_path)
    writer_db.create_session(session_id=session_id, source="test", model="test-model")

    agent = object.__new__(AIAgent)
    agent._session_db = writer_db
    agent._session_db_created = True
    agent.session_id = session_id

    agent.max_iterations = 100
    agent.iteration_budget = SimpleNamespace(remaining=99, used=1, max_total=100)
    agent.quiet_mode = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.platform = "cli"
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "ok"
    agent.session_cost_source = "none"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent._tool_guardrail_halt_decision = None
    agent._turn_failed_file_mutations = None
    agent._interrupt_message = None
    agent._stream_callback = None
    agent._response_was_previewed = False
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()

    agent.save_trajectories = False
    agent._persist_disabled = False
    agent._session_messages = None
    agent._session_json_enabled = False
    agent.logs_dir = logs_dir
    agent.session_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    agent.tools = []
    agent.verbose_logging = False
    agent._session_init_model_config = None
    agent._parent_session_id = None
    agent._cached_system_prompt = None
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent.commit_memory_session = lambda *a, **k: None

    agent._cleanup_task_resources = lambda task_id: None
    agent.clear_interrupt = lambda: None
    agent._sync_external_memory_for_turn = lambda **kwargs: None

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": _RAW_MARKER},
    ]

    # Mid-turn incremental flush: writes the raw row and stamps the dedup
    # marker, exactly as the tool-call persist path does.
    agent._persist_session(messages, [])
    assert messages[-1].get("_db_persisted") is True, (
        "precondition failed: the incremental flush did not stamp the dedup "
        "marker, so this test would not exercise the skip path"
    )

    def fake_invoke_hook(hook_name, **kwargs):
        if hook_name == "transform_llm_output":
            return [_XFORM_MARKER]
        return []

    monkeypatch.setattr(plugins_mod, "invoke_hook", fake_invoke_hook)

    result = finalize_turn(
        agent,
        final_response=_RAW_MARKER,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    restored = SessionDB(db_path=db_path).get_messages_as_conversation(session_id)
    restored_contents = [
        m.get("content") or "" for m in restored if m.get("role") == "assistant"
    ]
    assert any(_XFORM_MARKER in c for c in restored_contents), (
        f"Transformed text never reached SQLite for a pre-flushed assistant "
        f"row — the dedup marker skipped the re-write. "
        f"Assistant contents: {restored_contents}"
    )
    assert result["final_response"] == _XFORM_MARKER


def test_persist_survives_malformed_message_in_diagnostic_log(monkeypatch):
    """Deferring the persist must not let a diagnostic failure lose the turn.

    Moving ``_persist_session`` below ``transform_llm_output`` puts the
    turn-exit diagnostic block between the transcript prep and the write. That
    block indexes message dicts without an isinstance guard on every access, so
    a malformed non-dict entry raises while merely building a log line. Before
    this guard the raise propagated out of ``finalize_turn`` with the session
    never persisted — reintroducing the #8049 "cleanup lost the turn" failure
    that the surrounding guards exist to prevent.

    Uses an empty ``final_response`` so the prep block does not append a
    closing assistant row; the tail stays a tool result, which is what sends
    the diagnostic down its message-walking branch.
    """
    agent = _StubAgent()
    monkeypatch.setattr(plugins_mod, "invoke_hook", lambda hook_name, **kwargs: [])

    messages = [
        {"role": "user", "content": "hi"},
        "MALFORMED",  # non-dict entry the diagnostic walk chokes on
        {"role": "tool", "content": "{}", "tool_call_id": "c1"},
    ]

    # A malformed entry also trips the reasoning-extraction walk further down,
    # which is unguarded on clean main too and sits AFTER the persist. That
    # raise is pre-existing and out of scope here; what must not regress is
    # that the session is already written by the time it happens.
    try:
        finalize_turn(
            agent,
            final_response="",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=messages,
            conversation_history=[],
            effective_task_id="task-1",
            turn_id="turn-1",
            user_message="hi",
            original_user_message="hi",
            _should_review_memory=False,
            _turn_exit_reason="empty_response",
        )
    except AttributeError:
        pass

    assert "persist" in agent.events, (
        "session was never persisted — a diagnostic-log failure between the "
        "transcript prep and the deferred persist swallowed the turn (#8049)"
    )
