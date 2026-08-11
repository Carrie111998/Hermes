"""Regression coverage for Windows Playwright command resolution (#70787)."""

from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PS_TEST = REPO_ROOT / "scripts" / "tests" / "test-install-ps1-playwright-resolution.ps1"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
def test_playwright_resolver_behavior() -> None:
    subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PS_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )
