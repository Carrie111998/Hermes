"""Backend tests for the Async Delegation View (docked agents panel + steering).

Covers the two new gateway RPCs — ``delegation.async_list`` (read projection
of the async registry) and ``subagent.send`` (live steering) — plus the
``send_to_subagent`` helper they lean on.
"""

from unittest.mock import MagicMock

import tools.async_delegation as async_delegation
import tools.delegate_tool as delegate_tool
from tui_gateway import server


class _FakeSteerAgent:
    """Minimal stand-in for a live child ``AIAgent`` in the registry.

    Records every ``steer`` call so tests can assert exactly-once delivery
    and role-legal drain behaviour without spinning up a real agent loop.
    """

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return self.accept


def _clear_async_records():
    with async_delegation._records_lock:
        async_delegation._records.clear()


def _register_async(delegation_id: str, status: str = "running", steer_fn=None):
    with async_delegation._records_lock:
        async_delegation._records[delegation_id] = {
            "delegation_id": delegation_id,
            "goal": "patch token-bucket refill race",
            "role": "fixer",
            "model": "opus-4.8",
            "status": status,
            "depth": 1,
            "dispatched_at": 1.0,
            "completed_at": None if status == "running" else 2.0,
            # interrupt_fn must be stripped by list_async_delegations — assert it.
            "interrupt_fn": lambda: None,
            "steer_fn": steer_fn,
        }


# ── delegation.async_list ────────────────────────────────────────────────


def test_async_list_shape_and_running_count():
    _clear_async_records()
    try:
        _register_async("d-run", "running")
        _register_async("d-done", "completed")

        resp = server._methods["delegation.async_list"]("r1", {})
        result = resp["result"]

        assert result["running"] == 1  # only the running record counts
        assert len(result["delegations"]) == 2
        # Control closures (non-serialisable) must never leak into the payload.
        for d in result["delegations"]:
            assert "interrupt_fn" not in d
            assert "steer_fn" not in d
        goals = {d["delegation_id"]: d["goal"] for d in result["delegations"]}
        assert goals["d-run"] == "patch token-bucket refill race"
    finally:
        _clear_async_records()


def test_async_list_empty_registry():
    _clear_async_records()
    resp = server._methods["delegation.async_list"]("r1", {})
    result = resp["result"]
    assert result["running"] == 0
    assert result["delegations"] == []


# ── send_to_subagent helper ──────────────────────────────────────────────


def test_send_to_subagent_delivers_once():
    agent = _FakeSteerAgent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        ok = delegate_tool.send_to_subagent("b7c2", "prefer a sliding window")
        assert ok is True
        # Exactly one user turn queued — no double-append.
        assert agent.steers == ["prefer a sliding window"]
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_send_to_subagent_unknown_id_returns_false():
    assert delegate_tool.send_to_subagent("does-not-exist", "hi") is False


def test_send_to_subagent_empty_text_is_rejected():
    agent = _FakeSteerAgent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        assert delegate_tool.send_to_subagent("b7c2", "   ") is False
        assert agent.steers == []  # never reached the child
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_send_to_subagent_agent_without_steer_returns_false():
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": object()})
    try:
        assert delegate_tool.send_to_subagent("b7c2", "hi") is False
    finally:
        delegate_tool._unregister_subagent("b7c2")


# ── subagent.send RPC ────────────────────────────────────────────────────


def test_subagent_send_rpc_delivers_to_live_child():
    agent = _FakeSteerAgent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        resp = server._methods["subagent.send"](
            "r1", {"subagent_id": "b7c2", "text": "switch approach"}
        )
        assert resp["result"]["delivered"] is True
        assert resp["result"]["subagent_id"] == "b7c2"
        assert agent.steers == ["switch approach"]
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_subagent_send_rpc_dead_id_reports_not_delivered():
    resp = server._methods["subagent.send"](
        "r1", {"subagent_id": "ghost", "text": "hi"}
    )
    assert resp["result"]["delivered"] is False


def test_subagent_send_rpc_requires_id_and_text():
    missing_text = server._methods["subagent.send"]("r1", {"subagent_id": "b7c2"})
    assert "error" in missing_text
    missing_id = server._methods["subagent.send"]("r1", {"text": "hi"})
    assert "error" in missing_id
    blank_text = server._methods["subagent.send"](
        "r1", {"subagent_id": "b7c2", "text": "   "}
    )
    assert "error" in blank_text


