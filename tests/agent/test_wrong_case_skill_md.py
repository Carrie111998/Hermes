"""Wrong-case skill.md packages are diagnosed but never silently loaded."""

from pathlib import Path

from agent.skill_utils import (
    find_wrong_case_skill_md_files,
    format_wrong_case_skill_md_warning,
    iter_skill_index_files,
)


def test_wrong_case_skill_md_detected_but_not_indexed(tmp_path: Path):
    """Lowercase skill.md is warned about and excluded from SKILL.md index."""
    good = tmp_path / "good-skill"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good-skill\n---\n", encoding="utf-8")

    bad = tmp_path / "grafana-operations"
    bad.mkdir()
    (bad / "skill.md").write_text(
        "---\nname: grafana-operations\ndescription: ops.\n---\n",
        encoding="utf-8",
    )

    indexed = list(iter_skill_index_files(tmp_path, "SKILL.md"))
    wrong = find_wrong_case_skill_md_files(tmp_path)

    assert indexed == [good / "SKILL.md"]
    assert wrong == [bad / "skill.md"]
    warning = format_wrong_case_skill_md_warning(wrong, skills_dir=tmp_path)
    assert "will NOT be loaded" in warning
    assert "grafana-operations" in warning


def test_wrong_case_ignored_under_support_dirs(tmp_path: Path):
    """Archived skill.md under references/ is not an active-package warning."""
    real = tmp_path / "umbrella"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: umbrella\n---\n", encoding="utf-8")
    archived = real / "references" / "old"
    archived.mkdir(parents=True)
    (archived / "skill.md").write_text("---\nname: old\n---\n", encoding="utf-8")

    assert find_wrong_case_skill_md_files(tmp_path) == []


def test_canonical_skill_md_not_reported_as_wrong_case(tmp_path: Path):
    skill = tmp_path / "ok"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: ok\n---\n", encoding="utf-8")

    assert find_wrong_case_skill_md_files(tmp_path) == []
    assert list(iter_skill_index_files(tmp_path, "SKILL.md")) == [skill / "SKILL.md"]
