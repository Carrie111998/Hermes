import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestCliOneshotContextDiscovery(unittest.TestCase):
    """
    Regression test for #95577:
    One-shot CLI (hermes chat -q) and CLI commands lazily importing gateway.run
    must NOT clobber TERMINAL_CWD to home_fallback if _HERMES_GATEWAY is not '1'.
    """

    def setUp(self):
        self._orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_lazy_import_gateway_run_does_not_mutate_terminal_cwd_in_cli_mode(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            # Create a mock config.yaml with placeholder cwd
            config_file = Path(tmp_home) / "config.yaml"
            config_file.write_text("terminal:\n  cwd: .\n")

            os.environ.pop("TERMINAL_CWD", None)
            os.environ.pop("_HERMES_GATEWAY", None)
            os.environ["HERMES_HOME"] = tmp_home

            sys.modules.pop("gateway.run", None)
            import gateway.run  # noqa: F401

            self.assertIsNone(
                os.environ.get("TERMINAL_CWD"),
                "Lazy import of gateway.run in CLI mode mutated TERMINAL_CWD to home_fallback",
            )

    def test_gateway_daemon_mode_resolves_terminal_cwd(self):
        with tempfile.TemporaryDirectory() as tmp_home:
            config_file = Path(tmp_home) / "config.yaml"
            config_file.write_text("terminal:\n  cwd: .\n")

            os.environ["_HERMES_GATEWAY"] = "1"
            os.environ.pop("TERMINAL_CWD", None)
            os.environ["HERMES_HOME"] = tmp_home

            sys.modules.pop("gateway.run", None)
            import gateway.run  # noqa: F401

            self.assertEqual(os.environ.get("TERMINAL_CWD"), str(Path.home()))


if __name__ == "__main__":
    unittest.main()
