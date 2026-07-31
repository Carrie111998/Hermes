"""Tests for tools/skill_usage.py — sidecar telemetry + provenance filtering."""

import json
import multiprocessing as mp
import os
from contextlib import contextmanager
from pathlib import Path

import pytest


def _bump_view_many(hermes_home: str, skill_name: str, iterations: int) -> None:
    os.environ["HERMES_HOME"] = hermes_home
    from tools.skill_usage import bump_view

    for _ in range(iterations):
        bump_view(skill_name)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir for each test.

    Pins ``curator.prune_builtins`` OFF so the bundled/hub-protection tests in
    this module exercise the off-path semantics regardless of the shipped
    default. Tests that want built-ins to be curation-eligible flip it back on
    explicitly via ``monkeypatch.setattr(mod, "_prune_builtins_enabled", ...)``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force skill_usage module to re-resolve paths per test
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return home


def _write_skill(skills_dir: Path, name: str, category: str = ""):
    """Create a minimal SKILL.md with a name: frontmatter field."""
    if category:
        d = skills_dir / category / name
    else:
        d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
---

# body
""",
        encoding="utf-8",
    )
    return d


def _write_skill_with_physical_alias(
    skills_dir: Path, directory_name: str, canonical_name: str
):
    """Create a skill whose filesystem alias differs from its lifecycle name."""
    d = skills_dir / directory_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {canonical_name}\ndescription: test skill\n---\n\n# body\n",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_empty_usage_returns_empty_dict(skills_home):
    from tools.skill_usage import load_usage
    assert load_usage() == {}


def test_save_and_load_roundtrip(skills_home):
    from tools.skill_usage import load_usage, save_usage
    data = {"skill-a": {"use_count": 3, "state": "active"}}
    save_usage(data)
    loaded = load_usage()
    assert loaded["skill-a"]["use_count"] == 3
    assert loaded["skill-a"]["state"] == "active"


def test_get_record_missing_returns_empty_record(skills_home):
    from tools.skill_usage import get_record
    rec = get_record("nonexistent")
    assert rec["use_count"] == 0
    assert rec["view_count"] == 0
    assert rec["state"] == "active"
    assert rec["pinned"] is False
    assert rec["archived_at"] is None


def test_load_usage_handles_corrupt_file(skills_home):
    from tools.skill_usage import load_usage, _usage_file
    _usage_file().write_text("{ not json }", encoding="utf-8")
    assert load_usage() == {}


# ---------------------------------------------------------------------------
# Counter bumps
# ---------------------------------------------------------------------------

def test_bump_view_increments_and_timestamps(skills_home):
    from tools.skill_usage import bump_view, get_record
    bump_view("my-skill")
    bump_view("my-skill")
    rec = get_record("my-skill")
    assert rec["view_count"] == 2
    assert rec["last_viewed_at"] is not None


def test_bumps_do_not_corrupt_other_skills(skills_home):
    from tools.skill_usage import bump_view, bump_use, get_record
    bump_view("skill-a")
    bump_use("skill-b")
    bump_view("skill-a")
    assert get_record("skill-a")["view_count"] == 2
    assert get_record("skill-a")["use_count"] == 0
    assert get_record("skill-b")["use_count"] == 1


def test_concurrent_bump_view_preserves_all_updates(skills_home):
    from tools.skill_usage import get_record

    process_count = 6
    iterations = 25
    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_bump_view_many,
            args=(str(skills_home), "shared-skill", iterations),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    for process in processes:
        assert process.exitcode == 0
    assert get_record("shared-skill")["view_count"] == process_count * iterations


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_set_state_active(skills_home):
    from tools.skill_usage import set_state, get_record, STATE_ACTIVE
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["state"] == "active"


def test_restoring_from_archive_clears_timestamp(skills_home):
    from tools.skill_usage import set_state, get_record, STATE_ARCHIVED, STATE_ACTIVE
    set_state("x", STATE_ARCHIVED)
    assert get_record("x")["archived_at"] is not None
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["archived_at"] is None


def test_forget_removes_record(skills_home):
    from tools.skill_usage import bump_view, forget, load_usage
    bump_view("x")
    assert "x" in load_usage()
    forget("x")
    assert "x" not in load_usage()


# ---------------------------------------------------------------------------
# Provenance filter — the load-bearing safety check
# ---------------------------------------------------------------------------

def test_agent_created_excludes_bundled(skills_home):
    from tools.skill_usage import list_agent_created_skill_names, mark_agent_created
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-skill", category="github")
    _write_skill(skills_dir, "my-skill")
    mark_agent_created("my-skill")
    # Seed a bundled manifest marking bundled-skill as upstream
    (skills_dir / ".bundled_manifest").write_text(
        "bundled-skill:abc123\n", encoding="utf-8",
    )
    names = list_agent_created_skill_names()
    assert "my-skill" in names
    assert "bundled-skill" not in names


def test_is_agent_created(skills_home):
    from tools.skill_usage import is_agent_created
    skills_dir = skills_home / "skills"
    (skills_dir / ".bundled_manifest").write_text("bundled:abc\n", encoding="utf-8")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps({"installed": {"hubbed": {}}}), encoding="utf-8",
    )
    assert is_agent_created("my-skill") is True
    assert is_agent_created("bundled") is False
    assert is_agent_created("hubbed") is False


# ---------------------------------------------------------------------------
# Archive / restore
# ---------------------------------------------------------------------------

def test_archive_skill_moves_directory(skills_home):
    from tools.skill_usage import archive_skill, get_record
    import tools.skill_usage as usage
    skills_dir = skills_home / "skills"
    skill_dir = _write_skill(skills_dir, "old-skill")
    assert skill_dir.exists()

    ok, msg = archive_skill("old-skill")
    assert ok, msg
    assert not skill_dir.exists()
    assert (skills_dir / ".archive" / "old-skill" / "SKILL.md").exists()
    assert get_record("old-skill")["state"] == "archived"
    assert get_record("old-skill")["archived_at"] is not None


def test_archive_refuses_bundled_skill(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled")
    (skills_dir / ".bundled_manifest").write_text("bundled:abc\n", encoding="utf-8")

    ok, msg = archive_skill("bundled")
    assert not ok
    assert "bundled" in msg.lower() or "hub" in msg.lower()


def test_archive_refuses_hub_skill(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "hub-skill")
    hub_dir = skills_dir / ".hub"
    hub_dir.mkdir()
    (hub_dir / "lock.json").write_text(
        json.dumps({"installed": {"hub-skill": {}}}), encoding="utf-8",
    )

    ok, msg = archive_skill("hub-skill")
    assert not ok


def test_archive_refuses_external_skill(skills_home, monkeypatch):
    from tools.skill_usage import archive_skill

    skills_dir = skills_home / "skills"
    external = skills_dir / "shared-vault"
    skill_dir = _write_skill(external, "external-skill")
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [external.resolve()],
    )

    ok, msg = archive_skill("external-skill")
    assert not ok
    assert "external" in msg.lower()
    assert skill_dir.exists()


def test_archive_missing_skill_returns_error(skills_home):
    from tools.skill_usage import archive_skill
    ok, msg = archive_skill("nonexistent")
    assert not ok
    assert "not found" in msg.lower()


def test_direct_archive_fails_closed_without_secure_manager_backend(
    skills_home, monkeypatch
):
    from tools.skill_usage import archive_skill

    skills_dir = skills_home / "skills"
    skill_dir = _write_skill(skills_dir, "unsafe-archive")
    monkeypatch.setattr(
        "tools.skill_manager_tool._secure_directory_create_supported",
        lambda: False,
    )

    ok, message = archive_skill("unsafe-archive")

    assert ok is False
    assert "unavailable" in message
    assert skill_dir.exists()


def test_direct_restore_fails_closed_without_secure_manager_backend(
    skills_home, monkeypatch
):
    from tools.skill_usage import archive_skill, restore_skill

    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "unsafe-restore")
    ok, message = archive_skill("unsafe-restore")
    assert ok, message
    monkeypatch.setattr(
        "tools.skill_manager_tool._secure_directory_create_supported",
        lambda: False,
    )

    ok, message = restore_skill("unsafe-restore")

    assert ok is False
    assert "unavailable" in message
    assert (skills_dir / ".archive" / "unsafe-restore").exists()


def test_archive_metadata_failure_is_truthful_and_retry_reconciles(skills_home, monkeypatch):
    from tools.skill_usage import archive_skill, get_record
    import tools.skill_usage as usage

    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "reconcile-archive")
    real_persist = usage.persist_lifecycle_move_metadata_strict
    monkeypatch.setattr(
        "tools.skill_usage.persist_lifecycle_move_metadata_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state write failed")),
    )
    ok, message = archive_skill("reconcile-archive")
    assert ok is False
    assert "filesystem move committed" in message
    assert (skills_dir / ".archive" / "reconcile-archive").exists()
    monkeypatch.setattr(usage, "persist_lifecycle_move_metadata_strict", real_persist)
    ok, message = archive_skill("reconcile-archive")
    assert ok, message
    assert get_record("reconcile-archive")["state"] == "archived"


def test_restore_metadata_failure_is_truthful_and_retry_reconciles(skills_home, monkeypatch):
    from tools.skill_usage import archive_skill, restore_skill, get_record
    import tools.skill_usage as usage

    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "reconcile-restore")
    assert archive_skill("reconcile-restore")[0]
    real_persist = usage.persist_lifecycle_move_metadata_strict
    monkeypatch.setattr(
        "tools.skill_usage.persist_lifecycle_move_metadata_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state write failed")),
    )
    ok, message = restore_skill("reconcile-restore")
    assert ok is False
    assert "filesystem move committed" in message
    assert (skills_dir / "reconcile-restore").exists()
    monkeypatch.setattr(usage, "persist_lifecycle_move_metadata_strict", real_persist)
    ok, message = restore_skill("reconcile-restore")
    assert ok, message
    assert get_record("reconcile-restore")["state"] == "active"


def test_alias_archive_metadata_retry_uses_canonical_identity(skills_home, monkeypatch):
    """A post-move archive retry can use either canonical or physical alias."""
    from tools.skill_usage import archive_skill, get_record, mark_agent_created
    import tools.skill_usage as usage

    skills_dir = skills_home / "skills"
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    real_persist = usage.persist_lifecycle_move_metadata_strict
    monkeypatch.setattr(
        usage,
        "persist_lifecycle_move_metadata_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state write failed")),
    )

    ok, message = archive_skill("frontmatter-alias")
    assert ok is False
    assert "filesystem move committed" in message
    assert (skills_dir / ".archive" / "legacy-directory").is_dir()

    monkeypatch.setattr(usage, "persist_lifecycle_move_metadata_strict", real_persist)
    ok, message = archive_skill("legacy-directory")
    assert ok, message
    assert get_record("frontmatter-alias")["state"] == "archived"


def test_alias_restore_metadata_retry_preserves_physical_directory(skills_home, monkeypatch):
    """Restore keeps the physical alias but reconciles canonical metadata."""
    from tools.skill_usage import archive_skill, get_record, mark_agent_created, restore_skill
    import tools.skill_usage as usage

    skills_dir = skills_home / "skills"
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    assert archive_skill("legacy-directory")[0]
    real_persist = usage.persist_lifecycle_move_metadata_strict
    monkeypatch.setattr(
        usage,
        "persist_lifecycle_move_metadata_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state write failed")),
    )

    # Use the old physical archive alias. The restore path must normalize it
    # to the canonical lifecycle lock before it checks the active tree.
    ok, message = restore_skill("legacy-directory")
    assert ok is False
    assert "filesystem move committed" in message
    assert (skills_dir / "legacy-directory").is_dir()
    assert not (skills_dir / "frontmatter-alias").exists()

    monkeypatch.setattr(usage, "persist_lifecycle_move_metadata_strict", real_persist)
    ok, message = restore_skill("legacy-directory")
    assert ok, message
    assert get_record("frontmatter-alias")["state"] == "active"


def test_restore_refuses_active_canonical_name_at_new_physical_alias(skills_home):
    """Restore must not overwrite lifecycle identity held by a new alias."""
    from tools.skill_usage import archive_skill, mark_agent_created, restore_skill

    skills_dir = skills_home / "skills"
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    assert archive_skill("frontmatter-alias")[0]
    # A subsequent create can use the canonical physical basename while the
    # archived version retains an older physical alias.
    _write_skill(skills_dir, "frontmatter-alias")

    # Exercise the historical physical alias; this must still reserve the
    # canonical lifecycle lock before checking the active canonical owner.
    ok, message = restore_skill("legacy-directory")

    assert ok is False
    assert "already active" in message
    assert (skills_dir / "frontmatter-alias" / "SKILL.md").exists()
    assert (skills_dir / ".archive" / "legacy-directory" / "SKILL.md").exists()


def test_physical_alias_restore_locks_canonical_before_archive_identity(
    skills_home, monkeypatch
):
    """Physical archive aliases must not invert canonical-to-physical order."""
    from tools.skill_usage import archive_skill, mark_agent_created, restore_skill
    import tools.skill_manager_tool as manager

    skills_dir = skills_home / "skills"
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    assert archive_skill("legacy-directory")[0]
    real_lock = manager._skill_mutation_lock
    lock_names = []

    @contextmanager
    def recording_lock(lock_name):
        lock_names.append(lock_name)
        with real_lock(lock_name):
            yield

    monkeypatch.setattr(manager, "_skill_mutation_lock", recording_lock)
    ok, message = restore_skill("legacy-directory")

    assert ok, message
    assert lock_names[0] == "frontmatter-alias"
    assert lock_names[1].startswith("physical\x00")


def test_collision_archive_roundtrip_restores_physical_alias(skills_home):
    """A new collision container retains a non-canonical physical basename."""
    from tools.skill_usage import archive_skill, mark_agent_created, restore_skill

    skills_dir = skills_home / "skills"
    # This unrelated archived skill occupies the active skill's physical alias.
    _write_skill_with_physical_alias(
        skills_dir / ".archive", "legacy-directory", "unrelated-skill"
    )
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")

    ok, message = archive_skill("frontmatter-alias")
    assert ok, message
    collision_root = skills_dir / ".archive" / ".collisions"
    containers = [p for p in collision_root.iterdir() if p.is_dir()]
    assert len(containers) == 1
    assert (containers[0] / "legacy-directory" / "SKILL.md").exists()

    ok, message = restore_skill("frontmatter-alias")
    assert ok, message
    assert (skills_dir / "legacy-directory" / "SKILL.md").exists()
    assert not (skills_dir / "frontmatter-alias").exists()
    assert not collision_root.exists()


def test_collision_restore_refuses_occupied_physical_destination(skills_home):
    """Restore never overwrites a new entry at the recorded physical alias."""
    from tools.skill_usage import archive_skill, mark_agent_created, restore_skill

    skills_dir = skills_home / "skills"
    _write_skill_with_physical_alias(
        skills_dir / ".archive", "legacy-directory", "unrelated-skill"
    )
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    assert archive_skill("frontmatter-alias")[0]
    _write_skill_with_physical_alias(
        skills_dir, "legacy-directory", "replacement-skill"
    )

    ok, message = restore_skill("frontmatter-alias")
    assert ok is False
    assert "destination already exists" in message
    collision_root = skills_dir / ".archive" / ".collisions"
    assert any((p / "legacy-directory").is_dir() for p in collision_root.iterdir())


def test_collision_layout_preserves_legal_timestamped_physical_alias(skills_home):
    """A valid physical basename ending in a timestamp is never stripped."""
    from tools.skill_usage import restore_skill

    skills_dir = skills_home / "skills"
    archived = (
        skills_dir
        / ".archive"
        / ".collisions"
        / "reserved-container"
    )
    physical_name = "frontmatter-alias-20260101000000"
    _write_skill_with_physical_alias(
        archived, physical_name, "frontmatter-alias"
    )

    ok, message = restore_skill("frontmatter-alias")
    assert ok, message
    assert (skills_dir / physical_name / "SKILL.md").exists()
    assert not (skills_dir / "frontmatter-alias").exists()


@pytest.mark.parametrize("container", [None, "legacy-import"])
def test_restore_rejects_legacy_timestamp_leaf_independent_of_canonical_name(
    skills_home, container
):
    """Old timestamp leaves are unsafe even when canonical and basename differ."""
    from tools.skill_usage import restore_skill

    archive = skills_home / "skills" / ".archive"
    if container is not None:
        archive = archive / container
    physical_name = "legacy-directory-20260101000000"
    _write_skill_with_physical_alias(
        archive, physical_name, "frontmatter-alias"
    )

    ok, message = restore_skill("frontmatter-alias")
    assert ok is False
    assert "trustworthy" in message
    assert (archive / physical_name).is_dir()


def test_new_archive_roundtrip_preserves_timestamped_physical_alias(skills_home):
    """A current archive marks a timestamp-looking physical basename safely."""
    from tools.skill_usage import archive_skill, mark_agent_created, restore_skill

    skills_dir = skills_home / "skills"
    physical_name = "legacy-directory-20260101000000"
    _write_skill_with_physical_alias(
        skills_dir, physical_name, "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")

    ok, message = archive_skill("frontmatter-alias")
    assert ok, message
    collision_root = skills_dir / ".archive" / ".collisions"
    assert any((p / physical_name).is_dir() for p in collision_root.iterdir())

    ok, message = restore_skill("frontmatter-alias")
    assert ok, message
    assert (skills_dir / physical_name / "SKILL.md").exists()
    assert not collision_root.exists()


def test_failed_timestamp_archive_removes_empty_collision_container(skills_home, monkeypatch):
    """A pre-commit rename failure leaves no unique collision container behind."""
    from tools.skill_usage import archive_skill, mark_agent_created

    skills_dir = skills_home / "skills"
    physical_name = "legacy-directory-20260101000000"
    _write_skill_with_physical_alias(
        skills_dir, physical_name, "frontmatter-alias"
    )
    mark_agent_created("frontmatter-alias")
    real_rename = __import__("os").rename

    def fail_skill_rename(src, dst, **kwargs):
        if src == physical_name:
            raise OSError("injected rename failure")
        return real_rename(src, dst, **kwargs)

    import tools.skill_manager_tool as manager
    monkeypatch.setattr(manager.os, "rename", fail_skill_rename)
    monkeypatch.setattr(
        manager.os,
        "supports_dir_fd",
        set(manager.os.supports_dir_fd) | {fail_skill_rename},
    )
    ok, message = archive_skill("frontmatter-alias")
    assert ok is False
    assert "failed to securely archive" in message
    collision_root = skills_dir / ".archive" / ".collisions"
    assert not collision_root.exists() or not any(collision_root.iterdir())
    assert (skills_dir / physical_name).is_dir()


def test_restore_rejects_ambiguous_canonical_archive_candidates(skills_home):
    """Canonical archive recovery must not choose one of several aliases."""
    from tools.skill_usage import restore_skill

    archive = skills_home / "skills" / ".archive"
    for directory_name in ("legacy-one", "legacy-two"):
        _write_skill_with_physical_alias(
            archive, directory_name, "frontmatter-alias"
        )

    ok, message = restore_skill("frontmatter-alias")
    assert ok is False
    assert "ambiguous" in message.lower()
    assert (archive / "legacy-one").is_dir()
    assert (archive / "legacy-two").is_dir()


def test_restore_fails_closed_for_corrupt_archive_metadata(skills_home):
    """An unreadable canonical candidate is not silently skipped or restored."""
    from tools.skill_usage import restore_skill

    broken = skills_home / "skills" / ".archive" / "legacy-directory"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: [not-a-string]\ndescription: test\n---\n",
        encoding="utf-8",
    )

    ok, message = restore_skill("frontmatter-alias")
    assert ok is False
    assert "refusing" in message.lower() or "incomplete" in message.lower()
    assert broken.is_dir()
    assert not (skills_home / "skills" / "frontmatter-alias").exists()


def test_restore_fails_closed_when_skills_config_is_malformed(skills_home):
    """A broken config may hide an external canonical-name collision."""
    from agent import skill_utils
    from tools.skill_usage import restore_skill

    archive = skills_home / "skills" / ".archive"
    archived = _write_skill(archive, "config-guarded")
    config_path = skills_home / "config.yaml"
    config_path.write_text(
        "skills:\n  external_dirs: [\n",
        encoding="utf-8",
    )
    skill_utils._external_dirs_cache_clear()

    ok, message = restore_skill("config-guarded")

    assert ok is False
    assert "config" in message.lower()
    assert archived.is_dir()
    assert not (skills_home / "skills" / "config-guarded").exists()


def test_restore_fails_closed_when_skills_config_is_unreadable(
    skills_home, monkeypatch
):
    """A read failure must leave both archive and active namespaces untouched."""
    from agent import skill_utils
    from tools.skill_usage import restore_skill

    archive = skills_home / "skills" / ".archive"
    archived = _write_skill(archive, "config-unreadable")
    config_path = skills_home / "config.yaml"
    config_path.write_text("skills: {}\n", encoding="utf-8")
    real_read_text = Path.read_text

    def deny_config_read(path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("permission denied")
        return real_read_text(path, *args, **kwargs)

    skill_utils._external_dirs_cache_clear()
    monkeypatch.setattr(Path, "read_text", deny_config_read)

    ok, message = restore_skill("config-unreadable")

    assert ok is False
    assert "permission denied" in message.lower()
    assert archived.is_dir()
    assert not (skills_home / "skills" / "config-unreadable").exists()


def test_restore_fails_closed_when_skills_config_is_dangling_symlink(skills_home):
    """A dangling config entry must stop restore before its filesystem rename."""
    from agent import skill_utils
    from tools.skill_usage import restore_skill

    archive = skills_home / "skills" / ".archive"
    archived = _write_skill(archive, "config-dangling")
    config_path = skills_home / "config.yaml"
    try:
        config_path.symlink_to(skills_home / "missing-config.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    skill_utils._external_dirs_cache_clear()

    ok, message = restore_skill("config-dangling")

    assert ok is False
    assert "config" in message.lower()
    assert archived.is_dir()
    assert not (skills_home / "skills" / "config-dangling").exists()


def test_restore_skill_moves_back(skills_home):
    from tools.skill_usage import archive_skill, restore_skill, get_record
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "temp-skill")
    archive_skill("temp-skill")
    assert not (skills_dir / "temp-skill").exists()

    ok, msg = restore_skill("temp-skill")
    assert ok, msg
    assert (skills_dir / "temp-skill" / "SKILL.md").exists()
    assert get_record("temp-skill")["state"] == "active"


def test_restore_skill_finds_nested_archive_subdir(skills_home):
    """Skills archived under nested category subdirs (e.g.
    .archive/<category>/<skill>/) — left behind by older archive layouts or
    external imports — must still be restorable by name."""
    from tools.skill_usage import restore_skill, get_record
    skills_dir = skills_home / "skills"
    nested = skills_dir / ".archive" / "openclaw-imports" / "nested-skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested-skill\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("nested-skill")
    assert ok, msg
    assert (skills_dir / "nested-skill" / "SKILL.md").exists()
    assert not nested.exists()
    assert get_record("nested-skill")["state"] == "active"


def test_restore_rejects_legacy_nested_timestamped_archive(skills_home):
    """Old timestamp suffixes lack a reliable original physical basename."""
    from tools.skill_usage import restore_skill
    skills_dir = skills_home / "skills"
    nested = skills_dir / ".archive" / "imports" / "dup-skill-20260101000000"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: dup-skill\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("dup-skill")
    assert not ok
    assert "trustworthy" in msg
    assert nested.exists()


def test_archive_collision_gets_suffix(skills_home):
    from tools.skill_usage import archive_skill
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "dup")
    archive_skill("dup")
    _write_skill(skills_dir, "dup")  # recreate
    ok, msg = archive_skill("dup")
    assert ok
    # The second copy keeps its physical basename beneath a collision container.
    archived = sorted(p.name for p in (skills_dir / ".archive").iterdir() if p.is_dir())
    assert "dup" in archived
    collision_root = skills_dir / ".archive" / ".collisions"
    containers = [p for p in collision_root.iterdir() if p.is_dir()]
    assert len(containers) == 1
    assert (containers[0] / "dup" / "SKILL.md").exists()


def test_restore_does_not_pull_unrelated_sibling_out_of_archive(skills_home):
    """Restoring a name with no exact archive entry must NOT grab a different
    archived skill that merely shares a ``<name>-`` prefix.

    The timestamped-duplicate fallback recognises only the suffix
    ``archive_skill`` writes on a collision (``-YYYYMMDDHHMMSS``). A bare
    ``startswith(f"{name}-")`` also matches sibling skills, so restoring
    ``git`` would rip an archived ``git-helpers`` out of the archive, rename
    it to ``git``, and report success — destroying the sibling's only copy."""
    from tools.skill_usage import (
        archive_skill, restore_skill, list_archived_skill_names, mark_agent_created,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "git-helpers")
    mark_agent_created("git-helpers")
    ok, msg = archive_skill("git-helpers")
    assert ok, msg

    # "git" was never archived; only its prefix-sharing sibling was.
    ok, msg = restore_skill("git")
    assert not ok, f"restore('git') should not match 'git-helpers': {msg}"
    assert "not found" in msg.lower()

    # The sibling must be untouched: still in the archive, never moved to skills/git.
    assert (skills_dir / ".archive" / "git-helpers" / "SKILL.md").exists()
    assert "git-helpers" in list_archived_skill_names()
    assert not (skills_dir / "git").exists()


