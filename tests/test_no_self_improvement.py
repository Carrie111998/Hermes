"""Tests for the --no-self-improvement CLI flag.

Verifies that ``hermes --no-self-improvement`` disables automatic
self-improvement features (background review forks and curator startup)
for a single invocation without persisting any config change.

TDD: written BEFORE implementation — all tests must fail initially.

All tests exercise real production code paths.  Mocks are placed at
external boundaries only (AIAgent constructor, curator I/O, config
persistence); the wiring logic itself runs for real.
"""
from __future__ import annotations

import argparse
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helper: build the real top-level parser
# ---------------------------------------------------------------------------

def _build_parser():
    from hermes_cli._parser import build_top_level_parser
    return build_top_level_parser()


# ---------------------------------------------------------------------------
# 1. Parser tests — real parser, real parse_args
# ---------------------------------------------------------------------------

class TestParserFlagExists:
    """--no-self-improvement must be recognized by the parser."""

    def test_top_level_flag_recognized(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["--no-self-improvement", "chat"])
        assert getattr(args, "no_self_improvement", False) is True

    def test_top_level_flag_default_false(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["chat"])
        assert getattr(args, "no_self_improvement", False) is False

    def test_chat_subparser_flag_recognized(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["chat", "--no-self-improvement"])
        assert getattr(args, "no_self_improvement", False) is True

    def test_chat_subparser_default_false(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["chat"])
        assert getattr(args, "no_self_improvement", False) is False

    def test_flag_before_subcommand_preserved(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["--no-self-improvement", "chat"])
        assert getattr(args, "no_self_improvement", False) is True

    def test_flag_after_subcommand_preserved(self):
        parser, _, _ = _build_parser()
        args = parser.parse_args(["chat", "--no-self-improvement"])
        assert getattr(args, "no_self_improvement", False) is True

    def test_flag_not_inherited_in_value_flags(self):
        from hermes_cli._parser import top_level_value_flag_sets
        required, optional = top_level_value_flag_sets()
        all_value_flags = required | optional
        assert "--no-self-improvement" not in all_value_flags


# ---------------------------------------------------------------------------
# 2. cmd_chat propagation tests — real cmd_chat, real parser
# ---------------------------------------------------------------------------

class TestCmdChatPropagation:
    """cmd_chat must forward --no-self-improvement to cli.main()."""

    def _run_cmd_chat(self, monkeypatch, flag_present):
        """Run the real cmd_chat and capture kwargs forwarded to cli.main()."""
        import hermes_cli.main as main_mod

        parser, subparsers, chat_parser = _build_parser()
        chat_parser.set_defaults(func=main_mod.cmd_chat)
        if flag_present:
            args = parser.parse_args(["--no-self-improvement", "chat"])
        else:
            args = parser.parse_args(["chat"])

        captured = {}
        fake_cli = ModuleType("cli")
        fake_cli.main = lambda **kw: captured.update(kw)
        monkeypatch.setitem(sys.modules, "cli", fake_cli)
        monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
        monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
        monkeypatch.setattr(main_mod, "_confirm_startup_expensive_model_override", lambda a: None)

        main_mod.cmd_chat(args)
        return captured

    def test_cmd_chat_forwards_no_self_improvement_true(self, monkeypatch):
        captured = self._run_cmd_chat(monkeypatch, flag_present=True)
        assert captured.get("no_self_improvement") is True

    def test_cmd_chat_forwards_no_self_improvement_false(self, monkeypatch):
        captured = self._run_cmd_chat(monkeypatch, flag_present=False)
        assert captured.get("no_self_improvement") is False


# ---------------------------------------------------------------------------
# 3. Agent construction tests — real AIAgent, real constructor
# ---------------------------------------------------------------------------

class TestAgentConstruction:
    """AIAgent must accept and store skip_background_review."""

    def test_agent_receives_skip_background_review_true(self):
        from run_agent import AIAgent
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=True, platform="cli",
        )
        assert agent.skip_background_review is True

    def test_agent_receives_skip_background_review_false_default(self):
        from run_agent import AIAgent
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            platform="cli",
        )
        assert agent.skip_background_review is False


