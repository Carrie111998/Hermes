from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml


WORKER_TOOLS = {
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
    "kanban_heartbeat",
    "kanban_comment",
}


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {}},
    }


def test_dispatcher_worker_replaces_full_kanban_and_removes_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import model_tools

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setattr(model_tools, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setattr(model_tools, "_is_delegated_child_context", lambda: False)
    monkeypatch.setattr(
        model_tools.registry,
        "get_definitions",
        lambda names, quiet=False: [_schema(name) for name in sorted(names)],
    )

    definitions = model_tools._compute_tool_definitions(
        enabled_toolsets=["file", "clarify", "kanban"],
        quiet_mode=True,
    )
    names = {item["function"]["name"] for item in definitions}

    assert WORKER_TOOLS <= names
    assert "clarify" not in names
    assert not ({"kanban_create", "kanban_list", "kanban_link", "kanban_unblock"} & names)
    assert {"read_file", "write_file", "patch", "search_files"} <= names


def test_normal_session_does_not_receive_or_replace_worker_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import model_tools

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(model_tools, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(
        model_tools.registry,
        "get_definitions",
        lambda names, quiet=False: [_schema(name) for name in sorted(names)],
    )

    definitions = model_tools._compute_tool_definitions(
        enabled_toolsets=["clarify", "kanban"],
        quiet_mode=True,
    )
    names = {item["function"]["name"] for item in definitions}

    assert "clarify" in names
    assert {"kanban_create", "kanban_list", "kanban_link", "kanban_unblock"} <= names


def test_worker_guidance_requires_a_real_dispatcher_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.delegation_context import has_dispatcher_owned_worker_task

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "agent.delegation_context.is_dispatcher_owned_worker_context",
        lambda: True,
    )
    assert has_dispatcher_owned_worker_task() is False

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert has_dispatcher_owned_worker_task() is True


def test_project_context_off_keeps_soul_and_cannot_change_tool_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.runtime_cwd import resolve_agent_cwd, resolve_project_context

    tool_root = tmp_path / "tools"
    repo_root = tmp_path / "repo"
    tool_root.mkdir()
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(tool_root))

    result = resolve_project_context(
        "off", assigned_workdir=repo_root, platform="cli"
    )
    assert result.repository_context_active is False
    assert result.root is None
    assert result.skip_context_files is True
    assert result.load_soul_identity is True
    assert resolve_agent_cwd() == tool_root


def test_project_context_assigned_ignores_ambient_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.runtime_cwd import resolve_project_context

    ambient = tmp_path / "ambient"
    assigned = tmp_path / "assigned"
    ambient.mkdir()
    assigned.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(ambient))

    missing = resolve_project_context("assigned", platform="cli")
    present = resolve_project_context(
        "assigned", assigned_workdir=assigned, platform="cli"
    )
    assert missing.repository_context_active is False
    assert missing.root is None
    assert present.repository_context_active is True
    assert present.root == assigned.resolve()


def test_project_context_assigned_rejects_unowned_worker_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.runtime_cwd import resolve_project_context

    workspace = tmp_path / "worker"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    result = resolve_project_context("assigned", platform="api_server")

    assert result.repository_context_active is False
    assert result.root is None


def test_project_context_auto_is_marker_aware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.runtime_cwd import resolve_project_context

    neutral = tmp_path / "neutral"
    repository = tmp_path / "repository"
    neutral.mkdir()
    repository.mkdir()
    (repository / "agents.md").write_text("project instructions\n", encoding="utf-8")

    monkeypatch.chdir(neutral)
    missing = resolve_project_context("auto", platform="cli")
    monkeypatch.chdir(repository)
    present = resolve_project_context("auto", platform="cli")

    assert missing.repository_context_active is False
    assert present.repository_context_active is True
    assert present.root == repository.resolve()


