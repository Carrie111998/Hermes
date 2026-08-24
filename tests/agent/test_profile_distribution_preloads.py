"""Profile-distribution preloaded-skill system-prompt tests."""
from __future__ import annotations

from types import SimpleNamespace

from agent import skill_commands, system_prompt
from hermes_cli.profile_distribution import DistributionManifest, write_manifest


def _profile_with_preload(tmp_path, *, preload=True, extra_content=""):
    home = tmp_path / "profile"
    skill_dir = home / "skills" / "always-on-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: always-on-test\n"
        "description: Always active test skill.\n"
        "---\n"
        "# Always On Test\n\n"
        "PROFILE_PRELOAD_MARKER\n"
        f"{extra_content}",
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


def test_distribution_preload_never_executes_inline_shell_during_prompt_build(tmp_path, monkeypatch):
    literal = "!`printf PROFILE_PRELOAD_MUST_NOT_EXECUTE`"
    home = _profile_with_preload(tmp_path, extra_content=f"{literal}\n")

    monkeypatch.setattr(
        skill_commands,
        "_load_skills_config",
        lambda: {
            "template_vars": True,
            "inline_shell": True,
            "inline_shell_timeout": 10,
        },
    )

    def _unexpected_inline_shell(*_args, **_kwargs):
        raise AssertionError("automatic profile preload attempted inline shell execution")

    monkeypatch.setattr(skill_commands, "_expand_inline_shell", _unexpected_inline_shell)

    prompt = system_prompt._distribution_preloaded_skills_prompt(_agent(home))
    assert literal in prompt
    assert "PROFILE_PRELOAD_MARKER" in prompt


def test_explicit_skill_render_can_still_opt_into_existing_inline_shell_behavior(monkeypatch):
    calls = []
    monkeypatch.setattr(
        skill_commands,
        "_load_skills_config",
        lambda: {
            "template_vars": False,
            "inline_shell": True,
            "inline_shell_timeout": 7,
        },
    )

    def _expand(content, skill_dir, timeout):
        calls.append((content, skill_dir, timeout))
        return "EXPANDED_INLINE_SHELL"

    monkeypatch.setattr(skill_commands, "_expand_inline_shell", _expand)

    rendered = skill_commands._build_skill_message(
        {"content": "!`printf explicit`"},
        None,
        "explicit preload",
        allow_inline_shell=True,
    )

    assert "EXPANDED_INLINE_SHELL" in rendered
    assert calls == [("!`printf explicit`", None, 7)]
