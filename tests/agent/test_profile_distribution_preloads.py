"""Profile-distribution preloaded-skill system-prompt tests."""
from __future__ import annotations

from types import SimpleNamespace

from agent import system_prompt
from hermes_cli.profile_distribution import DistributionManifest, write_manifest


def _profile_with_preload(tmp_path, *, preload=True):
    home = tmp_path / "profile"
    skill_dir = home / "skills" / "always-on-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: always-on-test\n"
        "description: Always active test skill.\n"
        "---\n"
        "# Always On Test\n\n"
        "PROFILE_PRELOAD_MARKER\n",
        encoding="utf-8",
    )
    write_manifest(
        home,
        DistributionManifest(
            name="preload-profile",
            version="0.1.0",
            preload_skills=["always-on-test"] if preload else [],
        ),
    )
    return home


def _agent(home):
    db = SimpleNamespace(db_path=str(home / "state.db"))
    return SimpleNamespace(_session_db=db, session_id="preload-session")


def test_distribution_preload_renders_full_skill_from_profile_home(tmp_path):
    home = _profile_with_preload(tmp_path)
    prompt = system_prompt._distribution_preloaded_skills_prompt(_agent(home))

    assert 'profile distribution preloads the "always-on-test" skill' in prompt
    assert "PROFILE_PRELOAD_MARKER" in prompt
    assert "Treat its instructions as active guidance for this profile on every turn" in prompt


def test_distribution_without_preloads_adds_no_prompt(tmp_path):
    home = _profile_with_preload(tmp_path, preload=False)
    assert system_prompt._distribution_preloaded_skills_prompt(_agent(home)) == ""


def test_distribution_preload_does_not_resolve_from_ambient_profile(tmp_path, monkeypatch):
    target = _profile_with_preload(tmp_path / "target")
    ambient = _profile_with_preload(tmp_path / "ambient", preload=False)
    monkeypatch.setenv("HERMES_HOME", str(ambient))

    prompt = system_prompt._distribution_preloaded_skills_prompt(_agent(target))
    assert "PROFILE_PRELOAD_MARKER" in prompt
