"""The uv bootstrap installers must download a version-pinned astral.sh URL.

An unpinned URL installs the latest uv, whose changed handling of pyproject's
global ``exclude-newer`` makes ``--locked`` report a pristine checkout's
uv.lock as stale — silently knocking every fresh install off the hash-verified
Tier 0 and onto an unverified PyPI resolve (#90650). CI pins the same version
when generating/verifying the lockfile, so the user-facing installers must
agree with it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
SETUP_SH = REPO_ROOT / "setup-hermes.sh"
MANAGED_UV = REPO_ROOT / "hermes_cli" / "managed_uv.py"

CI_UV_VERSION_RE = re.compile(r'version:\s*"?(\d+\.\d+\.\d+)"?')
INSTALLER_VERSION_RE = re.compile(
    r'^(UV_VERSION|UV_PINNED_VERSION|PINNED_UV_INSTALL_VERSION)\s*=\s*["\'](\d+\.\d+\.\d+)["\']',
    re.MULTILINE,
)


def _ci_uv_version() -> str:
    workflow = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    match = CI_UV_VERSION_RE.search(workflow.read_text(encoding="utf-8"))
    assert match, "could not find the pinned uv version in tests.yml"
    return match.group(1)


def test_install_sh_pins_uv_to_the_ci_version():
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = INSTALLER_VERSION_RE.search(text)
    assert match, "scripts/install.sh must define UV_VERSION=<x.y.z>"
    assert match.group(2) == _ci_uv_version(), (
        "scripts/install.sh installs a different uv than CI verifies the "
        "lockfile with; the hash-verified install tier goes stale for every "
        "fresh install again (#90650)"
    )
    assert "https://astral.sh/uv/${UV_VERSION}/install.sh" in text
    # The unversioned form is the exact regression this pins away from.
    assert 'https://astral.sh/uv/install.sh -o' not in text.replace(
        "https://astral.sh/uv/${UV_VERSION}/install.sh", ""
    )


def test_setup_hermes_sh_pins_uv_to_the_ci_version():
    text = SETUP_SH.read_text(encoding="utf-8")
    match = INSTALLER_VERSION_RE.search(text)
    assert match, "setup-hermes.sh must define UV_PINNED_VERSION=<x.y.z>"
    assert match.group(2) == _ci_uv_version()


def test_managed_uv_pins_uv_to_the_ci_version():
    text = MANAGED_UV.read_text(encoding="utf-8")
    match = INSTALLER_VERSION_RE.search(text)
    assert match, (
        "hermes_cli/managed_uv.py must define PINNED_UV_INSTALL_VERSION"
    )
    assert match.group(2) == _ci_uv_version()
    assert "https://astral.sh/uv/install.sh" not in text.replace(
        f"https://astral.sh/uv/{match.group(2)}/install.sh", ""
    )
    # POSIX URL is a plain literal; the Windows cmd is an f-string on the
    # same constant, so assert its template references the pin.
    assert (
        "https://astral.sh/uv/{PINNED_UV_INSTALL_VERSION}/install.ps1" in text
    )
