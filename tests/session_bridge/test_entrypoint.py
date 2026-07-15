from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
POISONED_ENTRYPOINT = r"""
import importlib.abc
import sys

class PoisonUnrelatedRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agent.transports.codex_app_server":
            raise RuntimeError("poisoned transport import reached")
        return None

sys.meta_path.insert(0, PoisonUnrelatedRuntime())
from session_bridge.entrypoint import main
raise SystemExit(main())
"""


def _run_poisoned_entrypoint(
    codex_home: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT) if not prior else str(ROOT) + os.pathsep + prior
    )
    return subprocess.run(
        [sys.executable, "-c", POISONED_ENTRYPOINT, *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_exact_install_command_uses_thin_bootstrap_without_runtime_imports(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"

    completed = _run_poisoned_entrypoint(codex_home, "install-sidebar-skill")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "installed",
        "path": str(codex_home / "skills" / "session-sidebar-sync"),
    }
    assert (codex_home / "skills" / "session-sidebar-sync" / "SKILL.md").is_file()


def test_non_install_command_lazily_delegates_to_full_cli_and_hits_poison(
    tmp_path: Path,
) -> None:
    completed = _run_poisoned_entrypoint(tmp_path / "codex", "status", "--json")

    assert completed.returncode != 0
    assert "poisoned transport import reached" in completed.stderr


def test_console_script_points_at_thin_bootstrap() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'hermes-session-bridge = "session_bridge.entrypoint:main"' in pyproject