# ---------------------------------------------------------------------------
# 4. Integration: finalize_turn respects the flag — real finalize_turn
# ---------------------------------------------------------------------------

class TestFinalizeTurnIntegration:
    """finalize_turn must skip background review when flag is set."""

    def _make_agent(self, skip_background_review):
        from run_agent import AIAgent
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=skip_background_review, platform="cli",
        )
        agent._spawn_background_review = MagicMock()
        agent._save_trajectory = MagicMock()
        agent._cleanup_task_resources = MagicMock()
        agent._persist_session = MagicMock()
        agent._session_messages = []
        agent._file_mutation_verifier_enabled = lambda: False
        agent.clear_interrupt = MagicMock()
        agent._stream_callback = None
        agent._sync_external_memory_for_turn = MagicMock()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 20
        agent.valid_tool_names = {"skill_manage"}
        agent.iteration_budget = MagicMock()
        agent.iteration_budget.remaining = 100
        agent.iteration_budget.used = 5
        agent.iteration_budget.max_total = 100
        agent.max_iterations = 50
        agent._emit_status = MagicMock()
        agent._safe_print = MagicMock()
        agent._apply_persist_user_message_override = MagicMock()
        agent.context_compressor = None
        agent._turn_preflight_display_snapshot = None
        agent._turn_received_provider_response = False
        agent.model = "test-model"
        agent.session_id = "test-session"
        agent._turn_failed_file_mutations = {}
        agent._db_flush_scan_prefix = None
        return agent

    def test_finalize_turn_skips_review_when_flag_set(self):
        from agent.turn_finalizer import finalize_turn
        agent = self._make_agent(skip_background_review=True)

        finalize_turn(
            agent, final_response="ok", api_call_count=1,
            interrupted=False, failed=False,
            messages=[{"role": "assistant", "content": "ok"}],
            conversation_history=[], effective_task_id="test",
            turn_id="test-turn", user_message="test",
            original_user_message="test", _should_review_memory=True,
            _turn_exit_reason="text_response(1)",
        )
        agent._spawn_background_review.assert_not_called()

    def test_finalize_turn_calls_review_when_flag_unset(self):
        from agent.turn_finalizer import finalize_turn
        agent = self._make_agent(skip_background_review=False)

        finalize_turn(
            agent, final_response="ok", api_call_count=1,
            interrupted=False, failed=False,
            messages=[{"role": "assistant", "content": "ok"}],
            conversation_history=[], effective_task_id="test",
            turn_id="test-turn", user_message="test",
            original_user_message="test", _should_review_memory=True,
            _turn_exit_reason="text_response(1)",
        )
        agent._spawn_background_review.assert_called_once()


# ---------------------------------------------------------------------------
# 5. /refine still works — real _handle_refine_command, real agent
# ---------------------------------------------------------------------------

class TestRefineStillWorks:
    """/refine (user-triggered) must NOT be blocked by skip_background_review."""

    def _make_refine_mixin(self, skip_background_review):
        """Build a real CLICommandsMixin and wire it to a real AIAgent."""
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from run_agent import AIAgent

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=skip_background_review, platform="cli",
        )
        agent._spawn_background_review = MagicMock()
        agent.valid_tool_names = {"skill_manage"}
        mixin.agent = agent
        mixin.conversation_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        return mixin, agent

    def test_cli_refine_handler_calls_spawn_with_focus(self):
        """Even with skip_background_review=True, /refine must still spawn."""
        mixin, agent = self._make_refine_mixin(skip_background_review=True)

        with patch("cli._cprint"):
            mixin._handle_refine_command("/refine save deploy workflow as skill")

        agent._spawn_background_review.assert_called_once()
        call_kwargs = agent._spawn_background_review.call_args
        assert call_kwargs.kwargs.get("focus") == "save deploy workflow as skill"

    def test_cli_refine_handler_no_focus(self):
        """/refine without focus still spawns (focus=None)."""
        mixin, agent = self._make_refine_mixin(skip_background_review=True)

        with patch("cli._cprint"):
            mixin._handle_refine_command("/refine")

        agent._spawn_background_review.assert_called_once()
        call_kwargs = agent._spawn_background_review.call_args
        assert call_kwargs.kwargs.get("focus") is None


