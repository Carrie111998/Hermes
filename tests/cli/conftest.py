"""Shared fixtures for CLI tests.

prompt_toolkit / capsys isolation
---------------------------------
``cli._cprint`` renders through ``prompt_toolkit.print_formatted_text``,
which — when called with no explicit ``output=`` — lazily creates an
``Output`` from ``sys.stdout`` **and caches it on the process-global default
``AppSession``** (``prompt_toolkit.application.current._current_app_session``,
a ``ContextVar`` with a module-level default). The cache is keyed to nothing
and never re-reads ``sys.stdout``.

Under pytest, ``capsys`` swaps ``sys.stdout`` for a fresh buffer per test.
So the first CLI test that emits through ``_cprint`` (e.g. one exercising
``/queue``, which prints a "Queued: …" line) locks prompt_toolkit's cached
output onto *its* captured stdout. Every later ``capsys`` test that asserts
on ``_cprint`` output then reads an empty buffer, because the render went to
the first test's now-dead capture target. That is the mechanism behind the
order-dependent ``test_resume_quiet_stderr`` failure: it passes in isolation
and in its own file, but fails in a full ``tests/cli`` run.

Reset the cached output before every CLI test so each one re-creates a fresh
prompt_toolkit ``Output`` bound to its own ``sys.stdout`` on first use. This
is a no-op when prompt_toolkit isn't importable and cheap otherwise (the
property re-creates lazily).
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_prompt_toolkit_output_cache():
    """Clear prompt_toolkit's cached AppSession output around each CLI test.

    See the module docstring for the capsys/prompt_toolkit interaction this
    guards against.
    """

    def _clear() -> None:
        try:
            from prompt_toolkit.application.current import get_app_session

            get_app_session()._output = None
        except Exception:
            # prompt_toolkit not importable / internal shape changed — the
            # tests that rely on this simply keep their prior behavior.
            pass

    _clear()
    yield
    _clear()


def pytest_runtest_setup(item):
    """Restore ``cli._pt_print`` / ``cli._PT_ANSI`` if a previous test left
    them bound to MagicMocks.

    Several CLI test helpers (``_make_cli`` in test_cli_init.py,
    test_cli_user_message_preview.py, test_tool_progress_scrollback.py, …)
    use ``patch.dict(sys.modules, prompt_toolkit_stubs)`` +
    ``importlib.reload(cli)`` to exercise HermesCLI under mocked prompt_toolkit.
    ``patch.dict`` restores ``sys.modules`` on exit, but the reload already
    rebound ``cli._pt_print`` / ``cli._PT_ANSI`` to MagicMock objects — and
    those module globals are NOT restored. Every later test that routes output
    through ``cli._cprint`` then silently no-ops, because ``_pt_print(...)``
    hits a mock instead of ``print_formatted_text``. This is the mechanism
    behind the order-dependent ``test_resume_quiet_stderr`` failure: it passes
    in isolation and in its own file, but fails in a full ``tests/cli`` run
    when it runs after any of those reload-stubbing tests.

    Detect the mock here (before the test body) and reload cli once with the
    real modules visible so its globals rebind cleanly.
    """
    try:
        import cli
        import prompt_toolkit

        if cli._pt_print is not prompt_toolkit.print_formatted_text:
            import importlib

            importlib.reload(cli)
    except Exception:
        pass
