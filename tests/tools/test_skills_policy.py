"""Behavior tests for managed read-only skill content and external state.

The generic-writer cases intentionally use the real ``ShellFileOperations``
backend. Mocking ``_get_file_ops`` would bypass the backend policy boundary
these tests are required to prove.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_state_root_is_profile_scoped_and_side_effect_free(tmp_path, monkeypatch):
    from hermes_constants import get_skills_dir, get_skills_state_dir

    first = tmp_path / "profiles" / "first"
    second = tmp_path / "profiles" / "second"

    monkeypatch.setenv("HERMES_HOME", str(first))
    assert get_skills_dir() == first / "skills"
    assert get_skills_state_dir() == first / "state" / "skills"
    assert not first.exists()

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert get_skills_dir() == second / "skills"
    assert get_skills_state_dir() == second / "state" / "skills"
    assert not second.exists()


def test_all_metadata_resolvers_follow_active_profile_without_io(
    tmp_path,
    monkeypatch,
):
    from agent import curator, curator_backup
    from tools import skill_usage, skills_hub, skills_sync, skills_sync_client

    for profile_name in ("alpha", "beta"):
        home = tmp_path / "profiles" / profile_name
        monkeypatch.setenv("HERMES_HOME", str(home))

        assert skills_hub._hub_dir() == home / "state" / "skills" / "hub"
        assert skill_usage._usage_file() == home / "state" / "skills" / "usage.json"
        assert skills_sync._manifest_file() == (
            home / "state" / "skills" / "bundled-manifest"
        )
        assert skills_sync_client._sync_state_path() == (
            home / "state" / "skills" / "sync" / "state.json"
        )
        assert curator._state_file() == (
            home / "state" / "skills" / "curator-state.json"
        )
        assert curator_backup._backups_dir() == (
            home / "state" / "skills" / "curator-backups"
        )
        assert not home.exists()


def test_read_only_config_refuses_content_mutation(tmp_path, monkeypatch):
    from tools.skills_policy import (
        SKILLS_CONTENT_READ_ONLY,
        SkillsContentPolicyError,
        require_skills_content_writable,
    )

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: read_only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(SkillsContentPolicyError, match=SKILLS_CONTENT_READ_ONLY):
        require_skills_content_writable("test mutation")


@pytest.fixture
def read_only_skill_content(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skill = home / "skills" / "planning" / "ticket-decomposition"
    skill.mkdir(parents=True)
    target = skill / "SKILL.md"
    target.write_text("original\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: read_only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home, target


def _assert_read_only_tool_result(result_json: str) -> None:
    from tools.skills_policy import SKILLS_CONTENT_READ_ONLY

    result = json.loads(result_json)
    assert SKILLS_CONTENT_READ_ONLY in result["error"]


def test_generic_write_file_refuses_managed_content_before_mutation(
    read_only_skill_content,
):
    from tools.file_tools import write_file_tool

    _home, target = read_only_skill_content
    before = target.read_bytes()

    _assert_read_only_tool_result(write_file_tool(str(target), "overwritten\n"))

    assert target.read_bytes() == before


def test_generic_write_policy_precedes_backend_dispatch(
    read_only_skill_content,
):
    from tools.file_tools import write_file_tool

    _home, target = read_only_skill_content
    with patch("tools.file_tools._get_file_ops") as get_file_ops:
        _assert_read_only_tool_result(
            write_file_tool(str(target), "overwritten\n")
        )

    get_file_ops.assert_not_called()
    assert target.read_text(encoding="utf-8") == "original\n"


def test_generic_write_absolute_fallback_policy_precedes_backend_dispatch(
    read_only_skill_content,
):
    from tools.file_tools import write_file_tool

    _home, target = read_only_skill_content
    with (
        patch(
            "tools.file_tools._resolve_path_for_task",
            side_effect=PermissionError("resolver unavailable"),
        ),
        patch("tools.file_tools._get_file_ops") as get_file_ops,
    ):
        _assert_read_only_tool_result(
            write_file_tool(str(target), "overwritten\n")
        )

    get_file_ops.assert_not_called()
    assert target.read_text(encoding="utf-8") == "original\n"


def test_generic_patch_replace_refuses_managed_content_before_mutation(
    read_only_skill_content,
):
    from tools.file_tools import patch_tool

    _home, target = read_only_skill_content
    before = target.read_bytes()

    _assert_read_only_tool_result(
        patch_tool(
            mode="replace",
            path=str(target),
            old_string="original",
            new_string="overwritten",
        )
    )

    assert target.read_bytes() == before


@pytest.mark.parametrize("mode", ["replace", "patch"])
def test_generic_patch_policy_precedes_backend_dispatch(
    read_only_skill_content,
    mode,
):
    from tools.file_tools import patch_tool

    _home, target = read_only_skill_content
    kwargs = (
        {
            "mode": "replace",
            "path": str(target),
            "old_string": "original",
            "new_string": "overwritten",
        }
        if mode == "replace"
        else {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                f"*** Update File: {target}\n"
                "@@\n"
                "-original\n"
                "+overwritten\n"
                "*** End Patch\n"
            ),
        }
    )
    with patch("tools.file_tools._get_file_ops") as get_file_ops:
        _assert_read_only_tool_result(patch_tool(**kwargs))

    get_file_ops.assert_not_called()
    assert target.read_text(encoding="utf-8") == "original\n"


@pytest.mark.parametrize(
    "operation",
    ["update", "add", "delete", "move_source", "move_destination"],
)
def test_generic_v4a_refuses_every_operation_type_before_mutation(
    read_only_skill_content,
    tmp_path,
    operation,
):
    from tools.file_tools import patch_tool

    home, target = read_only_skill_content
    added = home / "skills" / "added.md"
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    if operation == "update":
        body = (
            f"*** Update File: {target}\n"
            "@@\n"
            "-original\n"
            "+overwritten\n"
        )
    elif operation == "add":
        body = f"*** Add File: {added}\n+added\n"
    elif operation == "delete":
        body = f"*** Delete File: {target}\n"
    elif operation == "move_source":
        body = f"*** Move File: {target} -> {outside}\n"
    else:
        body = f"*** Move File: {outside} -> {added}\n"
    patch = f"*** Begin Patch\n{body}*** End Patch\n"
    target_before = target.read_bytes()
    outside_before = outside.read_bytes()

    _assert_read_only_tool_result(patch_tool(mode="patch", patch=patch))

    assert target.read_bytes() == target_before
    assert outside.read_bytes() == outside_before
    assert not added.exists()


def test_generic_v4a_preflights_all_targets_before_any_mutation(
    read_only_skill_content,
    tmp_path,
):
    from tools.file_tools import patch_tool

    _home, target = read_only_skill_content
    outside = tmp_path / "outside-created.md"
    target_before = target.read_bytes()
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {outside}\n"
        "+outside\n"
        f"*** Update File: {target}\n"
        "@@\n"
        "-original\n"
        "+overwritten\n"
        "*** End Patch\n"
    )

    _assert_read_only_tool_result(patch_tool(mode="patch", patch=patch))

    assert not outside.exists()
    assert target.read_bytes() == target_before


def test_backend_v4a_policy_runs_before_parser(
    read_only_skill_content,
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools.skills_policy import SKILLS_CONTENT_READ_ONLY

    home, target = read_only_skill_content
    operations = ShellFileOperations(LocalEnvironment(cwd=str(home)))
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {target}\n"
        "@@\n"
        "-original\n"
        "+overwritten\n"
        "*** End Patch\n"
    )
    with patch(
        "tools.patch_parser.parse_v4a_patch",
        side_effect=AssertionError("parser must not run before policy"),
    ) as parser:
        result = operations.patch_v4a(patch_text)

    assert SKILLS_CONTENT_READ_ONLY in result.error
    parser.assert_not_called()
    assert target.read_text(encoding="utf-8") == "original\n"


def test_generic_write_refuses_resolved_alias_into_managed_content(
    read_only_skill_content,
    tmp_path,
):
    from tools.file_tools import write_file_tool

    home, _target = read_only_skill_content
    alias = tmp_path / "skills-alias"
    try:
        alias.symlink_to(home / "skills", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    target = alias / "alias-created.md"

    _assert_read_only_tool_result(write_file_tool(str(target), "blocked\n"))

    assert not target.exists()


def test_generic_write_allows_relocated_runtime_state_via_normalized_path(
    read_only_skill_content,
):
    from tools.file_tools import write_file_tool

    home, _target = read_only_skill_content
    state = home / "skills" / ".." / "state" / "skills" / "runtime.txt"

    result = json.loads(write_file_tool(str(state), "runtime\n"))

    assert not result.get("error"), result
    assert (
        home / "state" / "skills" / "runtime.txt"
    ).read_text(encoding="utf-8") == "runtime\n"


def test_relative_backend_path_uses_backend_cwd_for_policy(
    read_only_skill_content,
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools.skills_policy import SKILLS_CONTENT_READ_ONLY

    home, target = read_only_skill_content
    before = target.read_bytes()
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(home)),
        cwd=str(home),
    )

    result = operations.write_file(
        "skills/planning/ticket-decomposition/SKILL.md",
        "overwritten\n",
    )

    assert SKILLS_CONTENT_READ_ONLY in result.error
    assert target.read_bytes() == before


def test_backend_delete_refuses_managed_content(
    read_only_skill_content,
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools.skills_policy import SKILLS_CONTENT_READ_ONLY

    home, target = read_only_skill_content
    operations = ShellFileOperations(LocalEnvironment(cwd=str(home)))
    before = target.read_bytes()

    result = operations.delete_file(str(target))

    assert SKILLS_CONTENT_READ_ONLY in result.error
    assert target.read_bytes() == before


@pytest.mark.parametrize("managed_endpoint", ["source", "destination"])
def test_backend_move_refuses_each_managed_endpoint(
    read_only_skill_content,
    tmp_path,
    managed_endpoint,
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools.skills_policy import SKILLS_CONTENT_READ_ONLY

    home, managed = read_only_skill_content
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    operations = ShellFileOperations(LocalEnvironment(cwd=str(home)))
    source, destination = (
        (managed, outside)
        if managed_endpoint == "source"
        else (outside, home / "skills" / "moved.md")
    )
    managed_before = managed.read_bytes()
    outside_before = outside.read_bytes()

    result = operations.move_file(str(source), str(destination))

    assert SKILLS_CONTENT_READ_ONLY in result.error
    assert managed.read_bytes() == managed_before
    assert outside.read_bytes() == outside_before
    assert not (home / "skills" / "moved.md").exists()


def test_generic_write_file_preserves_writable_skill_content(
    tmp_path,
    monkeypatch,
):
    from tools.file_tools import write_file_tool

    home = tmp_path / ".hermes"
    target = home / "skills" / "local" / "SKILL.md"
    target.parent.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: writable\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = json.loads(write_file_tool(str(target), "writable\n"))

    assert not result.get("error"), result
    assert target.read_text(encoding="utf-8") == "writable\n"


def test_missing_config_preserves_writable_default(tmp_path, monkeypatch):
    from tools.skills_policy import skills_content_mode

    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert skills_content_mode() == "writable"
    # A policy read may resolve a path but must not initialize discovery or
    # state. The normal writable mutator creates what it needs later.
    assert not home.exists()


def test_invalid_config_fails_closed_for_content_mutation(tmp_path, monkeypatch):
    from tools.skills_policy import (
        SKILLS_CONFIG_UNAVAILABLE,
        SkillsContentPolicyError,
        require_skills_content_writable,
    )

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("skills: [unterminated\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(SkillsContentPolicyError, match=SKILLS_CONFIG_UNAVAILABLE):
        require_skills_content_writable("test mutation")


def test_unreadable_existing_config_fails_closed(tmp_path, monkeypatch):
    from tools import skills_policy
    from tools.skills_policy import (
        SKILLS_CONFIG_UNAVAILABLE,
        SkillsContentPolicyError,
        require_skills_content_writable,
    )

    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "skills:\n  content_mode: writable\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        skills_policy,
        "_read_raw_policy_config",
        lambda path: (_ for _ in ()).throw(PermissionError(str(path))),
    )

    with pytest.raises(SkillsContentPolicyError, match=SKILLS_CONFIG_UNAVAILABLE):
        require_skills_content_writable("test mutation")

    assert config_path.read_text(encoding="utf-8").endswith("writable\n")
    assert not (home / "state").exists()


def test_valid_config_on_read_only_filesystem_needs_no_policy_write(
    tmp_path,
    monkeypatch,
):
    from tools.skills_policy import skills_content_mode

    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "skills:\n  content_mode: read_only\n",
        encoding="utf-8",
    )
    config_path.chmod(0o444)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert skills_content_mode() == "read_only"
    assert config_path.stat().st_mode & 0o222 == 0
    assert not (home / "state").exists()


def test_unrecognized_content_mode_fails_closed(tmp_path, monkeypatch):
    from tools.skills_policy import (
        SKILLS_CONFIG_UNAVAILABLE,
        SkillsContentPolicyError,
        require_skills_content_writable,
    )

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "skills:\n  content_mode: sometimes\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(SkillsContentPolicyError, match=SKILLS_CONFIG_UNAVAILABLE):
        require_skills_content_writable("test mutation")


def test_state_reader_falls_back_without_migrating(tmp_path, monkeypatch):
    from tools.skills_policy import state_read_path

    home = tmp_path / ".hermes"
    legacy = home / "skills" / ".usage.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    selected = state_read_path(("usage.json",), (".usage.json",))

    assert selected == legacy
    assert not (home / "state").exists()


def test_legacy_migration_warning_is_once_per_source(
    tmp_path,
    monkeypatch,
    caplog,
):
    from tools import skills_policy

    home = tmp_path / ".hermes"
    legacy = home / "skills" / ".usage.json"
    state = home / "state" / "skills" / "usage.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    skills_policy._warned_legacy_sources.clear()

    migration = skills_policy.prepare_legacy_state_write(state, legacy)
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")

    with caplog.at_level("WARNING", logger="hermes.skills_state"):
        skills_policy.note_legacy_state_write(migration)
        skills_policy.note_legacy_state_write(migration)

    messages = [
        record.message
        for record in caplog.records
        if "Migrated legacy skills runtime metadata" in record.message
    ]
    assert len(messages) == 1
    assert str(legacy) in messages[0]


def test_legacy_migration_selects_first_existing_variadic_source(
    tmp_path,
    monkeypatch,
):
    from tools import skills_policy

    home = tmp_path / ".hermes"
    state = home / "state" / "skills" / "sync" / "state.json"
    missing = home / "skills" / ".sync_state"
    oldest = home / "skills" / ".sync_manifest"
    oldest.parent.mkdir(parents=True)
    oldest.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    migration = skills_policy.prepare_legacy_state_write(
        state,
        missing,
        oldest,
    )

    assert migration == oldest