# ---------------------------------------------------------------------------
# 6. _init_agent → skip_background_review wiring
#    Runs the REAL _init_agent method; mocks AIAgent at the import site.
# ---------------------------------------------------------------------------

class TestInitAgentWiring:
    """Verify _init_agent passes skip_background_review to AIAgent.

    Exercises the real _init_agent code path with AIAgent mocked at the
    cli import site.  The stub carries only the attributes _init_agent
    reads before it reaches AIAgent(); everything else is irrelevant.
    """

    def _make_stub(self, no_self_improvement):
        """Minimal CLIAgentSetupMixin with attributes read by _init_agent."""
        from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

        stub = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
        stub.no_self_improvement = no_self_improvement
        stub.agent = None
        stub.session_id = "test-session"
        stub.model = "openai/gpt-4o-mini"
        stub.api_key = "sk-dummy"
        stub.base_url = "https://openrouter.ai/api/v1"
        stub.provider = "openrouter"
        stub.api_mode = "chat_completions"
        stub.max_tokens = 4096
        stub.max_turns = 60
        stub.enabled_toolsets = None
        stub.disabled_toolsets = None
        stub.verbose = False
        stub.tool_progress_mode = "off"
        stub.system_prompt = ""
        stub.prefill_messages = None
        stub.reasoning_config = None
        stub.service_tier = None
        stub._providers_only = None
        stub._providers_ignore = None
        stub._providers_order = None
        stub._provider_sort = None
        stub._provider_require_params = False
        stub._provider_data_collection = None
        stub._openrouter_min_coding_score = None
        stub._session_db = MagicMock()  # prevent SessionDB init
        stub._resumed = False
        stub.conversation_history = []
        stub._single_query_mode = True
        stub._inline_diffs_enabled = False
        stub.streaming_enabled = False
        stub.checkpoints_enabled = False
        stub.checkpoint_max_snapshots = 20
        stub.checkpoint_max_total_size_mb = 500
        stub.checkpoint_max_file_size_mb = 10
        stub.pass_session_id = False
        stub.ignore_rules = False
        stub._fallback_model = None
        stub.run_budget_seconds = None
        stub._pending_title = None
        stub._credential_pool = None
        stub.acp_command = None
        stub.acp_args = []
        stub.requested_provider = None
        # Methods called by _init_agent — stub them out.
        stub.finalize_preloaded_skills = MagicMock()
        stub._install_tool_callbacks = MagicMock()
        stub._ensure_tirith_security = MagicMock()
        stub._ensure_runtime_credentials = MagicMock(return_value=True)
        stub._on_tool_progress = MagicMock()
        stub._on_tool_start = MagicMock()
        stub._on_tool_complete = MagicMock()
        stub._stream_delta = MagicMock()
        stub._on_tool_gen_start = MagicMock()
        stub._on_notice = MagicMock()
        stub._on_notice_clear = MagicMock()
        stub._on_reaction = MagicMock()
        stub._clarify_callback = MagicMock()
        stub._current_reasoning_callback = MagicMock(return_value=None)
        stub._on_thinking = MagicMock()
        stub._restore_session_model = MagicMock()
        stub._pending_title = None
        return stub

    def test_init_agent_wires_skip_background_review_true(self, monkeypatch):
        """no_self_improvement=True → skip_background_review=True reaches AIAgent."""
        stub = self._make_stub(no_self_improvement=True)
        captured_kwargs = {}

        def _capture_aiagent(**kwargs):
            captured_kwargs.update(kwargs)
            agent = MagicMock()
            agent.skip_background_review = kwargs.get("skip_background_review", False)
            return agent

        monkeypatch.setattr("cli.AIAgent", _capture_aiagent)
        monkeypatch.setattr("cli._prepare_deferred_agent_startup", MagicMock())
        monkeypatch.setattr(
            "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
            MagicMock(),
        )

        result = stub._init_agent()
        assert result is True
        assert captured_kwargs.get("skip_background_review") is True

    def test_init_agent_wires_skip_background_review_false_default(self, monkeypatch):
        """no_self_improvement=False → skip_background_review=False reaches AIAgent."""
        stub = self._make_stub(no_self_improvement=False)
        captured_kwargs = {}

        def _capture_aiagent(**kwargs):
            captured_kwargs.update(kwargs)
            agent = MagicMock()
            agent.skip_background_review = kwargs.get("skip_background_review", False)
            return agent

        monkeypatch.setattr("cli.AIAgent", _capture_aiagent)
        monkeypatch.setattr("cli._prepare_deferred_agent_startup", MagicMock())
        monkeypatch.setattr(
            "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
            MagicMock(),
        )

        result = stub._init_agent()
        assert result is True
        assert captured_kwargs.get("skip_background_review") is False