def test_delegation_send_rpc_steers_running_background_unit():
    steers = []
    _clear_async_records()
    try:
        _register_async("deleg_b7c2", steer_fn=lambda text: not steers.append(text))
        resp = server._methods["delegation.send"](
            "r1", {"delegation_id": "deleg_b7c2", "text": "switch approach"}
        )
        assert resp["result"]["delivered"] is True
        assert steers == ["switch approach"]
    finally:
        _clear_async_records()


def test_delegation_send_rpc_rejects_finished_background_unit():
    _clear_async_records()
    try:
        _register_async("deleg_b7c2", status="completed", steer_fn=lambda _text: True)
        resp = server._methods["delegation.send"](
            "r1", {"delegation_id": "deleg_b7c2", "text": "too late"}
        )
        assert resp["result"]["delivered"] is False
    finally:
        _clear_async_records()


# ── Flow / integration: steering a REAL AIAgent through the registry ──────
#
# These use a bare AIAgent (object.__new__, no __init__) — steer/_drain fall
# back to the lock-free path documented for test stubs, exercising the same
# _pending_steer slot the live conversation loop drains.


def _bare_agent():
    from run_agent import AIAgent

    return object.__new__(AIAgent)


def test_steer_reaches_child_via_registry_and_drains_exact_text():
    agent = _bare_agent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        assert delegate_tool.send_to_subagent("b7c2", "prefer a sliding window") is True
        # The loop drains this exact text at its next iteration boundary.
        assert agent._drain_pending_steer() == "prefer a sliding window"
        # Exactly once — a second drain is empty.
        assert agent._drain_pending_steer() is None
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_multiple_steers_concatenate_in_order():
    agent = _bare_agent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        delegate_tool.send_to_subagent("b7c2", "first")
        delegate_tool.send_to_subagent("b7c2", "second")
        assert agent._drain_pending_steer() == "first\nsecond"
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_send_after_unregister_returns_false():
    """A child that finished (unregistered) can no longer be steered."""
    agent = _bare_agent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    delegate_tool._unregister_subagent("b7c2")
    assert delegate_tool.send_to_subagent("b7c2", "too late") is False
    assert agent._drain_pending_steer() is None


def test_concurrent_sends_all_deliver():
    """Thread-safety: N concurrent steers all land (no lost update)."""
    import threading

    agent = _bare_agent()
    # Give it the real lock so the concurrent path (not the stub path) runs.
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        n = 50
        barrier = threading.Barrier(n)

        def _fire(i: int):
            barrier.wait()
            delegate_tool.send_to_subagent("b7c2", f"m{i}")

        threads = [threading.Thread(target=_fire, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        drained = agent._drain_pending_steer() or ""
        parts = [p for p in drained.split("\n") if p]
        assert len(parts) == n  # every steer survived
        assert {p for p in parts} == {f"m{i}" for i in range(n)}
    finally:
        delegate_tool._unregister_subagent("b7c2")


def test_steer_injection_preserves_role_alternation():
    """The loop appends the steer to the LAST tool message, never inserting a
    fresh user turn mid-tool — the invariant the async design was built on.

    Replicates the drain+inject from agent/conversation_loop.py to assert the
    resulting message sequence stays role-legal (…tool→assistant…, no user
    spliced between a tool result and the next assistant turn)."""
    from agent.prompt_builder import format_steer_marker

    agent = _bare_agent()
    delegate_tool._register_subagent({"subagent_id": "b7c2", "agent": agent})
    try:
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "tc_1"}]},
            {"role": "tool", "tool_call_id": "tc_1", "content": "ran ok"},
        ]
        delegate_tool.send_to_subagent("b7c2", "switch approach")

        steer = agent._drain_pending_steer()
        assert steer == "switch approach"
        # Inject exactly as the loop does: onto the last tool message.
        last_tool = next(m for m in reversed(messages) if m["role"] == "tool")
        last_tool["content"] += format_steer_marker(steer)

        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "tool"]  # no new user turn
        assert "switch approach" in messages[-1]["content"]
        # No two adjacent same-role messages (alternation intact).
        assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    finally:
        delegate_tool._unregister_subagent("b7c2")


