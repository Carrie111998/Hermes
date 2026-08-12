"""Tests for ``agent.skill_commands.reload_skills``.

Covers the helper that powers ``/reload-skills`` (CLI + gateway slash command).
The helper rescans the skills directory and returns a diff of what changed.
It does NOT invalidate the skills system-prompt cache — skills are invoked
at runtime via ``/skill-name``, ``skills_list``, or ``skill_view`` and don't
need to live in the system prompt.

``added`` and ``removed`` are lists of ``{"name": str, "description": str}``
dicts. Descriptions are truncated to 60 chars.
"""

import shutil
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _write_skill(skills_dir: Path, name: str, description: str = "") -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {description or f'{name} skill'}
            ---
            body
            """
        )
    )
    return skill_dir


@pytest.fixture
def hermes_home(monkeypatch):
    """Isolate HERMES_HOME for ``reload_skills`` tests.

    Rather than popping cache-bearing modules from ``sys.modules``,
    we monkeypatch the module-level ``HERMES_HOME`` / ``SKILLS_DIR``
    constants in place so the isolation is local to this fixture's scope.
    """
    td = tempfile.mkdtemp(prefix="hermes-reload-skills-")
    monkeypatch.setenv("HERMES_HOME", td)
    home = Path(td)
    (home / "skills").mkdir(parents=True, exist_ok=True)

    # Import lazily (inside fixture) so the modules are already resident,
    # then redirect their captured paths at the new temp dir.
    import tools.skills_tool as _st
    import agent.skill_commands as _sc

    monkeypatch.setattr(_st, "HERMES_HOME", home, raising=False)
    monkeypatch.setattr(_st, "SKILLS_DIR", home / "skills", raising=False)
    # Reset the in-process slash-command cache so each test starts from zero.
    monkeypatch.setattr(_sc, "_skill_commands", {}, raising=False)

    yield home

    shutil.rmtree(td, ignore_errors=True)


class TestReloadSkillsHelper:
    """``agent.skill_commands.reload_skills``."""



    def test_detects_removed_skill_carries_description(self, hermes_home):
        from agent.skill_commands import reload_skills

        skill_dir = _write_skill(hermes_home / "skills", "demo", "soon to be gone")
        # First reload: demo present
        first = reload_skills()
        assert first["total"] == 1
        assert first["added"] == [{"name": "demo", "description": "soon to be gone"}]

        # Remove and reload — the description must survive the removal diff
        # (we cached it from the pre-rescan snapshot).
        shutil.rmtree(skill_dir)
        second = reload_skills()

        assert second["removed"] == [{"name": "demo", "description": "soon to be gone"}]
        assert second["added"] == []
        assert second["total"] == 0



    def test_does_not_invalidate_prompt_cache_snapshot(self, hermes_home):
        """reload_skills must NOT delete the skills prompt-cache snapshot.

        Skills are called at runtime — the system prompt doesn't need to
        mention them for the model to use them — so reloading them should
        preserve prefix caching.
        """
        from agent.prompt_builder import _skills_prompt_snapshot_path
        from agent.skill_commands import reload_skills

        snapshot = _skills_prompt_snapshot_path()
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("{}")
        assert snapshot.exists()

        reload_skills()

        assert snapshot.exists(), (
            "prompt cache snapshot should be preserved — skills don't live "
            "in the system prompt so there's no reason to invalidate it"
        )


def test_warmed_import_keeps_profile_skill_catalogs_isolated(tmp_path, monkeypatch):
    """A later context-local profile must not reuse the import-time skills dir."""
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    _write_skill(profile_a / "skills", "alpha-only", "alpha private description")
    _write_skill(profile_b / "skills", "beta-only", "beta private description")
    monkeypatch.setenv("HERMES_HOME", str(profile_a))

    # Warm both modules while profile A is process-global.  This reproduces
    # long-lived multiplexed gateways where profile B is selected afterward.
    import agent.skill_commands as skill_commands
    import tools.skills_tool  # noqa: F401
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    skill_commands._skill_commands = {}
    assert set(skill_commands.scan_skill_commands()) == {"/alpha-only"}

    token = set_hermes_home_override(profile_b)
    try:
        beta = skill_commands.reload_skills()
        catalog = skill_commands.get_skill_commands()
    finally:
        reset_hermes_home_override(token)

    assert set(catalog) == {"/beta-only"}
    assert catalog["/beta-only"]["description"] == "beta private description"
    assert "alpha private description" not in repr(catalog)
    assert beta["added"] == [
        {"name": "beta-only", "description": "beta private description"}
    ]


def test_concurrent_profile_scans_publish_complete_isolated_snapshots(tmp_path, monkeypatch):
    """Readers must see one complete profile/platform snapshot, never a blend."""
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    for index in range(8):
        _write_skill(
            profile_a / "skills", f"alpha-{index}", f"alpha private {index}"
        )
        _write_skill(
            profile_b / "skills", f"beta-{index}", f"beta private {index}"
        )
    monkeypatch.setenv("HERMES_HOME", str(profile_a))

    import agent.skill_commands as skill_commands
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    skill_commands._skill_commands = {}
    expected = {
        profile_a: {f"/alpha-{index}" for index in range(8)},
        profile_b: {f"/beta-{index}" for index in range(8)},
    }

    def read_profile(home: Path) -> list[dict]:
        token = set_hermes_home_override(home)
        try:
            seen = []
            for _ in range(30):
                skill_commands.reload_skills()
                catalog = skill_commands.get_skill_commands()
                seen.append({
                    "keys": set(catalog),
                    "descriptions": {row["description"] for row in catalog.values()},
                })
            return seen
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(read_profile, profile_a),
            pool.submit(read_profile, profile_b),
            pool.submit(read_profile, profile_a),
            pool.submit(read_profile, profile_b),
        ]

    for home, future in zip(
        (profile_a, profile_b, profile_a, profile_b), futures, strict=True
    ):
        for snapshot in future.result():
            assert snapshot["keys"] == expected[home]
            private_prefix = "alpha" if home == profile_a else "beta"
            assert all(desc.startswith(private_prefix) for desc in snapshot["descriptions"])
