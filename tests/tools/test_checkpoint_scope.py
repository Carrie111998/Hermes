"""CheckpointManager snapshot scope: turn (default) vs task (#68877).

scope="turn" resets the per-directory dedup on every agent iteration, so each
turn can take one snapshot (historical behavior). scope="task" makes new_turn()
a no-op — the dedup persists across the task's iterations so only the first
file mutation snapshots the pre-task baseline — while new_task() re-arms it at
the next task boundary. These tests exercise the dedup bookkeeping directly
(no git needed) by inspecting ``_checkpointed_dirs``.
"""

from tools.checkpoint_manager import CheckpointManager


class TestScopeNormalization:
    def test_default_scope_is_turn(self):
        assert CheckpointManager().scope == "turn"

    def test_task_scope_honored(self):
        assert CheckpointManager(scope="task").scope == "task"

    def test_scope_case_insensitive_and_trimmed(self):
        assert CheckpointManager(scope="  TASK ").scope == "task"

    def test_unknown_scope_degrades_to_turn(self):
        assert CheckpointManager(scope="bogus").scope == "turn"
        assert CheckpointManager(scope="").scope == "turn"


class TestTurnScopeDedup:
    def test_new_turn_clears_dedup_each_iteration(self):
        mgr = CheckpointManager(enabled=True, scope="turn")
        # Simulate a snapshot having been taken this turn.
        mgr._checkpointed_dirs.add("/work")
        mgr.new_turn()
        # Cleared → the next iteration is free to snapshot again.
        assert "/work" not in mgr._checkpointed_dirs


class TestTaskScopeDedup:
    def test_new_turn_is_noop_in_task_scope(self):
        mgr = CheckpointManager(enabled=True, scope="task")
        mgr._checkpointed_dirs.add("/work")
        mgr.new_turn()
        # Persisted → later turns in the same task will skip re-snapshotting.
        assert "/work" in mgr._checkpointed_dirs

    def test_new_task_clears_even_in_task_scope(self):
        mgr = CheckpointManager(enabled=True, scope="task")
        mgr._checkpointed_dirs.add("/work")
        mgr.new_task()
        assert "/work" not in mgr._checkpointed_dirs

    def test_new_task_clears_in_turn_scope_too(self):
        mgr = CheckpointManager(enabled=True, scope="turn")
        mgr._checkpointed_dirs.add("/work")
        mgr.new_task()
        assert "/work" not in mgr._checkpointed_dirs


class TestEndToEndDedupSemantics:
    """Model the loop's calls (new_task once, new_turn per iteration) and
    assert how many times a directory would be eligible for a snapshot."""

    def _eligible_count(self, scope, iterations):
        """Count how many iterations would take a snapshot: a dir is eligible
        when it is NOT already in the dedup set. Mirrors ensure_checkpoint's
        ``if abs_dir in self._checkpointed_dirs: return False`` gate."""
        mgr = CheckpointManager(enabled=True, scope=scope)
        mgr.new_task()  # task boundary, before the loop
        took = 0
        for _ in range(iterations):
            mgr.new_turn()  # start of each agent iteration
            if "/work" not in mgr._checkpointed_dirs:
                took += 1
                mgr._checkpointed_dirs.add("/work")  # ensure_checkpoint records it
        return took

    def test_turn_scope_snapshots_every_iteration(self):
        assert self._eligible_count("turn", iterations=5) == 5

    def test_task_scope_snapshots_once_per_task(self):
        assert self._eligible_count("task", iterations=5) == 1

    def test_task_scope_rearms_on_next_task(self):
        mgr = CheckpointManager(enabled=True, scope="task")
        # Task 1
        mgr.new_task()
        mgr.new_turn()
        assert "/work" not in mgr._checkpointed_dirs
        mgr._checkpointed_dirs.add("/work")
        mgr.new_turn()
        assert "/work" in mgr._checkpointed_dirs  # no second snapshot in task 1
        # Task 2 — fresh baseline
        mgr.new_task()
        assert "/work" not in mgr._checkpointed_dirs