# ── Regression: steer races the child's final response (review #70899) ────
#
# send_to_subagent()/steer() only queues onto _pending_steer; the queue
# drains at the next tool batch (apply_pending_steer_to_tool_results). If
# the child's current LLM call turns out to be its LAST (no further
# tool_calls), that drain point never arrives. turn_finalizer hands the
# unconsumed text back as result["pending_steer"] instead of dropping it
# (mirrors the main turn's leftover-steer handling) — but a delegated child
# has no "next turn" to inject it into. Before this fix, _run_single_child
# built its entry from final_response only and silently discarded
# pending_steer, so a caller who got delivered=True from subagent.send had
# no way to learn the child never actually saw the text.


def test_run_single_child_surfaces_a_steer_the_child_never_saw():
    """The race: steer() queues successfully, but the child's last iteration
    produces no tool_calls, so turn_finalizer drains it as leftover
    (pending_steer) instead of splicing it into a tool result. The entry
    _run_single_child returns must surface that, not silently drop it."""
    from tests.tools.test_delegate import _make_mock_parent
    from tools.delegate_tool import _run_single_child

    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done before the steer could land",
        "completed": True,
        "interrupted": False,
        "api_calls": 3,
        "messages": [],
        # What turn_finalizer hands back when _drain_pending_steer() finds
        # text after the last tool batch already drained (agent/turn_finalizer.py).
        "pending_steer": "switch to the sliding-window approach",
    }

    result = _run_single_child(
        task_index=0,
        goal="Investigate rate limits",
        child=child,
        parent_agent=_make_mock_parent(),
    )

    assert result["status"] == "completed"
    assert result["missed_steer"] == "switch to the sliding-window approach"


def test_run_single_child_omits_missed_steer_when_none_pending():
    """No leftover steer → no missed_steer key (the common case stays clean)."""
    from tests.tools.test_delegate import _make_mock_parent
    from tools.delegate_tool import _run_single_child

    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }

    result = _run_single_child(
        task_index=0,
        goal="Investigate rate limits",
        child=child,
        parent_agent=_make_mock_parent(),
    )

    assert "missed_steer" not in result


def test_completion_event_carries_missed_steer(monkeypatch):
    """The completion event a _run_single_child-shaped result produces must
    surface missed_steer — the only place left to correct an earlier
    delivered=True once the child's real outcome (not seeing the text) is
    known."""
    captured = []
    from tools.process_registry import process_registry

    monkeypatch.setattr(process_registry.completion_queue, "put", captured.append)

    async_delegation._push_completion_event(
        {"delegation_id": "deleg_b7c2", "session_key": "", "dispatched_at": 1.0,
         "completed_at": 2.0, "goal": "patch token-bucket refill race"},
        {
            "status": "completed",
            "summary": "done before the steer could land",
            "api_calls": 3,
            "missed_steer": "switch to the sliding-window approach",
        },
        "completed",
    )

    assert captured, "completion event never pushed"
    assert captured[0]["missed_steer"] == "switch to the sliding-window approach"


def test_completion_event_omits_missed_steer_when_absent(monkeypatch):
    """The common case (no race) stays clean — no missed_steer key at all."""
    captured = []
    from tools.process_registry import process_registry

    monkeypatch.setattr(process_registry.completion_queue, "put", captured.append)

    async_delegation._push_completion_event(
        {"delegation_id": "deleg_b7c2", "session_key": "", "dispatched_at": 1.0,
         "completed_at": 2.0, "goal": "patch token-bucket refill race"},
        {"status": "completed", "summary": "done", "api_calls": 1},
        "completed",
    )

    assert captured, "completion event never pushed"
    assert "missed_steer" not in captured[0]


def test_formatted_completion_surfaces_missed_steer_to_the_model():
    """The re-injection block the model actually reads must call out a missed
    steer explicitly — a backend field nobody renders is as good as lost."""
    from tools.process_registry import format_process_notification

    text = format_process_notification({
        "type": "async_delegation",
        "delegation_id": "deleg_b7c2",
        "goal": "patch token-bucket refill race",
        "status": "completed",
        "summary": "done before the steer could land",
        "missed_steer": "switch to the sliding-window approach",
    })

    assert "switch to the sliding-window approach" in text
    assert "never saw" in text


def test_formatted_completion_omits_the_note_when_nothing_was_missed():
    from tools.process_registry import format_process_notification

    text = format_process_notification({
        "type": "async_delegation",
        "delegation_id": "deleg_b7c2",
        "goal": "patch token-bucket refill race",
        "status": "completed",
        "summary": "done",
    })

    assert "never saw" not in text