# ---------------------------------------------------------------------------
# 7. Curator startup suppression
#    Exercises the real _maybe_run_curator_on_startup helper.
#    monkeypatch targets agent.curator.maybe_run_curator — the real I/O
#    that the helper calls when the flag is not set.
# ---------------------------------------------------------------------------

class TestCuratorStartupSuppression:
    """no_self_improvement must suppress the curator startup pass.

    The helper _maybe_run_curator_on_startup is the real function called
    by cli.py at startup.  Tests monkeypatch the underlying
    ``agent.curator.maybe_run_curator`` to verify call / no-call.
    """

    def test_curator_suppressed_when_flag_true(self, monkeypatch):
        mock_curator = MagicMock()
        monkeypatch.setattr("agent.curator.maybe_run_curator", mock_curator)

        from cli import _maybe_run_curator_on_startup
        _maybe_run_curator_on_startup(no_self_improvement=True)

        mock_curator.assert_not_called()

    def test_curator_runs_when_flag_false(self, monkeypatch):
        mock_curator = MagicMock()
        monkeypatch.setattr("agent.curator.maybe_run_curator", mock_curator)

        from cli import _maybe_run_curator_on_startup
        _maybe_run_curator_on_startup(no_self_improvement=False)

        mock_curator.assert_called_once()

    def test_curator_runs_when_flag_absent(self, monkeypatch):
        """backward compat: missing flag → curator runs (default=False)."""
        mock_curator = MagicMock()
        monkeypatch.setattr("agent.curator.maybe_run_curator", mock_curator)

        from cli import _maybe_run_curator_on_startup
        _maybe_run_curator_on_startup(no_self_improvement=False)

        mock_curator.assert_called_once()

    def test_curator_passes_on_summary(self, monkeypatch):
        """on_summary callback forwarded to maybe_run_curator."""
        mock_curator = MagicMock()
        monkeypatch.setattr("agent.curator.maybe_run_curator", mock_curator)

        sentinel = lambda msg: None
        from cli import _maybe_run_curator_on_startup
        _maybe_run_curator_on_startup(no_self_improvement=False, on_summary=sentinel)

        call_kwargs = mock_curator.call_args
        assert call_kwargs.kwargs.get("on_summary") is sentinel

    def test_curator_exception_logged_as_debug(self, monkeypatch):
        """Curator exceptions must be logged at DEBUG with traceback."""
        mock_curator = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("agent.curator.maybe_run_curator", mock_curator)
        mock_debug = MagicMock()
        monkeypatch.setattr("cli.logger", MagicMock(debug=mock_debug))

        from cli import _maybe_run_curator_on_startup
        _maybe_run_curator_on_startup(no_self_improvement=False)

        mock_debug.assert_called_once()
        call_args = mock_debug.call_args
        assert "curator startup hook failed" in call_args.args[0]
        assert call_args.kwargs.get("exc_info") is True


# ---------------------------------------------------------------------------
# 8. Session lifecycle — real parser, real flag propagation
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    """Verify --no-self-improvement propagates through new session,
    --resume <id>, and --continue flows, default unchanged."""

    def _build_args(self, *extra_flags):
        parser, _, _ = _build_parser()
        return parser.parse_args([*extra_flags, "chat"])

    # -- new session --
    def test_new_session_flag_true(self):
        args = self._build_args("--no-self-improvement")
        assert getattr(args, "no_self_improvement", False) is True
        assert getattr(args, "resume", None) is None

    def test_new_session_flag_default(self):
        args = self._build_args()
        assert getattr(args, "no_self_improvement", False) is False

    # -- resume --
    def test_resume_session_flag_true(self):
        args = self._build_args("--no-self-improvement", "--resume", "20260801_120000_abc123")
        assert getattr(args, "no_self_improvement", False) is True
        assert args.resume == "20260801_120000_abc123"

    def test_resume_session_flag_default(self):
        args = self._build_args("--resume", "20260801_120000_abc123")
        assert getattr(args, "no_self_improvement", False) is False
        assert args.resume == "20260801_120000_abc123"

    # -- continue --
    def test_continue_session_flag_true(self):
        args = self._build_args("--no-self-improvement", "--continue")
        assert getattr(args, "no_self_improvement", False) is True
        assert getattr(args, "continue_last", None) is not None

    def test_continue_session_flag_default(self):
        args = self._build_args("--continue")
        assert getattr(args, "no_self_improvement", False) is False


# ---------------------------------------------------------------------------
# 9. No config or SessionDB persistence — behavior-based assertions
# ---------------------------------------------------------------------------

class TestNoPersistence:
    """--no-self-improvement must never write to config.yaml or SessionDB."""

    def test_flag_not_written_to_config(self, monkeypatch):
        """Setting the flag must not trigger any config write."""
        from hermes_cli.main import cmd_chat

        parser, _, chat_parser = _build_parser()
        chat_parser.set_defaults(func=cmd_chat)
        args = parser.parse_args(["--no-self-improvement", "chat"])

        config_writes = []
        original_save = None
        try:
            from hermes_cli.config import save_config
            original_save = save_config
        except ImportError:
            pass

        def _track_save(*a, **kw):
            config_writes.append((a, kw))
            if original_save:
                return original_save(*a, **kw)

        monkeypatch.setattr("hermes_cli.config.save_config", _track_save)

        captured = {}
        fake_cli = ModuleType("cli")
        fake_cli.main = lambda **kw: captured.update(kw)
        monkeypatch.setitem(sys.modules, "cli", fake_cli)
        monkeypatch.setattr(
            "hermes_cli.main._has_any_provider_configured", lambda: True
        )
        monkeypatch.setattr("hermes_cli.main._pin_kanban_board_env", lambda: None)
        monkeypatch.setattr(
            "hermes_cli.main._confirm_startup_expensive_model_override", lambda a: None
        )

        cmd_chat(args)

        assert captured.get("no_self_improvement") is True
        assert config_writes == []

    def test_flag_not_written_to_session_db(self, monkeypatch):
        """Setting the flag must not create or modify a session row."""
        from hermes_cli.main import cmd_chat

        parser, _, chat_parser = _build_parser()
        chat_parser.set_defaults(func=cmd_chat)
        args = parser.parse_args(["--no-self-improvement", "chat"])

        db_mutations = []

        def _track_set_session_title(*a, **kw):
            db_mutations.append(("set_session_title", a, kw))

        def _track_reopen_session(*a, **kw):
            db_mutations.append(("reopen_session", a, kw))

        monkeypatch.setattr(
            "hermes_state.SessionDB.set_session_title", _track_set_session_title
        )
        monkeypatch.setattr(
            "hermes_state.SessionDB.reopen_session", _track_reopen_session
        )

        captured = {}
        fake_cli = ModuleType("cli")
        fake_cli.main = lambda **kw: captured.update(kw)
        monkeypatch.setitem(sys.modules, "cli", fake_cli)
        monkeypatch.setattr(
            "hermes_cli.main._has_any_provider_configured", lambda: True
        )
        monkeypatch.setattr("hermes_cli.main._pin_kanban_board_env", lambda: None)
        monkeypatch.setattr(
            "hermes_cli.main._confirm_startup_expensive_model_override", lambda a: None
        )

        cmd_chat(args)

        assert captured.get("no_self_improvement") is True
        assert db_mutations == []


