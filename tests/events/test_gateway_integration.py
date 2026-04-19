"""Tests for events.gateway_integration — startup/shutdown wiring."""

import time

from events import gateway_integration as gi
from events.subscribers.mailbox_translator import MailboxTranslator


def test_mailbox_translator_registered_at_startup():
    gi.startup()
    try:
        subs = gi._registry.subscribers
        assert any(isinstance(s, MailboxTranslator) for s in subs), (
            "MailboxTranslator must be registered at gateway startup"
        )
    finally:
        gi.shutdown()


def test_poll_loop_survives_unexpected_exception_in_body():
    """Outer try/except must keep the polling thread alive when something
    escapes the inner per-block try/excepts.

    Regression guard against the silent-notification failure mode: if the
    poll thread dies, all subscribers stop forever and the user sees only
    silence.  We simulate that by replacing ``registry.subscribers`` with
    an iterable that raises on every iteration — without the outer safety
    net the thread would die on the first tick.
    """
    gi.startup()
    try:
        class _PoisonPillIterable:
            def __iter__(self):
                raise RuntimeError("synthetic iteration failure for test")

        gi._registry.subscribers = _PoisonPillIterable()

        # Wait long enough for at least two 1s loop ticks to occur.
        time.sleep(2.5)

        assert gi._subscriber_thread is not None
        assert gi._subscriber_thread.is_alive(), (
            "Poll loop thread must survive exceptions that escape the "
            "inner try/excepts — otherwise notifications silently stop"
        )
    finally:
        # Restore a real list so shutdown() can iterate subscribers.
        if gi._registry is not None:
            gi._registry.subscribers = []
        gi.shutdown()
