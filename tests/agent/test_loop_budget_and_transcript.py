"""Three control-loop defects that each undid a documented guarantee.

H-02: the max-iterations summary request was appended to the durable
      transcript, so a system-generated instruction was persisted attributed to
      the USER and replayed on the next turn.
H-03: IterationBudget.refund() decremented `_used` only. The loop is bounded by
      BOTH `api_call_count < max_iterations` AND `budget.remaining > 0`, and the
      budget is built as IterationBudget(max_iterations) — so after any refund
      the api_call_count half always binds first and the documented relief
      ("execute_code iterations are refunded") never happened.
H-04: the cron inactivity watchdog abandoned a NON-daemon worker, so a wedged
      turn was still joined at interpreter exit — the process hung on the very
      thread the watchdog existed to escape.
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from agent.iteration_budget import IterationBudget


# ── H-03: refunds must actually buy an iteration ─────────────────────────────

def test_refund_is_recorded_not_just_subtracted():
    budget = IterationBudget(10)
    for _ in range(4):
        budget.consume()
    budget.refund()
    assert budget.refunded == 1, "refund must be observable by the loop condition"
    assert budget.used == 3


def test_refund_grants_relief_the_old_condition_denied():
    """The exact scenario: budget has room, but api_call_count hit the wall."""
    MAX = 10
    budget = IterationBudget(MAX)
    api_calls = 0
    for i in range(MAX):
        budget.consume()
        api_calls += 1
        if i % 3 == 0:
            budget.refund()

    assert budget.remaining > 0, "budget still has room"
    assert not (api_calls < MAX), "old condition stops here — refund bought nothing"
    assert api_calls < MAX + budget.refunded, "new condition grants the relief"


def test_refunds_are_capped_at_half_the_budget():
    """Relief must be real but bounded — an agent that only calls execute_code
    must not be able to extend its own turn without limit."""
    budget = IterationBudget(10)
    for _ in range(10):
        budget.consume()
    for _ in range(20):
        budget.refund()
    assert budget.refunded == budget.max_refunds == 5


def test_refund_on_an_unused_budget_is_a_noop():
    budget = IterationBudget(10)
    budget.refund()
    assert budget.refunded == 0
    assert budget.used == 0


def test_consume_still_stops_at_max_total():
    budget = IterationBudget(3)
    assert [budget.consume() for _ in range(4)] == [True, True, True, False]


def test_loop_condition_uses_the_refund_allowance():
    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)
    assert "agent.max_iterations + agent.iteration_budget.refunded" in src, (
        "the loop no longer extends its allowance by refunds — refund() is inert again"
    )


# ── H-02: the durable transcript must stay clean ─────────────────────────────

def test_summary_request_is_not_appended_to_the_transcript():
    from agent.chat_completion_helpers import handle_max_iterations

    import re

    src = inspect.getsource(handle_max_iterations)
    # \b anchors the identifier: "api_messages.append(...)" CONTAINS
    # "messages.append(...)" as a substring, so a plain `not in` check can
    # never pass and would assert nothing.
    persisted = re.search(
        r'(?<!api_)\bmessages\.append\(\{"role": "user", "content": summary_request\}\)',
        src,
    )
    assert not persisted, (
        "the summary request is being persisted to the durable transcript again "
        "— it will be replayed next turn as though the user said it"
    )
    assert 'api_messages.append({"role": "user", "content": summary_request})' in src, (
        "the request must still reach the provider for this one call"
    )


def test_summary_request_still_forbids_further_tools():
    from agent.chat_completion_helpers import handle_max_iterations

    src = inspect.getsource(handle_max_iterations)
    assert "without calling any more tools" in src


# ── H-04: a wedged cron turn must be abandonable ─────────────────────────────

def test_cron_uses_daemon_workers():
    from cron import scheduler

    src = inspect.getsource(scheduler)
    assert "DaemonThreadPoolExecutor(max_workers=1)" in src, (
        "cron is back on the stdlib pool — a wedged agent turn will be joined "
        "at interpreter exit despite shutdown(wait=False)"
    )


def test_daemon_pool_really_abandons_a_wedged_worker():
    """Behavioural, not just structural."""
    from concurrent.futures.thread import _threads_queues

    from tools.daemon_pool import DaemonThreadPoolExecutor

    release = threading.Event()
    started = threading.Event()
    pool = DaemonThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(lambda: (started.set(), release.wait(60)))
        assert started.wait(5)
        worker = list(pool._threads)[0]
        assert worker.daemon, "non-daemon worker blocks interpreter exit"
        assert worker not in _threads_queues, "still joined by the atexit hook"

        began = time.monotonic()
        pool.shutdown(wait=False, cancel_futures=True)
        assert time.monotonic() - began < 5.0, "shutdown blocked on the wedged turn"
    finally:
        release.set()
