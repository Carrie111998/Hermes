"""Behavior contracts for the UX/UI wireframe architect skill."""

from agent.skill_utils import parse_frontmatter
from tools.skills_hub import OptionalSkillSource


IDENTIFIER = "official/web-development/ux-ui-wireframe-architect"
SUPPORT_FILES = {
    "SKILL.md",
    "references/universal-rules.md",
    "references/specialized-patterns.md",
    "references/output-templates.md",
}


def test_official_source_discovers_wireframe_skill():
    source = OptionalSkillSource()

    meta = source.inspect(IDENTIFIER)

    assert meta is not None
    assert meta.name == "ux-ui-wireframe-architect"
    assert meta.source == "official"
    assert meta.trust_level == "builtin"
    assert {"wireframe", "mobile-first", "dashboard", "forms"}.issubset(
        set(meta.tags)
    )


def test_official_source_packages_all_wireframe_guidance():
    source = OptionalSkillSource()

    bundle = source.fetch(IDENTIFIER)

    assert bundle is not None
    assert bundle.name == "ux-ui-wireframe-architect"
    assert bundle.source == "official"
    assert bundle.trust_level == "builtin"
    normalized_files = {path.replace("\\", "/"): content for path, content in bundle.files.items()}
    assert SUPPORT_FILES.issubset(normalized_files)
    assert all(normalized_files[path] for path in SUPPORT_FILES)


def test_packaged_frontmatter_uses_supported_contract():
    source = OptionalSkillSource()
    bundle = source.fetch(IDENTIFIER)
    assert bundle is not None

    skill_md = bundle.files["SKILL.md"]
    assert isinstance(skill_md, bytes)
    frontmatter, body = parse_frontmatter(skill_md.decode("utf-8"))

    assert frontmatter["name"] == bundle.name
    assert len(frontmatter["description"]) <= 60
    assert frontmatter["description"].endswith(".")
    assert set(frontmatter["platforms"]) == {"linux", "macos", "windows"}
    assert frontmatter["author"].split(",")[0].strip() != "Hermes Agent"
    assert body.startswith("# UX/UI Wireframe Architect Skill")
