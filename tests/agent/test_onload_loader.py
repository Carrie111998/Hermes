"""Tests for onload-script loader — trust gate, path containment, env sandbox."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, PropertyMock

import pytest

from agent.skill_preprocessing import (
    _is_onload_authorized,
    _build_onload_env,
    _validate_onload_path,
    run_onload_script,
)


# =========================================================================
# Trust gate (_is_onload_authorized)
# =========================================================================


class TestTrustGate:
    def test_default_false(self):
        """Neither inline_shell nor onload_enabled → blocked."""
        assert _is_onload_authorized({"inline_shell": False}) is False
        assert _is_onload_authorized({}) is False
        assert _is_onload_authorized(None) is False

    def test_inline_shell_enables(self):
        """Gate opens when inline_shell is true."""
        assert _is_onload_authorized({"inline_shell": True}) is True
        assert _is_onload_authorized({"inline_shell": True, "onload_enabled": False}) is True

    def test_onload_enabled_enables(self):
        """Gate opens when onload_enabled is true."""
        assert _is_onload_authorized({"onload_enabled": True}) is True
        assert _is_onload_authorized({"inline_shell": False, "onload_enabled": True}) is True

    def test_both_false_still_blocked(self):
        """Neither gate opens → blocked."""
        assert _is_onload_authorized({"inline_shell": False, "onload_enabled": False}) is False


# =========================================================================
# Environment allowlist (_build_onload_env)
# =========================================================================


class TestBuildOnloadEnv:
    def test_strips_secrets(self, monkeypatch):
        """Child env must NOT inherit API keys / tokens from parent."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-12345")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wxyz")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes")
        child = _build_onload_env()
        assert "OPENAI_API_KEY" not in child
        assert "AWS_SECRET_ACCESS_KEY" not in child
        assert child.get("PATH") == "/usr/bin"
        assert child.get("HERMES_HOME") == "/tmp/hermes"

    def test_allowlisted_vars_present(self, monkeypatch):
        """Allowlisted standard vars are carried through."""
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        monkeypatch.setenv("HERMES_HOME", "/home/user/.hermes")
        monkeypatch.setenv("HERMES_MODEL", "gpt-4")
        child = _build_onload_env()
        assert child.get("PATH") == "/bin:/usr/bin"
        assert child.get("HERMES_HOME") == "/home/user/.hermes"
        assert child.get("HERMES_MODEL") == "gpt-4"

    def test_windows_vars_included_when_on_windows(self):
        """SYSTEMROOT, TEMP, TMP are included on Windows."""
        from hermes_cli._subprocess_compat import IS_WINDOWS
        if IS_WINDOWS:
            child = _build_onload_env()
            for key in ("SYSTEMROOT", "TEMP", "TMP"):
                assert key in child, f"{key} should be in allowlist on Windows"

    def test_extra_env_merged(self):
        """extra_env items are merged into the child env."""
        child = _build_onload_env({"HERMES_MODEL": "claude", "HERMES_PROVIDER": "anthropic"})
        assert child.get("HERMES_MODEL") == "claude"
        assert child.get("HERMES_PROVIDER") == "anthropic"

    def test_no_secrets_via_extra_env_key_leak(self, monkeypatch):
        """extra_env only passes what's explicitly given — not ambient secrets."""
        monkeypatch.setenv("DANGEROUS_SECRET", "exposed!")
        child = _build_onload_env({"HERMES_MODEL": "test"})
        assert "DANGEROUS_SECRET" not in child
        assert child.get("HERMES_MODEL") == "test"


# =========================================================================
# Path containment (_validate_onload_path)
# =========================================================================


class TestValidateOnloadPath:
    def test_relative_path_resolves(self, tmp_path):
        """A normal relative path inside the skill dir is accepted."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.sh"
        script.write_text("echo INJECT")
        result = _validate_onload_path("onload.sh", skill_dir)
        assert result is not None
        assert result == script.resolve()

    def test_absolute_path_rejected(self, tmp_path):
        """Absolute script paths are rejected immediately."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        result = _validate_onload_path("/etc/passwd", skill_dir)
        assert result is None

    def test_dotdot_traversal_rejected(self, tmp_path):
        """../ traversal that escapes the skill root is rejected."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir(parents=True)
        # Create a file outside the skill dir
        outside = tmp_path / "outside.sh"
        outside.write_text("echo pwned")
        result = _validate_onload_path("../outside.sh", skill_dir)
        assert result is None

    def test_deep_dotdot_traversal_rejected(self, tmp_path):
        """Multiple levels of ../ are rejected."""
        skill_dir = tmp_path / "a" / "b" / "c"
        skill_dir.mkdir(parents=True)
        result = _validate_onload_path("../../../../etc/passwd", skill_dir)
        assert result is None

    def test_symlink_escape_rejected(self, tmp_path):
        """A symlink pointing outside the skill dir is rejected."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir(parents=True)
        outside = tmp_path / "outside.sh"
        outside.write_text("echo pwned")
        link = skill_dir / "escape.sh"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("Filesystem does not support symlinks")
        result = _validate_onload_path("escape.sh", skill_dir)
        assert result is None

    def test_valid_relative_inside_subdir(self, tmp_path):
        """A relative script inside a subdirectory of the skill works."""
        skill_dir = tmp_path / "my-skill"
        sub = skill_dir / "scripts"
        sub.mkdir(parents=True)
        script = sub / "load.py"
        script.write_text("print('INJECT')")
        result = _validate_onload_path("scripts/load.py", skill_dir)
        assert result is not None
        assert result == script.resolve()


# =========================================================================
# run_onload_script integration
# =========================================================================


class TestRunOnloadScript:
    def test_blocked_when_unauthorized(self, tmp_path):
        """Script is NOT called when trust gate is not enabled."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.sh"
        script.write_text("echo INJECT")
        output = run_onload_script(
            "onload.sh", skill_dir,
            skills_cfg={"inline_shell": False, "onload_enabled": False},
        )
        assert output == ""

    def test_called_when_authorized_via_inline_shell(self, tmp_path):
        """Script IS called when inline_shell is true."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.py"
        script.write_text("print('INJECT')")
        output = run_onload_script(
            "onload.py", skill_dir,
            skills_cfg={"inline_shell": True},
        )
        assert output.strip().upper() == "INJECT"

    def test_called_when_authorized_via_onload_enabled(self, tmp_path):
        """Script IS called when onload_enabled is true."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.py"
        script.write_text("print('INJECT')")
        output = run_onload_script(
            "onload.py", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert output.strip().upper() == "INJECT"

    def test_child_env_no_parent_secrets(self, tmp_path, monkeypatch):
        """Child process does NOT inherit parent env secrets (sentinel test)."""
        monkeypatch.setenv("MY_TEST_SECRET", "super-secret-value")
        monkeypatch.setenv("HERMES_HOME", "/tmp/hermes")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.py"
        # Script reads the env and prints it — we'll see it's empty
        script.write_text(
            'import os; print("SECRET=[%s]" % os.environ.get("MY_TEST_SECRET", "unset"))'
        )
        output = run_onload_script(
            "onload.py", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert "MY_TEST_SECRET" not in output
        assert "SECRET=[unset]" in output

    def test_dotdot_traversal_blocked_at_run(self, tmp_path):
        """run_onload_script returns empty for ../ traversal attempts."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        output = run_onload_script(
            "../outside.sh", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert output == ""

    def test_absolute_path_blocked_at_run(self, tmp_path):
        """run_onload_script returns empty for absolute paths."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        output = run_onload_script(
            "/tmp/nonexistent.sh", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert output == ""

    def test_inject_python_script(self, tmp_path):
        """A .py script returning INJECT works when authorized."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.py"
        script.write_text("print('INJECT')")
        output = run_onload_script(
            "onload.py", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert output.strip().upper() == "INJECT"

    def test_missing_script_returns_empty(self, tmp_path):
        """Non-existent script path returns empty string."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        output = run_onload_script(
            "nonexistent.sh", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert output == ""

    def test_template_vars_available(self, tmp_path, monkeypatch):
        """The onload script can read HERMES_HOME from the allowlisted env."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        script = skill_dir / "onload.py"
        script.write_text(
            'import os; print("HERMES_HOME=%s" % os.environ.get("HERMES_HOME", ""))'
        )
        output = run_onload_script(
            "onload.py", skill_dir,
            skills_cfg={"onload_enabled": True},
        )
        assert str(tmp_path / "hermes_home") in output


# =========================================================================
# get_onload_entries contract
# =========================================================================


class TestGetOnloadEntries:
    def test_build_skills_system_prompt_returns_str(self):
        """The public function contract is preserved: returns str."""
        from agent.prompt_builder import build_skills_system_prompt, get_onload_entries

        # Use a non-existent skills dir so the function returns empty quickly
        with patch("agent.prompt_builder.get_skills_dir") as mock_gsd, \
             patch("agent.prompt_builder.get_all_skills_dirs") as mock_gasd, \
             patch("agent.skill_utils.get_project_skills_dirs") as mock_gpsd:
            mock_gsd.return_value = Path("/nonexistent-skills-dir-for-test")
            mock_gasd.return_value = [Path("/nonexistent-skills-dir-for-test")]
            mock_gpsd.return_value = []

            result = build_skills_system_prompt()
            assert isinstance(result, str)

    def test_get_onload_entries_returns_list(self):
        """get_onload_entries returns a list of dicts."""
        from agent.prompt_builder import get_onload_entries

        with patch("agent.prompt_builder.get_skills_dir") as mock_gsd, \
             patch("agent.prompt_builder.get_all_skills_dirs") as mock_gasd, \
             patch("agent.skill_utils.get_project_skills_dirs") as mock_gpsd:
            mock_gsd.return_value = Path("/nonexistent-skills-dir-for-test")
            mock_gasd.return_value = [Path("/nonexistent-skills-dir-for-test")]
            mock_gpsd.return_value = []

            entries = get_onload_entries()
            assert isinstance(entries, list)

    def test_onload_entry_shape(self, tmp_path):
        """Onload entries have the expected keys."""
        from agent.prompt_builder import get_onload_entries

        # Create a minimal skill with onload
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        cat_dir = skills_dir / "test-cat"
        cat_dir.mkdir()
        skill_md = cat_dir / "SKILL.md"
        skill_md.write_text(
            "---\n"
            "name: test-onload-skill\n"
            "onload: scripts/init.sh\n"
            "---\n"
            "# Test Skill\n"
            "Body content.\n"
        )
        scripts_dir = cat_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "init.sh").write_text("echo INJECT")

        with patch("agent.prompt_builder.get_skills_dir") as mock_gsd, \
             patch("agent.prompt_builder.get_all_skills_dirs") as mock_gasd, \
             patch("agent.skill_utils.get_project_skills_dirs") as mock_gpsd:
            mock_gsd.return_value = skills_dir
            mock_gasd.return_value = [skills_dir]
            mock_gpsd.return_value = []

            entries = get_onload_entries()
            assert len(entries) >= 1
            entry = entries[0]
            assert "onload" in entry
            assert "skill_dir" in entry
            assert "skill_md_path" in entry
            assert "skill_name" in entry
            assert entry["onload"] == "scripts/init.sh"

    def test_skill_without_onload_not_included(self, tmp_path):
        """Skills without onload: frontmatter are NOT in onload entries."""
        from agent.prompt_builder import get_onload_entries

        skills_dir = tmp_path / "skills2"
        skills_dir.mkdir(parents=True)
        cat_dir = skills_dir / "other-cat"
        cat_dir.mkdir()
        skill_md = cat_dir / "SKILL.md"
        skill_md.write_text(
            "---\n"
            "name: no-onload-skill\n"
            "---\n"
            "# No Onload\n"
        )

        with patch("agent.prompt_builder.get_skills_dir") as mock_gsd, \
             patch("agent.prompt_builder.get_all_skills_dirs") as mock_gasd, \
             patch("agent.skill_utils.get_project_skills_dirs") as mock_gpsd:
            mock_gsd.return_value = skills_dir
            mock_gasd.return_value = [skills_dir]
            mock_gpsd.return_value = []

            entries = get_onload_entries()
            onload_names = [e.get("skill_name") for e in entries]
            assert "no-onload-skill" not in onload_names


# =========================================================================
# HERMES_HOME template substitution in skill_preprocessing
# =========================================================================


class TestHermesHomeTemplateSubstitution:
    def test_hermes_home_substituted(self, monkeypatch):
        """${HERMES_HOME} in skill content is replaced."""
        from agent.skill_preprocessing import substitute_template_vars

        monkeypatch.setenv("HERMES_HOME", "/custom/hermes/home")
        content = "Path: ${HERMES_HOME}/skills/my-skill"
        result = substitute_template_vars(content, skill_dir=Path("/tmp"), session_id=None)
        assert "/custom/hermes/home/skills/my-skill" in result

    def test_hermes_home_unset_unchanged(self, monkeypatch):
        """${HERMES_HOME} stays as-is when HERMES_HOME is unset."""
        from agent.skill_preprocessing import substitute_template_vars

        monkeypatch.delenv("HERMES_HOME", raising=False)
        content = "Path: ${HERMES_HOME}/data"
        result = substitute_template_vars(content, skill_dir=Path("/tmp"), session_id=None)
        assert "${HERMES_HOME}" in result