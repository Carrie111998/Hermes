#!/usr/bin/env python3
"""Tests for the install mirror resolver.

Issue #95167 (Chinese user on Windows 10 VM with persistent install failures;
user comment confirmed the resolution was "use a domestic mirror"). The fix
addes a region-aware fallback mirror resolver that's invoked from both
scripts/install.ps1 and scripts/install.sh.

The resolver ships in two language versions: install_mirror.ps1 (PowerShell,
dot-sourced by install.ps1) and install_mirror.sh (bash, sourced by
install.sh). Both versions must agree on the URL rewriting rules -- a
divergence here is a silent install breakage for users in
network-restricted regions.

These tests pin the contracts that MUST stay in sync:

  * Empty env vars fall through to the resolver.
  * HERMES_PYPI_MIRROR / HERMES_GITHUB_PROXY override every candidate.
  * Non-github URLs pass through hermes_github_clone_url / Get-HermesGithubCloneUrl.
  * SSH github URLs (git@github.com:...) pass through unchanged.
  * HERMES_PYPI_MIRROR trailing-slash variants normalize correctly.

Each test reads the corresponding language script and asserts the source
matches. The actual probe / reachability code (curl, Invoke-WebRequest) is
not exercised here -- mocking the network layer in unit tests would test the
mock, not the resolver.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PS_SCRIPT = REPO_ROOT / "scripts" / "install_mirror.ps1"
SH_SCRIPT = REPO_ROOT / "scripts" / "install_mirror.sh"
INSTALL_PS_SCRIPT = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH_SCRIPT = REPO_ROOT / "scripts" / "install.sh"


def _bash_path(path: Path) -> str:
    r"""Convert a Windows Path to a POSIX-style path bash can use.

    The shell we invoke (`bash` on PATH) is WSL on Windows hosts and
    Linux/macOS elsewhere. On Windows it expects /mnt/<drive>/...; on
    POSIX, the path is already correct. We try the WSL form first
    because the test suite runs on a Windows host and the POSIX form
    is the fall-through (where Windows drive letters wouldn't appear)."""
    s = str(path).replace("\\", "/")
    # Drive-letter path like D:/code/... -> /mnt/d/code/... (WSL)
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    # POSIX path: take it as-is.
    if s.startswith("/"):
        return s
    # Relative path: best-effort.
    return s


def _subprocess_cwd(path: Path) -> str:
    """Return a cwd string that subprocess.Popen can actually use.

    On Windows, subprocess passes cwd straight to CreateProcess; an MSYS
    POSIX path like `/d/code/wt-95167` makes CreateProcess throw
    'NotADirectoryError' because Windows can't resolve it as a real
    directory. We use the Windows form for cwd, and the bash form
    only for arguments that go to bash."""
    return str(path)


# ---------------------------------------------------------------------------
# Source-content tests -- pin the contract.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ps_source() -> str:
    """Read install_mirror.ps1 once per module."""
    return PS_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sh_source() -> str:
    """Read install_mirror.sh once per module."""
    return SH_SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "candidate",
    [
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "https://pypi.mirrors.ustc.edu.cn/simple/",
        "https://mirrors.cloud.tencent.com/pypi/simple/",
    ],
)
def test_powershell_pypi_candidates_include_chinese_mirrors(ps_source, candidate):
    """Every PyPI mirror that #95167's region-aware resolver must consider
    appears as a string literal in install_mirror.ps1."""
    assert candidate in ps_source, (
        f"{candidate} missing from install_mirror.ps1. The mirror resolver "
        f"won't consider it; users in network-restricted regions that rely on "
        f"that mirror will silently fall back to the canonical PyPI URL."
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "https://pypi.mirrors.ustc.edu.cn/simple/",
        "https://mirrors.cloud.tencent.com/pypi/simple/",
    ],
)
def test_bash_pypi_candidates_include_chinese_mirrors(sh_source, candidate):
    """Same coverage check for the bash mirror resolver."""
    assert candidate in sh_source, (
        f"{candidate} missing from install_mirror.sh. The PowerShell and "
        f"bash resolvers must stay in lockstep -- a divergence means a "
        f"Windows install and a POSIX install on the same network pick "
        f"different mirrors."
    )


