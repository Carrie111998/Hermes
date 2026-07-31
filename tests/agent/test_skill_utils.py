"""Tests for agent/skill_utils.py."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.skill_utils import (
    SkillsConfigError,
    discover_all_skill_config_vars,
    extract_skill_config_vars,
    extract_skill_conditions,
    get_disabled_skill_names,
    get_external_skills_dirs,
    is_excluded_skill_path,
    is_external_skill_path,
    is_skill_support_path,
    iter_skill_index_files,
    parse_frontmatter,
    resolve_skill_config_values,
    skill_matches_platform,
    skill_matches_platform_list,
)


def _write_config_skill(
    root: Path,
    name: str,
    *,
    platforms: str = "",
    environments: str = "",
) -> Path:
    """Create a minimal, valid skill that declares one config variable."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Test skill\n"
        f"{platforms}"
        f"{environments}"
        "metadata:\n"
        "  hermes:\n"
        "    config:\n"
        f"      - key: {name}.token\n"
        "        description: A test token\n"
        "---\n",
        encoding="utf-8",
    )
    return skill_dir / "SKILL.md"


def _configure_local_config_discovery(monkeypatch, skills_dir: Path) -> None:
    monkeypatch.setattr("agent.skill_utils.get_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(
        "agent.skill_utils.get_scannable_external_skills_dirs", lambda **_: []
    )
    monkeypatch.setattr("agent.skill_utils.get_disabled_skill_names", lambda: set())


def test_discover_config_vars_refuses_partial_result_for_malformed_skill(
    tmp_path, monkeypatch
):
    skills_dir = tmp_path / "skills"
    _write_config_skill(skills_dir, "valid")
    malformed_dir = skills_dir / "malformed"
    malformed_dir.mkdir(parents=True)
    (malformed_dir / "SKILL.md").write_text(
        "---\nname: malformed\n", encoding="utf-8"
    )
    _configure_local_config_discovery(monkeypatch, skills_dir)

    assert discover_all_skill_config_vars() == []


