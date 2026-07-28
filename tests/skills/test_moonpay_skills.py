from __future__ import annotations

import re
from pathlib import Path


MOONPAY_DIR = Path(__file__).resolve().parents[2] / "optional-skills" / "moonpay"


def test_all_moonpay_descriptions_meet_hardline_limit():
    skill_files = sorted(MOONPAY_DIR.glob("*/SKILL.md"))
    assert len(skill_files) == 25

    for skill_file in skill_files:
        match = re.search(
            r'^description:\s*["\']?(.*?)["\']?\s*$',
            skill_file.read_text(),
            re.MULTILINE,
        )
        assert match, f"missing description: {skill_file}"
        description = match.group(1)
        assert len(description) <= 60, f"{skill_file}: {len(description)} characters"
        assert description.endswith("."), (
            f"description must end with a period: {skill_file}"
        )


def test_export_data_has_an_installable_source_bundle():
    source = MOONPAY_DIR / "export-data" / "SKILL.md"
    assert source.is_file()
    assert "name: export-data" in source.read_text(encoding="utf-8")


def test_export_data_fetches_as_an_official_bundle():
    from tools.skills_hub import OptionalSkillSource

    bundle = OptionalSkillSource().fetch("official/moonpay/export-data")

    assert bundle is not None
    assert bundle.identifier == "official/moonpay/export-data"
    assert "SKILL.md" in bundle.files


def test_automation_skills_reference_tested_scripts_and_authorization():
    expectations = {
        "iron-dca": "scripts/iron_dca.py",
        "trading-automation": "scripts/bounded_swap.py",
    }
    for skill, script_reference in expectations.items():
        skill_text = (MOONPAY_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        assert script_reference in skill_text
        assert "explicit" in skill_text.lower()
        assert "authorization" in skill_text.lower()
        assert "total cap" in skill_text.lower()
