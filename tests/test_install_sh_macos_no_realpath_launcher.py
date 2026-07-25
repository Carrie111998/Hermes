"""Regression for #71320: `hermes update` breaks stock macOS when the
uv-generated venv console-script wrapper needs an external `realpath` binary.

Stock macOS (no Homebrew/coreutils) does not ship `realpath` on PATH. The
uv-generated console-script at `venv/bin/hermes` resolves its own location
via `realpath` at runtime on some uv/Python versions; when that lookup fails,
the launcher errors out with `realpath: command not found` and then a
misleading `ModuleNotFoundError: No module named 'yaml'` (PyYAML is actually
present in the venv — the launcher just never reaches that interpreter).

Fix: for venv installs, the user-facing shim written by `setup_path()` must
exec the venv's python interpreter directly against the checked-in `hermes`
entrypoint script, bypassing the uv wrapper (and its `realpath` dependency)
entirely. This also means the shim gets recreated correctly on every
`hermes update`, since it no longer depends on what uv regenerates at
`venv/bin/hermes`.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_venv_launcher_shim_does_not_exec_hermes_bin_directly() -> None:
    """Static guard: for USE_VENV, the shim must not shell out via $HERMES_BIN."""
    text = INSTALL_SH.read_text()
    assert 'exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/hermes"' in text, (
        "setup_path() must, for venv installs, write a shim that execs the "
        "venv python interpreter directly against the checked-in `hermes` "
        "entrypoint script instead of the uv console-script wrapper at "
        "$HERMES_BIN. See #71320."
    )


def test_venv_launcher_shim_runs_without_realpath_on_path(tmp_path: Path) -> None:
    """Behavioral repro: simulate the venv-branch shim with no `realpath` on PATH.

    Builds a fake INSTALL_DIR with a venv python and a checked-in `hermes`
    entrypoint script, drives the exact shim body used for USE_VENV=true, and
    runs it with a PATH stripped of `realpath` (simulating stock macOS) to
    confirm the launcher does not depend on that binary.
    """
    install_dir = tmp_path / "install"
    venv_bin = install_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)

    # Fake venv python: just execs the real interpreter's binary via a shell
    # wrapper so this stays a tiny, fast, dependency-free stand-in.
    import sys

    python_stub = venv_bin / "python"
    python_stub.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    python_stub.chmod(python_stub.stat().st_mode | stat.S_IXUSR)

    hermes_entry = install_dir / "hermes"
    hermes_entry.write_text(
        "import sys\n"
        "print('LAUNCHED:' + ','.join(sys.argv[1:]))\n"
    )

    command_link_dir = tmp_path / "local_bin"
    command_link_dir.mkdir()
    shim_path = command_link_dir / "hermes"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "unset PYTHONPATH\n"
        "unset PYTHONHOME\n"
        f'exec "{install_dir}/venv/bin/python" "{install_dir}/hermes" "$@"\n'
    )
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR)

    # PATH with no `realpath` on it — a minimal directory of symlinks to the
    # binaries actually needed (bash, python), excluding realpath, so this
    # simulates stock macOS regardless of what else lives alongside bash on
    # the host running the test.
    import os
    import shutil

    bash_path = shutil.which("bash")
    assert bash_path is not None, "test setup invalid: bash not found on PATH"

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    os.symlink(bash_path, fake_bin / "bash")
    os.symlink(sys.executable, fake_bin / Path(sys.executable).name)
    minimal_path = str(fake_bin)

    result = subprocess.run(
        ["bash", str(shim_path), "foo", "bar"],
        capture_output=True,
        text=True,
        env={"PATH": minimal_path, "HOME": str(tmp_path)},
    )

    assert shutil.which("realpath", path=minimal_path) is None, (
        "test setup invalid: `realpath` must be absent from the minimal PATH "
        "used to simulate stock macOS"
    )
    assert result.returncode == 0, (
        f"launcher shim failed without `realpath` on PATH:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "LAUNCHED:foo,bar" in result.stdout