def test_discover_config_vars_refuses_canonical_skill_symlink(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    _write_config_skill(skills_dir, "valid")
    outside = _write_config_skill(tmp_path / "outside", "linked")
    link_dir = skills_dir / "linked"
    link_dir.mkdir()
    try:
        (link_dir / "SKILL.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    _configure_local_config_discovery(monkeypatch, skills_dir)

    assert discover_all_skill_config_vars() == []


def test_discover_config_vars_applies_platform_and_environment_gates(
    tmp_path, monkeypatch
):
    skills_dir = tmp_path / "skills"
    _write_config_skill(skills_dir, "available")
    _write_config_skill(skills_dir, "other-platform", platforms="platforms: [windows]\n")
    _write_config_skill(skills_dir, "other-environment", environments="environments: [kanban]\n")
    _configure_local_config_discovery(monkeypatch, skills_dir)
    monkeypatch.setattr("agent.skill_utils.sys.platform", "linux")
    monkeypatch.setattr("agent.skill_utils.is_termux", lambda: False)
    monkeypatch.setattr("agent.skill_utils._detect_environment", lambda _: False)

    assert [item["key"] for item in discover_all_skill_config_vars()] == [
        "available.token"
    ]


def test_discover_config_vars_refuses_partial_result_on_reader_error(
    tmp_path, monkeypatch
):
    skills_dir = tmp_path / "skills"
    _write_config_skill(skills_dir, "valid")
    broken = _write_config_skill(skills_dir, "broken")
    _configure_local_config_discovery(monkeypatch, skills_dir)

    from agent import skill_utils

    real_reader = skill_utils.read_strict_skill_index_file

    def fail_for_broken(path):
        if path == broken:
            raise OSError("simulated read failure")
        return real_reader(path)

    monkeypatch.setattr(skill_utils, "read_strict_skill_index_file", fail_for_broken)

    assert discover_all_skill_config_vars() == []


def test_discover_config_vars_refuses_malformed_external_scope(
    tmp_path, monkeypatch
):
    skills_dir = tmp_path / "skills"
    _write_config_skill(skills_dir, "valid")
    monkeypatch.setattr("agent.skill_utils.get_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(
        "agent.skill_utils._resolve_external_skills_dirs",
        lambda **_: (_ for _ in ()).throw(
            SkillsConfigError("malformed skills.external_dirs")
        ),
    )
    monkeypatch.setattr(
        "agent.skill_utils.get_disabled_skill_names", lambda: set()
    )

    assert discover_all_skill_config_vars() == []


def test_metadata_as_dict_with_hermes():
    """Normal case: metadata is a dict containing hermes keys."""
    frontmatter = {
        "metadata": {
            "hermes": {
                "fallback_for_toolsets": ["toolset_a"],
                "requires_toolsets": ["toolset_b"],
                "fallback_for_tools": ["tool_x"],
                "requires_tools": ["tool_y"],
            }
        }
    }
    result = extract_skill_conditions(frontmatter)
    assert result["fallback_for_toolsets"] == ["toolset_a"]
    assert result["requires_toolsets"] == ["toolset_b"]
    assert result["fallback_for_tools"] == ["tool_x"]
    assert result["requires_tools"] == ["tool_y"]










def test_iter_skill_index_files_breaks_directory_symlink_cycles(tmp_path):
    skill = tmp_path / "real-skill"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text("---\nname: real-skill\n---\n", encoding="utf-8")
    try:
        (skill / "loop").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    assert list(iter_skill_index_files(tmp_path, "SKILL.md")) == [skill_md]


def test_iter_skill_index_files_reports_walk_and_stat_errors(tmp_path, monkeypatch):
    from agent import skill_utils

    walk_error = OSError("walk failed")
    stat_error = OSError("stat failed")
    errors = []

    def fake_walk(root, *, followlinks, onerror):
        assert root == str(tmp_path)
        assert followlinks is True
        onerror(walk_error)
        yield str(tmp_path / "unreadable"), [], []

    def fake_stat(path, *, follow_symlinks):
        assert path == str(tmp_path / "unreadable")
        assert follow_symlinks is True
        raise stat_error

    monkeypatch.setattr(
        skill_utils,
        "os",
        SimpleNamespace(walk=fake_walk, stat=fake_stat, path=os.path),
    )

    assert list(
        iter_skill_index_files(tmp_path, "SKILL.md", on_error=errors.append)
    ) == []
    assert errors == [walk_error, stat_error]


def test_skill_environment_fingerprint_tracks_dynamic_kanban_env(monkeypatch):
    from agent.skill_utils import get_skill_environment_fingerprint

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    with patch(
        "tools.kanban_tools._profile_has_kanban_toolset",
        return_value=False,
    ):
        initial = dict(get_skill_environment_fingerprint())
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
        active = dict(get_skill_environment_fingerprint())
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        restored = dict(get_skill_environment_fingerprint())

    assert initial["kanban"] is False
    assert active["kanban"] is True
    assert restored["kanban"] is False


def test_skill_config_helpers_share_raw_config_parse_cache(tmp_path, monkeypatch):
    """Repeated skill config helpers should parse config.yaml only once."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    external = tmp_path / "external-skills"
    external.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        f"""
skills:
  disabled:
    - hidden-skill
  external_dirs:
    - {external}
  config:
    wiki:
      path: ~/wiki
""".strip(),
        encoding="utf-8",
    )
    parse_count = 0
    real_yaml_load = skill_utils.yaml_load

    def counting_yaml_load(text):
        nonlocal parse_count
        parse_count += 1
        return real_yaml_load(text)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()
    getattr(skill_utils, "_raw_config_cache_clear", lambda: None)()
    monkeypatch.setattr(skill_utils, "yaml_load", counting_yaml_load)

    assert get_disabled_skill_names() == {"hidden-skill"}
    assert get_external_skills_dirs() == [external.resolve()]
    assert resolve_skill_config_values([
        {"key": "wiki.path", "description": "Wiki path"}
    ])["wiki.path"].endswith("/wiki")
    assert parse_count == 1




def test_strict_skills_scope_rejects_malformed_config_but_ordinary_lookup_is_compatible(
    tmp_path, monkeypatch
):
    """Mutation scans can detect config loss without breaking read-only callers."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "skills:\n  external_dirs: [\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()

    # Historical best-effort callers retain their empty-list behavior.
    assert skill_utils.get_external_skills_dirs() == []
    with pytest.raises(skill_utils.SkillsConfigError, match="config.yaml"):
        skill_utils.get_all_skills_dirs(require_valid_config=True)


def test_strict_skills_scope_rejects_unreadable_config(tmp_path, monkeypatch):
    """A stat-able config that cannot be read must not look like no externals."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("skills: {}\n", encoding="utf-8")
    real_read_text = Path.read_text

    def deny_config_read(path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("permission denied")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()
    monkeypatch.setattr(Path, "read_text", deny_config_read)

    assert skill_utils.get_external_skills_dirs() == []
    with pytest.raises(skill_utils.SkillsConfigError, match="permission denied"):
        skill_utils.get_all_skills_dirs(require_valid_config=True)


@pytest.mark.parametrize(
    "yaml_value",
    [
        "false",
        "0",
        "{}",
        "[false]",
        "['']",
    ],
)
def test_strict_skills_scope_rejects_falsey_non_path_types(
    tmp_path, monkeypatch, yaml_value
):
    """Falsey YAML scalars/mappings must not collapse to an empty strict scope."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"skills:\n  external_dirs: {yaml_value}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()

    # Ordinary discovery keeps its historical best-effort result, including
    # stringifying legacy list entries. Only strict mutation scope hardens it.
    expected_ordinary = (
        [hermes_home / "False"] if yaml_value == "[false]" else []
    )
    assert skill_utils.get_external_skills_dirs() == expected_ordinary
    with pytest.raises(skill_utils.SkillsConfigError, match="external_dirs"):
        skill_utils.get_all_skills_dirs(require_valid_config=True)


def test_strict_skills_scope_rejects_dangling_config_symlink(
    tmp_path, monkeypatch
):
    """A dangling config entry exists and cannot mean an absent configuration."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    try:
        config_path.symlink_to(hermes_home / "missing-config.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()

    assert skill_utils.get_external_skills_dirs() == []
    with pytest.raises(skill_utils.SkillsConfigError, match="config.yaml"):
        skill_utils.get_all_skills_dirs(require_valid_config=True)


@pytest.mark.parametrize("yaml_value", ["null", "''", "[]"])
def test_strict_skills_scope_accepts_explicit_empty_values(
    tmp_path, monkeypatch, yaml_value
):
    """Only the documented null/empty-string/empty-list forms mean no externals."""
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"skills:\n  external_dirs: {yaml_value}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()

    assert skill_utils.get_all_skills_dirs(require_valid_config=True) == [
        hermes_home / "skills"
    ]


def test_is_external_skill_path_matches_configured_external_dir(tmp_path, monkeypatch):
    from agent import skill_utils

    hermes_home = tmp_path / ".hermes"
    local_skills = hermes_home / "skills"
    external = tmp_path / "external-skills"
    local_skills.mkdir(parents=True)
    external.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {external}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skill_utils._external_dirs_cache_clear()

    assert is_external_skill_path(external / "team-skill" / "SKILL.md") is True
    assert is_external_skill_path(local_skills / "local-skill" / "SKILL.md") is False


def test_iter_skill_index_files_prunes_skill_support_dirs(tmp_path):
    """Archived package SKILL.md files under support dirs are not active skills."""
    real = tmp_path / "umbrella"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: umbrella\n---\n", encoding="utf-8")

    package = real / "references" / "old-skill-package"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: old-skill\n---\n", encoding="utf-8")
    (package / "DESCRIPTION.md").write_text(
        "---\ndescription: archived package\n---\n", encoding="utf-8"
    )

    script_package = real / "scripts" / "helper-skill"
    script_package.mkdir(parents=True)
    (script_package / "SKILL.md").write_text("---\nname: helper\n---\n", encoding="utf-8")

    found = list(iter_skill_index_files(tmp_path, "SKILL.md"))
    desc_found = list(iter_skill_index_files(tmp_path, "DESCRIPTION.md"))

    assert found == [real / "SKILL.md"]
    assert desc_found == []
    assert is_skill_support_path(package / "SKILL.md") is True
    assert is_excluded_skill_path(package / "SKILL.md") is True


def test_iter_skill_index_files_keeps_support_named_categories(tmp_path):
    """A category named scripts/templates/assets/references is still valid."""
    scripts_skill = tmp_path / "scripts" / "bash-helper"
    scripts_skill.mkdir(parents=True)
    (scripts_skill / "SKILL.md").write_text(
        "---\nname: bash-helper\n---\n", encoding="utf-8"
    )

    templates_skill = tmp_path / "templates" / "deck-template"
    templates_skill.mkdir(parents=True)
    (templates_skill / "SKILL.md").write_text(
        "---\nname: deck-template\n---\n", encoding="utf-8"
    )

    found = list(iter_skill_index_files(tmp_path, "SKILL.md"))

    assert found == [scripts_skill / "SKILL.md", templates_skill / "SKILL.md"]
    assert is_skill_support_path(scripts_skill / "SKILL.md") is False
    assert is_excluded_skill_path(scripts_skill / "SKILL.md") is False


def test_skill_support_path_uses_explicit_discovery_root_not_cwd(tmp_path, monkeypatch):
    discovery_root = tmp_path / "site-packages" / "skills"
    umbrella = discovery_root / "category" / "umbrella"
    nested = umbrella / "references" / "archived" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    (umbrella / "SKILL.md").write_text("---\nname: umbrella\n---\n", encoding="utf-8")
    nested.write_text("---\nname: archived\n---\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relative = nested.relative_to(discovery_root)
    assert is_skill_support_path(relative, root=discovery_root) is True
    assert is_excluded_skill_path(relative, root=discovery_root) is True


# ── skill_matches_platform on Termux ──────────────────────────────────────


class TestSkillMatchesPlatformTermux:
    """Termux is Linux userland on Android. Skills tagged platforms:[linux]
    must load there regardless of whether Python reports sys.platform as
    "linux" (pre-3.13) or "android" (3.13+). Reported by user @LikiusInik
    in May 2026 — only 3 built-in skills appeared on Termux because every
    github/productivity/mlops skill is tagged platforms:[linux,macos,windows]
    and sys.platform=="android" did not start with "linux".
    """

    def test_no_platforms_field_matches_everywhere(self):
        # Backward-compat default — skills without a platforms tag load
        # on any OS, Termux included.
        with patch("agent.skill_utils.sys.platform", "android"), patch(
            "agent.skill_utils.is_termux", return_value=True
        ):
            assert skill_matches_platform({}) is True
            assert skill_matches_platform({"name": "foo"}) is True







    def test_non_termux_android_does_not_widen(self):
        # If we're somehow on a plain Android Python (not Termux), don't
        # silently load Linux skills — Termux is the supported environment.
        fm = {"platforms": ["linux"]}
        with patch("agent.skill_utils.sys.platform", "android"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            assert skill_matches_platform(fm) is False
            assert skill_matches_platform_list(fm["platforms"]) is False

    def test_linux_skill_on_real_linux_unaffected(self):
        # The non-Termux Linux path must not change.
        fm = {"platforms": ["linux"]}
        with patch("agent.skill_utils.sys.platform", "linux"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            assert skill_matches_platform(fm) is True
            assert skill_matches_platform_list(fm["platforms"]) is True



class TestNormalizeSkillLookupName:
    def test_active_profile_root_is_resolved_at_call_time(self, tmp_path):
        from agent.skill_utils import normalize_skill_lookup_name
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        skill_a = home_a / "skills" / "a-skill"
        skill_b = home_b / "skills" / "b-skill"
        skill_a.mkdir(parents=True)
        skill_b.mkdir(parents=True)

        token = set_hermes_home_override(str(home_a))
        try:
            assert normalize_skill_lookup_name(str(skill_a)) == "a-skill"
        finally:
            reset_hermes_home_override(token)

        token = set_hermes_home_override(str(home_b))
        try:
            assert normalize_skill_lookup_name(str(skill_b)) == "b-skill"
            assert normalize_skill_lookup_name(str(skill_a)) == str(skill_a)
        finally:
            reset_hermes_home_override(token)

    def test_relative_path_unchanged(self, tmp_path, monkeypatch):
        from agent.skill_utils import normalize_skill_lookup_name

        # Relative identifiers early-return before any root lookup.
        assert normalize_skill_lookup_name("foo/bar") == "foo/bar"


    def test_absolute_via_symlink_uses_lexical_relative_path(self, tmp_path, monkeypatch):
        from agent.skill_utils import normalize_skill_lookup_name

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        external = tmp_path / "external" / "my-skill"
        external.mkdir(parents=True)
        link = skills_dir / "my-skill"
        try:
            link.symlink_to(external)
        except OSError:
            pytest.skip("Symlinks not supported")
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
        assert normalize_skill_lookup_name(str(link)) == "my-skill"



# ── parse_frontmatter: UTF-8 BOM tolerance ─────────────────────────────────


class TestParseFrontmatterBOM:
    """A UTF-8 BOM (U+FEFF) on a Windows-saved SKILL.md must not defeat
    frontmatter parsing.

    Notepad and PowerShell ``>`` prepend a BOM when saving UTF-8;
    ``read_text(encoding="utf-8")`` (what ``_parse_skill_file`` uses) keeps
    it, so the bytes handed to ``parse_frontmatter`` start with a BOM ahead of
    the ``---`` fence. Before the fix the ``startswith("---")`` check returned
    False and the whole frontmatter was silently dropped — the skill loaded
    nameless, platform gating fell open, and env-var/config setup never fired.
    """

    SKILL = (
        "---\n"
        "name: my-skill\n"
        "description: Does a thing.\n"
        "platforms: [macos]\n"
        "metadata:\n"
        "  hermes:\n"
        "    config:\n"
        "      - key: my.key\n"
        "        description: A configured value\n"
        "---\n\n"
        "# My Skill\n\nBody text.\n"
    )

    def test_bom_frontmatter_matches_plain(self):
        plain_fm, plain_body = parse_frontmatter(self.SKILL)
        bom_fm, bom_body = parse_frontmatter("\ufeff" + self.SKILL)
        assert bom_fm == plain_fm
        assert bom_body == plain_body
        assert bom_fm["name"] == "my-skill"
        assert bom_fm["description"] == "Does a thing."




    def test_bom_platform_gating_regression(self):
        # The concrete harm: a macOS-only skill must stay hidden on non-macOS
        # whether or not the file carries a BOM. Empty frontmatter (the bug)
        # reads as "no platform restriction" and leaks the skill everywhere.
        with patch("agent.skill_utils.sys.platform", "win32"), patch(
            "agent.skill_utils.is_termux", return_value=False
        ):
            plain_fm, _ = parse_frontmatter(self.SKILL)
            bom_fm, _ = parse_frontmatter("\ufeff" + self.SKILL)
            assert skill_matches_platform(plain_fm) is False
            assert skill_matches_platform(bom_fm) is False


    def test_real_file_read_path(self, tmp_path):
        # End-to-end: write the file the way a Windows editor does (utf-8-sig
        # emits a BOM), read it the way _parse_skill_file does (plain utf-8),
        # and confirm the frontmatter survives the round trip.
        f = tmp_path / "SKILL.md"
        f.write_text(self.SKILL, encoding="utf-8-sig")
        raw = f.read_text(encoding="utf-8")
        assert raw.startswith("\ufeff")  # BOM really is present on disk
        fm, _ = parse_frontmatter(raw)
        assert fm["name"] == "my-skill"
        assert fm["platforms"] == ["macos"]


class TestBOMToleranceSiblingSites:
    """The BOM fix must cover every independent frontmatter parser, not just
    the canonical ``parse_frontmatter`` — several modules reimplement the
    ``---`` fence check locally."""

    SKILL = "---\nname: bom-skill\ndescription: Saved by Notepad\n---\n\n# Body\n"


    def test_prompt_builder_strips_bom_frontmatter(self):
        # A BOM'd context file (AGENTS.md etc.) must not leak raw
        # frontmatter into the system prompt.
        from agent.prompt_builder import _strip_yaml_frontmatter

        out = _strip_yaml_frontmatter("\ufeff---\nfoo: bar\n---\nBody text\n")
        assert out.strip() == "Body text"

    def test_blueprints_split_frontmatter_bom(self):
        # str.lstrip() does NOT strip U+FEFF (it is not whitespace), so the
        # pre-existing lstrip() in _split_frontmatter never covered it.
        from tools.blueprints import _split_frontmatter

        fm = _split_frontmatter("\ufeff---\nname: bp\n---\nbody")
        assert fm is not None
        assert fm.get("name") == "bp"
