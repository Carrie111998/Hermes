"""Snapshot entries expose folder slug and declared name unambiguously."""

from pathlib import Path

from agent.prompt_builder import _build_snapshot_entry


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
