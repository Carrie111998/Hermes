"""Tests for the per-invocation ``--reasoning`` CLI flag.

Covers:
- argparse: ``--reasoning`` accepted on top-level and ``chat`` subparser with
  the documented choices; absent flag leaves ``args.reasoning`` unset.
- ``cmd_chat``: ``HERMES_REASONING`` env var set from ``--reasoning``.
- ``_launch_tui``: ``HERMES_TUI_REASONING`` env forwarded to the TUI process.
- ``HermesCLI.__init__``: ``reasoning_effort`` / ``HERMES_REASONING`` override
  the resolved ``reasoning_config``.
- ``run_oneshot`` → ``_run_agent``: ``reasoning_config`` plumbed into AIAgent.
- ``tui_gateway.server._load_reasoning_config``: ``HERMES_TUI_REASONING`` wins
  over config.yaml.
"""

import os

import pytest


def _build_parser():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    return parser


class TestArgparseReasoningFlag:
    def test_top_level_accepts_reasoning(self):
        parser = _build_parser()
        args = parser.parse_args(["--reasoning", "high", "chat", "-q", "hi"])
        assert args.reasoning == "high"

    def test_chat_subparser_accepts_reasoning(self):
        parser = _build_parser()
        args = parser.parse_args(["chat", "--reasoning", "low", "-q", "hi"])
        assert args.reasoning == "low"

    def test_absent_reasoning_defaults_none(self):
        parser = _build_parser()
        args = parser.parse_args(["chat", "-q", "hi"])
        assert getattr(args, "reasoning", None) is None

    def test_invalid_reasoning_rejected(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["chat", "--reasoning", "bogus", "-q", "hi"])

    def test_reasoning_none_is_valid(self):
        parser = _build_parser()
        args = parser.parse_args(["chat", "--reasoning", "none", "-q", "hi"])
        assert args.reasoning == "none"

    def test_oneshot_accepts_reasoning(self):
        parser = _build_parser()
        args = parser.parse_args(["-z", "hello", "--reasoning", "max"])
        assert args.reasoning == "max"


class TestCmdChatReasoningEnv:
    def test_cmd_chat_sets_hermes_reasoning_env(self, monkeypatch):
        from hermes_cli.main import cmd_chat

        class Args:
            reasoning = "high"
            source = None
            yolo = False
            ignore_user_config = False
            ignore_rules = False
            safe_mode = False
            resume = None
            continue_last = None
            no_restore_cwd = False
            worktree = False
            model = None
            provider = None
            toolsets = None
            skills = None
            verbose = None
            quiet = False
            query = None
            image = None
            checkpoints = False
            pass_session_id = False
            max_turns = None
            accept_hooks = False
            tui_dev = False
            compact = False

        monkeypatch.delenv("HERMES_REASONING", raising=False)
        # Avoid actually launching anything.
        monkeypatch.setattr(
            "hermes_cli.main._resolve_use_tui", lambda args: False
        )
        monkeypatch.setattr(
            "hermes_cli.main._apply_safe_mode", lambda args: None
        )
        monkeypatch.setattr(
            "hermes_cli.main._has_any_provider_configured", lambda: True
        )
        monkeypatch.setattr(
            "hermes_cli.main._sync_bundled_skills_for_startup",
            lambda: None,
        )
        monkeypatch.setattr(
            "hermes_cli.main._pin_kanban_board_env", lambda: None
        )
        monkeypatch.setattr(
            "hermes_cli.main._termux_should_prefetch_update_check",
            lambda: False,
        )
        # cmd_chat imports cli.main at the end; monkeypatch the module-level
        # name so the real REPL never starts.
        import cli as cli_mod

        monkeypatch.setattr(cli_mod, "main", lambda **kw: None)

        cmd_chat(Args())
        assert os.environ.get("HERMES_REASONING") == "high"


