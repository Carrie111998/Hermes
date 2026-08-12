"""One bound for every ``asyncio.wait_for`` that exists only to catch a hang.

Most ``wait_for`` calls in ``tests/gateway`` are NOT timing assertions. They
wrap an operation that returns immediately once some signal fires -- a stop
flag is set, an abort path runs, a task is cancelled -- and every real
assertion in the test comes *after* the await. The bound is there for one
reason: a genuine deadlock should fail the test instead of hanging until
pytest-timeout kills the process, which reports every remaining test in the
file as "no tests ran".

Those guards were written with the wall clock of an idle box in mind: 0.2s,
0.5s, 1s, 2s. That is a *speed* claim in disguise. The nightly gate runs the
suite through ``scripts/run_tests_parallel.py`` at 12 workers, and under that
contention an operation that takes microseconds when idle can take seconds.
On 2026-08-12 the gate went red on
``test_startup_restart_race.py::test_startup_aborts_when_restart_begins_during_platform_connect``
with a bare ``asyncio.TimeoutError`` -- ``runner.start()`` needed more than
its 2s guard under load -- while the same test passed standalone in ~11s.

A correct run never spends this, so it costs nothing; it is only paid when
something is genuinely wedged.

**The value must stay strictly below the tightest per-test cap in play.**
Two are: ``pyproject.toml`` addopts carry ``--timeout=30``, and the nightly
gate overrides that with an explicit ``--timeout=60``. pytest-timeout's
``thread`` method HARD-EXITS the whole pytest process, so if it wins the race
the entire FILE is lost and reports as "no tests ran" instead of one
TimeoutError naming the slow await. 30 would tie with the local cap -- fine
under the gate, useless under a plain ``python -m pytest``. 20 fires first in
both regimes and is still ~10x the bounds it replaces.

DO NOT use this for a wait_for whose timeout IS the assertion -- where the
test proves an operation is *prompt* or that the event loop is *not blocked*.
Widening one of those makes it silently vacuous, and in the bounded-teardown
idiom below it does something worse: it hangs the file.

Deliberately NOT swept:

  * The bounded-teardown idiom, which appears in THREE files --
    ``test_bounded_adapter_teardown.py``, ``test_runner_fatal_adapter.py``,
    ``test_safe_adapter_disconnect.py``. Shape::

        done, _pending = await asyncio.wait({operation}, timeout=0.2)
        try:
            assert operation in done          # <- asserts teardown is BOUNDED
        finally:
            release.set()
            await asyncio.wait({operation}, timeout=0.2)
            await asyncio.wait_for(finished.wait(), timeout=0.2)

    The 0.2 in the ``finally`` looks like cleanup but belongs to the same
    bounded contract. Widening the whole file's guards took
    test_runner_fatal_adapter.py from "7 passed in 23.6s" to a pytest-timeout
    kill on its FIRST test, and the same for
    test_pending_drain_no_recursion.py ("5 passed in 20.3s" -> killed).
    Both were reverted 2026-08-12.

That wants a barrier or an ordering primitive, not a bigger number.

Two worked examples of that conversion, if you need one:
``test_telegram_background_connect.py`` / ``test_whatsapp_background_connect.py``
each carried two ``wait_for(runner.start(), timeout=30)`` calls whose timeout WAS
the assertion ("start() does not block on a hung platform adapter"), boxed in
with no legal range -- 30 already tied pytest-timeout's local cap, and the
WhatsApp one measured ~30.7s standalone, so it was losing. They were rewritten
on 2026-08-12 to park the connect on an unreleased ``asyncio.Event`` and assert
the *ordering* instead: the api_server stand-in records, at the instant of its
bind, whether the platform connect had returned yet. The bounds that remain are
real hang guards -- semantic assertions follow them -- so they use the constant
below. Verified both ways: green per file, and both ordering tests fail with a
TimeoutError at the bind barrier when ``_should_connect_in_background`` is
stubbed to ``False``.

``test_ddp_approval_commands.py`` carried the other one:
``wait_for(ticked.wait(), timeout=0.2)`` after a ``call_soon``, asserting the
loop was not blocked by a sync ledger read. Converted 2026-08-12 by giving the
parked read a ``read_in_progress`` threading.Event that is set for exactly as
long as it is parked; the test schedules the callback, waits for it, and asserts
``read_in_progress`` is STILL SET. If the read were back on the loop the
callback could not run until the read finished, and the flag would be clear by
then -- so the discriminator is state, not a deadline. Verified by moving the
``asyncio.to_thread`` in ``gateway/slash_commands.py`` back onto the loop: the
test fails with its own message ("the blocking read is back on the event loop").
Note the parked read still needs a bound of its own (``_PARKED_READ_MAX_S``) so
that regression FAILS rather than wedging the file -- that bound is a guard, and
deliberately not inside an ``assert``.

Verify per FILE, never by passing many files to one pytest. These files leak
module-global state across file boundaries (``gateway/delivery_ledger._DB_LOCK``
wedges a 25-file single-process run), and the nightly harness always spawns one
process per file, so a single-process run reports failures that cannot happen
in production.
"""

HANG_GUARD_S = 20
