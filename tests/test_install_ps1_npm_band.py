"""Windows installer must reject npm 11.10-11.16 the same way install.sh does.

Node 24.x currently ships with npm 11.16.x.  That band honors
``min-release-age`` but ignores ``min-release-age-exclude`` (both set in
``.npmrc``), so ``engine-strict=true`` turns the mismatch into a hard
``EBADENGINE`` during the desktop ``npm ci`` stage.  POSIX ``install.sh``
already falls through to Hermes-managed Node via ``npm_supports_npmrc``;
``install.ps1`` must mirror that gate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
PACKAGE_JSON = REPO_ROOT / "package.json"


def _install_ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def _root_npm_range() -> str:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["engines"]["npm"]


def _npm_supports_npmrc(version: str) -> bool:
    """Python mirror of install.ps1's Test-NpmSupportsNpmrc / install.sh's helper."""
    ver = version.lstrip("v").split("-", 1)[0]
    parts = ver.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return False
    if major == 11 and 10 <= minor <= 16:
        return False
    return True


class TestNpmRangeConstant:
    def test_fallback_npm_range_matches_package_json(self) -> None:
        text = _install_ps1()
        match = re.search(
            r'\$NpmRange\s*=\s*"([^"]+)"',
            text,
        )
        assert match is not None, "install.ps1 must define $NpmRange"
        assert match.group(1) == _root_npm_range(), (
            "install.ps1 $NpmRange must stay in sync with package.json "
            "engines.npm — Test-Node runs before the repo is cloned, so the "
            "constant is the only range Update-ManagedNpm can see on a fresh "
            "install."
        )


class TestNpmSupportsNpmrcContract:
    @pytest.mark.parametrize("bad_npm", ["11.10.0", "11.12.1", "11.16.0"])
    def test_python_mirror_rejects_bad_band(self, bad_npm: str) -> None:
        assert not _npm_supports_npmrc(bad_npm)

    @pytest.mark.parametrize("good_npm", ["10.9.8", "11.9.9", "11.17.0", "12.0.2"])
    def test_python_mirror_accepts_good_versions(self, good_npm: str) -> None:
        assert _npm_supports_npmrc(good_npm)

    def test_helper_function_is_wired_into_test_node(self) -> None:
        text = _install_ps1()
        assert "function Test-NpmSupportsNpmrc" in text
        assert "function Get-NpmVersion" in text
        # System Node success path must require a usable npm, not Node alone.
        assert re.search(
            r"function Test-Node \{[\s\S]{0,1200}?Get-NpmVersion[\s\S]{0,400}?Test-NpmSupportsNpmrc",
            text,
        ), "Test-Node must probe npm version and reject the 11.10-11.16 band"
        assert "cannot honor this repo's .npmrc" in text


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell not available to execute Test-NpmSupportsNpmrc",
)
class TestNpmSupportsNpmrcRuntime:
    """Execute the real PowerShell helper when a host is available."""

    @staticmethod
    def _powershell() -> str:
        return shutil.which("pwsh") or shutil.which("powershell") or "pwsh"

    def _eval(self, version: str) -> bool:
        # Dot-source only the helper by extracting it into a tiny script, so
        # we do not run the full installer entrypoint.
        text = _install_ps1()
        match = re.search(
            r"(function Test-NpmSupportsNpmrc \{[\s\S]*?\n\})\n\nfunction Get-NpmVersion",
            text,
        )
        assert match is not None, "could not extract Test-NpmSupportsNpmrc"
        script = (
            match.group(1)
            + f"; if (Test-NpmSupportsNpmrc '{version}') {{ 'yes' }} else {{ 'no' }}"
        )
        result = subprocess.run(
            [self._powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip().splitlines()[-1].strip() == "yes"

    @pytest.mark.parametrize("bad_npm", ["11.10.0", "11.16.0"])
    def test_runtime_rejects_bad_band(self, bad_npm: str) -> None:
        assert self._eval(bad_npm) is False

    @pytest.mark.parametrize("good_npm", ["10.9.8", "11.17.0"])
    def test_runtime_accepts_good_versions(self, good_npm: str) -> None:
        assert self._eval(good_npm) is True
