"""Test that skill_view registers required env vars in the passthrough registry."""

import json
from contextlib import contextmanager

import pytest

import tools.env_passthrough as _ep_mod
import tools.skills_tool as skills_tool_module
from tools.env_passthrough import clear_env_passthrough, is_env_passthrough


@contextmanager
def _secret_capture(callback):
    token = skills_tool_module.bind_secret_capture_callback(callback)
    try:
        yield
    finally:
        skills_tool_module.reset_secret_capture_callback(token)


@pytest.fixture(autouse=True)
def _clean_passthrough():
    clear_env_passthrough()
    _ep_mod._config_passthrough = None
    yield
    clear_env_passthrough()
    _ep_mod._config_passthrough = None


def _create_skill(tmp_path, name, frontmatter_extra=""):
    """Create a minimal skill directory with SKILL.md."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: Test skill\n"
        f"{frontmatter_extra}"
        f"---\n\n"
        f"# {name}\n\n"
        f"Test content.\n"
    )
    return skill_dir


class TestSkillViewRegistersPassthrough:
    def test_available_env_vars_registered(self, tmp_path, monkeypatch):
        """When a skill declares required_environment_variables and the var IS set,
        it should be registered in the passthrough."""
        _create_skill(
            tmp_path,
            "test-skill",
            frontmatter_extra=(
                "required_environment_variables:\n"
                "  - name: TENOR_API_KEY\n"
                "    prompt: Enter your Tenor API key\n"
            ),
        )
        monkeypatch.setattr(
            "tools.skills_tool.SKILLS_DIR", tmp_path
        )
        # Set the env var so it's "available"
        monkeypatch.setenv("TENOR_API_KEY", "test-value-123")

        # Patch the secret capture callback to not prompt
        with _secret_capture(None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="test-skill"))

        assert result["success"] is True
        assert is_env_passthrough("TENOR_API_KEY")

    def test_remote_backend_persisted_env_vars_registered(self, tmp_path, monkeypatch):
        """Remote-backed skills still register locally available env vars."""
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        _create_skill(
            tmp_path,
            "test-skill",
            frontmatter_extra=(
                "required_environment_variables:\n"
                "  - name: TENOR_API_KEY\n"
                "    prompt: Enter your Tenor API key\n"
            ),
        )
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)

        from hermes_cli.config import save_env_value

        save_env_value("TENOR_API_KEY", "persisted-value-123")
        monkeypatch.delenv("TENOR_API_KEY", raising=False)

        with _secret_capture(None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="test-skill"))

        assert result["success"] is True
        assert result["setup_needed"] is False
        assert result["missing_required_environment_variables"] == []
        assert is_env_passthrough("TENOR_API_KEY")

    def test_missing_env_vars_not_registered(self, tmp_path, monkeypatch):
        """When a skill declares required_environment_variables but the var is NOT set,
        it should NOT be registered in the passthrough."""
        _create_skill(
            tmp_path,
            "test-skill",
            frontmatter_extra=(
                "required_environment_variables:\n"
                "  - name: NONEXISTENT_SKILL_KEY_XYZ\n"
                "    prompt: Enter your key\n"
            ),
        )
        monkeypatch.setattr(
            "tools.skills_tool.SKILLS_DIR", tmp_path
        )
        monkeypatch.delenv("NONEXISTENT_SKILL_KEY_XYZ", raising=False)

        with _secret_capture(None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="test-skill"))

        assert result["success"] is True
        assert not is_env_passthrough("NONEXISTENT_SKILL_KEY_XYZ")

    def test_no_env_vars_skill_no_registration(self, tmp_path, monkeypatch):
        """Skills without required_environment_variables shouldn't register anything."""
        _create_skill(tmp_path, "simple-skill")
        monkeypatch.setattr(
            "tools.skills_tool.SKILLS_DIR", tmp_path
        )

        with _secret_capture(None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="simple-skill"))

        assert result["success"] is True
        from tools.env_passthrough import get_all_passthrough
        assert len(get_all_passthrough()) == 0
