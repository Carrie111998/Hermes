from pathlib import Path

from prompt_toolkit.document import Document


def _make_skill(skills_dir: Path, name: str, body: str = "Do the thing.") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""\
---
name: {name}
description: Description for {name}.
---

# {name}

{body}
""",
        encoding="utf-8",
    )


def _configure_protected_governance(home: Path) -> None:
    (home / "governance").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        """\
skills:
  governance:
    registry_path: governance/skills-registry.yaml
    task_class: ardyn_engineering
    protected_task_classes:
      - ardyn_engineering
""",
        encoding="utf-8",
    )
    (home / "governance" / "skills-registry.yaml").write_text(
        """\
version: 1
skills:
  - name: ToolTrust
    classification: COMPATIBILITY_ONLY
  - name: SafeSkill
    classification: CURRENT
""",
        encoding="utf-8",
    )


def _make_cli():
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj.config = {}
    return obj


def test_show_help_hides_governance_blocked_skills(monkeypatch, tmp_path):
    import agent.skill_commands as skill_commands_mod
    import cli as cli_mod

    home = tmp_path / "home"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _configure_protected_governance(home)
    _make_skill(skills_dir, "ToolTrust", body="blocked")
    _make_skill(skills_dir, "SafeSkill", body="allowed")

    printed: list[str] = []

    class _FakeChatConsole:
        def print(self, *args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

    cli_obj = _make_cli()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(cli_mod, "_skill_commands", None)
    monkeypatch.setattr(skill_commands_mod, "_skill_commands", {})
    monkeypatch.setattr(skill_commands_mod, "_skill_commands_platform", None)
    monkeypatch.setattr(cli_mod, "ChatConsole", _FakeChatConsole)
    monkeypatch.setattr(cli_mod, "_cprint", lambda text: printed.append(str(text)))

    cli_obj.show_help()

    output = "\n".join(printed)
    assert "/safeskill" in output
    assert "/tooltrust" not in output


def test_cli_completer_hides_governance_blocked_skills(monkeypatch, tmp_path):
    import agent.skill_commands as skill_commands_mod
    import cli as cli_mod
    from hermes_cli.commands import SlashCommandCompleter

    home = tmp_path / "home"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _configure_protected_governance(home)
    _make_skill(skills_dir, "ToolTrust", body="blocked")
    _make_skill(skills_dir, "SafeSkill", body="allowed")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(cli_mod, "_skill_commands", None)
    monkeypatch.setattr(skill_commands_mod, "_skill_commands", {})
    monkeypatch.setattr(skill_commands_mod, "_skill_commands_platform", None)

    completer = SlashCommandCompleter(
        skill_commands_provider=lambda: cli_mod.get_skill_commands(),
    )
    completions = list(completer.get_completions(Document("/s"), None))
    texts = {item.text for item in completions}

    assert "safeskill" in texts
    assert "tooltrust" not in texts
