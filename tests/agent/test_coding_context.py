"""Coding posture is explicit configuration, never filename/model routing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent import coding_context as cc


def _git_init(path: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(path),
    }
    (path / "opaque.data").write_text("content\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial state"],
    ):
        subprocess.run(
            [shutil.which("git"), "-C", str(path), *args],
            check=True,
            env=env,
        )


def test_coding_guidance_advertises_persistent_terminal_state():
    assert "Terminal state persists across calls" in cc.CODING_AGENT_GUIDANCE
    assert "Activate a virtualenv" in cc.CODING_AGENT_GUIDANCE


@pytest.mark.parametrize(
    "filename",
    [
        "main.py",
        "test_password_rotation.py",
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "AGENTS.md",
        "Makefile",
    ],
)
def test_auto_mode_never_classifies_filename_or_extension(tmp_path, filename):
    cfg = {"agent": {"coding_context": "auto"}}
    before = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config=cfg)
    (tmp_path / filename).write_text("pytest tests/test_passwords.py\n")
    after = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config=cfg)

    assert before.kind == after.kind == "general"
    assert before.system_blocks() == after.system_blocks() == []
    assert before.toolset_selection(cfg) is after.toolset_selection(cfg) is None


def test_auto_mode_stays_general_inside_git_repo(tmp_path):
    _git_init(tmp_path)
    mode = cc.resolve_runtime_mode(
        platform="desktop",
        cwd=tmp_path,
        config={"agent": {"coding_context": "auto"}},
    )

    assert mode.is_coding is False
    assert mode.system_blocks() == []
    assert mode.toolset_selection() is None


@pytest.mark.parametrize("mode_name", ["on", "focus"])
def test_explicit_operator_mode_activates_posture_anywhere(tmp_path, mode_name):
    mode = cc.resolve_runtime_mode(
        platform="discord",
        cwd=tmp_path,
        config={"agent": {"coding_context": mode_name}},
    )

    assert mode.is_coding is True
    assert mode.system_blocks()[0] == cc.CODING_AGENT_GUIDANCE


def test_focus_is_the_only_mode_that_selects_coding_toolset(tmp_path):
    focus = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config={"agent": {"coding_context": "focus"}},
    )
    on = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config={"agent": {"coding_context": "on"}},
    )
    auto = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config={"agent": {"coding_context": "auto"}},
    )

    assert focus.toolset_selection({}) == [cc.CODING_TOOLSET]
    assert on.toolset_selection({}) is None
    assert auto.toolset_selection({}) is None


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.6-sol",
        "anthropic/claude-opus-4.8",
        "moonshot/kimi-k3",
        "acme/opaque-model",
        None,
    ],
)
def test_model_identifier_is_opaque_to_prompt_and_tools(tmp_path, model):
    cfg = {"agent": {"coding_context": "on"}}
    baseline = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config=cfg,
        model=None,
    )
    candidate = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config=cfg,
        model=model,
    )

    assert candidate.system_prompt_parts() == baseline.system_prompt_parts()
    assert candidate.toolset_selection(cfg) == baseline.toolset_selection(cfg)
    assert candidate.model == model


def test_skill_category_labels_never_filter_prompt_index(tmp_path):
    focus = cc.resolve_runtime_mode(
        cwd=tmp_path,
        config={"agent": {"coding_context": "focus"}},
    )

    assert focus.compact_skill_categories() == frozenset()
    assert cc.coding_compact_skill_categories(
        cwd=tmp_path,
        config={"agent": {"coding_context": "focus"}},
    ) == frozenset()


def test_non_git_marker_names_do_not_create_workspace_snapshot(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "AGENTS.md").write_text("instructions")

    assert cc.build_coding_workspace_block(tmp_path) == ""
    assert cc.resolve_runtime_mode(cwd=tmp_path).is_coding is False


def test_project_facts_are_observations_not_runtime_routing_authority(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "pytest.ini").write_text("")
    (tmp_path / "Makefile").write_text("lint:\n")

    facts = cc.detect_project_facts(tmp_path)

    assert facts.manifests == ["package.json", "Makefile"]
    assert facts.package_managers == ["pnpm"]
    assert facts.verify_commands == ["pnpm run test", "pytest", "make lint"]
    assert facts.context_files == []
    assert cc.resolve_runtime_mode(cwd=tmp_path).is_coding is False
    assert cc.build_coding_workspace_block(tmp_path) == ""


def test_git_workspace_prompt_contains_only_structural_git_facts(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"pytest tests/test_passwords.py"}}'
    )
    block = cc.build_coding_workspace_block(tmp_path)
    facts = cc.project_facts_for(tmp_path)

    assert "Workspace" in block
    assert "Branch: main" in block
    assert "Status:" in block
    assert "Project:" not in block
    assert "Verify:" not in block
    assert "package.json" not in block
    assert facts["root"] == str(tmp_path.resolve())
    assert facts["manifests"] == ["package.json"]
    assert facts["packageManagers"] == []
    assert facts["verifyCommands"] == ["npm run test"]


def test_worktree_snapshot_does_not_expose_primary_path(tmp_path):
    main_tree = tmp_path / "main"
    main_tree.mkdir()
    _git_init(main_tree)
    worktree = tmp_path / "worktree"
    subprocess.run(
        [
            "git",
            "-C",
            str(main_tree),
            "worktree",
            "add",
            "-b",
            "wt-branch",
            str(worktree),
        ],
        check=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )

    block = cc.build_coding_workspace_block(worktree)

    assert "Worktree: linked" in block
    assert str(main_tree.resolve()) not in block
    assert f"Root: {worktree.resolve()}" in block


def test_parse_status_counts_and_branch():
    porcelain = (
        "# branch.head feature\n"
        "# branch.upstream origin/feature\n"
        "# branch.ab +2 -1\n"
        "1 M. N... 100644 100644 100644 aaa bbb staged.opaque\n"
        "1 .M N... 100644 100644 100644 ccc ddd modified.opaque\n"
        "? new.opaque\n"
        "u UU N... 1 2 3 abc def conflict.opaque\n"
    )

    branch, counts = cc._parse_status(porcelain)

    assert branch == {
        "head": "feature",
        "upstream": "origin/feature",
        "ahead": "2",
        "behind": "1",
    }
    assert counts == {
        "staged": 1,
        "modified": 1,
        "untracked": 1,
        "conflicts": 1,
    }


def test_explicit_operator_instructions_are_stable_trailing_block(tmp_path):
    cfg = {
        "agent": {
            "coding_context": "on",
            "coding_instructions": ["first", "second"],
        }
    }
    mode = cc.resolve_runtime_mode(cwd=tmp_path, config=cfg)

    prefix, workspace, trailing = mode.system_prompt_parts()

    assert prefix == [cc.CODING_AGENT_GUIDANCE]
    assert workspace == []
    assert trailing == ["Operator instructions (from config):\nfirst\nsecond"]
