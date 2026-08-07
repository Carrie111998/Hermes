"""Snapshot entries expose folder slug and declared name unambiguously."""

import json
from pathlib import Path

from agent.prompt_builder import (
    _SKILLS_SNAPSHOT_VERSION,
    _build_skills_manifest,
    _build_snapshot_entry,
    _load_skills_snapshot,
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
)


def test_snapshot_entry_exposes_folder_slug_and_declared_name(tmp_path: Path):
    """Folder/declared mismatch keeps both fields plus backward-compat keys."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "mlops" / "models" / "audiocraft"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: audiocraft-audio-generation\ndescription: audio.\n---\n",
        encoding="utf-8",
    )

    entry = _build_snapshot_entry(
        skill_file,
        skills_dir,
        {"name": "audiocraft-audio-generation"},
        "audio.",
    )

    assert entry["folder_slug"] == "audiocraft"
    assert entry["declared_name"] == "audiocraft-audio-generation"
    # Backward compatibility: historical keys remain populated.
    assert entry["skill_name"] == "audiocraft"
    assert entry["frontmatter_name"] == "audiocraft-audio-generation"
    assert entry["category"] == "mlops/models"


def test_snapshot_entry_matches_when_slug_equals_declared(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "demo" / "plain-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: plain-skill\n---\n", encoding="utf-8")

    entry = _build_snapshot_entry(
        skill_file, skills_dir, {"name": "plain-skill"}, "desc"
    )

    assert entry["folder_slug"] == entry["declared_name"] == "plain-skill"
    assert entry["skill_name"] == entry["frontmatter_name"] == "plain-skill"


def test_old_snapshot_version_invalidates_and_rebuilds_identity_fields(
    tmp_path: Path, monkeypatch
):
    """Persisted pre-v3 snapshots are rejected even when the file manifest matches."""
    from agent import prompt_builder as pb

    hermes_home = tmp_path / ".hermes"
    skills_dir = hermes_home / "skills"
    skill_dir = skills_dir / "mlops" / "models" / "audiocraft"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: audiocraft-audio-generation\ndescription: audio.\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(pb, "get_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [skills_dir])
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda *a, **k: set())
    snap_path = hermes_home / ".skills_prompt_snapshot.json"
    monkeypatch.setattr(pb, "_skills_prompt_snapshot_path", lambda: snap_path)

    # Matching mtime/size manifest, but old schema version and no identity aliases.
    old_payload = {
        "version": 2,
        "manifest": _build_skills_manifest(skills_dir),
        "skills": [
            {
                "skill_name": "audiocraft",
                "frontmatter_name": "audiocraft-audio-generation",
                "category": "mlops/models",
                "description": "audio.",
                "platforms": [],
                "conditions": {},
            }
        ],
        "category_descriptions": {},
    }
    snap_path.write_text(json.dumps(old_payload), encoding="utf-8")
    clear_skills_system_prompt_cache()

    assert _SKILLS_SNAPSHOT_VERSION >= 3
    assert _load_skills_snapshot(skills_dir) is None

    prompt = build_skills_system_prompt()
    assert "audiocraft" in prompt

    rebuilt = json.loads(snap_path.read_text(encoding="utf-8"))
    assert rebuilt["version"] == _SKILLS_SNAPSHOT_VERSION
    assert len(rebuilt["skills"]) == 1
    entry = rebuilt["skills"][0]
    assert entry["folder_slug"] == "audiocraft"
    assert entry["declared_name"] == "audiocraft-audio-generation"
    assert entry["skill_name"] == "audiocraft"
    assert entry["frontmatter_name"] == "audiocraft-audio-generation"
