"""Behavioral coverage for cache-safe session refresh payloads."""

from pathlib import Path


def _write_skill(home: Path, name: str) -> None:
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use {name}.\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_build_soft_refresh_reads_active_profile_memory_and_rescans_skills(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profile"
    memories = profile_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("remember the profile rule", encoding="utf-8")
    (memories / "USER.md").write_text("user prefers concise replies", encoding="utf-8")
    _write_skill(profile_home, "fresh-skill")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    from agent.session_refresh import build_soft_refresh

    result = build_soft_refresh()

    assert "remember the profile rule" in result.context_note
    assert "user prefers concise replies" in result.context_note
    assert any(item["name"] == "fresh-skill" for item in result.skills["added"])
    assert "MEMORY.md" in result.report
    assert "USER.md" in result.report
    assert "fresh-skill" in result.report


def test_build_soft_refresh_handles_missing_and_empty_memory_files(tmp_path, monkeypatch):
    profile_home = tmp_path / "empty-profile"
    (profile_home / "memories").mkdir(parents=True)
    (profile_home / "memories" / "USER.md").write_text("\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    from agent.session_refresh import build_soft_refresh

    result = build_soft_refresh()

    assert "No non-empty MEMORY.md or USER.md content was found" in result.context_note
    assert "MEMORY.md missing or empty" in result.report
    assert "USER.md missing or empty" in result.report


def test_build_soft_refresh_honors_context_local_profile_override(tmp_path, monkeypatch):
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "active-profile"
    for home, text in ((process_home, "wrong profile"), (profile_home, "right profile")):
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "MEMORY.md").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(process_home))

    from agent.session_refresh import build_soft_refresh
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(profile_home)
    try:
        result = build_soft_refresh()
    finally:
        reset_hermes_home_override(token)

    assert "right profile" in result.context_note
    assert "wrong profile" not in result.context_note
