"""The shell installer must preserve uv's lock cutoff under UV_NO_CONFIG."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_sh_passes_the_lock_cutoff_to_uv_commands() -> None:
    source = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    match = re.search(r'^UV_EXCLUDE_NEWER="([^"]+)"$', source, re.MULTILINE)
    assert match, "install.sh must define one explicit uv exclude-newer cutoff"
    cutoff = match.group(1)
    assert cutoff == lock["options"]["exclude-newer"]
    assert cutoff == pyproject["tool"]["uv"]["exclude-newer"]

    assert 'sync --extra all --locked --exclude-newer "$UV_EXCLUDE_NEWER"' in source
    assert 'pip install --exclude-newer "$UV_EXCLUDE_NEWER"' in source
    assert '"${UV_EXCLUDE_NEWER_PACKAGE_ARGS[@]}"' in source
    for name, enabled in lock["options"]["exclude-newer-package"].items():
        value = "true" if enabled is True else "false" if enabled is False else str(enabled)
        assert f"--exclude-newer-package {name}={value}" in source
