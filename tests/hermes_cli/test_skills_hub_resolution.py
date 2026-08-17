"""Tests for CLI skill install source resolution."""

from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
from tools.skills_hub import SkillBundle, SkillMeta


class _FakeGitHubSource:
    def __init__(self, tree_paths):
        self.tree_paths = tree_paths
        self.repo_tree_requests = []
        self.fetched = []
        self.inspected = []

    def source_id(self):
        return "github"

    def _get_repo_tree(self, repo):
        self.repo_tree_requests.append(repo)
        return (
            "main",
            [{"type": "blob", "path": path} for path in self.tree_paths],
        )

    def fetch(self, identifier):
        self.fetched.append(identifier)
        if identifier == "mvanhorn/last30days-skill/skills/last30days":
            return SkillBundle(
                name="last30days",
                files={"SKILL.md": "---\nname: last30days\n---\n"},
                source="github",
                identifier=identifier,
                trust_level="community",
            )
        return None

    def inspect(self, identifier):
        self.inspected.append(identifier)
        if identifier == "mvanhorn/last30days-skill/skills/last30days":
            return SkillMeta(
                name="last30days",
                description="Current GitHub runtime skill",
                source="github",
                identifier=identifier,
                trust_level="community",
                repo="mvanhorn/last30days-skill",
                path="skills/last30days",
            )
        return None


class _FakeClawHubSource:
    def source_id(self):
        return "clawhub"

    def fetch(self, identifier):
        if identifier == "mvanhorn/last30days-skill":
            return SkillBundle(
                name="last30days-skill",
                files={"SKILL.md": "stale root package"},
                source="clawhub",
                identifier="last30days-skill",
                trust_level="community",
            )
        return None

    def inspect(self, identifier):
        if identifier == "mvanhorn/last30days-skill":
            return SkillMeta(
                name="Last30days Skill",
                description="Stale registry package",
                source="clawhub",
                identifier="last30days-skill",
                trust_level="community",
            )
        return None


class _FakeOfficialSource:
    def source_id(self):
        return "official"

    def fetch(self, identifier):
        if identifier == "official/last30days":
            return SkillBundle(
                name="last30days",
                files={"SKILL.md": "official skill"},
                source="official",
                identifier=identifier,
                trust_level="builtin",
            )
        return None

    def inspect(self, identifier):
        if identifier == "official/last30days":
            return SkillMeta(
                name="last30days",
                description="Official skill",
                source="official",
                identifier=identifier,
                trust_level="builtin",
            )
        return None


def test_bare_github_repo_prefers_current_repo_skill_over_registry_alias():
    github = _FakeGitHubSource(["skills/last30days/SKILL.md"])
    clawhub = _FakeClawHubSource()

    meta, bundle, matched = _resolve_source_meta_and_bundle(
        "mvanhorn/last30days-skill",
        # Put ClawHub first to prove explicit owner/repo still prefers GitHub.
        [clawhub, github],
    )

    assert matched is github
    assert bundle is not None
    assert bundle.source == "github"
    assert bundle.identifier == "mvanhorn/last30days-skill/skills/last30days"
    assert meta is not None
    assert meta.path == "skills/last30days"
    assert github.fetched == ["mvanhorn/last30days-skill/skills/last30days"]


def test_bare_github_repo_falls_back_to_sources_when_repo_skill_is_ambiguous():
    github = _FakeGitHubSource([
        "skills/alpha/SKILL.md",
        "skills/beta/SKILL.md",
    ])
    clawhub = _FakeClawHubSource()

    meta, bundle, matched = _resolve_source_meta_and_bundle(
        "mvanhorn/last30days-skill",
        [github, clawhub],
    )

    assert matched is clawhub
    assert bundle is not None
    assert bundle.source == "clawhub"
    assert meta is not None
    assert meta.source == "clawhub"


def test_registered_source_identifier_bypasses_bare_github_preflight():
    github = _FakeGitHubSource(["skills/last30days/SKILL.md"])
    official = _FakeOfficialSource()

    meta, bundle, matched = _resolve_source_meta_and_bundle(
        "official/last30days",
        [github, official],
    )

    assert github.repo_tree_requests == []
    assert matched is official
    assert bundle is not None
    assert bundle.source == "official"
    assert meta is not None
    assert meta.source == "official"