def test_restore_rejects_legacy_flat_timestamped_duplicate(skills_home):
    """A legacy suffix could also be a user's original physical basename."""
    from tools.skill_usage import restore_skill
    skills_dir = skills_home / "skills"
    dupe = skills_dir / ".archive" / "report-tool-20260101000000"
    dupe.mkdir(parents=True)
    (dupe / "SKILL.md").write_text(
        "---\nname: report-tool\ndescription: x\n---\n", encoding="utf-8",
    )

    ok, msg = restore_skill("report-tool")
    assert not ok
    assert "trustworthy" in msg
    assert dupe.exists()


def test_restore_rejects_legacy_timestamped_dupe_with_unrelated_sibling(skills_home):
    """A sibling never makes an ambiguous legacy suffix safe to restore."""
    from tools.skill_usage import restore_skill
    archive = skills_home / "skills" / ".archive"

    dupe = archive / "report-20260101000000"          # real collision dupe of "report"
    sibling = archive / "report-card"                  # unrelated sibling skill
    for d, frontname in ((dupe, "report"), (sibling, "report-card")):
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {frontname}\ndescription: x\n---\n", encoding="utf-8",
        )

    ok, msg = restore_skill("report")
    assert not ok
    assert "trustworthy" in msg
    assert dupe.exists()
    assert sibling.exists()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Telemetry vs curation — usage is tracked for ALL skills; curation is not
