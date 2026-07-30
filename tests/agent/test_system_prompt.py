"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


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


class TestUniversalProfilePolicy:
    def test_named_profile_injects_root_policy_and_keeps_cwd_agents_local(
        self, tmp_path, monkeypatch
    ):
        """Exercise the real profile, policy, and cwd context-file loaders."""
        root = tmp_path / "hermes"
        profile = root / "profiles" / "recall-specialist"
        project = tmp_path / "project"
        profile.mkdir(parents=True)
        project.mkdir()
        (root / "AGENTS.md").write_text(
            "# SoLo Briefs / SoLoRecall Agent Hub\n\n"
            "Universal reporting policy.\n\n"
            "# SoLoRecall Deliverable Persistence Rule\n\n"
            "Persist deliverables in SoLoRecall.\n",
            encoding="utf-8",
        )
        (project / "AGENTS.md").write_text(
            "CWD_LOCAL_AGENTS_POLICY", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("TERMINAL_CWD", str(project))

        parts = build_system_prompt_parts(_make_agent())

        assert "SoLo Briefs / SoLoRecall Agent Hub" in parts["stable"]
        assert "SoLoRecall Deliverable Persistence Rule" in parts["stable"]
        assert "CWD_LOCAL_AGENTS_POLICY" not in parts["stable"]
        assert "CWD_LOCAL_AGENTS_POLICY" in parts["context"]
        assert "SoLo Briefs / SoLoRecall Agent Hub" not in parts["context"]

    def test_named_profile_policy_is_stable_and_not_project_context(self):
        agent = _make_agent()
        marker = "UNIVERSAL_SOLORECALL_POLICY_MARKER"
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
            patch("run_agent.load_universal_policy_md", return_value=marker),
        ):
            parts = build_system_prompt_parts(agent)

        assert marker in parts["stable"]
        assert "Universal Profile Policy" in parts["stable"]
        assert marker not in parts["context"]

    def test_policy_is_not_injected_twice_when_root_cwd_loaded_it(self):
        agent = _make_agent()
        marker = "UNIVERSAL_SOLORECALL_POLICY_MARKER"
        root_context = f"# Project Context\n\n## AGENTS.md\n\n{marker}"
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=root_context),
            patch("run_agent.load_universal_policy_md", return_value=marker),
        ):
            parts = build_system_prompt_parts(agent)

        assert marker not in parts["stable"]
        assert parts["context"].count(marker) == 1

    def test_ignore_rules_skips_universal_policy(self):
        agent = _make_agent(skip_context_files=True)
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.load_universal_policy_md", return_value="policy") as policy_loader,
        ):
            parts = build_system_prompt_parts(agent)

        policy_loader.assert_not_called()
        assert "policy" not in parts["stable"]


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
        patch("run_agent.load_universal_policy_md", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


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
        stable = _stable_prompt(agent)
        assert "coding agent" in stable
        assert "Workspace" in stable

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