class TestLaunchTuiReasoningEnv:
    def test_launch_tui_forwards_reasoning_env(self, monkeypatch, tmp_path):
        import hermes_cli.main as main_mod
        from hermes_cli.main import _launch_tui

        captured = {}

        def fake_make_tui_argv(tui_dir, tui_dev):
            return ["node", "entry.js"], str(tmp_path)

        monkeypatch.setattr(
            "hermes_cli.main._make_tui_argv", fake_make_tui_argv
        )
        monkeypatch.setattr(
            "hermes_cli.main._apply_tui_python_env", lambda env: None
        )
        monkeypatch.setattr(
            "hermes_cli.main._resolve_tui_heap_mb", lambda: 4096
        )
        monkeypatch.setattr(
            "hermes_cli.main._normalize_tui_toolsets", lambda t: None
        )
        monkeypatch.setattr(
            "hermes_cli.main._termux_should_prefetch_update_check",
            lambda: False,
        )
        # The wrapper calls subprocess.call at the end; capture instead of
        # actually spawning node.
        def fake_call(argv, cwd=None, env=None):
            captured["env"] = env
            return 0

        monkeypatch.setattr("hermes_cli.main.subprocess.call", fake_call)

        try:
            _launch_tui(
                resume_session_id=None,
                model=None,
                provider=None,
                toolsets=None,
                skills=None,
                verbose=None,
                quiet=False,
                query=None,
                image=None,
                worktree=False,
                checkpoints=False,
                pass_session_id=False,
                max_turns=None,
                accept_hooks=False,
                reasoning_effort="medium",
            )
        except SystemExit:
            # _launch_tui ends with sys.exit(code); code=0 here.
            pass

        env = captured.get("env")
        assert env is not None
        assert env.get("HERMES_TUI_REASONING") == "medium"


class TestHermesCLIReasoningEffort:
    def test_init_accepts_reasoning_effort_kwarg(self, monkeypatch):
        import cli as cli_mod

        orig = cli_mod.CLI_CONFIG
        cli_mod.CLI_CONFIG = {
            "agent": {},
            "display": {},
            "model": {},
            "compression": {},
            "terminal": {},
        }
        try:
            from cli import HermesCLI

            h = HermesCLI(reasoning_effort="low")
            assert h.reasoning_config == {"enabled": True, "effort": "low"}
        finally:
            cli_mod.CLI_CONFIG = orig

    def test_env_var_override(self, monkeypatch):
        import cli as cli_mod

        monkeypatch.setenv("HERMES_REASONING", "high")
        orig = cli_mod.CLI_CONFIG
        cli_mod.CLI_CONFIG = {
            "agent": {},
            "display": {},
            "model": {},
            "compression": {},
            "terminal": {},
        }
        try:
            from cli import HermesCLI

            h = HermesCLI()
            assert h.reasoning_config == {"enabled": True, "effort": "high"}
        finally:
            cli_mod.CLI_CONFIG = orig


class TestOneshotReasoningEffort:
    def test_run_agent_plumbs_reasoning_config(self, monkeypatch):
        import hermes_cli.oneshot as oneshot_mod
        from hermes_cli.oneshot import _run_agent

        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured["reasoning_config"] = kwargs.get("reasoning_config")

            def run_conversation(self, prompt):
                return {"final_response": "ok", "failed": False}

        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"model": {"default": "test-model"}},
        )
        monkeypatch.setattr(
            oneshot_mod, "_normalize_toolsets", lambda t: None
        )
        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools", lambda cfg, p: []
        )
        monkeypatch.setattr(
            oneshot_mod, "_create_session_db_for_oneshot", lambda: None
        )
        monkeypatch.setattr(
            oneshot_mod, "get_fallback_chain", lambda cfg: {}
        )
        monkeypatch.setattr(
            oneshot_mod, "declare_stateless_channel", lambda: None
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda **kw: {
                "api_key": "k",
                "base_url": "b",
                "provider": "p",
                "requested_provider": "p",
                "api_mode": "chat",
                "credential_pool": None,
            },
        )
        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

        resp, result = _run_agent(
            "hello", model="test-model", reasoning_effort="high"
        )
        assert resp == "ok"
        assert captured["reasoning_config"] == {
            "enabled": True,
            "effort": "high",
        }


class TestTuiGatewayReasoningEnv:
    def test_load_reasoning_config_env_override(self, monkeypatch):
        from tui_gateway.server import _load_reasoning_config

        monkeypatch.setenv("HERMES_TUI_REASONING", "xhigh")
        monkeypatch.setattr(
            "tui_gateway.server._load_cfg",
            lambda: {"agent": {"reasoning_effort": "low"}},
        )
        assert _load_reasoning_config() == {"enabled": True, "effort": "xhigh"}

    def test_load_reasoning_config_falls_back_to_cfg(self, monkeypatch):
        from tui_gateway.server import _load_reasoning_config

        monkeypatch.delenv("HERMES_TUI_REASONING", raising=False)
        monkeypatch.setattr(
            "tui_gateway.server._load_cfg",
            lambda: {"agent": {"reasoning_effort": "low"}},
        )
        assert _load_reasoning_config("") == {
            "enabled": True,
            "effort": "low",
        }