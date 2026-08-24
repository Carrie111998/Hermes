"""Behavioral coverage for the installer's documented stdin entrypoint."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"


def test_installer_dispatches_manifest_mode_when_executed_from_stdin() -> None:
    """`curl install.sh | bash -s -- --manifest` must actually dispatch."""

    with INSTALLER.open("rb") as installer:
        completed = subprocess.run(
            ["bash", "-s", "--", "--manifest"],
            stdin=installer,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(completed.stdout)
    assert manifest["protocol_version"] == 1
    assert manifest["stages"]
