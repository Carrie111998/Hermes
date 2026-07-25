"""Guards for the hermetic env allowlist in ``scripts/run_tests.sh``.

``run_tests.sh`` runs the suite under ``env -i`` so no credential variable can
leak into tests. On POSIX, forwarding ``HOME`` is enough. Native Windows
CPython resolves ``Path.home()`` from ``USERPROFILE`` (or
``HOMEDRIVE``+``HOMEPATH``), and other platform paths come from
``LOCALAPPDATA``/``APPDATA``; dropping those breaks collection on Windows.

See issue #70813.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_tests.sh"

WINDOWS_LOCATION_VARS = (
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
)

# Variables that must never be forwarded through the hermetic boundary.
CREDENTIAL_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "HERMES_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
)


@pytest.fixture(scope="module")
def runner_source() -> str:
    return RUNNER.read_text(encoding="utf-8")


@pytest.mark.parametrize("var", WINDOWS_LOCATION_VARS)
def test_windows_location_var_is_forwarded(runner_source: str, var: str) -> None:
    """Windows home/appdata location variables survive ``env -i``."""
    assert var in runner_source, (
        f"{var} is dropped by env -i; native Windows CPython needs it to "
        "resolve Path.home() and platform directories"
    )


def test_windows_vars_are_forwarded_into_the_env_i_invocation(runner_source: str) -> None:
    """The forwarding happens in the ``exec env -i`` block, not just a comment."""
    exec_block = runner_source.split("exec env -i", 1)
    assert len(exec_block) == 2, "expected an `exec env -i` invocation"
    assert "WIN_ENV" in exec_block[1], (
        "Windows location variables must be expanded into the env -i argument list"
    )


def test_windows_vars_are_only_forwarded_when_set(runner_source: str) -> None:
    """POSIX runs stay byte-for-byte identical: unset variables are skipped."""
    assert re.search(r'if \[ -n "\$\{!_win_var:-\}" \]', runner_source), (
        "each Windows variable should only be forwarded when actually set, so a "
        "POSIX run does not gain empty USERPROFILE/APPDATA entries"
    )


def test_pythonioencoding_is_pinned_to_utf8(runner_source: str) -> None:
    """Child pytest output decodes as UTF-8 even on CP936/CP932 hosts."""
    assert "PYTHONIOENCODING=utf-8" in runner_source


@pytest.mark.parametrize("var", CREDENTIAL_VARS)
def test_credentials_are_still_scrubbed(runner_source: str, var: str) -> None:
    """The isolation intent is unchanged: no credential variable is forwarded."""
    assert var not in runner_source
