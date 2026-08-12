"""Boot-time daemon threads must be retired on EVERY ``start_gateway`` exit.

Companion to ``test_early_boot_heartbeat_binding.py``. That file fixed the
early-boot heartbeat and the nous auth keepalive; the same defect survives in
two more threads started by ``start_gateway``, both of which re-resolve
``HERMES_HOME`` on every tick:

* ``_run_planned_stop_watcher`` — started ~70 lines BEFORE
  ``await runner.start()``, but ``_planned_stop_watcher_stop.set()`` sits at the
  very bottom of the function. Both the ``if not success:`` return and the
  ``if runner.should_exit_with_failure:`` return skip it, so a failed start
  leaks a 0.5s poll loop for the life of the process. It calls
  ``planned_stop_marker_targets_self()``, which re-resolves the marker path and
  *unlinks* stale/malformed markers.

* ``_start_gateway_housekeeping`` — shares ``cron_stop``, which is only set
  after the ``should_exit_with_failure`` return. Its hourly chore calls
  ``cleanup_image_cache()``, which resolves ``get_image_cache_dir()`` live and
  ``unlink()``s every file older than 24h. A leaked housekeeping thread whose
  env has been restored deletes real ``~/.hermes/image_cache`` entries.

The discriminator for this bug class is NOT "is it a daemon" — daemon-ness only
governs interpreter exit, which is irrelevant inside a long pytest session
where the next tick lands minutes earlier. It is: *an unbounded poll loop whose
stop is not reachable from every return, including the raising one.*

See GBrain ``concepts/import-time-hermes-home-snapshot-bug``.
"""

import inspect

import pytest

from gateway import run as gateway_run


@pytest.fixture(scope="module")
def start_gateway_src() -> str:
    return inspect.getsource(gateway_run.start_gateway)


def _slice_between(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    tail = src[start:]
    return tail[: tail.index(end_marker)]


def _aborted_startup_block(src: str) -> str:
    """The ``if not runner._running:`` early-exit region.

    Reached when startup is aborted by restart/shutdown before running mode.
    It sits AFTER the planned-stop watcher starts but BEFORE ``cron_stop``
    exists, so only the watcher can leak here.
    """
    return _slice_between(src, "if not runner._running:", "# Start the background cron scheduler")


def _final_failure_exit_block(src: str) -> str:
    """The BODY of the post-``wait_for_shutdown()`` failure return.

    Anchored on the LAST ``should_exit_with_failure`` — the first one belongs
    to the aborted-startup block above. Sliced to its own ``return False`` so
    the assertion cannot sweep in the ``.set()`` calls sitting below it, which
    is exactly what this branch skips.
    """
    start = src.rindex("if runner.should_exit_with_failure:")
    tail = src[start:]
    return tail[: tail.index("return False")]


def test_planned_stop_watcher_is_retired_before_the_failed_start_return(
    start_gateway_src,
):
    """`if not success: return False` must not leak the marker-poll thread."""
    between = _slice_between(
        start_gateway_src,
        "_planned_stop_watcher_stop = threading.Event()",
        "await runner.wait_for_shutdown()",
    )

    assert "_planned_stop_watcher_stop.set()" in between, (
        "start_gateway() returns early (failed start / clean exit) without "
        "setting _planned_stop_watcher_stop — the watcher thread keeps polling "
        "for the life of the process and re-resolves the planned-stop marker "
        "path on every 0.5s tick"
    )


def test_planned_stop_watcher_is_retired_on_the_failure_exit_return(
    start_gateway_src,
):
    """`if runner.should_exit_with_failure: return False` must retire it too."""
    between = _final_failure_exit_block(start_gateway_src)

    assert "_planned_stop_watcher_stop.set()" in between, (
        "the should_exit_with_failure return path skips the watcher stop that "
        "sits below it"
    )


def test_planned_stop_watcher_is_retired_on_the_aborted_startup_path(
    start_gateway_src,
):
    """`if not runner._running:` returns twice, both after the watcher started."""
    between = _aborted_startup_block(start_gateway_src)

    assert "_planned_stop_watcher_stop.set()" in between, (
        "startup aborted before running mode returns without retiring the "
        "planned-stop watcher; cron_stop does not exist yet on this path, so "
        "the watcher is the only thread that leaks here"
    )


def test_housekeeping_loop_is_retired_on_the_failure_exit_return(
    start_gateway_src,
):
    """cron_stop gates the housekeeping loop that unlinks the image cache."""
    between = _final_failure_exit_block(start_gateway_src)

    assert "cron_stop.set()" in between, (
        "should_exit_with_failure returns before cron_stop.set(), leaking the "
        "gateway-housekeeping thread; its hourly cleanup_image_cache() "
        "resolves get_image_cache_dir() live and unlinks files older than 24h, "
        "so a restored HERMES_HOME means deleting real ~/.hermes/image_cache "
        "entries"
    )
