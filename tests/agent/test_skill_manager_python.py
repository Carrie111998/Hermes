#!/usr/bin/env python3
"""Tests for skill_manage(action='create_python') — Python-backed skill creation."""

import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch

# Patch get_hermes_home before importing skill_manager_tool
import sys
import os
import tempfile

@pytest.fixture()
def tmp_hermes_home(tmp_path):
    """Create a temporary HERMES_HOME with a skills directory."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    with patch("tools.skill_manager_tool.get_hermes_home", return_value=str(tmp_path)):
        # Also patch the module-level SKILLS_DIR
        import tools.skill_manager_tool as sm
        original_skills_dir = sm.SKILLS_DIR
        sm.SKILLS_DIR = skills_dir
        yield skills_dir
        sm.SKILLS_DIR = original_skills_dir


def test_create_python_basic(tmp_hermes_home):
    """Basic create_python creates _skill.py with correct structure."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-basic",
        functions=[
            {"name": "do_thing", "description": "Do a thing"},
        ],
    )
    data = json.loads(result)
    assert data["success"] is True
    assert data["functions"] == ["do_thing"]

    skill_dir = tmp_hermes_home / "test-basic"
    assert skill_dir.exists()
    skill_py = skill_dir / "_skill.py"
    assert skill_py.exists()

    content = skill_py.read_text()
    assert "SKILL_INFO" in content
    assert '"name": "test-basic"' in content
    assert "do_thing" in content
    assert 'async def do_thing' in content
    assert '"""Do a thing"""' in content


def test_create_python_multiple_functions(tmp_hermes_home):
    """Multiple functions are all generated."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-multi",
        functions=[
            {"name": "fn_one", "description": "First function"},
            {"name": "fn_two", "description": "Second function"},
            {"name": "fn_three", "description": "Third function"},
        ],
    )
    data = json.loads(result)
    assert data["success"] is True
    assert set(data["functions"]) == {"fn_one", "fn_two", "fn_three"}

    skill_py = tmp_hermes_home / "test-multi" / "_skill.py"
    content = skill_py.read_text()
    assert "async def fn_one" in content
    assert "async def fn_two" in content
    assert "async def fn_three" in content


def test_create_python_with_category(tmp_hermes_home):
    """create_python with category places skill in category subdirectory."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-cat",
        category="devops",
        functions=[{"name": "deploy", "description": "Deploy app"}],
    )
    data = json.loads(result)
    assert data["success"] is True

    skill_dir = tmp_hermes_home / "devops" / "test-cat"
    assert skill_dir.exists()
    assert (skill_dir / "_skill.py").exists()


def test_create_python_with_instructions(tmp_hermes_home):
    """create_python with instructions creates _skill.md."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-instr",
        functions=[{"name": "run", "description": "Run something"}],
        instructions="# Instructions\n\nDo the thing carefully.",
    )
    data = json.loads(result)
    assert data["success"] is True

    skill_md = tmp_hermes_home / "test-instr" / "_skill.md"
    assert skill_md.exists()
    assert "Do the thing carefully" in skill_md.read_text()


def test_create_python_with_version(tmp_hermes_home):
    """create_python with custom version uses it."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-ver",
        functions=[{"name": "go", "description": "Go"}],
        version="2.1.0",
    )
    data = json.loads(result)
    assert data["success"] is True

    content = (tmp_hermes_home / "test-ver" / "_skill.py").read_text()
    assert '"version": "2.1.0"' in content


def test_create_python_default_version(tmp_hermes_home):
    """create_python without version defaults to 1.0.0."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-def-ver",
        functions=[{"name": "go", "description": "Go"}],
    )
    data = json.loads(result)
    assert data["success"] is True

    content = (tmp_hermes_home / "test-def-ver" / "_skill.py").read_text()
    assert '"version": "1.0.0"' in content


def test_create_python_duplicate_fails(tmp_hermes_home):
    """Creating a skill that already exists returns error."""
    from tools.skill_manager_tool import skill_manage

    skill_manage(
        action="create_python",
        name="test-dup",
        functions=[{"name": "a", "description": "A"}],
    )
    result = skill_manage(
        action="create_python",
        name="test-dup",
        functions=[{"name": "b", "description": "B"}],
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "already exists" in data["error"]


def test_create_python_no_functions_fails(tmp_hermes_home):
    """create_python without functions returns error."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-nofn",
        functions=[],
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "functions is required" in data["error"]


def test_create_python_invalid_name_fails(tmp_hermes_home):
    """create_python with invalid name returns error."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="Invalid-Name",
        functions=[{"name": "a", "description": "A"}],
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid skill name" in data["error"]


def test_create_python_invalid_function_name_fails(tmp_hermes_home):
    """create_python with invalid function name returns error."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-bad-fn",
        functions=[{"name": "Bad-Name", "description": "Bad"}],
    )
    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid function name" in data["error"]


def test_create_python_with_config(tmp_hermes_home):
    """create_python with config creates _skill_config.yaml."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-cfg",
        functions=[{"name": "x", "description": "X"}],
        config={"timeout": 30, "retries": 3},
    )
    data = json.loads(result)
    assert data["success"] is True

    config_file = tmp_hermes_home / "test-cfg" / "_skill_config.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "timeout: 30" in content
    assert "retries: 3" in content


def test_create_python_generated_description(tmp_hermes_home):
    """When no description provided, it's generated from functions."""
    from tools.skill_manager_tool import skill_manage

    result = skill_manage(
        action="create_python",
        name="test-gen-desc",
        functions=[
            {"name": "do_a", "description": "Do action A"},
            {"name": "do_b", "description": "Do action B"},
        ],
    )
    data = json.loads(result)
    assert data["success"] is True

    content = (tmp_hermes_home / "test-gen-desc" / "_skill.py").read_text()
    assert "Do action A" in content
    assert "Do action B" in content
