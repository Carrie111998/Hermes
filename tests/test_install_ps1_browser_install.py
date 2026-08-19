"""Regression test for install.ps1 browser setup (PR #44772 review).

agent-browser resolves lazily via npx everywhere else in the system
(tools/browser_tool.py::_find_agent_browser); Install-AgentBrowser was the
last place that still eagerly npm-installed a second, separately
version-pinned copy of it. Removed: agent-browser acquisition now happens
only via `hermes update`'s npx cache warm or an actual browser-tool call's
lazy npx resolution.

Linux CI cannot execute the PowerShell installer, so verification here is
source-text-level only, matching tests/test_install_sh_browser_install.py
and tests/test_install_ps1_ascii_only.py.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _extract_function_body(source: str, name: str) -> str:
    m = re.search(
        rf"^function {re.escape(name)} \{{.*?^\}}", source, re.MULTILINE | re.DOTALL
    )
    assert m, f"could not extract function {name} from install.ps1"
    return m.group(0)


def test_install_agent_browser_installs_managed_cli() -> None:
    body = _extract_function_body(INSTALL_PS1.read_text(), "Install-AgentBrowser")

    assert "agent-browser@^0.26.0" in body
    assert "Installing agent-browser..." in body
    assert "Installing Chromium via agent-browser install" not in body
    assert "Find-SystemBrowser" in body
    assert "Write-BrowserEnv" in body


def test_install_agent_browser_drops_unused_skip_chromium_param() -> None:
    """$SkipChromium is not part of the managed CLI install path."""
    body = _extract_function_body(INSTALL_PS1.read_text(), "Install-AgentBrowser")

    assert "SkipChromium" not in body

    text = INSTALL_PS1.read_text()
    call_site = re.search(r"^\s*Install-AgentBrowser.*$", text, re.MULTILINE)
    assert call_site, "could not find Install-AgentBrowser call site"
    assert "SkipChromium" not in call_site.group(0)


def test_install_agent_browser_still_ignore_scripts_hardened() -> None:
    """The managed install retains supply-chain hardening."""
    body = _extract_function_body(INSTALL_PS1.read_text(), "Install-AgentBrowser")

    assert "--prefix $prefixDir" in body
