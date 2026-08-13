"""Tests for agent/python_skills.py — Python-backed skill loader."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.python_skills import (
    PythonSkillInfo,
    build_python_skill_message,
    build_python_skills_index,
    get_python_skill,
    get_python_skills,
    reload_python_skills,
    scan_python_skills,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_python_skill(
    skills_dir: Path,
    name: str,
    description: str = "Test Python skill",
    functions: dict | None = None,
    instructions: str = "",
) -> Path:
    """Create a minimal Python-backed skill directory."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Build function code using string concatenation to avoid triple-quote
    # collision inside f-strings
    func_code = ""
    if functions:
        for func_name, func_body in functions.items():
            func_code += (
                f'\n@python_skill\n'
                f'async def {func_name}(**kwargs) -> dict:\n'
                f'    """{func_body}"""\n'
                f'    return {{"func": "{func_name}"}}\n'
                f'\n'
            )

    # Build _skill.py
    skill_py = skill_dir / "_skill.py"
    skill_py.write_text(
        'from agent.python_skills import python_skill\n'
        '\n'
        'SKILL_INFO = {\n'
        f'    "name": "{name}",\n'
        f'    "description": "{description}",\n'
        '    "version": "1.0.0",\n'
        '}\n'
        '\n'
        f'{func_code}'
    )

    # Optional markdown instructions
    if instructions:
        (skill_dir / "_skill.md").write_text(instructions)

    return skill_dir


def _cleanup_module(name: str):
    """Remove a test module from sys.modules."""
    key = f"hermes_python_skill_{name}"
    sys.modules.pop(key, None)


def _patch_skills_dir(skills_dir: Path):
    """Patch hermes_constants.get_skills_dir in python_skills module namespace."""
    import agent.python_skills as ps_mod
    return patch.object(ps_mod, "get_skills_dir", return_value=skills_dir)


# ── scan_python_skills ──────────────────────────────────────────────────

class TestScanPythonSkills:
    def test_finds_python_skills(self, tmp_path):
        _make_python_skill(tmp_path, "test-skill")
        _cleanup_module("test-skill")
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert "test-skill" in result
        assert result["test-skill"].name == "test-skill"
        assert result["test-skill"].description == "Test Python skill"

    def test_empty_dir(self, tmp_path):
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert result == {}

    def test_skips_non_python_dirs(self, tmp_path):
        (tmp_path / "not-a-skill").mkdir()
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert result == {}

    def test_loads_functions(self, tmp_path):
        funcs = {"my_function": "Does something cool"}
        _make_python_skill(tmp_path, "func-skill", functions=funcs)
        _cleanup_module("func-skill")
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert len(result["func-skill"].functions) == 1
        assert result["func-skill"].functions[0].name == "my_function"

    def test_loads_instructions_from_md(self, tmp_path):
        instructions = "Follow these steps carefully."
        _make_python_skill(tmp_path, "md-skill", instructions=instructions)
        _cleanup_module("md-skill")
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert instructions in result["md-skill"].instructions

    def test_ignores_hidden_dirs(self, tmp_path):
        (tmp_path / ".hidden-skill").mkdir()
        (tmp_path / ".hidden-skill" / "_skill.py").write_text("")
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert ".hidden-skill" not in result

    def test_multiple_skills(self, tmp_path):
        _make_python_skill(tmp_path, "skill-a")
        _make_python_skill(tmp_path, "skill-b")
        _cleanup_module("skill-a")
        _cleanup_module("skill-b")
        with _patch_skills_dir(tmp_path):
            result = scan_python_skills()
        assert "skill-a" in result
        assert "skill-b" in result
        assert len(result) == 2


# ── get_python_skill ────────────────────────────────────────────────────

class TestGetPythonSkill:
    def test_get_by_name(self, tmp_path):
        _make_python_skill(tmp_path, "my-skill")
        _cleanup_module("my-skill")
        with _patch_skills_dir(tmp_path):
            skill = get_python_skill("my-skill")
        assert skill is not None
        assert skill.name == "my-skill"

    def test_returns_none_for_missing(self, tmp_path):
        with _patch_skills_dir(tmp_path):
            skill = get_python_skill("nonexistent")
        assert skill is None

    def test_case_insensitive(self, tmp_path):
        _make_python_skill(tmp_path, "my-skill")
        _cleanup_module("my-skill")
        with _patch_skills_dir(tmp_path):
            skill = get_python_skill("MY-SKILL")
        assert skill is not None


# ── build_python_skill_message ──────────────────────────────────────────

