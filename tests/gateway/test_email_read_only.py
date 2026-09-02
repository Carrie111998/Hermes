"""Email read-only / no-auto-reply mode (#99876).

``platforms.email.extra.read_only: true`` (or ``EMAIL_READ_ONLY=1``) lets a
mailbox be used purely as an inbound feed: IMAP polling / dispatch is
unchanged, but every outgoing send is suppressed before it reaches SMTP. A
suppressed send returns ``success=True`` so the gateway's delivery ledger
marks it delivered rather than retrying — the failure loop that disabling the
SMTP credential would cause. These tests pin that no SMTP helper is invoked in
read-only mode, that the default (unset) still sends, and that all three
adapter send entry points are covered.
"""
import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch


def _make_adapter(*, extra=None, env=None):
    from gateway.config import PlatformConfig
    from plugins.platforms.email.adapter import EmailAdapter

    with patch.dict(os.environ, env or {}, clear=False):
        return EmailAdapter(PlatformConfig(enabled=True, extra=extra or {}))


class TestEmailReadOnly(unittest.TestCase):
    def test_read_only_via_extra_suppresses_send(self):
        adapter = _make_adapter(extra={"read_only": True})
        adapter._send_email = MagicMock(name="_send_email")

        result = asyncio.run(adapter.send("user@example.com", "hi"))

        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "read-only-suppressed")
        adapter._send_email.assert_not_called()

    def test_read_only_via_env_suppresses_send(self):
        adapter = _make_adapter(env={"EMAIL_READ_ONLY": "true"})
        adapter._send_email = MagicMock(name="_send_email")

        result = asyncio.run(adapter.send("user@example.com", "hi"))

        self.assertTrue(result.success)
        adapter._send_email.assert_not_called()

    def test_default_is_not_read_only_and_sends(self):
        adapter = _make_adapter()  # no extra, no env -> read_only stays False
        self.assertFalse(adapter._read_only)
        adapter._send_email = MagicMock(return_value="<mid@localhost>")

        result = asyncio.run(adapter.send("user@example.com", "hi"))

        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "<mid@localhost>")
        adapter._send_email.assert_called_once()

    def test_extra_overrides_env(self):
        # Explicit config.yaml value wins over the env mirror.
        adapter = _make_adapter(extra={"read_only": False}, env={"EMAIL_READ_ONLY": "true"})
        self.assertFalse(adapter._read_only)

    def test_read_only_suppresses_document_and_image_batch(self):
        adapter = _make_adapter(extra={"read_only": True})
        adapter._send_email_with_attachment = MagicMock(name="doc")
        adapter._send_email_with_attachments = MagicMock(name="imgs")

        doc = asyncio.run(adapter.send_document("user@example.com", "/tmp/x.pdf"))
        self.assertTrue(doc.success)
        self.assertEqual(doc.message_id, "read-only-suppressed")
        adapter._send_email_with_attachment.assert_not_called()

        # send_multiple_images returns None; the point is no SMTP is attempted.
        asyncio.run(
            adapter.send_multiple_images(
                "user@example.com", [("file:///tmp/a.png", "")],
            )
        )
        adapter._send_email_with_attachments.assert_not_called()


if __name__ == "__main__":
    unittest.main()