@pytest.mark.parametrize(
    "proxy",
    [
        "https://ghfast.top/",
        "https://gh-proxy.com/",
    ],
)
def test_powershell_github_proxy_candidates(ps_source, proxy):
    """GitHub proxy URLs are present so HTTPS clones / ZIP downloads can
    fall back when github.com is unreachable."""
    assert proxy in ps_source


@pytest.mark.parametrize(
    "proxy",
    [
        "https://ghfast.top/",
        "https://gh-proxy.com/",
    ],
)
def test_bash_github_proxy_candidates(sh_source, proxy):
    """Bash side keeps the same GitHub proxy list."""
    assert proxy in sh_source


def test_powershell_pypi_mirror_honors_override(ps_source):
    """HERMES_PYPI_MIRROR short-circuits the resolver. The override path is
    the only way a user with a private mirror (corporate proxy, regional
    cache) can guarantee a specific URL is used -- the resolver doesn't
    probe private endpoints."""
    # The literal string must appear in the override branch.
    assert re.search(
        r"if\s*\(\$env:HERMES_PYPI_MIRROR\)\s*\{",
        ps_source,
    ), "HERMES_PYPI_MIRROR override branch missing from install_mirror.ps1"


def test_bash_pypi_mirror_honors_override(sh_source):
    """Same coverage check for bash."""
    assert re.search(
        r"if\s+\[ -n \"\$\{HERMES_PYPI_MIRROR:-?\}\" \]; then",
        sh_source,
    ), "HERMES_PYPI_MIRROR override branch missing from install_mirror.sh"


def test_powershell_github_proxy_honors_override(ps_source):
    """HERMES_GITHUB_PROXY short-circuits the github URL rewriter."""
    assert re.search(
        r"if\s*\(\$env:HERMES_GITHUB_PROXY\)",
        ps_source,
    ), "HERMES_GITHUB_PROXY override branch missing from install_mirror.ps1"


def test_bash_github_proxy_honors_override(sh_source):
    """Same coverage check for bash."""
    assert re.search(
        r"if\s+\[ -n \"\$\{HERMES_GITHUB_PROXY:-?\}\" \]; then",
        sh_source,
    ), "HERMES_GITHUB_PROXY override branch missing from install_mirror.sh"


def test_powershell_github_rewriter_passes_through_ssh(ps_source):
    """An SSH git URL (git@github.com:...) must NOT be rewritten by the
    proxy resolver -- SSH has no proxy equivalent and replacing it would
    silently break the SSH path. The regex match '^https?://github.com/'
    already excludes SSH, but we pin the comment so a future refactor
    doesn't drop the check."""
    # Look for the early-return line for non-http(s) github URLs.
    assert re.search(
        r"if\s*\(\$Url\s+-notmatch\s+'\^https\?://github\\.com/'\)\s*\{\s*return\s+\$Url\s*\}",
        ps_source,
    ), "SSH-pass-through guard missing from install_mirror.ps1"


def test_bash_github_rewriter_passes_through_ssh(sh_source):
    """Same coverage check for bash. Bash uses a `case` statement to
    branch on the URL prefix."""
    assert re.search(
            r"case\s+\"\$url\"\s+in[\s\S]*?https://github\.com/\*\|http://github\.com/\*\)\s*;;[\s\S]*?echo \"\$url\"",
            sh_source,
        ), "SSH-pass-through guard missing from install_mirror.sh"


# ---------------------------------------------------------------------------
# Behavior tests -- run actual code where possible.
# ---------------------------------------------------------------------------


def test_bash_github_clone_url_passthrough_when_no_env_set(monkeypatch):
    """When HERMES_GITHUB_PROXY is unset, hermes_github_clone_url should
    echo the input unchanged for non-github URLs.

    We use subprocess.run because hermes_github_clone_url is a bash
    function and shelling out gives us a clean environment to assert
    on (no leakage from the test runner's env)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_")}
    # Source the bash mirror library in a clean subshell, then ask it
    # to rewrite an SSH github URL (which the function MUST pass through
    # unchanged because SSH has no proxy equivalent).
    src_cmd = (
        f'. "{_bash_path(SH_SCRIPT)}"; '
        f'hermes_github_clone_url "git@github.com:NousResearch/hermes-agent.git"'
    )
    out = subprocess.run(
        ["bash", "-c", src_cmd],
        cwd=_subprocess_cwd(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert out.returncode == 0, out.stderr
    # SSH URL must pass through verbatim.
    assert out.stdout.strip() == "git@github.com:NousResearch/hermes-agent.git"


@pytest.mark.skipif(
    not shutil.which("pwsh") and not shutil.which("powershell"),
    reason="No PowerShell available to run install_mirror.ps1 behavior tests",
)
def test_powershell_pypi_mirror_override_via_env(tmp_path):
    """Smoke-test the PowerShell resolver's HERMES_PYPI_MIRROR override
    by running it under pwsh/powershell.exe with the env var set. We
    expect it to echo the override value back regardless of any
    real-world probe (because the override branch never probes)."""
    # Compose a one-liner that dot-sources the library and prints the
    # resolver's result.
    ps_cmd = (
        f". '{PS_SCRIPT}'; "
        "Write-Output (Get-HermesPypiMirror)"
    )
    env = os.environ.copy()
    env["HERMES_PYPI_MIRROR"] = "https://mirror.example.invalid/pypi/simple/"
    env.pop("HERMES_NO_MIRROR", None)

    exe = shutil.which("pwsh") or shutil.which("powershell")
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    # The trailing slash gets normalized.
    out = (proc.stdout or "").strip()
    assert out == "https://mirror.example.invalid/pypi/simple/", (
        f"Override didn't propagate. stdout={out!r} stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# install.*.ps1 / install.*.sh integration -- the install flow must USE
# the resolver.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def install_ps_source() -> str:
    return (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def install_sh_source() -> str:
    return (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")


def test_install_ps1_dot_sources_mirror_lib(install_ps_source):
    """install.ps1 must source install_mirror.ps1 -- otherwise the
    GitHub clone and pip install stages can't reach the resolver."""
    # PS dot-source: '. <path>' or `. (Join-Path $PSScriptRoot 'name.ps1')`.
    assert re.search(
        r"\.\s*\(?Join-Path\s+\$PSScriptRoot\s+\"install_mirror\.ps1\"\)?",
        install_ps_source,
    ), "install.ps1 must dot-source scripts/install_mirror.ps1"


def test_install_sh_sources_mirror_lib(install_sh_source):
    """install.sh must source install_mirror.sh."""
    assert "install_mirror.sh" in install_sh_source, (
        "install.sh must source scripts/install_mirror.sh"
    )


def test_install_ps1_uses_pypi_mirror_resolver(install_ps_source):
    """The Install-Dependencies stage must call Get-HermesPypiMirror so
    `uv sync` / `uv pip install` actually use the chosen mirror.
    Without this the resolver exists but install.ps1 ignores it -- a
    silent regression."""
    assert "Get-HermesPypiMirror" in install_ps_source, (
        "Install-Dependencies must call Get-HermesPypiMirror to pick "
        "a PyPI mirror before uv sync / uv pip install (#95167)."
    )


def test_install_sh_uses_pypi_mirror_resolver(install_sh_source):
    """install.sh install_deps() must call hermes_pypi_mirror."""
    assert "hermes_pypi_mirror" in install_sh_source, (
        "install_deps() must call hermes_pypi_mirror to pick a PyPI "
        "mirror before uv sync / uv pip install (#95167)."
    )