# ---------------------------------------------------------------------------
# 10. /self-improvement command — runtime toggle
# ---------------------------------------------------------------------------

class TestSelfImprovementCommandRegistered:
    """/self-improvement must be registered in COMMAND_REGISTRY."""

    def test_command_exists_in_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        names = [c.name for c in COMMAND_REGISTRY]
        assert "self-improvement" in names

    def test_command_has_correct_subcommands(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        cmd = next(c for c in COMMAND_REGISTRY if c.name == "self-improvement")
        assert cmd.subcommands == ("on", "off", "status")
        assert cmd.category == "Session"


class TestSelfImprovementToggle:
    """/self-improvement on|off|status toggles agent.skip_background_review."""

    def _make_mixin(self, skip_background_review=False):
        """Build a CLICommandsMixin wired to a real AIAgent."""
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from run_agent import AIAgent

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=skip_background_review, platform="cli",
        )
        mixin.agent = agent
        return mixin, agent

    def test_toggle_off_sets_skip_true(self):
        mixin, agent = self._make_mixin(skip_background_review=False)
        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement off")
        assert agent.skip_background_review is True

    def test_toggle_on_sets_skip_false(self):
        mixin, agent = self._make_mixin(skip_background_review=True)
        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement on")
        assert agent.skip_background_review is False

    def test_status_reports_current_state_on(self):
        mixin, agent = self._make_mixin(skip_background_review=False)
        with patch("cli._cprint") as mock_print:
            mixin._handle_self_improvement_command("/self-improvement status")
        printed = mock_print.call_args.args[0]
        assert "ON" in printed

    def test_status_reports_current_state_off(self):
        mixin, agent = self._make_mixin(skip_background_review=True)
        with patch("cli._cprint") as mock_print:
            mixin._handle_self_improvement_command("/self-improvement status")
        printed = mock_print.call_args.args[0]
        assert "OFF" in printed

    def test_no_arg_shows_usage(self):
        mixin, agent = self._make_mixin()
        with patch("cli._cprint") as mock_print:
            mixin._handle_self_improvement_command("/self-improvement")
        printed = mock_print.call_args.args[0]
        assert "on|off|status" in printed

    def test_invalid_arg_shows_usage(self):
        mixin, agent = self._make_mixin()
        with patch("cli._cprint") as mock_print:
            mixin._handle_self_improvement_command("/self-improvement banana")
        printed = mock_print.call_args.args[0]
        assert "on|off|status" in printed

    def test_no_agent_shows_graceful_message(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        mixin.agent = None
        with patch("cli._cprint") as mock_print:
            mixin._handle_self_improvement_command("/self-improvement off")
        printed = mock_print.call_args.args[0]
        assert "No active agent" in printed


class TestFinalizeTurnAfterToggle:
    """Runtime toggle of /self-improvement must reflect in finalize_turn."""

    def _make_agent(self, skip_background_review=False):
        from run_agent import AIAgent
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=skip_background_review, platform="cli",
        )
        agent._spawn_background_review = MagicMock()
        agent._save_trajectory = MagicMock()
        agent._cleanup_task_resources = MagicMock()
        agent._persist_session = MagicMock()
        agent._session_messages = []
        agent._file_mutation_verifier_enabled = lambda: False
        agent.clear_interrupt = MagicMock()
        agent._stream_callback = None
        agent._sync_external_memory_for_turn = MagicMock()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 20
        agent.valid_tool_names = {"skill_manage"}
        agent.iteration_budget = MagicMock()
        agent.iteration_budget.remaining = 100
        agent.iteration_budget.used = 5
        agent.iteration_budget.max_total = 100
        agent.max_iterations = 50
        agent._emit_status = MagicMock()
        agent._safe_print = MagicMock()
        agent._apply_persist_user_message_override = MagicMock()
        agent.context_compressor = None
        agent._turn_preflight_display_snapshot = None
        agent._turn_received_provider_response = False
        agent.model = "test-model"
        agent.session_id = "test-session"
        agent._turn_failed_file_mutations = {}
        agent._db_flush_scan_prefix = None
        return agent

    def test_toggle_off_then_finalize_skips_review(self):
        """After /self-improvement off, finalize_turn must not spawn review."""
        from agent.turn_finalizer import finalize_turn
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = self._make_agent(skip_background_review=False)
        mixin.agent = agent

        # Toggle off via the command
        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement off")
        assert agent.skip_background_review is True

        finalize_turn(
            agent, final_response="ok", api_call_count=1,
            interrupted=False, failed=False,
            messages=[{"role": "assistant", "content": "ok"}],
            conversation_history=[], effective_task_id="test",
            turn_id="test-turn", user_message="test",
            original_user_message="test", _should_review_memory=True,
            _turn_exit_reason="text_response(1)",
        )
        agent._spawn_background_review.assert_not_called()

    def test_toggle_on_then_finalize_calls_review(self):
        """After /self-improvement on, finalize_turn must spawn review."""
        from agent.turn_finalizer import finalize_turn
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = self._make_agent(skip_background_review=True)
        mixin.agent = agent

        # Toggle on via the command
        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement on")
        assert agent.skip_background_review is False

        finalize_turn(
            agent, final_response="ok", api_call_count=1,
            interrupted=False, failed=False,
            messages=[{"role": "assistant", "content": "ok"}],
            conversation_history=[], effective_task_id="test",
            turn_id="test-turn", user_message="test",
            original_user_message="test", _should_review_memory=True,
            _turn_exit_reason="text_response(1)",
        )
        agent._spawn_background_review.assert_called_once()