class TestBuildPythonSkillMessage:
    def test_includes_instructions(self, tmp_path):
        instructions = "Do the thing."
        _make_python_skill(tmp_path, "msg-skill", instructions=instructions)
        _cleanup_module("msg-skill")
        with _patch_skills_dir(tmp_path):
            skill = get_python_skill("msg-skill")
            assert skill is not None
        msg = build_python_skill_message(skill)
        assert instructions in msg
        assert "msg-skill" in msg

    def test_includes_function_signatures(self, tmp_path):
        _make_python_skill(
            tmp_path, "sig-skill",
            functions={"do_thing": "Does the thing"}
        )
        _cleanup_module("sig-skill")
        with _patch_skills_dir(tmp_path):
            skill = get_python_skill("sig-skill")
            assert skill is not None
        msg = build_python_skill_message(skill)
        assert "do_thing" in msg
        assert "Does the thing" in msg


# ── build_python_skills_index ───────────────────────────────────────────

class TestBuildPythonSkillsIndex:
    def test_includes_python_skills(self, tmp_path):
        _make_python_skill(tmp_path, "index-test", description="Indexable skill")
        _cleanup_module("index-test")
        with _patch_skills_dir(tmp_path):
            idx = build_python_skills_index()
        assert "index-test" in idx
        assert "Indexable skill" in idx

    def test_empty_when_no_python_skills(self, tmp_path):
        # Clear the cache first
        import agent.python_skills as ps_mod
        ps_mod._python_skills_cache.clear()
        ps_mod._python_skills_mtime = 0
        with _patch_skills_dir(tmp_path):
            idx = build_python_skills_index()
        assert idx == ""


# ── get_python_skills (cached) ──────────────────────────────────────────

class TestGetPythonSkillsCached:
    def test_caches_results(self, tmp_path):
        _make_python_skill(tmp_path, "cached-skill")
        _cleanup_module("cached-skill")
        with _patch_skills_dir(tmp_path):
            skills1 = get_python_skills()
            # Patch mtime to be higher so second call doesn't rescan
            import agent.python_skills as ps_mod
            ps_mod._python_skills_mtime = 9999999999
            skills2 = get_python_skills()
        assert "cached-skill" in skills1
        assert skills1 is skills2  # Same cached object

    def test_rescans_on_reload(self, tmp_path):
        _make_python_skill(tmp_path, "reload-skill")
        _cleanup_module("reload-skill")
        with _patch_skills_dir(tmp_path):
            result = reload_python_skills()
        assert result["total"] == 1
        assert any(s["name"] == "reload-skill" for s in result["added"])


# ── Integration: scan_skill_commands includes Python skills ─────────────

class TestPythonSkillsIntegration:
    def _clear_python_skills_cache(self):
        """Clear the global Python skills cache so tests are isolated."""
        import agent.python_skills as ps_mod
        ps_mod._python_skills_cache.clear()
        ps_mod._python_skills_mtime = 0

    def test_python_skill_in_scan_skill_commands(self, tmp_path):
        """Python-backed skills should appear in scan_skill_commands()."""
        self._clear_python_skills_cache()
        _make_python_skill(tmp_path, "int-skill")
        _cleanup_module("int-skill")
        with _patch_skills_dir(tmp_path):
            from agent.skill_commands import scan_skill_commands
            result = scan_skill_commands()
        assert "/int-skill" in result
        assert result["/int-skill"]["python_skill"] is True

    def test_python_skill_invocation_message(self, tmp_path):
        """build_skill_invocation_message should work for Python skills."""
        self._clear_python_skills_cache()
        _make_python_skill(
            tmp_path, "inv-skill",
            functions={"hello": "Say hello"},
            instructions="Say hello to everyone."
        )
        _cleanup_module("inv-skill")
        with _patch_skills_dir(tmp_path):
            from agent.skill_commands import (
                scan_skill_commands,
                build_skill_invocation_message,
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/inv-skill", "hi there")
        assert msg is not None
        assert "Say hello to everyone" in msg
        assert "hi there" in msg
        assert "hello" in msg

    def test_python_skill_preloaded(self, tmp_path):
        """build_preloaded_skills_prompt should load Python skills."""
        self._clear_python_skills_cache()
        _make_python_skill(tmp_path, "preload-skill")
        _cleanup_module("preload-skill")
        with _patch_skills_dir(tmp_path):
            from agent.skill_commands import build_preloaded_skills_prompt
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["preload-skill"]
            )
        assert "preload-skill" in loaded
        assert missing == []
        assert "preloaded" in prompt.lower()

    def test_python_skill_override_markdown(self, tmp_path):
        """Python skill with same name as markdown skill should win."""
        self._clear_python_skills_cache()
        # Create markdown skill
        skill_dir = tmp_path / "override-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: override-skill\ndescription: Markdown version\n---\n\nBody."
        )
        # Create Python skill
        _make_python_skill(tmp_path, "override-skill", description="Python version")
        _cleanup_module("override-skill")
        with _patch_skills_dir(tmp_path):
            from agent.skill_commands import scan_skill_commands
            result = scan_skill_commands()
        # The command should exist and point to Python skill
        assert "/override-skill" in result
        assert result["/override-skill"]["python_skill"] is True
