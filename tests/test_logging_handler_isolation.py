"""Cross-test isolation for hermes_logging's module-global file handlers.

``hermes_logging`` keeps its rotating file handlers in a module-global list
(``_queued_file_handlers``) that a background ``QueueListener`` dispatches to.
A test that points ``HERMES_HOME`` at its own ``tmp_path`` (every test does —
see the autouse ``_hermetic_environment`` fixture) and triggers
``setup_logging()`` registers handlers rooted in that tempdir.  With
``tmp_path_retention_policy = "failed"`` (pyproject.toml) pytest deletes that
directory as soon as the test passes, but the handler stays registered in the
global — so the next test in the same process that emits a record makes the
listener thread write into a directory that no longer exists::

    --- Logging error ---
    concurrent_log_handler ... FileNotFoundError: [Errno 2] No such file or
    directory: '...\\pytest-NNN\\test_xxx0\\hermes_test\\logs\\.__errors.lock'

The spew is written from the listener thread, so it is attributed to no test
and fails nothing — it just corrupts the output of whatever ran afterwards.

The autouse ``_reset_queued_handlers()`` teardown in ``tests/conftest.py`` is
what prevents this.  The two tests below must run in file order: the first
registers a handler under its own HERMES_HOME, the second asserts that nothing
survived into a fresh test.
"""
import os
from pathlib import Path

import hermes_logging


def test_setup_logging_registers_a_handler_under_this_tests_home():
    """Precondition for the isolation check below: a handler really is
    registered, and it really does point into this test's throwaway home."""
    home = Path(os.environ["HERMES_HOME"])
    hermes_logging.setup_logging(hermes_home=home, force=True)

    handlers = hermes_logging.rotating_file_handlers()
    assert handlers, "setup_logging() registered no rotating file handlers"
    log_dir = (home / "logs").resolve()
    assert all(
        Path(h.baseFilename).resolve().parent == log_dir for h in handlers
    ), f"handlers not rooted in this test's home: {[h.baseFilename for h in handlers]}"


def test_no_handler_survives_into_the_next_test():
    """The previous test's handlers must not still be registered.

    Its HERMES_HOME is a tmp_path that pytest has already deleted, so any
    surviving handler writes into a vanished directory on the listener thread.
    """
    leaked = [h.baseFilename for h in hermes_logging.rotating_file_handlers()]
    assert leaked == [], (
        "rotating file handlers leaked from the previous test; they point at "
        f"deleted tmp dirs and spew logging errors from the listener thread: {leaked}"
    )