# ---------------------------------------------------------------------------


def test_end_to_end_telemetry_tracked_but_lifecycle_refused(skills_home):
    """The combined guarantee under decoupled telemetry/curation:

    - Usage telemetry (view/use/patch) IS recorded for bundled & hub skills.
    - Lifecycle mutations (set_state, set_pinned, archive) are REFUSED for them
      (with pruning off, the fixture default), so no state/pinned/archived flag
      lands and the directories stay on disk.
    """
    from tools.skill_usage import (
        bump_view, bump_use, bump_patch, set_state, set_pinned,
        archive_skill, load_usage, STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED,
    )
    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "bundled-one")
    _write_skill(skills_dir, "hub-one")
    _write_skill(skills_dir, "mine")

    (skills_dir / ".bundled_manifest").write_text(
        "bundled-one:abc\n", encoding="utf-8",
    )
    hub = skills_dir / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        json.dumps({"installed": {"hub-one": {}}}), encoding="utf-8",
    )

    for name in ("bundled-one", "hub-one"):
        bump_view(name)
        bump_use(name)
        bump_patch(name)
        set_state(name, STATE_STALE)
        set_state(name, STATE_ARCHIVED)
        set_pinned(name, True)
        ok, _msg = archive_skill(name)
        assert not ok, f"archive_skill(\"{name}\") should refuse"

    data = load_usage()
    # Telemetry landed for both.
    for name in ("bundled-one", "hub-one"):
        assert name in data, f"{name} telemetry should be recorded"
        assert data[name]["view_count"] == 1
        assert data[name]["use_count"] == 1
        assert data[name]["patch_count"] == 1
        # But lifecycle mutators were refused — state stays the default, never
        # archived/stale/pinned, and created_by is never agent.
        assert data[name]["state"] == STATE_ACTIVE
        assert data[name]["archived_at"] is None
        assert data[name]["pinned"] is False
        assert data[name].get("created_by") != "agent"

    # Directories must still be in place on disk.
    assert (skills_dir / "bundled-one" / "SKILL.md").exists()
    assert (skills_dir / "hub-one" / "SKILL.md").exists()

    # The agent-created skill can still be mutated normally.
    bump_view("mine")
    assert load_usage()["mine"]["view_count"] == 1


# ---------------------------------------------------------------------------
# Unmanaged enumeration + adoption
#
# A skill only becomes curator-managed when ``created_by: agent`` lands on its
# usage record, and that only happens for background-review creations. Records
# written before the marker existed carry no key at all, and every foreground
# `skill_manage(create)` leaves it unset — both are curation-eligible yet
# invisible to every automatic transition. These tests pin the contract that
# the blind spot is enumerable and that adoption is an explicit declaration:
# never inferred from telemetry, never silently reached by the curator.
# ---------------------------------------------------------------------------

def _seed_usage(skills_dir: Path, records: dict) -> None:
    (skills_dir / ".usage.json").write_text(
        json.dumps(records, indent=1), encoding="utf-8"
    )


def test_adopt_preserves_the_inactivity_clock(skills_home):
    """Adoption must not reset staleness — it hands over an EXISTING history.

    If adopting re-anchored the clock to now, every legacy skill would buy a
    fresh archive_after_days window, which is the opposite of what the user
    wants when they hand over a library they already stopped using.
    """
    from tools.skill_usage import adopt_skill, get_record, latest_activity_at

    skills_dir = skills_home / "skills"
    _write_skill(skills_dir, "legacy")
    _seed_usage(skills_dir, {
        "legacy": {
            "use_count": 5,
            "patch_count": 7,
            "last_used_at": "2026-04-29T00:00:00+00:00",
            "created_at": "2026-04-28T00:00:00+00:00",
        }
    })
    before = latest_activity_at(get_record("legacy"))

    ok, _msg = adopt_skill("legacy")
    assert ok is True
    rec = get_record("legacy")
    assert latest_activity_at(rec) == before
    assert rec["use_count"] == 5
    assert rec["patch_count"] == 7


