"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt, build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(
        cwd=None, skip_soul=False, context_length=None,
        allow_install_tree_fallback=False,
    ):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _prompt_parts(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)


def _init_code_repo(path):
    """A git repo that actually holds code — the coding posture requires a source
    file (or manifest), not a bare ``.git`` (a prose/notes repo stays general)."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "main.py").write_text("print('hi')\n")


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        parts = _prompt_parts(agent)
        assert "coding agent" in parts["stable"]
        assert "Workspace" in parts["context"]

    def test_absent_when_off(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


def test_build_system_prompt_records_stable_prefix():
    agent = _make_agent()
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value="context"),
    ):
        prompt = build_system_prompt(agent)

    assert prompt.startswith(agent._cached_system_prompt_static)
    assert prompt[len(agent._cached_system_prompt_static):].startswith("\n\ncontext")


def test_coding_prompt_preserves_legacy_workspace_order(monkeypatch):
    """The cache split must not reorder the stored coding prompt."""
    import agent.system_prompt as system_prompt

    agent = _make_agent(
        valid_tool_names=["read_file"],
        _parallel_tool_call_guidance=False,
    )
    monkeypatch.setattr(system_prompt, "DEFAULT_AGENT_IDENTITY", "IDENTITY")
    monkeypatch.setattr(system_prompt, "HERMES_AGENT_HELP_GUIDANCE", "HELP")
    monkeypatch.setattr(system_prompt, "STEER_CHANNEL_NOTE", "STEER")
    monkeypatch.setattr(system_prompt, "get_hermes_home", lambda: Path("/hermes"))

    expected_profile = (
        "Active Hermes profile: default. Other profiles (if any) live "
        "under /hermes/profiles/<name>/. Each profile has its own skills/, "
        "plugins/, cron/, and memories/ that affect a different session than "
        "this one. Do not modify another profile's skills/plugins/cron/memories "
        "unless the user explicitly directs you to."
    )
    expected = "\n\n".join((
        "IDENTITY",
        "HELP",
        "STEER",
        "CODING_STABLE",
        "WORKSPACE",
        "Operator instructions (from config):\nOPERATOR",
        expected_profile,
        "SYSTEM_MESSAGE",
        "CONTEXT_FILES",
        "Conversation started: Friday, January 02, 2026",
    ))

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value="CONTEXT_FILES"),
        patch(
            "agent.coding_context.coding_system_prompt_parts",
            return_value=(
                ["CODING_STABLE"],
                ["WORKSPACE"],
                ["Operator instructions (from config):\nOPERATOR"],
            ),
        ),
        patch("agent.file_safety._resolve_active_profile_name", return_value="default"),
        patch("hermes_time.now", return_value=datetime(2026, 1, 2)),
    ):
        prompt = build_system_prompt(agent, system_message="SYSTEM_MESSAGE")

    assert prompt == expected
    assert agent._cached_system_prompt_static == "\n\n".join(expected.split("\n\n")[:4])


class TestTelegramRichMessagesHint:
    """Verify that TELEGRAM_RICH_MESSAGES_HINT is conditionally included."""

    def test_base_hint_without_rich_messages(self, monkeypatch):
        """When rich_messages is False (default), only the base hint is used."""
        agent = _make_agent(platform="telegram")
        # Mock config to return rich_messages: false (default)
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "platforms": {"telegram": {"extra": {"rich_messages": False}}}
            }
            stable = _stable_prompt(agent)
        # Base hint should be present
        assert "Standard Markdown is automatically converted" in stable
        # Rich-messages extension should NOT be present
        assert "lean into it" not in stable
        assert "task lists" not in stable

    def test_rich_hint_with_rich_messages_enabled(self, monkeypatch):
        """When rich_messages is True, the rich-messages extension is appended."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "platforms": {"telegram": {"extra": {"rich_messages": True}}}
            }
            stable = _stable_prompt(agent)
        # Base hint should be present
        assert "Standard Markdown is automatically converted" in stable
        # Rich-messages extension should be present
        assert "lean into it" in stable
        assert "task lists" in stable
        assert "math/formulas" in stable

    def test_base_hint_without_config(self, monkeypatch):
        """When config has no telegram section, only base hint is used."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {}
            stable = _stable_prompt(agent)
        assert "Standard Markdown is automatically converted" in stable
        assert "lean into it" not in stable


class TestCronDeliveryInvariants:
    """CRON_DELIVERY_INVARIANTS ([SILENT] suppression + a no-self-delivery
    reminder) must reach the system prompt for every cron run, and must
    survive a platform_hints.cron override — unlike PLATFORM_HINTS["cron"]
    (the descriptive hint), it is NOT subject to the replace/append override
    resolved by _resolve_platform_hint. The [SILENT] marker is the
    load-bearing part (cron/scheduler.py::run_job detects it to suppress
    delivery). See agent/prompt_builder.py's CRON_DELIVERY_INVARIANTS
    docstring for the full rationale."""

    def test_present_for_cron_platform(self):
        stable = _stable_prompt(_make_agent(platform="cron"))
        # Base descriptive hint (PLATFORM_HINTS["cron"], via _effective_hint)
        # must still land alongside the invariants — this guards against a
        # regression where _effective_hint silently stops resolving while
        # CRON_DELIVERY_INVARIANTS alone keeps this test green.
        assert "There is no user present" in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable

    def test_absent_for_non_cron_platform(self):
        stable = _stable_prompt(_make_agent(platform="cli"))
        assert "[SILENT]" not in stable

    def test_survives_platform_hints_replace_override(self):
        """Regression guard: an admin using the documented
        platform_hints.cron.replace override to customize the descriptive
        cron hint's wording must never accidentally drop the [SILENT]
        suppression contract cron/scheduler.py::run_job relies on (the
        no-self-delivery reminder rides along with it)."""
        agent = _make_agent(
            platform="cron",
            _platform_hint_overrides={
                "cron": {"replace": "Custom cron hint with no mention of delivery mechanics."}
            },
        )
        stable = _stable_prompt(agent)
        assert "Custom cron hint with no mention of delivery mechanics." in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable

    def test_survives_platform_hints_append_override(self):
        agent = _make_agent(
            platform="cron",
            _platform_hint_overrides={"cron": {"append": "Extra operator note."}},
        )
        stable = _stable_prompt(agent)
        assert "Extra operator note." in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable
