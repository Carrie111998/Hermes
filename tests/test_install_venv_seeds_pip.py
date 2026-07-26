"""Regression test for #7309: the Hermes venv must ship pip.

``uv venv`` creates a venv without pip.  The terminal tool puts the venv's
bin dir at the front of PATH so the agent's Python installs stay inside the
Hermes environment — but with no pip in there, ``pip install`` falls through
to the next interpreter on PATH (a pyenv shim, the system Python) and the
packages land outside the venv again.  Both installers seed it with ``--seed``.
"""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def test_install_sh_creates_the_venv_with_pip() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert re.search(
        r'\$UV_CMD venv venv --python "\$PYTHON_VERSION" --seed', text
    ), "install.sh must pass --seed to `uv venv` so the venv gets pip"


def test_install_ps1_creates_the_venv_with_pip() -> None:
    """Installer parity — install.sh and install.ps1 stay in lockstep."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert re.search(
        r"& \$UvCmd venv venv --python \$PythonVersion --seed", text
    ), "install.ps1 must pass --seed to `uv venv` so the venv gets pip"
