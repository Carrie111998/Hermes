"""The sandbox orphan sweep must be bound to the home it was registered under.

``cli.py::_run_cleanup`` is registered with ``atexit`` and calls
``cleanup_all_environments()`` unconditionally (via the ``_cleanup_all_terminals``
passthrough at cli.py:945). That function resolves the scratch root *when it
runs* — and an atexit hook runs at interpreter shutdown, after pytest's
``monkeypatch`` teardown has restored the real ``HERMES_HOME``.

This is the mirror of the import-time snapshot bug: resolving inside the
function is the prescribed fix for "too early", and it is exactly what makes
this case "too late". The generalized rule is to resolve at the moment the
value's meaning is fixed and then CARRY it; for a deferred writer that moment
is REGISTRATION.

Two things make the leak worse than stray directories:

  * ``get_sandbox_dir()`` (tools/environments/base.py) calls
    ``p.mkdir(parents=True, exist_ok=True)``, so merely *resolving* the path
    writes. A pytest process creates ``<real home>/sandboxes/singularity/``
    on the way out even when it never sandboxed anything.
  * the sweep then ``shutil.rmtree``s every ``hermes-*`` entry under that
    root, with no owner-liveness check — unlike the browser reaper, which
    checks ``owner_pid``. ``singularity.py:189`` puts a live
    ``hermes-overlays`` directory there, so an exiting test process can
    delete a *running* process's overlays.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import tools.terminal_tool as terminal_tool


def test_resolve_scratch_dir_does_not_create_anything(tmp_path, monkeypatch):
    """Capturing the path at registration must not materialise a sandbox tree.

    Registration happens on every CLI start, including runs that never open a
    sandbox. If capture created the directory, the fix would trade a leak at
    exit for eager litter at startup.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TERMINAL_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

    resolved = terminal_tool.resolve_scratch_dir()

    assert resolved is not None
    assert not (home / "sandboxes").exists(), (
        "resolving the scratch root created it — capture must be side-effect free"
    )


def test_cleanup_honors_a_scratch_dir_captured_earlier(tmp_path, monkeypatch):
    """A captured root wins over whatever HERMES_HOME says at call time."""
    registration_home = tmp_path / "registration"
    restored_home = tmp_path / "restored"
    for h in (registration_home, restored_home):
        h.mkdir()
    monkeypatch.delenv("TERMINAL_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

    monkeypatch.setenv("HERMES_HOME", str(registration_home))
    captured = terminal_tool.resolve_scratch_dir()
    # Make the captured root real, so the sweep has somewhere to look.
    captured.mkdir(parents=True, exist_ok=True)

    # Teardown restores the other home.
    monkeypatch.setenv("HERMES_HOME", str(restored_home))
    terminal_tool.cleanup_all_environments(scratch_dir=captured)

    assert not (restored_home / "sandboxes").exists(), (
        "the sweep resolved HERMES_HOME at call time instead of using the "
        "captured root"
    )


def test_captured_scratch_dir_is_not_recreated_once_its_home_is_gone(
    tmp_path, monkeypatch
):
    """A hook bound to a deleted pytest tmp_path must leave no litter behind."""
    registration_home = tmp_path / "registration"
    registration_home.mkdir()
    monkeypatch.delenv("TERMINAL_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(registration_home))

    captured = terminal_tool.resolve_scratch_dir()

    # The tmp home goes away, as pytest's tmp_path does.
    import shutil

    shutil.rmtree(registration_home)

    terminal_tool.cleanup_all_environments(scratch_dir=captured)  # must not raise

    assert not registration_home.exists(), (
        "the sweep recreated a home that no longer exists"
    )


def test_exit_cleanup_never_writes_to_a_restored_hermes_home(tmp_path):
    """Regression guard for the leak, at real atexit timing.

    Runs in a subprocess because that is the only way to exercise genuine
    atexit ordering: the hook must actually outlive the env restore. Both
    homes are throwaway dirs, so proving the bug cannot itself pollute
    the real ~/.hermes.
    """
    registration_home = tmp_path / "registration"
    restored_home = tmp_path / "restored"
    for h in (registration_home, restored_home):
        h.mkdir()

    repo_root = Path(terminal_tool.__file__).resolve().parents[1]
    script = tmp_path / "exiting_process.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import atexit, os, sys
            sys.path.insert(0, {str(repo_root)!r})
            os.environ["HERMES_HOME"] = {str(registration_home)!r}
            os.environ.pop("TERMINAL_SCRATCH_DIR", None)
            os.environ.pop("TERMINAL_SANDBOX_DIR", None)

            import tools.terminal_tool as tt

            # Test body: register the exit sweep under the per-test home,
            # exactly as cli.py::_run_cleanup is registered.
            scratch_dir = tt.resolve_scratch_dir()
            atexit.register(tt.cleanup_all_environments, scratch_dir=scratch_dir)

            # monkeypatch teardown: the real HERMES_HOME comes back.
            os.environ["HERMES_HOME"] = {str(restored_home)!r}
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, result.stderr
    assert not (restored_home / "sandboxes").exists(), (
        "the atexit sweep created sandboxes/ in the RESTORED home — this is "
        "the production leak"
    )