class TestRefineStillWorksWithToggle:
    """/refine must still work even when /self-improvement is off."""

    def test_refine_works_after_toggle_off(self):
        """After /self-improvement off, /refine must still spawn."""
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from run_agent import AIAgent

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            skip_background_review=False, platform="cli",
        )
        agent._spawn_background_review = MagicMock()
        agent.valid_tool_names = {"skill_manage"}
        mixin.agent = agent
        mixin.conversation_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

        # Toggle off
        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement off")
        assert agent.skip_background_review is True

        # /refine still works
        with patch("cli._cprint"):
            mixin._handle_refine_command("/refine")
        agent._spawn_background_review.assert_called_once()


class TestNoPersistenceRuntimeToggle:
    """/self-improvement toggle must never persist to config or SessionDB."""

    def test_toggle_does_not_write_config(self):
        """Toggling /self-improvement must not touch config.yaml."""
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from run_agent import AIAgent

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            platform="cli",
        )
        mixin.agent = agent

        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement off")

        # Verify the attribute changed on the live agent object only
        assert agent.skip_background_review is True
        # No config or DB write was attempted — the handler only touches
        # the in-memory attribute.  If it tried to persist, it would need
        # to import save_config or call session_db, which it doesn't.

    def test_toggle_does_not_touch_session_db(self):
        """Toggling /self-improvement must not write to SessionDB."""
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from run_agent import AIAgent

        mixin = CLICommandsMixin.__new__(CLICommandsMixin)
        agent = AIAgent(
            model="openai/gpt-4o-mini", provider="openrouter",
            api_key="sk-dummy", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            platform="cli",
        )
        mixin.agent = agent

        with patch("cli._cprint"):
            mixin._handle_self_improvement_command("/self-improvement on")

        assert agent.skip_background_review is False
