"""A skill's own supporting files must not register as rival skills.

`skill_view` collects candidates three ways; the third is
``search_dir.rglob(f"{name}.md")``, which matches ANY file with that basename. A
workflow skill keeping ``references/<topic>.md`` beside its SKILL.md follows the
layout skills_tool itself reads (references/, templates/, assets/, scripts/) — but
each of those files registered as a second candidate for <topic>, and skill_view
then refused the real skill as ambiguous.

Observed on one install: `notion` and `airtable` each matched three candidates — the
real productivity/<name>/SKILL.md, creative/web-design-prototype/references/<name>.md,
and an archived templates/<name>.md — so neither skill could be loaded at all.
Refusing a genuine collision is right; these candidates were not skills.

The github-pr-workflow style of name is NOT an instance of this: those live under
.archive/ and are meant to be unresolvable, having been consolidated into
github-workflow / structured-development / ml-ops. A first pass at measuring the
blast radius counted them as live skills by rglob-ing SKILL.md without excluding
.archive — the same over-broad matching this patch fixes.

.archive/ was the other source. EXCLUDED_SKILL_DIRS lists it and
is_excluded_skill_path says to apply it to every rglob result; Strategy 2 gets that
via iter_skill_index_files, and this loop was the one scanning site that did not.

Drives the real skill_view with SKILLS_DIR pointed at a temp tree, rather than
reproducing the loop — a copy of the logic cannot fail when the module changes.
"""

import json

import pytest

import agent.skill_utils as skill_utils
import tools.skills_tool as skills_tool


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """Point skill_view at an isolated tree with no external dirs.

    get_external_skills_dirs is imported INSIDE skill_view, so patching the attribute on
    skills_tool would create a name nothing reads and let the real external dirs leak in —
    the tests would then depend on whatever is installed on the machine running them.
    Patch it where it is defined.
    """
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda *a, **k: [])
    return tmp_path


def _skill(root, rel, name):
    d = root / rel / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\nbody\n")
    return d


def _view(name):
    return json.loads(skills_tool.skill_view(name))


class TestASupportingFileIsNotASkill:
    def test_a_reference_named_after_another_skill(self, skills_root):
        """The 41-skill case: github-workflow documents github-pr-workflow."""
        _skill(skills_root, "github", "github-pr-workflow")
        owner = _skill(skills_root, "github", "github-workflow")
        (owner / "references").mkdir()
        (owner / "references" / "github-pr-workflow.md").write_text("# how PRs work\n")

        res = _view("github-pr-workflow")
        assert res.get("success") is True, res.get("error")

    @pytest.mark.parametrize("sub", ["references", "templates", "assets", "scripts"])
    def test_every_content_dir(self, skills_root, sub):
        _skill(skills_root, "productivity", "notion")
        owner = _skill(skills_root, "creative", "web-design-prototype")
        (owner / sub).mkdir()
        (owner / sub / "notion.md").write_text("# Design System: Notion\n")

        res = _view("notion")
        assert res.get("success") is True, res.get("error")

    def test_archived_content(self, skills_root):
        """.archive is in EXCLUDED_SKILL_DIRS and was scanned anyway."""
        _skill(skills_root, "productivity", "notion")
        arch = skills_root / ".archive" / "popular-web-designs" / "templates"
        arch.mkdir(parents=True)
        (arch / "notion.md").write_text("# archived\n")

        res = _view("notion")
        assert res.get("success") is True, res.get("error")


class TestItStillRefusesRealAmbiguity:
    """Non-vacuity. A filter that dropped every candidate would make the tests above
    pass while destroying the collision check they depend on."""

    def test_two_real_flat_skills_still_collide(self, skills_root):
        for sub in ("a", "b"):
            (skills_root / sub).mkdir()
            (skills_root / sub / "deploy.md").write_text("---\nname: deploy\n---\n# deploy\n")

        res = _view("deploy")
        assert res.get("success") is False
        assert "Ambiguous" in str(res.get("error")), res

    def test_a_flat_skill_is_still_findable(self, skills_root):
        """Strategy 3 exists for legacy flat <name>.md skills; it must still find one."""
        (skills_root / "legacy").mkdir()
        (skills_root / "legacy" / "deploy.md").write_text("---\nname: deploy\n---\n# deploy\nbody\n")

        res = _view("deploy")
        assert res.get("success") is True, res.get("error")

    def test_a_real_duplicate_skill_dir_still_collides(self, skills_root):
        _skill(skills_root, "a", "deploy")
        _skill(skills_root, "b", "deploy")

        res = _view("deploy")
        assert res.get("success") is False
        assert "Ambiguous" in str(res.get("error")), res
