from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "optional-skills" / "productivity" / "shopify-checkout-payment-connection-check" / "SKILL.md"
SCRIPT = ROOT / "optional-skills" / "productivity" / "shopify-checkout-payment-connection-check" / "scripts" / "checkout-admin-read.mjs"


class CheckoutSkillContractTest(unittest.TestCase):
    def test_skill_keeps_the_customer_facing_audit_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        for heading in ("## When to Use", "## Prerequisites", "## Procedure", "## Verification"):
            self.assertIn(heading, text)
        self.assertIn("Do not submit the checkout", text)
        self.assertNotIn("shpat_", text)

    def test_helper_remains_read_only_by_design(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("requiredReadScopes", source)
        self.assertIn("scopeAlternatives", source)
        for write_operation in ("productUpdate", "orderCreate", "checkoutCreate"):
            self.assertNotIn(write_operation, source)
