"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.14+ crash.

    Python 3.14 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.
    """
    # Import the module where the parser is constructed in a SUBPROCESS.
    # If there are duplicate 'skills' subparsers, the import raises
    # argparse.ArgumentError at module load time and the process exits
    # non-zero.  A subprocess is used because importing hermes_cli.main
    # executes module-level startup code (dotenv load, plugin discovery,
    # config caches) with global side effects; a fresh in-process re-import
    # leaks those into the shared interpreter and breaks later tests
    # (cross-test pollution).
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", "import hermes_cli.main"],
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        if "conflicting subparser" in result.stderr:
            raise AssertionError(
                f"Duplicate subparser detected: {result.stderr[-1000:]} "
                "See issue #898 for details."
            )
        raise AssertionError(
            "import hermes_cli.main failed in subprocess:\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