class TestRedirectReArmsTaskBaseline:
    """A mid-turn correction is a user-message boundary too (#68877 review).

    ``run_conversation`` calls ``new_task()`` once before the iteration loop,
    but a redirect appends a real user message *inside* that loop and keeps
    going. Without a reset there, a scope="task" run would keep rolling back
    to the state before the *original* instruction — discarding the work the
    correction just asked for.
    """

    def test_redirect_branch_calls_new_task(self):
        import inspect

        from agent import conversation_loop

        src = inspect.getsource(conversation_loop.run_conversation)
        redirect_at = src.index("_apply_active_turn_redirect(agent, messages")
        # The redirect branch ends where the per-iteration reset begins.
        turn_reset_at = src.index("_checkpoint_mgr.new_turn()", redirect_at)
        redirect_branch = src[redirect_at:turn_reset_at]

        assert "_checkpoint_mgr.new_task()" in redirect_branch, (
            "the redirect branch must re-arm the task baseline; otherwise a "
            "scope='task' rollback discards the corrected work"
        )

    def test_new_task_after_simulated_redirect_allows_a_fresh_baseline(self):
        mgr = CheckpointManager(enabled=True, scope="task")
        # First mutation of the task snapshots and dedups the dir.
        mgr._checkpointed_dirs.add("/work")
        # Later iterations in the same task must NOT re-snapshot...
        mgr.new_turn()
        assert "/work" in mgr._checkpointed_dirs
        # ...but a correction arrives, which is a new task boundary.
        mgr.new_task()
        assert "/work" not in mgr._checkpointed_dirs


class TestScopeReachesConstructionPaths:
    """checkpoints.scope must survive the trip to every agent surface."""

    def test_gateway_kwargs_forward_scope(self):
        from gateway.run import _checkpoint_agent_kwargs

        kwargs = _checkpoint_agent_kwargs({"checkpoints": {"enabled": True, "scope": "task"}})
        assert kwargs["checkpoint_scope"] == "task"

    def test_gateway_kwargs_default_scope_is_turn(self):
        from gateway.run import _checkpoint_agent_kwargs

        assert _checkpoint_agent_kwargs({"checkpoints": {"enabled": True}})["checkpoint_scope"] == "turn"

    def test_tui_reads_scope_from_config(self, monkeypatch):
        """HERMES_TUI_CHECKPOINTS only gates *whether* checkpoints run.

        The cadence still comes from config, so a TUI session must not force
        "turn" when the user configured "task".
        """
        from tui_gateway import server

        monkeypatch.setattr(server, "_load_cfg", lambda: {"checkpoints": {"scope": "task"}})
        assert server._load_checkpoint_scope() == "task"

    def test_tui_scope_defaults_to_turn_when_unset_or_malformed(self, monkeypatch):
        from tui_gateway import server

        for cfg in ({}, {"checkpoints": True}, {"checkpoints": {}}, {"checkpoints": {"scope": "  "}}):
            monkeypatch.setattr(server, "_load_cfg", lambda cfg=cfg: cfg)
            assert server._load_checkpoint_scope() == "turn"

    def test_tui_agent_construction_passes_the_scope(self):
        """The helper must actually be wired into _make_agent's AIAgent call.

        Reading config is useless if the constructor never receives it — that
        was the original gap: HERMES_TUI_CHECKPOINTS turned checkpoints on and
        every other checkpoint setting was left at the constructor default.
        """
        import inspect

        from tui_gateway import server

        src = inspect.getsource(server._make_agent)
        assert "checkpoint_scope=_load_checkpoint_scope()" in src

    def test_agent_constructor_threads_scope_to_manager(self):
        import inspect

        from agent.agent_init import init_agent

        src = inspect.getsource(init_agent)
        assert "scope=checkpoint_scope" in src