def test_async_list_reflects_status_transition():
    _clear_async_records()
    try:
        _register_async("d1", "running")
        assert server._methods["delegation.async_list"]("r", {})["result"]["running"] == 1

        # Flip to completed — active_count drops, record still listed.
        with async_delegation._records_lock:
            async_delegation._records["d1"]["status"] = "completed"
            async_delegation._records["d1"]["completed_at"] = 2.0

        result = server._methods["delegation.async_list"]("r", {})["result"]
        assert result["running"] == 0
        assert len(result["delegations"]) == 1
        assert result["delegations"][0]["status"] == "completed"
    finally:
        _clear_async_records()


# ── delegation.async_list server-side projection ─────────────────────────


def _register_fat_async(delegation_id: str, **extra):
    """A registry record carrying the whole dispatch spec, as the real
    ``dispatch_async_delegation*`` writers leave it."""
    with async_delegation._records_lock:
        record = {
            "delegation_id": delegation_id,
            "goal": "3 parallel subagents: a; b; c",
            "goals": ["a" * 4000, "b", "c"],
            "context": "x" * 20000,
            "toolsets": ["files", "shell"],
            "role": "fixer",
            "model": "opus-4.8",
            "session_key": "agent:main:deadbeef",
            "origin_ui_session_id": "ui-1",
            "origin_session_id": "sess-1",
            "parent_session_id": "sess-0",
            "status": "running",
            "depth": 1,
            "dispatched_at": 1.0,
            "completed_at": None,
            "result": {"results": [{"summary": "y" * 10000}]},
            "summary": "z" * 10000,
            "interrupt_fn": lambda: None,
            "steer_fn": lambda _t: True,
        }
        record.update(extra)
        async_delegation._records[delegation_id] = record


# Every key the docked panel / overlay may read, mirroring
# ``AsyncDelegationRecord`` in ui-tui/src/gatewayTypes.ts.
_PANEL_KEYS = {
    "completed_at",
    "delegation_id",
    "dispatched_at",
    "goal",
    "is_batch",
    "model",
    "role",
    "status",
    "subagent_ids",
}


def test_async_list_projects_away_the_dispatch_spec():
    """The 1.5s poll must not carry context/goals/results/session routing."""
    _clear_async_records()
    try:
        _register_fat_async("d-fat")

        payload = server._methods["delegation.async_list"]("r", {})["result"]
        record = payload["delegations"][0]

        for leaked in (
            "context",
            "goals",
            "toolsets",
            "session_key",
            "origin_ui_session_id",
            "origin_session_id",
            "parent_session_id",
            "result",
            "summary",
            "interrupt_fn",
            "steer_fn",
        ):
            assert leaked not in record, f"{leaked} still on the wire"

        # What the panel does read survives intact.
        assert record["delegation_id"] == "d-fat"
        assert record["goal"] == "3 parallel subagents: a; b; c"
        assert record["role"] == "fixer"
        assert record["model"] == "opus-4.8"
        assert record["status"] == "running"
        assert record["dispatched_at"] == 1.0
        assert record["completed_at"] is None
    finally:
        _clear_async_records()


def test_async_list_projection_stays_within_the_declared_contract():
    """No key may reach the client that gatewayTypes.ts does not declare."""
    _clear_async_records()
    try:
        _register_fat_async("d-a")
        _register_fat_async("d-b", is_batch=True, subagent_ids=["b7c2"])

        payload = server._methods["delegation.async_list"]("r", {})["result"]

        for record in payload["delegations"]:
            assert set(record) <= _PANEL_KEYS, set(record) - _PANEL_KEYS
    finally:
        _clear_async_records()


def test_async_list_projection_is_small():
    """A fat record must not turn into a fat frame — size is the whole point."""
    import json

    _clear_async_records()
    try:
        _register_fat_async("d-fat")
        payload = server._methods["delegation.async_list"]("r", {})["result"]

        assert len(json.dumps(payload)) < 512
    finally:
        _clear_async_records()


def test_async_list_carries_batch_join_keys_only_for_batches():
    """The dedupe join key rides along for batches, and only for batches."""
    _clear_async_records()
    try:
        _register_fat_async("d-plain")
        _register_fat_async(
            "d-batch", is_batch=True, subagent_ids=["b7c2", "a11a", "3f0d"]
        )

        payload = server._methods["delegation.async_list"]("r", {})["result"]
        by_id = {d["delegation_id"]: d for d in payload["delegations"]}

        assert by_id["d-batch"]["is_batch"] is True
        assert by_id["d-batch"]["subagent_ids"] == ["b7c2", "a11a", "3f0d"]
        # A single-subagent record has no children to dedupe against.
        assert "subagent_ids" not in by_id["d-plain"]
        assert "is_batch" not in by_id["d-plain"]
    finally:
        _clear_async_records()