@pytest.mark.parametrize("kind", ["bundled", "hub", "protected", "missing"])
def test_adopt_refuses_skills_the_user_does_not_own(skills_home, monkeypatch, kind):
    """Adoption writes a provenance claim, so it must refuse anything with an
    external owner rather than stamping a lie onto the record.

    ``prune_builtins`` is forced ON here — the shipped default — because that
    is the configuration in which a bundled skill is otherwise curation-
    eligible. With it off, ``mark_agent_created``'s own eligibility gate would
    block the write and this test would pass without exercising adopt's guard
    at all.
    """
    from tools import skill_usage
    from tools.skill_usage import adopt_skill, load_usage

    monkeypatch.setattr(skill_usage, "_prune_builtins_enabled", lambda: True)

    skills_dir = skills_home / "skills"
    if kind == "bundled":
        name = "bundled-one"
        _write_skill(skills_dir, name)
        (skills_dir / ".bundled_manifest").write_text(f"{name}:abc\n", encoding="utf-8")
    elif kind == "hub":
        name = "hub-one"
        _write_skill(skills_dir, name)
        hub = skills_dir / ".hub"
        hub.mkdir()
        (hub / "lock.json").write_text(
            json.dumps({"installed": {name: {}}}), encoding="utf-8",
        )
    elif kind == "protected":
        name = sorted(skill_usage.PROTECTED_BUILTIN_SKILLS)[0]
        _write_skill(skills_dir, name)
    else:
        name = "no-such-skill"

    ok, _msg = adopt_skill(name)
    assert ok is False
    assert load_usage().get(name, {}).get("created_by") != "agent"


def test_adopt_rejects_empty_name(skills_home):
    from tools.skill_usage import adopt_skill

    assert adopt_skill("")[0] is False
