"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.11+ crash.

    Python 3.11 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.
    """
    # Force fresh import of the module where parser is constructed
    # If there are duplicate 'skills' subparsers, this import will raise
    # argparse.ArgumentError at module load time
    import sys

    # Remove cached module if present. The ORIGINAL module object is kept and
    # restored in the finally below: re-importing installs a NEW object, and
    # any other test module holding `from hermes_cli import main as cli_main`
    # would keep patching the dead one while production code resolves the live
    # one. That divergence caused real `git stash push` calls against the repo
    # from the update-guard tests (see Operations.md). Note importlib.reload()
    # would NOT have this problem (it mutates in place) but cannot be used
    # here: this test needs a genuinely fresh import to re-run the
    # module-level argparse registration it is asserting on.
    import hermes_cli

    original = sys.modules.get('hermes_cli.main')
    if 'hermes_cli.main' in sys.modules:
        del sys.modules['hermes_cli.main']

    try:
        import hermes_cli.main  # noqa: F401
    except argparse.ArgumentError as e:
        if "conflicting subparser" in str(e):
            raise AssertionError(
                f"Duplicate subparser detected: {e}. "
                "See issue #898 for details."
            ) from e
        raise
    finally:
        if original is not None:
            # BOTH bindings must be restored. `import hermes_cli.main` sets
            # sys.modules['hermes_cli.main'] AND the `main` attribute on the
            # hermes_cli package, and `from hermes_cli import main` — which is
            # what production's update_cmd._m() uses — resolves via the
            # PACKAGE ATTRIBUTE. Restoring sys.modules alone leaves the
            # from-import still handing out the replacement module (verified
            # empirically), so the identity split this is meant to undo would
            # survive.
            sys.modules['hermes_cli.main'] = original
            hermes_cli.main = original
