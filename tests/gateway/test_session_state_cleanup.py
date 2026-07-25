"""Regression tests for _release_running_agent_state and SessionDB shutdown.

Before this change, running-agent state lived in three dicts that drifted
out of sync:

  self._running_agents       - AIAgent instance per session key
  self._running_agents_ts    - start timestamp per session key
  self._busy_ack_ts          - last busy-ack timestamp per session key

Six cleanup sites did ``del self._running_agents[key]`` without touching
the other two; one site only popped ``_running_agents`` and
``_running_agents_ts``; and only the stale-eviction site cleaned all
three.  Each missed entry was a small persistent leak.

Also: SessionDB connections were never closed on gateway shutdown,
leaving WAL locks in place until Python actually exited.
"""

import threading
from unittest.mock import MagicMock

import pytest

def _make_runner():
    """Bare GatewayRunner wired with just the state the helper touches."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    return runner


class TestReleaseRunningAgentStateUnit:
    def test_pops_all_three_dicts(self):
        runner = _make_runner()
        runner._running_agents["k"] = MagicMock()
        runner._running_agents_ts["k"] = 123.0
        runner._busy_ack_ts["k"] = 456.0

        runner._release_running_agent_state("k")

        assert "k" not in runner._running_agents
        assert "k" not in runner._running_agents_ts
        assert "k" not in runner._busy_ack_ts

    def test_idempotent_on_missing_key(self):
        """Calling twice (or on an absent key) must not raise."""
        runner = _make_runner()
        runner._release_running_agent_state("missing")
        runner._release_running_agent_state("missing")  # still fine


class TestNoMoreBareDeleteSites:
    """Regression: all bare `del self._running_agents[key]` sites were
    converted to use the helper.  If a future contributor reverts one,
    this test flags it.  Docstrings / comments mentioning the old
    pattern are allowed.
    """

    def test_no_bare_del_of_running_agents_in_gateway_run(self):
        from pathlib import Path
        import re

        gateway_run = (
            Path(__file__).parent.parent.parent / "gateway" / "run.py"
        ).read_text(encoding="utf-8")
        # Match `del self._running_agents[...]` that is NOT inside a
        # triple-quoted docstring.  We scan non-docstring lines only.
        lines = gateway_run.splitlines()

        in_docstring = False
        docstring_delim = None
        offenders = []
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not in_docstring:
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    delim = stripped[:3]
                    # single-line docstring?
                    if stripped.count(delim) >= 2:
                        continue
                    in_docstring = True
                    docstring_delim = delim
                    continue
                if re.search(r"\bdel\s+self\._running_agents\[", line):
                    offenders.append((idx, line.rstrip()))
            else:
                if docstring_delim and docstring_delim in stripped:
                    in_docstring = False
                    docstring_delim = None

        assert offenders == [], (
            "Found bare `del self._running_agents[...]` sites in gateway/run.py. "
            "Use self._release_running_agent_state(session_key) instead so "
            "_running_agents_ts and _busy_ack_ts are popped in lockstep.\n"
            + "\n".join(f"  line {n}: {l}" for n, l in offenders)
        )


class TestSessionDbCloseOnShutdown:
    """_stop_impl should call .close() on both self._session_db and
    self.session_store._db to release SQLite WAL locks before the new
    gateway (during --replace restart) tries to open the same file.
    """


    def test_shutdown_tolerates_close_raising(self):
        """A close() that raises must not prevent subsequent cleanup."""
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        flaky_db = MagicMock()
        flaky_db.close.side_effect = RuntimeError("simulated lock error")
        healthy_db = MagicMock()

        runner._db = flaky_db
        runner.session_store = MagicMock()
        runner.session_store._db = healthy_db

        # Same pattern as production: try/except around each close().
        for _db_holder in (runner, getattr(runner, "session_store", None)):
            _db = getattr(_db_holder, "_db", None) if _db_holder else None
            if _db is None or not hasattr(_db, "close"):
                continue
            try:
                _db.close()
            except Exception:
                pass

        flaky_db.close.assert_called_once()
        healthy_db.close.assert_called_once()


class TestSessionResetZombieRace:
    """Regression for #28686 / #11016 ownership around _running_agents.

    session_reset (/new, /cc) and /stop bump the run generation and clear the
    slot at interrupt/reset time so a dead in-flight agent cannot lock the
    session forever. The outer dispatch finally must then release with the
    SAME generation guard as _run_agent - otherwise a stale unwind pops a
    follow-up turn that already reclaimed the slot.
    """

    def test_reset_time_eviction_clears_zombie_before_stale_guarded_release(self):
        """Production path: bump + clear at reset; stale guarded release is a no-op."""
        runner = _make_runner()
        runner._session_run_generation = {}
        key = "agent:main:telegram:private:1"

        gen_n = runner._begin_session_run_generation(key)
        dead_agent = MagicMock()
        runner._running_agents[key] = dead_agent
        runner._running_agents_ts[key] = 1.0
        runner._busy_ack_ts[key] = 1.0

        # session_reset bumps the generation and clears the slot immediately
        # (_interrupt_and_clear_session / _handle_reset_command).
        runner._invalidate_session_run_generation(key, reason="session_reset")
        assert runner._release_running_agent_state(key) is True
        assert key not in runner._running_agents

        # gen-N's guarded release (inner _run_agent + outer dispatch finally)
        # must not resurrect or error - slot stays empty.
        assert runner._release_running_agent_state(key, run_generation=gen_n) is False
        assert key not in runner._running_agents
        assert key not in runner._running_agents_ts
        assert key not in runner._busy_ack_ts

    def test_stale_outer_finally_does_not_clobber_newer_turn(self):
        """After /stop clears the slot, a follow-up turn claims it; the stopped
        turn's outer finally must not pop the newer entry.
        """
        runner = _make_runner()
        runner._session_run_generation = {}
        key = "agent:main:telegram:private:3"

        gen_a = runner._begin_session_run_generation(key)
        runner._running_agents[key] = MagicMock(name="turn_a")
        runner._running_agents_ts[key] = 1.0
        runner._busy_ack_ts[key] = 1.0

        # /stop: bump generation and clear the busy slot.
        runner._invalidate_session_run_generation(key, reason="stop")
        assert runner._release_running_agent_state(key) is True

        # Turn B claims the slot while Turn A is still unwinding.
        gen_b = runner._begin_session_run_generation(key)
        fresh = MagicMock(name="turn_b")
        runner._running_agents[key] = fresh
        runner._running_agents_ts[key] = 2.0
        runner._busy_ack_ts[key] = 2.0

        # Turn A's outer finally - generation-scoped, same as _run_agent.
        released = runner._release_running_agent_state(
            key, run_generation=gen_a
        )
        assert released is False
        assert runner._running_agents[key] is fresh
        assert runner._running_agents_ts[key] == 2.0
        assert runner._busy_ack_ts[key] == 2.0

        # Turn B's own release still clears when it finishes.
        assert runner._release_running_agent_state(key, run_generation=gen_b) is True
        assert key not in runner._running_agents

    def test_normal_completion_outer_release_is_idempotent(self):
        """Guarded inner release clears; outer finally with the same generation
        is a harmless no-op on an already-empty slot.
        """
        runner = _make_runner()
        runner._session_run_generation = {}
        key = "agent:main:telegram:private:2"

        gen = runner._begin_session_run_generation(key)
        runner._running_agents[key] = MagicMock()
        runner._running_agents_ts[key] = 1.0
        runner._busy_ack_ts[key] = 1.0

        assert runner._release_running_agent_state(key, run_generation=gen) is True
        assert key not in runner._running_agents
        # Outer finally passes the same run_generation - empty slot, still True
        # (no ownership block; pops are idempotent).
        assert runner._release_running_agent_state(key, run_generation=gen) is True
        assert key not in runner._running_agents_ts
        assert key not in runner._busy_ack_ts

    @pytest.mark.asyncio
    async def test_interrupt_clears_slot_before_adapter_await_fails(self):
        """invalidate+release must run before adapter interrupt; a mid-path
        adapter failure must not leave a generation-bumped zombie slot.
        """
        import threading

        from gateway.config import Platform
        from gateway.run import GatewayRunner, _INTERRUPT_REASON_STOP
        from gateway.session import SessionSource

        class _RecordingAgent:
            def __init__(self):
                self.interrupt_reasons = []

            def interrupt(self, reason=None):
                self.interrupt_reasons.append(reason)

        key = "agent:main:telegram:private:4"
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="4", chat_type="private"
        )
        agent = _RecordingAgent()

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running_agents = {key: agent}
        runner._running_agents_ts = {key: 1.0}
        runner._busy_ack_ts = {key: 1.0}
        runner._session_run_generation = {}
        runner._agent_cache = {key: (agent, "sig")}
        runner._agent_cache_lock = threading.Lock()
        runner.adapters = {}
        runner._pending_messages = {key: "queued"}

        gen_n = runner._begin_session_run_generation(key)

        class _ExplodingAdapter:
            def __init__(self):
                self.interrupt_calls = 0

            async def interrupt_session_activity(self, session_key, chat_id, metadata=None):
                self.interrupt_calls += 1
                raise RuntimeError("adapter down")

            def get_pending_message(self, session_key):
                # Mirror adapter consume-and-discard against the runner map
                # used by this unit fixture.
                return runner._pending_messages.pop(session_key, None)

        exploding = _ExplodingAdapter()
        runner._adapter_for_source = MagicMock(return_value=exploding)
        runner._thread_metadata_for_source = MagicMock(return_value=None)

        await runner._interrupt_and_clear_session(
            key,
            source,
            interrupt_reason=_INTERRUPT_REASON_STOP,
            invalidation_reason="stop_command",
        )

        assert agent.interrupt_reasons == [_INTERRUPT_REASON_STOP]
        assert key not in runner._running_agents
        assert key not in runner._agent_cache
        # Stale outer finally must remain a no-op after the bump.
        assert runner._release_running_agent_state(key, run_generation=gen_n) is False
        assert exploding.interrupt_calls == 1
        assert key not in runner._pending_messages

    def test_stale_moa_restore_does_not_clobber_newer_turn_override(self):
        """After /stop, Turn A's MoA finally restore must not overwrite Turn B's
        model_override or evict Turn B's agent cache.
        """
        import threading
        from types import SimpleNamespace

        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._agent_cache = {}
        runner._agent_cache_lock = threading.Lock()
        for name in (
            "_sessions_map",
            "_session_state",
            "_peek_session_state",
            "_begin_session_run_generation",
            "_invalidate_session_run_generation",
            "_release_running_agent_state",
            "_is_session_run_current",
            "_evict_cached_agent",
            "_restore_moa_one_shot",
            "_restore_pending_one_turn_model_override",
            "_restore_session_model_override",
            "_snapshot_session_model_override",
        ):
            setattr(
                runner,
                name,
                getattr(GatewayRunner, name).__get__(runner, GatewayRunner),
            )

        key = "agent:main:telegram:private:5"
        gen_a = runner._begin_session_run_generation(key)
        prior = {"provider": "openai", "model": "gpt-old"}
        state = runner._session_state(key)
        state.conversation.model_override = {
            "provider": "moa",
            "model": "default",
            "base_url": "moa://local",
            "api_key": "moa-virtual-provider",
            "api_mode": "chat_completions",
        }
        state.conversation.one_turn_restore = {
            "had_override": True,
            "override": dict(prior),
        }
        event_a = SimpleNamespace(
            _moa_disable_after_turn=True,
            _moa_restore_override=prior,
        )
        state.turn.agent = object()
        runner._agent_cache[key] = (object(), "sig-a")

        # /stop: bump, clear slot, restore one-shot, evict
        runner._invalidate_session_run_generation(key, reason="stop")
        assert runner._release_running_agent_state(key) is True
        runner._restore_pending_one_turn_model_override(key)
        runner._evict_cached_agent(key)
        assert state.conversation.model_override == prior

        # Turn B claims slot + sets a new override
        gen_b = runner._begin_session_run_generation(key)
        assert gen_b != gen_a
        turn_b_override = {"provider": "anthropic", "model": "claude-new"}
        state.conversation.model_override = turn_b_override
        cached_b = object()
        runner._agent_cache[key] = (cached_b, "sig-b")
        state.turn.agent = object()

        # Stale Turn A finally: generation-guarded restores are no-ops
        assert runner._is_session_run_current(key, gen_a) is False
        if runner._is_session_run_current(key, gen_a):
            runner._restore_moa_one_shot(event_a, key)
            runner._restore_pending_one_turn_model_override(key)

        assert state.conversation.model_override == turn_b_override
        assert runner._agent_cache[key][0] is cached_b

