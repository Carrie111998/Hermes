"""Tests for events.gateway_integration — startup/shutdown wiring."""

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
