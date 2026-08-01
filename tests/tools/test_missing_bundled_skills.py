"""Diagnostic for source/manifest-present but installed-missing bundled skills."""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from hermes_cli.doctor import report_missing_bundled_skills_check
from hermes_cli.skills_hub import _report_missing_bundled_skills
from tools.skills_sync import (
    ACTIONABLE_MISSING_BUNDLED_PROVENANCE,
    INTENTIONAL_MISSING_BUNDLED_PROVENANCE,
    format_missing_bundled_restore_guidance,
    is_actionable_missing_bundled_provenance,
    list_missing_bundled_skills,
    partition_missing_bundled_skills,
    provenance_counts,
    _write_manifest,
)


def _make_bundled(bundled: Path, folder: str, declared: str) -> Path:
    skill_dir = bundled / "research" / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared}\ndescription: test.\n---\nbody\n",
        encoding="utf-8",
    )
    return skill_dir


def _entry(name: str, provenance: str) -> dict:
    return {
        "name": name,
        "folder_slug": name,
        "bundled_src": None,
        "expected_dest": Path(name),
        "in_manifest": provenance != "source_present_never_installed",
        "in_source": provenance != "manifest_orphan_no_source",
        "provenance": provenance,
    }


def test_missing_bundled_reports_provenance(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir(parents=True)
    bundled = tmp_path / "bundled_skills"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    suppressed_src = _make_bundled(bundled, "suppressed-skill", "suppressed-skill")
    tracked_src = _make_bundled(bundled, "tracked-absent", "tracked-absent")
    never_src = _make_bundled(bundled, "never-installed", "never-installed")
    present_src = _make_bundled(bundled, "present-skill", "present-skill")

    # One skill is actually installed.
    present_dest = skills_dir / "research" / "present-skill"
    present_dest.mkdir(parents=True)
    (present_dest / "SKILL.md").write_text(
        (present_src / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    manifest_file = skills_dir / ".bundled_manifest"
    with patch("tools.skills_sync.MANIFEST_FILE", manifest_file), patch(
        "tools.skills_sync.SKILLS_DIR", skills_dir
    ), patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
        "tools.skills_sync._get_bundled_dir", return_value=bundled
    ), patch(
        "tools.skills_sync._read_suppressed_names",
        return_value={"suppressed-skill"},
    ):
        _write_manifest(
            {
                "tracked-absent": "abc",
                "present-skill": "def",
                "suppressed-skill": "ghi",
            }
        )
        missing = list_missing_bundled_skills()

    by_name = {e["name"]: e for e in missing}
    assert "present-skill" not in by_name
    assert by_name["suppressed-skill"]["provenance"] == "curator_suppressed"
    assert by_name["suppressed-skill"]["in_manifest"] is True
    assert by_name["suppressed-skill"]["in_source"] is True
    assert by_name["tracked-absent"]["provenance"] == "manifest_tracked_absent"
    assert by_name["never-installed"]["provenance"] == "source_present_never_installed"
    assert by_name["never-installed"]["folder_slug"] == "never-installed"
    assert by_name["tracked-absent"]["bundled_src"] == tracked_src
    assert suppressed_src.exists() and never_src.exists()


def test_missing_bundled_opt_out_provenance(tmp_path: Path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir(parents=True)
    (hermes_home / ".no-bundled-skills").write_text("opted out\n", encoding="utf-8")
    bundled = tmp_path / "bundled_skills"
    _make_bundled(bundled, "seed-me", "seed-me")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with patch("tools.skills_sync.MANIFEST_FILE", skills_dir / ".bundled_manifest"), patch(
        "tools.skills_sync.SKILLS_DIR", skills_dir
    ), patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
        "tools.skills_sync._get_bundled_dir", return_value=bundled
    ), patch("tools.skills_sync._read_suppressed_names", return_value=set()):
        missing = list_missing_bundled_skills()

    assert len(missing) == 1
    assert missing[0]["name"] == "seed-me"
    assert missing[0]["provenance"] == "opt_out"


def test_actionable_provenance_classification():
    for prov in INTENTIONAL_MISSING_BUNDLED_PROVENANCE:
        assert is_actionable_missing_bundled_provenance(prov) is False
    for prov in ACTIONABLE_MISSING_BUNDLED_PROVENANCE:
        assert is_actionable_missing_bundled_provenance(prov) is True
    assert is_actionable_missing_bundled_provenance(None) is True
    assert is_actionable_missing_bundled_provenance("") is True
    assert is_actionable_missing_bundled_provenance("weird") is True


def test_partition_and_provenance_counts():
    missing = [
        _entry("a", "opt_out"),
        _entry("b", "curator_suppressed"),
        _entry("c", "manifest_tracked_absent"),
        _entry("d", "unknown"),
    ]
    intentional, actionable = partition_missing_bundled_skills(missing)
    assert {e["name"] for e in intentional} == {"a", "b"}
    assert {e["name"] for e in actionable} == {"c", "d"}
    assert provenance_counts(missing) == {
        "opt_out": 1,
        "curator_suppressed": 1,
        "manifest_tracked_absent": 1,
        "unknown": 1,
    }


def test_restore_guidance_all_intentional_does_not_recommend_restore():
    missing = [
        _entry("a", "opt_out"),
        _entry("b", "curator_suppressed"),
    ]
    text = format_missing_bundled_restore_guidance(missing)
    assert "opt_out" in text
    assert "curator_suppressed" in text
    assert "manifest_tracked_absent" in text  # category legend still present
    assert "restore is not recommended" in text
    assert "hermes skills reset" not in text


def test_restore_guidance_actionable_scopes_restore_away_from_intentional():
    missing = [
        _entry("a", "opt_out"),
        _entry("b", "manifest_tracked_absent"),
        _entry("c", "source_present_never_installed"),
    ]
    text = format_missing_bundled_restore_guidance(missing)
    assert "Restore unexpected absences" in text
    assert "hermes skills reset <name> --restore" in text
    assert "Do not restore opt_out or curator_suppressed" in text
    # Must not use the old blanket "when intentional absence is ruled out" phrasing
    assert "when intentional absence is ruled out" not in text


def test_doctor_all_intentional_is_ok_not_warn(capsys):
    missing = [
        _entry("opted", "opt_out"),
        _entry("pruned", "curator_suppressed"),
    ]
    severity = report_missing_bundled_skills_check(missing)
    out = capsys.readouterr().out
    assert severity == "ok_intentional"
    assert "Bundled skill absences are intentional" in out
    assert "opt_out=1" in out
    assert "curator_suppressed=1" in out
    assert "intentional absence(s)" in out
    assert "⚠" not in out


def test_doctor_actionable_absence_warns(capsys):
    missing = [
        _entry("opted", "opt_out"),
        _entry("gone", "manifest_tracked_absent"),
        _entry("never", "source_present_never_installed"),
        _entry("orphan", "manifest_orphan_no_source"),
        _entry("mystery", "unknown"),
    ]
    severity = report_missing_bundled_skills_check(missing)
    out = capsys.readouterr().out
    assert severity == "warn_actionable"
    assert "⚠" in out
    assert "bundled skill(s) missing from install" in out
    assert "unexpected=4" in out
    assert "opt_out=1" in out  # full provenance counts still shown


def test_doctor_no_missing_is_ok_match(capsys):
    severity = report_missing_bundled_skills_check([])
    out = capsys.readouterr().out
    assert severity == "ok_match"
    assert "Bundled skills installed match source/manifest baseline" in out
    assert "⚠" not in out


def test_skills_check_lists_all_provenance_and_scoped_restore(monkeypatch):
    missing = [
        _entry("opted", "opt_out"),
        _entry("pruned", "curator_suppressed"),
        _entry("gone", "manifest_tracked_absent"),
    ]
    monkeypatch.setattr(
        "tools.skills_sync.list_missing_bundled_skills",
        lambda: missing,
    )
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    count = _report_missing_bundled_skills(console)
    # Rich wraps long dim footers; compare on whitespace-normalized text.
    out = " ".join(sink.getvalue().split())
    assert count == 3
    assert "opted" in out and "opt_out" in out
    assert "pruned" in out and "curator_suppressed" in out
    assert "gone" in out and "manifest_tracked_absent" in out
    assert "Restore unexpected absences" in out
    assert "Do not restore opt_out or curator_suppressed" in out
    assert "when intentional absence is ruled out" not in out


def test_skills_check_all_intentional_guidance(monkeypatch):
    missing = [
        _entry("opted", "opt_out"),
        _entry("pruned", "curator_suppressed"),
    ]
    monkeypatch.setattr(
        "tools.skills_sync.list_missing_bundled_skills",
        lambda: missing,
    )
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    count = _report_missing_bundled_skills(console)
    out = " ".join(sink.getvalue().split())
    assert count == 2
    assert "opted" in out and "pruned" in out
    assert "restore is not recommended" in out
    assert "hermes skills reset" not in out
