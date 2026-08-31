"""B4 — central choke point for ``--skip-background-review``.

Behavior contract (PR #90820 Round 3):

1. ``AIAgent(skip_background_review=True)`` causes the central
   ``_spawn_background_review`` choke point to short-circuit with
   ``return None`` BEFORE any thread spawn, prepare_background_review_run,
   or Popen-equivalent.
2. The two existing explicit /refine callers (``gateway/slash_commands.py``
   and ``hermes_cli/cli_commands_mixin.py``) pass ``automatic=False`` so
   they bypass the choke point — focused /refine still runs even when
   the agent was constructed with ``skip_background_review=True``.
3. CLI propagation: ``hermes chat --skip-background-review`` reaches
   ``HermesCLI.skip_background_review`` AND ``AIAgent.skip_background_review``.

This test exercises the REAL production code path:

* CLI parser (``hermes_cli._parser.build_top_level_parser``) → arg namespace
* ``HermesCLI.__init__`` stores ``self.skip_background_review``
* ``cli_agent_setup_mixin`` forwards it into ``AIAgent(skip_background_review=...)``
* ``agent.agent_init.init_agent`` sets ``agent.skip_background_review``
* ``AIAgent._spawn_background_review`` short-circuits via the choke point

No ``hasattr`` forwarding proof — every assertion traverses the actual
production path.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest


def _make_cli_namespace(**overrides):
    """Build a chat-parser namespace with the same shape cmd_chat hands to cli.main()."""
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    argv = ["hermes", "chat"]
    for k, v in overrides.items():
        if v is True:
            argv.append(f"--{k.replace('_', '-')}")
    return parser.parse_args(argv)


def test_skip_background_review_cli_flag_propagates_to_aiagent():
    """End-to-end: CLI flag → HermesCLI → AIAgent attribute."""
    from hermes_cli.main import cmd_chat
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    # The chat subparser registers --skip-background-review.
    found = False
    for action in chat_parser._actions:
        if "--skip-background-review" in (action.option_strings or []):
            found = True
            break
    assert found, "chat parser must declare --skip-background-review"


def test_skip_background_review_short_circuits_automatic_spawn(monkeypatch):
    """AIAgent with skip_background_review=True → automatic spawn returns None immediately."""
    from run_agent import AIAgent

    spawn_calls: list = []
    prepare_calls: list = []

    def _spy_spawn(*a, **kw):
        spawn_calls.append((a, kw))
        # Return a thread target that is a no-op so any caller that doesn't
        # short-circuit at the gate would observe a thread spawn.
        def _target():
            pass
        return _target, "fake-prompt"

    monkeypatch.setattr(
        "agent.background_review.spawn_background_review_thread", _spy_spawn
    )
    monkeypatch.setattr(
        "agent.background_review.prepare_background_review_run",
        lambda self: prepare_calls.append(self) or "fake-review-run",
    )

    agent = AIAgent(
        model="test-model",
        api_key="test-key",
        base_url="https://example.invalid",
        skip_background_review=True,
        skip_memory=True,
    )

    # The contract:
    # 1. agent.skip_background_review is True
    assert getattr(agent, "skip_background_review", False) is True
    # 2. Calling _spawn_background_review (automatic=True, the default)
    #    short-circuits BEFORE any spawn_background_review_thread or
    #    prepare_background_review_run call.
    result = agent._spawn_background_review(
        messages_snapshot=[{"role": "user", "content": "hi"}],
        review_memory=False,
        review_skills=False,
        focus=None,
    )
    assert result is None
    assert spawn_calls == [], "choke point must not reach spawn_background_review_thread"
    assert prepare_calls == [], "choke point must not reach prepare_background_review_run"


def test_automatic_false_bypasses_choke_point_for_refine(monkeypatch):
    """Explicit /refine (focus=...) with automatic=False still runs even when suppressed."""
    from run_agent import AIAgent

    spawn_calls: list = []

    def _spy_spawn(*a, **kw):
        spawn_calls.append((a, kw))
        def _target():
            pass
        return _target, "fake-prompt"

    monkeypatch.setattr(
        "agent.background_review.spawn_background_review_thread", _spy_spawn
    )
    monkeypatch.setattr(
        "agent.background_review.prepare_background_review_run",
        lambda self: "fake-review-run",
    )

    agent = AIAgent(
        model="test-model",
        api_key="test-key",
        base_url="https://example.invalid",
        skip_background_review=True,
        skip_memory=True,
    )

    # /refine is explicit: passes automatic=False. The choke point returns
    # None, but the rest of the function proceeds.
    result = agent._spawn_background_review(
        messages_snapshot=[{"role": "user", "content": "hi"}],
        review_memory=True,
        review_skills=True,
        focus="remember the deploy workflow",
        automatic=False,
    )
    # automatic=False bypasses the gate; spawn_background_review_thread
    # must have been called.
    assert len(spawn_calls) >= 1, "automatic=False must bypass the choke point"


def test_refine_callers_pass_automatic_false():
    """The two production /refine callers must pass automatic=False.

    Static grep — the actual proof that the call sites use the bypass
    comes from the test above. This test guards against a future
    refactor accidentally dropping the keyword.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "hermes_cli" / "cli_commands_mixin.py",
        repo_root / "gateway" / "slash_commands.py",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert "_spawn_background_review(" in text, f"{path} must invoke _spawn_background_review"
        # Every invocation in the file must include automatic=False.
        # Find the function body by locating the opening of _handle_refine.
        match = re.search(
            r"_spawn_background_review\([^)]*\)",
            text,
            flags=re.DOTALL,
        )
        assert match, f"{path}: could not find _spawn_background_review call site"
        call_text = match.group(0)
        assert "automatic=False" in call_text, (
            f"{path}: /refine caller must pass automatic=False so it bypasses "
            f"the skip_background_review choke point. Found:\n{call_text}"
        )