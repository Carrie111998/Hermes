"""``scripts/run_tests.sh`` must not drop the Windows OS path vars.

The wrapper does ``cd "$REPO_ROOT"`` and then

    exec env -i "${CLEAN_ENV[@]}" "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py"

``env -i`` starts from an *empty* environment, so ``CLEAN_ENV`` is an
allowlist: anything not named there is gone for the runner and — because
``run_tests_parallel.py`` hands workers ``env=os.environ`` — for every
pytest worker it spawns as well.

When ``SYSTEMDRIVE`` is missing, a Windows child cannot expand the
``REG_EXPAND_SZ`` known-folder template ``%SystemDrive%\\ProgramData`` held
in ``HKLM\\...\\ProfileList``.  The literal string is then used as a
*relative* path and the known-folder cache is built under the process CWD
— which the ``cd "$REPO_ROOT"`` above pinned to the checkout root.  The
result is a stray ``%SystemDrive%/ProgramData/Microsoft/Windows/Caches/``
tree in the repo.

Observed on 2026-08-16 18:29:34 in the shared checkout and reproduced
directly: the same ``env -i`` allowlist plus the MSIX/WindowsApps python
writes the tree; adding ``SYSTEMDRIVE=C:`` and nothing else makes it stop.

This is the wrapper-side twin of
``tests/secret_sources/test_child_env_windows_essentials.py``, which
guards the same failure mode in the secret-helper allowlists.  The test is
a static read of the script so it passes on POSIX too — what is under
test is the allowlist, not the host OS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_tests.sh"

# Absence of these is what produces the literal-%SystemDrive% tree.
_REQUIRED = ("SYSTEMDRIVE", "PROGRAMDATA")


def _clean_env_names() -> set[str]:
    """Names the script forwards through ``env -i``.

    The script forwards a name two ways, and both count:

    * literally, in the ``CLEAN_ENV=( ... )`` block or a ``CLEAN_ENV+=( ... )``
      append (``"PATH=$PATH"``, ``"USERPROFILE=$USERPROFILE"``);
    * indirectly, via a ``for _var in A B C; do ... CLEAN_ENV+=("$_var=$_val")``
      loop, where the names live in the loop header.

    A ``"$_var=..."`` entry is deliberately NOT counted as the literal name
    ``_var`` — the real names come from the loop header instead.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    names: set[str] = set()
    for block in re.findall(r"CLEAN_ENV\+?=\(\s*(.*?)\)", text, re.DOTALL):
        # (?<![$\w]) keeps `"$_var=..."` and `FOO_BAR=` tails out of the set.
        names.update(re.findall(r'(?<![$\w])([A-Za-z_][A-Za-z0-9_]*)=', block))
    for header, body in re.findall(r"for _var in\s+(.*?);?\s*do(.*?)done", text, re.DOTALL):
        if "CLEAN_ENV+=" in body:
            names.update(re.findall(r"\b([A-Z][A-Z0-9_]*)\b", header))
    return names


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_runner_is_launched_through_env_i() -> None:
    """Guard the premise: if ``env -i`` goes away, this test's subject does too."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'env -i "${CLEAN_ENV[@]}"' in text, (
        "run_tests.sh no longer launches the runner via `env -i \"${CLEAN_ENV[@]}\"`; "
        "re-derive what the child environment is before trusting this file's guarantees"
    )


@pytest.mark.parametrize("var", _REQUIRED)
def test_clean_env_forwards_windows_os_path_var(var: str) -> None:
    """Each var must be named in CLEAN_ENV, else the child can't expand the template."""
    names = _clean_env_names()
    assert var in names, (
        f"{var} is not forwarded through `env -i` in scripts/run_tests.sh. "
        f"Without it a Windows child writes a literal %SystemDrive% tree into "
        f"$REPO_ROOT. Forwarded names: {sorted(names)}"
    )


def test_clean_env_parse_found_the_block() -> None:
    """Falsifier for the parser: a regex that matches nothing would pass vacuously."""
    names = _clean_env_names()
    assert {"PATH", "HOME", "PYTHONUTF8"} <= names, (
        f"CLEAN_ENV parsing looks broken — expected the known baseline vars, got {sorted(names)}"
    )
