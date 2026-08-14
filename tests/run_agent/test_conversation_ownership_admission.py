"""Core agent lifecycle: admission, holder lifetime, cancellation-safe release.

``AIAgent.run_conversation`` is the narrow waist every surface funnels through
— CLI, gateway, HTTP API, TUI, ACP, cron, batch. Gating it there is what makes
one authority cover every mutating path instead of eight per-surface locks.

Admission happens BEFORE the transcript is loaded, so a refused turn leaves the
conversation byte-identical: no row created, no message appended, no synthetic
"someone else is here" message spliced into history (that would break role
alternation and the per-conversation prompt cache).
"""

import concurrent.futures
import threading

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

from agent.session_ownership import (
    ConversationOwnershipConflict,
    ownership_admission_surface,
    should_own_conversation,
    new_holder_id,
    own_conversation,
)


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def _agent(db, session_id="conv-1", **kwargs):
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id=session_id,
        session_db=db,
        platform=kwargs.pop("platform", "cli"),
        **kwargs,
    )
    agent.compression_enabled = False
    return agent


# ── who contends, and who must not ─────────────────────────────────────────


class _Fake:
    """Minimal agent-shaped object — eligibility is input→output, not I/O."""

    def __init__(self, **attrs):
        self.session_id = "s1"
        self.platform = "cli"
        self._session_db = object()
        self._persist_disabled = False
        self._parent_session_id = ""
        for key, value in attrs.items():
            setattr(self, key, value)


def test_an_ordinary_turn_contends_for_the_conversation(db):
    assert should_own_conversation(_Fake(_session_db=db)) is True


def test_a_persist_disabled_fork_never_contends():
    """Background-review forks share the live session id for cache warmth but
    can never write the transcript — contending would starve the real turn."""
    assert should_own_conversation(_Fake(_persist_disabled=True)) is False


def test_a_delegate_subagent_never_contends():
    """A subagent runs INSIDE its parent's conversation, concurrently and by
    design. Its root resolves to the parent's, so acquiring would deadlock
    delegation against the very turn that launched it."""
    assert should_own_conversation(_Fake(platform="subagent")) is False
    assert should_own_conversation(_Fake(_parent_session_id="parent")) is False


def test_real_delegate_builder_metadata_enables_owned_parent_publication(
    db, monkeypatch
):
    """Exercise delegate_tool's real child assembly, not a hand-planted source row."""
    from tools.delegate_tool import _build_child_agent

    parent = _agent(db, session_id="parent")
    parent._ensure_db_session()
    # Real gateway children can inherit the gateway source instead of the
    # literal platform="subagent". The durable _delegate_from marker must be
    # sufficient to identify their lineage boundary.
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tui")
    child = _build_child_agent(
        task_index=0,
        goal="verify ownership metadata",
        context=None,
        toolsets=None,
        model=None,
        max_iterations=2,
        task_count=1,
        parent_agent=parent,
        role="leaf",
    )
    child._ensure_db_session()
    row = db.get_session(child.session_id)
    assert child.platform == "subagent"
    assert child._parent_session_id == "parent"
    assert should_own_conversation(child) is False
    assert row["parent_session_id"] == "parent"
    assert row["source"] == "tui"

    def _publish():
        writer = SessionDB(db.db_path)
        try:
            writer.append_message(child.session_id, "assistant", "child-result")
        finally:
            writer.close()

    with own_conversation(db, "parent", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_publish).result()
    assert [m["content"] for m in db.get_messages(child.session_id)] == [
        "child-result"
    ]


def test_compression_session_rotation_does_not_disable_normal_admission(db):
    """Exercise production's live-child adoption path, not a hand assignment."""
    from agent.conversation_compression import _adopt_live_compression_child

    agent = _agent(db, session_id="root")
    agent._ensure_db_session()
    db.end_session("root", "compression")
    db.create_session(
        "compression-child", source="compression", parent_session_id="root"
    )
    db.append_message("compression-child", "user", "compacted history")

    recovered = _adopt_live_compression_child(agent, db, "root")
    assert recovered is not None
    assert agent.session_id == "compression-child"
    assert getattr(agent, "_parent_session_id", None) is None
    assert should_own_conversation(agent) is True


def test_an_agent_with_no_store_never_contends():
    assert should_own_conversation(_Fake(_session_db=None)) is False
    assert should_own_conversation(_Fake(session_id="")) is False


def test_admission_surface_names_the_holder_usefully():
    """The conflict message has to name a surface a human recognises."""
    assert ownership_admission_surface(_Fake(platform="telegram")) == "telegram"
    assert ownership_admission_surface(_Fake(platform="")) == "agent"


# ── admission ──────────────────────────────────────────────────────────────


def test_turn_is_refused_before_any_transcript_work_when_owned_elsewhere(db):
    """The core invariant, at the agent boundary."""
    db.try_acquire_conversation_ownership(
        "conv-1",
        new_holder_id(surface="cli"),
        surface="cli",
        session_id="conv-1",
        ttl_seconds=120.0,
    )
    agent = _agent(db)

    with pytest.raises(ConversationOwnershipConflict) as excinfo:
        agent.run_conversation("hello")

    assert excinfo.value.conversation_root == "conv-1"
    # Refused at admission: nothing was created, loaded or written.
    assert db.get_session("conv-1") is None
    assert db.get_messages("conv-1") == []


def test_turn_holds_the_conversation_for_its_whole_body_then_releases(db):
    seen = {}

    def _fake_loop(agent_self, *args, **kwargs):
        seen["owner"] = db.get_conversation_owner("conv-1")
        return {"final_response": "ok", "messages": []}

    agent = _agent(db)
    _run_with_stub_loop(agent, _fake_loop)

    assert seen["owner"] is not None, "the turn body ran without owning the conversation"
    assert seen["owner"]["surface"] == "cli"
    # ...and the grant is gone once the turn returns.
    assert db.get_conversation_owner("conv-1") is None


def test_release_survives_a_raising_turn(db):
    def _boom(agent_self, *args, **kwargs):
        raise RuntimeError("model exploded")

    agent = _agent(db)
    with pytest.raises(RuntimeError):
        _run_with_stub_loop(agent, _boom)
    assert db.get_conversation_owner("conv-1") is None


def test_release_survives_cancellation(db):
    """KeyboardInterrupt/CancelledError unwind through the same ``finally``."""

    def _cancel(agent_self, *args, **kwargs):
        raise KeyboardInterrupt()

    agent = _agent(db)
    with pytest.raises(KeyboardInterrupt):
        _run_with_stub_loop(agent, _cancel)
    assert db.get_conversation_owner("conv-1") is None


def test_a_second_thread_on_the_same_conversation_is_refused(db):
    """Re-entrancy is thread-scoped: a nested call in the OWNING thread reuses
    the grant, a genuinely concurrent one still collides."""
    entered = threading.Event()
    proceed = threading.Event()
    other_thread_error = {}

    def _hold(agent_self, *args, **kwargs):
        entered.set()
        proceed.wait(timeout=30)
        return {"final_response": "ok", "messages": []}

    def _second_turn():
        entered.wait(timeout=30)
        try:
            second = _agent(db, session_id="conv-1")
            _run_with_stub_loop(second, lambda *a, **k: {"final_response": "x"})
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            other_thread_error["exc"] = exc
        finally:
            proceed.set()

    worker = threading.Thread(target=_second_turn, daemon=True)
    worker.start()
    agent = _agent(db)
    _run_with_stub_loop(agent, _hold)
    worker.join(timeout=30)

    assert isinstance(other_thread_error.get("exc"), ConversationOwnershipConflict)


def test_nested_reentry_in_the_owning_thread_reuses_the_grant(db):
    """A rewrite invoked from inside a held turn must not deadlock itself."""
    from agent.session_ownership import own_conversation

    outer_holder = {}

    def _nested(agent_self, *args, **kwargs):
        with own_conversation(db, "conv-1", surface="compression") as inner:
            assert inner is not None
            assert inner.nested is True
            outer_holder["fence"] = inner.fence_token
        # Leaving the nested scope must NOT release the outer grant.
        assert db.get_conversation_owner("conv-1") is not None
        return {"final_response": "ok", "messages": []}

    agent = _agent(db)
    _run_with_stub_loop(agent, _nested)
    assert outer_holder["fence"] >= 1


# ── helper ─────────────────────────────────────────────────────────────────


def _run_with_stub_loop(agent, loop_fn):
    """Drive ``AIAgent.run_conversation`` with the model loop stubbed out.

    Only the inner ``agent.conversation_loop.run_conversation`` is replaced, so
    the admission/release scaffolding under test is the real thing.
    """
    import agent.conversation_loop as loop_module
    from unittest.mock import patch

    with patch.object(loop_module, "run_conversation", loop_fn):
        return agent.run_conversation("hello")
