"""Behavior tests for managed read-only skill content and external state."""

from pathlib import Path

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
