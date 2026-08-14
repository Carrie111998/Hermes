from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _run_child(source: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


class BootstrapIsolationTests(unittest.TestCase):
    def test_raw_argv_classifier_fails_ambiguous_forms_to_normal(self):
        from hermes_cli.bootstrap_policy import BootstrapPolicy, classify_argv

        self.assertIs(
            classify_argv(["-z", "FICTIONAL", "--isolated"]),
            BootstrapPolicy.ISOLATED_ONESHOT,
        )
        self.assertIs(
            classify_argv(["--oneshot", "FICTIONAL", "--isolated"]),
            BootstrapPolicy.ISOLATED_ONESHOT,
        )
        self.assertIs(classify_argv(["--isolated"]), BootstrapPolicy.NORMAL)
        self.assertIs(classify_argv(["-z", "FICTIONAL"]), BootstrapPolicy.NORMAL)

    def test_isolated_main_import_is_zero_write_and_skips_heavy_tools(self):
        result = _run_child(
            """
            import os, socket, sys, tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                os.environ["HERMES_HOME"] = td
                sys.argv = ["hermes", "-z", "FICTIONAL", "--isolated"]
                socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("network attempted")
                )
                from hermes_cli.bootstrap_policy import classify_argv, set_policy
                set_policy(classify_argv(sys.argv[1:]))
                before = set(Path(td).rglob("*"))
                import hermes_cli.main
                after = set(Path(td).rglob("*"))
                assert after == before, sorted(str(p) for p in after - before)
                for name in ("model_tools", "tools.process_registry", "tools.async_delegation"):
                    assert name not in sys.modules, name
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_isolated_does_not_rewrite_malformed_dotenv(self):
        result = _run_child(
            """
            import os, sys, tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                env_file = home / ".env"
                original = b"FAKE_KEY=fake-value\\x00tail\\n"
                env_file.write_bytes(original)
                os.environ["HERMES_HOME"] = td
                sys.argv = ["hermes", "-z", "FICTIONAL", "--isolated"]
                from hermes_cli.bootstrap_policy import classify_argv, set_policy
                set_policy(classify_argv(sys.argv[1:]))
                import hermes_cli.main
                assert env_file.read_bytes() == original
                assert set(home.iterdir()) == {env_file}
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_normal_bootstrap_and_tool_discovery_remain_enabled(self):
        result = _run_child(
            """
            import os, sys, tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                os.environ["HERMES_HOME"] = td
                sys.argv = ["hermes", "--version"]
                from hermes_cli.bootstrap_policy import classify_argv, set_policy
                set_policy(classify_argv(sys.argv[1:]))
                import hermes_cli.main
                home = Path(td)
                assert (home / "SOUL.md").is_file()
                assert (home / "logs").is_dir()
                import hermes_cli.env_loader as env_loader
                env_loader.load_hermes_dotenv = lambda **kwargs: []
                import run_agent
                assert "model_tools" in sys.modules
                assert "tools.process_registry" in sys.modules
                assert (home / "cache" / "tool_discovery_cache.json").is_file()
                assert (home / "state.db").is_file()
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
