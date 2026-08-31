"""Email adapter robustness against malformed IMAP responses (salvage of #2794).

Validates that:
- Malformed IMAP fetch responses are skipped instead of aborting the batch
  (UIDs are marked seen before fetch, so an abort permanently loses messages)
- Message-ID generation handles a missing '@' in EMAIL_ADDRESS
"""

import asyncio
import os
import ssl
import unittest
import uuid
from email.mime.text import MIMEText
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_adapter(address="hermes@test.com"):
    from gateway.config import PlatformConfig

    with patch.dict(os.environ, {
        "EMAIL_ADDRESS": address,
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
    }):
        from plugins.platforms.email.adapter import EmailAdapter

        adapter = EmailAdapter(PlatformConfig(enabled=True))
    return adapter


def _raw_email(sender="user@test.com", subject="Hello"):
    msg = MIMEText("Test body", "plain", "utf-8")
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex[:8]}@test.com>"
    return msg.as_bytes()


class TestImapResponseGuard(unittest.TestCase):
    """_fetch_new_messages skips messages with unexpected IMAP structure."""

    def _fetch_with(self, fetch_responses):
        adapter = _make_adapter()
        uids = b" ".join(
            str(i + 1).encode() for i in range(len(fetch_responses))
        )
        fetch_iter = iter(fetch_responses)

        def uid_handler(command, *args):
            if command == "search":
                return ("OK", [uids])
            if command == "fetch":
                return next(fetch_iter)
            return ("NO", [])

        mock_imap = MagicMock()
        mock_imap.uid.side_effect = uid_handler
        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            return adapter._fetch_new_messages()

    def test_normal_response_parses(self):
        results = self._fetch_with([("OK", [(b"1 (RFC822 {123}", _raw_email())])])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sender_addr"], "user@test.com")

    def test_none_element_skipped(self):
        results = self._fetch_with([("OK", [None])])
        self.assertEqual(results, [])


class TestMailSecurityModes(unittest.TestCase):
    """Explicit bridge TLS settings are honored by IMAP and SMTP paths."""

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "127.0.0.1",
        "EMAIL_IMAP_PORT": "1143",
        "EMAIL_IMAP_SECURITY": "starttls",
        "EMAIL_IMAP_TLS_VERIFY": "false",
        "EMAIL_SMTP_HOST": "127.0.0.1",
        "EMAIL_SMTP_PORT": "1025",
        "EMAIL_SMTP_SECURITY": "starttls",
        "EMAIL_SMTP_TLS_VERIFY": "false",
    })
    def test_starttls_uses_plain_clients_and_unverified_contexts(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.email.adapter import EmailAdapter

        adapter = EmailAdapter(PlatformConfig(enabled=True))

        imap = MagicMock()
        smtp = MagicMock()
        with patch("imaplib.IMAP4", return_value=imap) as imap_cls, \
             patch("imaplib.IMAP4_SSL") as imap_ssl_cls, \
             patch("smtplib.SMTP", return_value=smtp) as smtp_cls, \
             patch("smtplib.SMTP_SSL") as smtp_ssl_cls:
            self.assertIs(adapter._connect_imap(), imap)
            self.assertIs(adapter._connect_smtp(), smtp)

        imap_cls.assert_called_once_with("127.0.0.1", 1143, timeout=30)
        imap_ssl_cls.assert_not_called()
        imap.starttls.assert_called_once()
        self.assertIsInstance(
            imap.starttls.call_args.kwargs["ssl_context"], ssl.SSLContext
        )
        self.assertEqual(
            imap.starttls.call_args.kwargs["ssl_context"].verify_mode,
            ssl.CERT_NONE,
        )
        smtp_cls.assert_called_once()
        smtp_ssl_cls.assert_not_called()
        smtp.starttls.assert_called_once()
        self.assertEqual(
            smtp.starttls.call_args.kwargs["context"].verify_mode,
            ssl.CERT_NONE,
        )

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_SMTP_HOST": "127.0.0.1",
        "EMAIL_SMTP_PORT": "1025",
        "EMAIL_SMTP_SECURITY": "starttls",
        "EMAIL_SMTP_TLS_VERIFY": "false",
    })
    def test_standalone_sender_honors_starttls_without_delivery(self):
        from plugins.platforms.email.adapter import _standalone_send

        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp) as smtp_cls, \
             patch("smtplib.SMTP_SSL") as smtp_ssl_cls:
            result = asyncio.run(_standalone_send(
                SimpleNamespace(extra={}),
                "nobody@example.invalid",
                "test",
            ))

        self.assertEqual(result["success"], True)
        smtp_cls.assert_called_once_with("127.0.0.1", 1025)
        smtp_ssl_cls.assert_not_called()
        smtp.starttls.assert_called_once()
        self.assertEqual(
            smtp.starttls.call_args.kwargs["context"].verify_mode,
            ssl.CERT_NONE,
        )
        smtp.send_message.assert_called_once()


class TestMessageIdDomain(unittest.TestCase):
    """Message-ID generation tolerates EMAIL_ADDRESS without '@'."""


    def test_address_without_at(self):
        adapter = _make_adapter("not-an-email")
        self.assertEqual(adapter._message_id_domain(), "localhost")


if __name__ == "__main__":
    unittest.main()
