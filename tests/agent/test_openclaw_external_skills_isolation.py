import importlib
from pathlib import Path


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_config(hermes_home: Path, body: str) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "skills").mkdir(exist_ok=True)
    (hermes_home / "config.yaml").write_text(body, encoding="utf-8")


def _reload_skill_utils(monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import agent.skill_utils as skill_utils

    importlib.reload(skill_utils)
    skill_utils._external_dirs_cache_clear()
    return skill_utils


def test_openclaw_external_skills_dir_skipped_by_default(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    openclaw_skills = tmp_path / ".openclaw" / "skills"
    _write_skill(openclaw_skills, "openclaw-only")
    _write_config(
        hermes_home,
        "skills:\n"
        "  external_dirs:\n"
        f"    - {openclaw_skills}\n",
    )

    skill_utils = _reload_skill_utils(monkeypatch, hermes_home)

    assert skill_utils.get_external_skills_dirs() == []


def test_openclaw_external_skills_dir_allowed_by_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    openclaw_skills = tmp_path / ".openclaw" / "skills"
    _write_skill(openclaw_skills, "openclaw-only")
    _write_config(
        hermes_home,
        "skills:\n"
        "  allow_openclaw_external_dirs: true\n"
        "  external_dirs:\n"
        f"    - {openclaw_skills}\n",
    )

    skill_utils = _reload_skill_utils(monkeypatch, hermes_home)

    assert skill_utils.get_external_skills_dirs() == [openclaw_skills.resolve()]


def test_non_openclaw_external_skills_still_work(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    external_skills = tmp_path / "shared-skills"
    _write_skill(external_skills, "legit-skill")
    _write_config(
        hermes_home,
        "skills:\n"
        "  external_dirs:\n"
        f"    - {external_skills}\n",
    )

    skill_utils = _reload_skill_utils(monkeypatch, hermes_home)

    assert skill_utils.get_external_skills_dirs() == [external_skills.resolve()]


def test_mixed_external_dirs_skip_openclaw_but_keep_normal(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    openclaw_skills = tmp_path / ".openclaw" / "skills"
    external_skills = tmp_path / "shared-skills"
    _write_skill(openclaw_skills, "openclaw-only")
    _write_skill(external_skills, "legit-skill")
    _write_config(
        hermes_home,
        "skills:\n"
        "  external_dirs:\n"
        f"    - {openclaw_skills}\n"
        f"    - {external_skills}\n",
    )

    skill_utils = _reload_skill_utils(monkeypatch, hermes_home)

    assert skill_utils.get_external_skills_dirs() == [external_skills.resolve()]


def test_is_openclaw_owned_detects_marker_in_parent(tmp_path):
    openclaw_home = tmp_path / "OpenClawHome"
    (openclaw_home / "skills").mkdir(parents=True)
    (openclaw_home / "openclaw.json").write_text("{}", encoding="utf-8")

    from agent.skill_utils import _is_openclaw_owned

    assert _is_openclaw_owned(openclaw_home / "skills")


def test_is_openclaw_owned_does_not_walk_past_home(tmp_path, monkeypatch):
    fake_root = tmp_path / "root"
    home = fake_root / "home"
    skill_dir = home / "project" / "skills"
    skill_dir.mkdir(parents=True)
    (fake_root / "openclaw.json").write_text("{}", encoding="utf-8")

    from agent.skill_utils import _is_openclaw_owned

    monkeypatch.setattr(Path, "home", lambda: home)

    assert not _is_openclaw_owned(skill_dir)
