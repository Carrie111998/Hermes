import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "checkout-admin-read.mjs"


def run_node(*args):
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR") if key in os.environ}
    return subprocess.run(["node", str(SCRIPT), *args], cwd=ROOT, env=env, text=True, capture_output=True, check=False)


class CheckoutSkillTests(unittest.TestCase):
    def test_help_is_available_without_credentials_or_network(self):
        result = run_node("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("connection-check", result.stdout)

    def test_init_env_writes_private_template_only(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "skill-hub.env"
            result = run_node("init-env", "--method", "shopify_cli_oauth", "--env", str(config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["created"])
            contents = config.read_text(encoding="utf-8")
            self.assertIn("SKILL_HUB_SHOPIFY_ACCESS_METHOD=shopify_cli_oauth", contents)
            self.assertNotIn("shpat_", contents)


if __name__ == "__main__":
    unittest.main()