def _prompt_agent(**overrides) -> SimpleNamespace:
    values = {
        "load_soul_identity": True,
        "skip_context_files": True,
        "repository_context_active": False,
        "repository_context_root": None,
        "valid_tool_names": {"terminal"},
        "effective_toolsets": {"terminal"},
        "_execution_guidance_mode": "compact",
        "_hermes_help": False,
        "_task_completion_guidance": True,
        "_parallel_tool_call_guidance": True,
        "_tool_use_enforcement": True,
        "_execution_guidance": True,
        "_environment_probe": False,
        "_kanban_worker_guidance": "",
        "_memory_store": None,
        "_memory_manager": None,
        "model": "openai/gpt-5.6-sol",
        "provider": "openai-codex",
        "platform": "cli",
        "pass_session_id": False,
        "session_id": "test",
        "_emit_status": lambda *_args, **_kwargs: None,
        "_plugin_system_prompt_sections_snapshot": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_compact_execution_is_exclusive_and_help_is_gated() -> None:
    from agent.system_prompt import build_system_prompt_parts

    with (
        patch("run_agent.load_soul_md", return_value="SOUL MARKER"),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt") as context_files,
    ):
        stable = build_system_prompt_parts(_prompt_agent())["stable"]

    assert "SOUL MARKER" in stable
    assert "# Execution" in stable
    assert "Task completion" not in stable
    assert "Tool-use enforcement" not in stable
    assert "Execution discipline" not in stable
    assert "Hermes Agent Help" not in stable
    assert "# Repository work" not in stable
    context_files.assert_not_called()


def test_compact_no_tool_role_omits_profile_path_boilerplate() -> None:
    from agent.system_prompt import build_system_prompt_parts

    with (
        patch("run_agent.load_soul_md", return_value="SOUL MARKER"),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt") as context_files,
    ):
        stable = build_system_prompt_parts(
            _prompt_agent(valid_tool_names=set(), effective_toolsets=set())
        )["stable"]

    assert "SOUL MARKER" in stable
    assert "Active Hermes profile:" not in stable
    context_files.assert_not_called()


@pytest.fixture
def skill_policy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def write_skill(name: str, description: str) -> None:
        skill_dir = skills / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )

    write_skill("alpha", "Alpha description.")
    write_skill("beta", "Beta description.")
    write_skill("gamma", "Gamma description.")
    return home


def _write_policy(home: Path, skills: dict) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"skills": skills}, sort_keys=False), encoding="utf-8"
    )
    from agent import skill_utils
    from agent.prompt_builder import clear_skills_system_prompt_cache
    from tools import skills_tool

    skill_utils._raw_config_cache_clear()
    clear_skills_system_prompt_cache(clear_snapshot=True)
    skills_tool._SKILLS_CACHE.clear()


def test_skill_policy_filters_access_and_demotes_descriptions(
    skill_policy_home: Path,
) -> None:
    from agent.prompt_builder import build_skills_system_prompt
    from tools.skills_tool import skill_view, validate_configured_skill_policy

    _write_policy(
        skill_policy_home,
        {
            "project_discovery": False,
            "allowed": ["alpha", "beta"],
            "index_described": ["alpha"],
        },
    )
    prompt = build_skills_system_prompt(available_tools={"skill_view"})
    sources = validate_configured_skill_policy()

    assert "Alpha description." in prompt
    assert "beta" in prompt
    assert "Beta description." not in prompt
    assert "gamma" not in prompt
    assert set(sources) == {"alpha", "beta"}
    denied = json.loads(skill_view("gamma", preprocess=False))
    allowed = json.loads(skill_view("alpha", preprocess=False))
    assert denied["success"] is False
    assert "skills.allowed" in denied["error"]
    assert allowed["success"] is True


def test_unknown_allowed_skill_fails_validation(skill_policy_home: Path) -> None:
    from tools.skills_tool import validate_configured_skill_policy

    _write_policy(
        skill_policy_home,
        {"project_discovery": False, "allowed": ["missing-skill"]},
    )
    with pytest.raises(ValueError, match="missing: missing-skill"):
        validate_configured_skill_policy()


def test_skill_manage_policy_denies_single_and_batch_targets(
    skill_policy_home: Path,
) -> None:
    from tools.skill_manager_tool import skill_manage

    _write_policy(
        skill_policy_home,
        {"project_discovery": False, "allowed": ["alpha"]},
    )
    single = json.loads(
        skill_manage(
            action="patch",
            name="gamma",
            old_string="old",
            new_string="new",
        )
    )
    batch = json.loads(
        skill_manage(
            action="batch",
            name="",
            operations=[{
                "action": "patch",
                "name": "gamma",
                "old_string": "old",
                "new_string": "new",
            }],
        )
    )

    assert single["success"] is False
    assert batch["success"] is False
    assert "skills.allowed" in single["error"]
    assert "skills.allowed" in batch["error"]