def test_async_list_batch_join_keys_are_strings_only():
    """Ids come from child agents by getattr — nothing else may pass."""
    _clear_async_records()
    try:
        _register_fat_async(
            "d-batch", is_batch=True, subagent_ids=["b7c2", None, 7, "a11a"]
        )

        payload = server._methods["delegation.async_list"]("r", {})["result"]

        assert payload["delegations"][0]["subagent_ids"] == ["b7c2", "a11a"]
    finally:
        _clear_async_records()


def test_async_list_batch_without_children_projects_empty_list():
    """A batch whose children never got ids still renders — as its own row."""
    _clear_async_records()
    try:
        _register_fat_async("d-batch", is_batch=True, subagent_ids=[])

        record = server._methods["delegation.async_list"]("r", {})["result"][
            "delegations"
        ][0]

        assert record["is_batch"] is True
        assert record["subagent_ids"] == []
    finally:
        _clear_async_records()


def test_async_list_tolerates_a_record_missing_optional_fields():
    """Partially-written records must not break the poll."""
    _clear_async_records()
    try:
        with async_delegation._records_lock:
            async_delegation._records["d-thin"] = {"delegation_id": "d-thin"}

        record = server._methods["delegation.async_list"]("r", {})["result"][
            "delegations"
        ][0]

        assert record["delegation_id"] == "d-thin"
        assert record["status"] is None
        assert record["goal"] is None
    finally:
        _clear_async_records()


# ── batch dispatch records the ids of the children it stands for ─────────


class _NoopExecutor:
    """Keeps the dispatch bookkeeping under test without running the batch."""

    def submit(self, *_args, **_kwargs):
        return None


def _dispatch_batch(monkeypatch, subagent_ids):
    monkeypatch.setattr(async_delegation, "_persist_dispatch", lambda _r: None)
    monkeypatch.setattr(
        async_delegation, "_get_executor", lambda *_a, **_kw: _NoopExecutor()
    )

    return async_delegation.dispatch_async_delegation_batch(
        goals=["a", "b"],
        context=None,
        toolsets=None,
        role="fixer",
        model="opus-4.8",
        session_key="agent:main:deadbeef",
        runner=lambda: {"results": []},
        subagent_ids=subagent_ids,
    )


def test_batch_dispatch_records_its_child_subagent_ids(monkeypatch):
    _clear_async_records()
    try:
        dispatch = _dispatch_batch(monkeypatch, ["b7c2", "a11a"])

        assert dispatch["status"] == "dispatched"
        record = async_delegation._records[dispatch["delegation_id"]]
        assert record["is_batch"] is True
        assert record["subagent_ids"] == ["b7c2", "a11a"]
    finally:
        _clear_async_records()


def test_batch_dispatch_drops_non_string_child_ids(monkeypatch):
    """``getattr(child, "_subagent_id", None)`` yields None for a child that
    never got one — a None must never become a join key."""
    _clear_async_records()
    try:
        dispatch = _dispatch_batch(monkeypatch, ["b7c2", None, 3])

        record = async_delegation._records[dispatch["delegation_id"]]
        assert record["subagent_ids"] == ["b7c2"]
    finally:
        _clear_async_records()


def test_batch_dispatch_without_child_ids_defaults_to_empty(monkeypatch):
    """Older callers pass nothing; the field must still exist and be a list."""
    _clear_async_records()
    try:
        dispatch = _dispatch_batch(monkeypatch, None)

        record = async_delegation._records[dispatch["delegation_id"]]
        assert record["subagent_ids"] == []
    finally:
        _clear_async_records()


def test_batch_dispatch_child_ids_reach_the_panel_payload(monkeypatch):
    """End to end: dispatch → registry → RPC projection, one join key set."""
    _clear_async_records()
    try:
        dispatch = _dispatch_batch(monkeypatch, ["b7c2", "a11a"])

        payload = server._methods["delegation.async_list"]("r", {})["result"]
        record = payload["delegations"][0]

        assert record["delegation_id"] == dispatch["delegation_id"]
        assert record["subagent_ids"] == ["b7c2", "a11a"]
        assert "context" not in record
    finally:
        _clear_async_records()