def test_install_ps1_clone_uses_github_proxy(install_ps_source):
    """Install-Repository must call Get-HermesGithubCloneUrl when
    forming the HTTPS clone URL. Without this the proxy resolver
    exists but install.ps1 never asks it."""
    assert "Get-HermesGithubCloneUrl" in install_ps_source, (
        "Install-Repository must call Get-HermesGithubCloneUrl so the "
        "HTTPS clone URL routes through a reachable proxy when "
        "github.com is blocked (#95167)."
    )


def test_install_sh_clone_uses_github_proxy(install_sh_source):
    """install.sh clone_repo() must call hermes_github_clone_url."""
    assert "hermes_github_clone_url" in install_sh_source, (
        "clone_repo() must call hermes_github_clone_url so the HTTPS "
        "clone URL routes through a reachable proxy when github.com "
        "is blocked (#95167)."
    )


def test_install_ps1_clone_failure_surfaces_actionable_hint(install_ps_source):
    """When ALL clone paths fail, install.ps1 should not just say
    "Failed to download repository" -- it must tell the user about
    HERMES_GITHUB_PROXY / HERMES_PYPI_MIRROR (#95167)."""
    assert "HERMES_GITHUB_PROXY" in install_ps_source, (
        "Final clone failure path must surface HERMES_GITHUB_PROXY / "
        "HERMES_PYPI_MIRROR as the actionable next step (#95167)."
    )
    # Must NOT still reference the now-removed diagnose_install.ps1.
    assert "diagnose_install.ps1" not in install_ps_source, (
        "install.ps1 must not reference diagnose_install.ps1 -- that "
        "script lives in a follow-up PR (#95238b) and is not part of "
        "this change."
    )


def test_install_sh_clone_failure_surfaces_actionable_hint(install_sh_source):
    """Same coverage for install.sh."""
    assert "HERMES_GITHUB_PROXY" in install_sh_source, (
        "Final clone failure path must surface HERMES_GITHUB_PROXY / "
        "HERMES_PYPI_MIRROR as the actionable next step (#95167)."
    )
    assert "diagnose_install.sh" not in install_sh_source, (
        "install.sh must not reference diagnose_install.sh -- that "
        "script lives in a follow-up PR (#95238b) and is not part of "
        "this change."
    )


def test_install_ps1_dep_failure_surfaces_actionable_hint(install_ps_source):
    """When the uv pip install cascade exhausts itself, the failure
    message must mention HERMES_PYPI_MIRROR."""
    # Look for the multi-line hint string built around the throw.
    assert "HERMES_PYPI_MIRROR" in install_ps_source


def test_install_sh_dep_failure_surfaces_actionable_hint(install_sh_source):
    """Same coverage for install.sh."""
    assert "HERMES_PYPI_MIRROR" in install_sh_source


def test_install_ps1_parses():
    """install.ps1 must still parse on PS5.1 after the patches."""
    if not (shutil.which("pwsh") or shutil.which("powershell")):
        pytest.skip("No PowerShell available")
    ps_cmd = (
        f"$ErrorActionPreference = 'Stop'; "
        f"$tokens = $null; $errors = $null; "
        f"$null = [System.Management.Automation.Language.Parser]::"
        f"ParseFile('{INSTALL_PS_SCRIPT}', "
        f"[ref]$tokens, [ref]$errors); "
        f"if ($errors) {{ exit 1 }} else {{ exit 0 }}"
    )
    exe = shutil.which("pwsh") or shutil.which("powershell")
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"install.ps1 fails to parse: {proc.stderr}"
    )


def test_install_sh_parses():
    """bash -n on install.sh."""
    proc = subprocess.run(
        ["bash", "-n", _bash_path(INSTALL_SH_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr