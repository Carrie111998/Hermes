from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "optional-skills" / "productivity" / "seo-backlink-opportunity-finder"
SKILL = BASE / "SKILL.md"
SCRIPT = BASE / "scripts" / "validate-opportunity-ledger.mjs"
PROTOCOL = BASE / "references" / "research-protocol.md"


class BacklinkSkillContractTest(unittest.TestCase):
    def test_skill_preserves_the_research_and_review_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        for heading in ("## When to Use", "## Prerequisites", "## Procedure", "## Verification"):
            self.assertIn(heading, text)
        self.assertIn("public", text.lower())
        self.assertNotIn("shpat_", text)

    def test_ledger_validator_enforces_breadth_and_safe_targets(self):
        source = SCRIPT.read_text(encoding="utf-8")
        protocol = PROTOCOL.read_text(encoding="utf-8")
        for marker in ("TIERS", "LANES", "ROUTES", "asPublicUrl"):
            self.assertIn(marker, source)
        self.assertIn("Do not access private address ranges", protocol)
