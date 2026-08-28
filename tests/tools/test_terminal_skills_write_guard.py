"""terminal_tool wiring tests for the un-ledgered skills-write guard (#96962)."""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

import tools.self_repo_guard as self_repo_guard
import tools.skills_write_guard as skills_write_guard


def _make_env_config(**overrides):
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    config.update(overrides)
    return config


@pytest.fixture
def skills_root(tmp_path):
    root = tmp_path / "skills"
    (root / "narrow-skill" / "references").mkdir(parents=True)
    (root / "umbrella").mkdir()
    return root.resolve()


def _run(command, config, monkeypatch, skills_root, guard_on=True, **kwargs):
    from tools.terminal_tool import terminal_tool

    # Keep the neighbouring self-repo guard out of the way.
    monkeypatch.setattr(self_repo_guard, "guard_active", lambda: False)
    monkeypatch.setattr(skills_write_guard, "guard_active", lambda: guard_on)
    monkeypatch.setattr(
        skills_write_guard, "_protected_roots", lambda: [skills_root]
    )

    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    mock_env.cwd = config["cwd"]

    with ExitStack() as stack:
        stack.enter_context(
            patch("tools.terminal_tool._get_env_config", return_value=config)
        )
        stack.enter_context(patch("tools.terminal_tool._start_cleanup_thread"))
        stack.enter_context(
            patch("tools.terminal_tool._active_environments", {"default": mock_env})
        )
        stack.enter_context(patch("tools.terminal_tool._last_activity", {"default": 0}))
        stack.enter_context(patch("tools.terminal_tool._session_cwd", {}))
        stack.enter_context(
            patch(
                "tools.terminal_tool._check_all_guards", return_value={"approved": True}
            )
        )
        result = json.loads(terminal_tool(command=command, **kwargs))
    return result, mock_env


class TestSkillsWriteGuardWiring:
    def test_blocks_terminal_rehome_of_support_files(
        self, skills_root, monkeypatch, tmp_path
    ):
        """The exact #96962 shape: mv references/ into the umbrella."""
        config = _make_env_config(cwd=str(tmp_path))
        command = (
            f"mkdir -p {skills_root}/umbrella/references && "
            f"mv {skills_root}/narrow-skill/references/extra.md "
            f"{skills_root}/umbrella/references/extra.md"
        )
        result, env = _run(command, config, monkeypatch, skills_root)

        assert result["status"] == "blocked"
        assert "96962" in result["error"]
        env.execute.assert_not_called()

    def test_force_cannot_bypass(self, skills_root, monkeypatch, tmp_path):
        """The curator runs headless — force is not a user consenting."""
        config = _make_env_config(cwd=str(tmp_path))
        result, env = _run(
            f"rm -rf {skills_root}/narrow-skill",
            config,
            monkeypatch,
            skills_root,
            force=True,
        )
        assert result["status"] == "blocked"
        env.execute.assert_not_called()

    def test_workdir_relative_write_is_blocked(
        self, skills_root, monkeypatch, tmp_path
    ):
        config = _make_env_config(cwd=str(tmp_path))
        result, env = _run(
            "rm -rf narrow-skill/references",
            config,
            monkeypatch,
            skills_root,
            workdir=str(skills_root),
        )
        assert result["status"] == "blocked"
        env.execute.assert_not_called()

    def test_reads_pass_through(self, skills_root, monkeypatch, tmp_path):
        config = _make_env_config(cwd=str(tmp_path))
        result, env = _run(
            f"cat {skills_root}/narrow-skill/SKILL.md",
            config,
            monkeypatch,
            skills_root,
        )
        assert result.get("status") != "blocked"
        env.execute.assert_called_once()

    def test_writes_outside_the_tree_pass_through(
        self, skills_root, monkeypatch, tmp_path
    ):
        config = _make_env_config(cwd=str(tmp_path))
        result, env = _run(
            f"rm -rf {tmp_path}/scratch", config, monkeypatch, skills_root
        )
        assert result.get("status") != "blocked"
        env.execute.assert_called_once()

    def test_foreground_origin_passes_through(
        self, skills_root, monkeypatch, tmp_path
    ):
        """A user-directed shell edit of their own skills tree still works."""
        config = _make_env_config(cwd=str(tmp_path))
        result, env = _run(
            f"rm -rf {skills_root}/narrow-skill",
            config,
            monkeypatch,
            skills_root,
            guard_on=False,
        )
        assert result.get("status") != "blocked"
        env.execute.assert_called_once()

    def test_guard_active_tracks_write_origin(self):
        from tools.skill_provenance import (
            BACKGROUND_REVIEW,
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert skills_write_guard.guard_active() is True
        finally:
            reset_current_write_origin(token)
        assert skills_write_guard.guard_active() is False
