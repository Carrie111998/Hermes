"""Tests for the Email gateway platform adapter.

Covers:
1. Platform enum exists with correct value
2. Config loading from env vars via _apply_env_overrides
3. Adapter init and config parsing
4. Helper functions (header decoding, body extraction, address extraction, HTML stripping)
5. Authorization integration (platform in allowlist maps)
6. Send message tool routing (platform in platform_map)
7. check_email_requirements function
8. Attachment extraction and caching
9. Message dispatch and threading
"""

import os
import unittest
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from unittest.mock import patch, MagicMock, AsyncMock, ANY

from gateway.platforms.base import SendResult


class TestConfigEnvOverrides(unittest.TestCase):
    """Verify email config is loaded from environment variables."""


    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_HOME_ADDRESS": "user@test.com",
    }, clear=False)
    def test_email_home_channel_loaded(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)
        home = config.platforms[Platform.EMAIL].home_channel
        self.assertIsNotNone(home)
        self.assertEqual(home.chat_id, "user@test.com")


class TestCheckRequirements(unittest.TestCase):
    """Verify check_email_requirements function."""

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "a@b.com",
        "EMAIL_PASSWORD": "pw",
        "EMAIL_IMAP_HOST": "imap.b.com",
        "EMAIL_SMTP_HOST": "smtp.b.com",
    }, clear=False)
    def test_requirements_met(self):
        from plugins.platforms.email.adapter import check_email_requirements
        self.assertTrue(check_email_requirements())


class TestHelperFunctions(unittest.TestCase):
    """Test email parsing helper functions."""


    def test_decode_header_encoded(self):
        from plugins.platforms.email.adapter import _decode_header_value
        # RFC 2047 encoded subject
        encoded = "=?utf-8?B?TWVyaGFiYQ==?="  # "Merhaba" in base64
        result = _decode_header_value(encoded)
        self.assertEqual(result, "Merhaba")

    def test_extract_email_address_with_name(self):
        from plugins.platforms.email.adapter import _extract_email_address
        self.assertEqual(
            _extract_email_address("John Doe <john@example.com>"),
            "john@example.com"
        )


    def test_strip_html_basic(self):
        from plugins.platforms.email.adapter import _strip_html
        html = "<p>Hello <b>world</b></p>"
        result = _strip_html(html)
        self.assertIn("Hello", result)
        self.assertIn("world", result)
        self.assertNotIn("<p>", result)
        self.assertNotIn("<b>", result)


class TestExtractTextBody(unittest.TestCase):
    """Test email body extraction from different message formats."""

    def test_plain_text_body(self):
        from plugins.platforms.email.adapter import _extract_text_body
        msg = MIMEText("Hello, this is a test.", "plain", "utf-8")
        result = _extract_text_body(msg)
        self.assertEqual(result, "Hello, this is a test.")


    def test_multipart_prefers_plain(self):
        from plugins.platforms.email.adapter import _extract_text_body
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("<p>HTML version</p>", "html", "utf-8"))
        msg.attach(MIMEText("Plain version", "plain", "utf-8"))
        result = _extract_text_body(msg)
        self.assertEqual(result, "Plain version")


class TestExtractAttachments(unittest.TestCase):
    """Test attachment extraction and caching."""

    def test_no_attachments(self):
        from plugins.platforms.email.adapter import _extract_attachments
        msg = MIMEText("No attachments here.", "plain", "utf-8")
        result = _extract_attachments(msg)
        self.assertEqual(result, [])


class TestDispatchMessage(unittest.TestCase):
    """Test email message dispatch logic."""

    def setUp(self):
        # These tests exercise dispatch mechanics (subject formatting,
        # attachment typing, source building), not the authorization gate.
        # The adapter now fails closed at dispatch when no allowlist / allow-all
        # is configured (SECURITY.md 2.6), so opt into allow-all here to keep
        # exercising the dispatch path. Auth-contract tests below override this.
        self._prev_allow_all = os.environ.get("EMAIL_ALLOW_ALL_USERS")
        os.environ["EMAIL_ALLOW_ALL_USERS"] = "true"

    def tearDown(self):
        if self._prev_allow_all is None:
            os.environ.pop("EMAIL_ALLOW_ALL_USERS", None)
        else:
            os.environ["EMAIL_ALLOW_ALL_USERS"] = self._prev_allow_all

    def _make_adapter(self):
        """Create an EmailAdapter with mocked env vars."""
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_IMAP_PORT": "993",
            "EMAIL_SMTP_HOST": "smtp.test.com",
            "EMAIL_SMTP_PORT": "587",
            "EMAIL_POLL_INTERVAL": "15",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter

    def test_self_message_filtered(self):
        """Messages from the agent's own address should be skipped."""
        import asyncio
        adapter = self._make_adapter()
        adapter._message_handler = MagicMock()

        msg_data = {
            "uid": b"1",
            "sender_addr": "hermes@test.com",
            "sender_name": "Hermes",
            "subject": "Test",
            "message_id": "<msg1@test.com>",
            "in_reply_to": "",
            "body": "Self message",
            "attachments": [],
            "date": "",
        }

        asyncio.run(adapter._dispatch_message(msg_data))
        adapter._message_handler.assert_not_called()

    def test_subject_included_in_text(self):
        """Subject should be prepended to body for non-reply emails."""
        import asyncio
        adapter = self._make_adapter()
        captured_events = []

        async def mock_handler(event):
            captured_events.append(event)
            return None

        adapter._message_handler = mock_handler
        # Override handle_message to capture the event directly
        original_handle = adapter.handle_message

        async def capture_handle(event):
            captured_events.append(event)

        adapter.handle_message = capture_handle

        msg_data = {
            "uid": b"2",
            "sender_addr": "user@test.com",
            "sender_name": "User",
            "subject": "Help with Python",
            "message_id": "<msg2@test.com>",
            "in_reply_to": "",
            "body": "How do I use lists?",
            "attachments": [],
            "date": "",
        }

        asyncio.run(adapter._dispatch_message(msg_data))
        self.assertEqual(len(captured_events), 1)
        self.assertIn("[Subject: Help with Python]", captured_events[0].text)
        self.assertIn("How do I use lists?", captured_events[0].text)

    def test_reply_subject_not_duplicated(self):
        """Re: subjects should not be prepended to body."""
        import asyncio
        adapter = self._make_adapter()
        captured_events = []

        async def capture_handle(event):
            captured_events.append(event)

        adapter.handle_message = capture_handle

        msg_data = {
            "uid": b"3",
            "sender_addr": "user@test.com",
            "sender_name": "User",
            "subject": "Re: Help with Python",
            "message_id": "<msg3@test.com>",
            "in_reply_to": "<msg2@test.com>",
            "body": "Thanks for the help!",
            "attachments": [],
            "date": "",
        }

        asyncio.run(adapter._dispatch_message(msg_data))
        self.assertEqual(len(captured_events), 1)
        self.assertNotIn("[Subject:", captured_events[0].text)
        self.assertEqual(captured_events[0].text, "Thanks for the help!")


    def test_image_attachment_sets_photo_type(self):
        """Email with image attachment should set message type to PHOTO."""
        import asyncio
        from gateway.platforms.base import MessageType
        adapter = self._make_adapter()
        captured_events = []

        async def capture_handle(event):
            captured_events.append(event)

        adapter.handle_message = capture_handle

        msg_data = {
            "uid": b"5",
            "sender_addr": "user@test.com",
            "sender_name": "User",
            "subject": "Re: photo",
            "message_id": "<msg5@test.com>",
            "in_reply_to": "",
            "body": "Check this photo",
            "attachments": [{"path": "/tmp/img.jpg", "filename": "img.jpg", "type": "image", "media_type": "image/jpeg"}],
            "date": "",
        }

        asyncio.run(adapter._dispatch_message(msg_data))
        self.assertEqual(len(captured_events), 1)
        self.assertEqual(captured_events[0].message_type, MessageType.PHOTO)
        self.assertEqual(captured_events[0].media_urls, ["/tmp/img.jpg"])


    def test_empty_allowlist_denies_without_optin(self):
        """No allowlist and no allow-all opt-in → adapter fails closed (2.6)."""
        import asyncio
        with patch.dict(os.environ, {}, clear=False):
            # No allowlist, and explicitly no allow-all opt-in.
            for k in ("EMAIL_ALLOWED_USERS", "EMAIL_ALLOW_ALL_USERS",
                      "GATEWAY_ALLOW_ALL_USERS"):
                os.environ.pop(k, None)

            adapter = self._make_adapter()
            adapter._message_handler = MagicMock()

            msg_data = {
                "uid": b"101",
                "sender_addr": "anyone@test.com",
                "sender_name": "Anyone",
                "subject": "Hey",
                "message_id": "<any@test.com>",
                "in_reply_to": "",
                "body": "Hi",
                "attachments": [],
                "date": "",
            }

            asyncio.run(adapter._dispatch_message(msg_data))
            # Fail closed: an unset allowlist without allow-all drops the sender.
            adapter._message_handler.assert_not_called()


    def test_unauthenticated_allowed_with_allow_all(self):
        """EMAIL_ALLOW_ALL_USERS=true makes sender identity moot — gate skipped.

        With allow-all and no restrictive allowlist, an unauthenticated sender
        is forwarded: the operator has explicitly chosen to accept anyone.
        """
        import asyncio
        with patch.dict(os.environ, {
            "EMAIL_ALLOW_ALL_USERS": "true",
        }):
            os.environ.pop("EMAIL_ALLOWED_USERS", None)
            os.environ.pop("GATEWAY_ALLOWED_USERS", None)
            adapter = self._make_adapter()
            captured = []

            async def capture_handle(event):
                captured.append(event)

            adapter.handle_message = capture_handle

            msg_data = {
                "uid": b"203",
                "sender_addr": "stranger@elsewhere.com",
                "sender_name": "Stranger",
                "subject": "Hi",
                "message_id": "<s@elsewhere.com>",
                "in_reply_to": "",
                "body": "Hello",
                "attachments": [],
                "date": "",
                "sender_authenticated": False,
                "auth_reason": "no Authentication-Results header",
            }

            asyncio.run(adapter._dispatch_message(msg_data))
            self.assertEqual(len(captured), 1)


class TestThreadContext(unittest.TestCase):
    """Test email reply threading logic."""

    def setUp(self):
        # Thread-context storage is a dispatch-mechanics test, not an auth test.
        # The adapter fails closed at dispatch without allow-all (SECURITY.md 2.6),
        # so opt into allow-all to keep exercising the threading path.
        self._prev_allow_all = os.environ.get("EMAIL_ALLOW_ALL_USERS")
        os.environ["EMAIL_ALLOW_ALL_USERS"] = "true"

    def tearDown(self):
        if self._prev_allow_all is None:
            os.environ.pop("EMAIL_ALLOW_ALL_USERS", None)
        else:
            os.environ["EMAIL_ALLOW_ALL_USERS"] = self._prev_allow_all

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter


    def test_reply_uses_re_prefix(self):
        """Reply subject should have Re: prefix."""
        adapter = self._make_adapter()
        adapter._thread_context["user@test.com"] = {
            "subject": "Project question",
            "message_id": "<original@test.com>",
        }

        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server

            adapter._send_email("user@test.com", "Here is the answer.", None)

            # Check the sent message
            send_call = mock_server.send_message.call_args[0][0]
            self.assertEqual(send_call["Subject"], "Re: Project question")
            self.assertEqual(send_call["In-Reply-To"], "<original@test.com>")
            self.assertEqual(send_call["References"], "<original@test.com>")
            self.assertIn("Date", send_call)


class TestSendMethods(unittest.TestCase):
    """Test email send methods."""

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter


    def test_send_document_with_attachment(self):
        """send_document should send email with file attachment."""
        import asyncio
        import tempfile
        adapter = self._make_adapter()

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"Test document content")
            tmp_path = f.name

        try:
            with patch("smtplib.SMTP") as mock_smtp:
                mock_server = MagicMock()
                mock_smtp.return_value = mock_server

                result = asyncio.run(
                    adapter.send_document("user@test.com", tmp_path, "Here is the file")
                )

                self.assertTrue(result.success)
                mock_server.send_message.assert_called_once()
                sent_msg = mock_server.send_message.call_args[0][0]
                # Should be multipart with attachment
                parts = list(sent_msg.walk())
                has_attachment = any(
                    "attachment" in str(p.get("Content-Disposition", ""))
                    for p in parts
                )
                self.assertTrue(has_attachment)
        finally:
            os.unlink(tmp_path)


    def test_get_chat_info(self):
        """get_chat_info should return email address as chat info."""
        import asyncio
        adapter = self._make_adapter()
        adapter._thread_context["user@test.com"] = {"subject": "Test", "message_id": "<m@t>"}

        info = asyncio.run(
            adapter.get_chat_info("user@test.com")
        )

        self.assertEqual(info["name"], "user@test.com")
        self.assertEqual(info["type"], "dm")
        self.assertEqual(info["subject"], "Test")


def _configure_seed_imap(mock_imap, *, count, uids=()):
    """Configure a mock IMAP connection for connect()'s seen-UID seeding.

    connect() seeds ``_seen_uids`` by SELECTing INBOX (whose response carries
    the message count) and then FETCHing the most recent ``_seen_uids_max``
    *sequence numbers* to read their UIDs — it deliberately does not issue
    ``UID SEARCH ALL``, which returns every UID on one multi-MB line.

    ``count`` is the mailbox size reported by SELECT; ``uids`` are the UIDs the
    FETCH should report, numbered from the start of the fetched range.
    """
    mock_imap.select.return_value = ("OK", [str(count).encode()])
    start = max(1, count - len(uids) + 1) if uids else 1
    mock_imap.fetch.return_value = (
        "OK",
        [b"%d (UID %s)" % (start + i, u) for i, u in enumerate(uids)],
    )
    # Also answer UID SEARCH the way a real server would. connect() must not
    # use it, but serving it keeps the "no UID SEARCH ALL" assertions honest:
    # a regression back to the search path fails on that assertion rather than
    # on an unconfigured-mock error.
    mock_imap.uid.return_value = ("OK", [b" ".join(uids)])
    return mock_imap


class TestConnectDisconnect(unittest.TestCase):
    """Test IMAP/SMTP connection lifecycle."""

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter

    def test_connect_success(self):
        """Successful IMAP + SMTP connection returns True."""
        import asyncio
        adapter = self._make_adapter()

        mock_imap = MagicMock()
        _configure_seed_imap(mock_imap, count=3, uids=(b"101", b"102", b"103"))

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server

            result = asyncio.run(adapter.connect())

            self.assertTrue(result)
            self.assertTrue(adapter._running)
            # Existing messages are seeded as "seen" so they are not dispatched.
            self.assertEqual(adapter._seen_uids, {b"101", b"102", b"103"})
            # Cleanup
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

    def test_connect_seeds_only_recent_window_on_large_mailbox(self):
        """A mailbox larger than _seen_uids_max seeds via a bounded FETCH range.

        Regression test for the endless reconnect loop on large INBOXes:
        ``UID SEARCH ALL`` returned every UID on a single line, overflowing
        imaplib's 1 MB ``_MAXLINE`` cap (a 423k-message Gmail INBOX produced a
        ~2.8 MB line) and aborting connect(). connect() must instead FETCH only
        the newest ``_seen_uids_max`` sequence numbers.
        """
        import asyncio
        adapter = self._make_adapter()

        count = 423832
        window = adapter._seen_uids_max
        seeded = tuple(str(431000 + i).encode() for i in range(window))

        mock_imap = MagicMock()
        _configure_seed_imap(mock_imap, count=count, uids=seeded)

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()

            result = asyncio.run(adapter.connect())

            self.assertTrue(result)
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

        # The bounded range covers exactly the newest _seen_uids_max messages.
        expected_lo = count - window + 1
        mock_imap.fetch.assert_called_once_with(f"{expected_lo}:{count}", "(UID)")

        # And the unbounded whole-mailbox search must not be issued at all.
        search_all_calls = [
            c for c in mock_imap.uid.call_args_list
            if c.args and c.args[0] == "search" and "ALL" in c.args
        ]
        self.assertEqual(
            search_all_calls, [],
            "connect() must not issue UID SEARCH ALL — on a large mailbox its "
            "single-line response overflows imaplib._MAXLINE and wedges the "
            "platform in a reconnect loop.",
        )
        self.assertEqual(len(adapter._seen_uids), window)

    def test_connect_seeds_whole_small_mailbox_from_first_message(self):
        """A mailbox smaller than _seen_uids_max is fetched from sequence 1."""
        import asyncio
        adapter = self._make_adapter()

        mock_imap = MagicMock()
        _configure_seed_imap(mock_imap, count=5, uids=(b"1", b"2", b"3", b"4", b"5"))

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            result = asyncio.run(adapter.connect())
            self.assertTrue(result)
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

        # lo clamps to 1 rather than going negative.
        mock_imap.fetch.assert_called_once_with("1:5", "(UID)")

    def test_connect_empty_mailbox_skips_fetch(self):
        """An empty INBOX seeds nothing and issues no FETCH."""
        import asyncio
        adapter = self._make_adapter()

        mock_imap = MagicMock()
        _configure_seed_imap(mock_imap, count=0)

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            result = asyncio.run(adapter.connect())
            self.assertTrue(result)
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

        mock_imap.fetch.assert_not_called()
        self.assertEqual(adapter._seen_uids, set())

    def test_connect_smtp_failure(self):
        """SMTP connection failure returns False."""
        import asyncio
        adapter = self._make_adapter()

        mock_imap = MagicMock()
        # IMAP must genuinely succeed so the failure under test is SMTP's.
        _configure_seed_imap(mock_imap, count=0)

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP", side_effect=Exception("SMTP down")):
            result = asyncio.run(adapter.connect())
            self.assertFalse(result)


class TestFetchNewMessages(unittest.TestCase):
    """Test IMAP message fetching logic."""

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter

    def test_fetch_skips_seen_uids(self):
        """Already-seen UIDs should not be fetched again."""
        adapter = self._make_adapter()
        adapter._seen_uids = {b"1", b"2"}

        raw_email = MIMEText("Hello", "plain", "utf-8")
        raw_email["From"] = "user@test.com"
        raw_email["Subject"] = "Test"
        raw_email["Message-ID"] = "<msg@test.com>"

        mock_imap = MagicMock()

        def uid_handler(command, *args):
            if command == "search":
                return ("OK", [b"1 2 3"])
            if command == "fetch":
                return ("OK", [(b"3", raw_email.as_bytes())])
            return ("NO", [])

        mock_imap.uid.side_effect = uid_handler

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            results = adapter._fetch_new_messages()

        # Only UID 3 should be fetched (1 and 2 already seen)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sender_addr"], "user@test.com")
        self.assertIn(b"3", adapter._seen_uids)


class TestSeedDeliveryNeutrality(unittest.TestCase):
    """The bounded connect() seed must not change delivery.

    Addresses the message-delivery risk of switching connect() from
    ``UID SEARCH ALL`` to a bounded ``FETCH`` window: the seed exists purely to
    mark pre-existing mail as already-seen so it is not re-dispatched as "new".
    Delivery must stay neutral in both directions —

      * no *suppression*: mail arriving after connect is still delivered;
      * no *duplication / re-delivery*: mail that existed at connect and was
        seeded is not dispatched on a later poll.

    Both properties hold because the poll dispatches exactly
    ``UNSEEN ∧ uid ∉ _seen_uids``, and the bounded seed populates the highest
    (most recent) UIDs — a superset of what the old trimmed ``SEARCH ALL``
    retained (``_seen_uids_max`` vs ``_seen_uids_max // 2``), never a subset.
    """

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            return EmailAdapter(PlatformConfig(enabled=True))

    def test_seeded_message_not_redispatched_but_new_mail_is(self):
        import asyncio
        adapter = self._make_adapter()

        # --- connect(): a 3-message inbox; the seed marks all three seen ---
        seed_imap = MagicMock()
        _configure_seed_imap(seed_imap, count=3, uids=(b"101", b"102", b"103"))
        with patch("imaplib.IMAP4_SSL", return_value=seed_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            self.assertTrue(asyncio.run(adapter.connect()))
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()
        self.assertEqual(adapter._seen_uids, {b"101", b"102", b"103"})

        # --- a new message (UID 104) arrives; 103 is still UNSEEN server-side ---
        new_mail = MIMEText("fresh", "plain", "utf-8")
        new_mail["From"] = "sender@test.com"
        new_mail["Subject"] = "New"
        new_mail["Message-ID"] = "<104@test.com>"

        poll_imap = MagicMock()

        def uid_handler(command, *args):
            if command == "search":            # UNSEEN returns a seeded one + the new one
                return ("OK", [b"103 104"])
            if command == "fetch":
                return ("OK", [(args[0], new_mail.as_bytes())])
            return ("NO", [])

        poll_imap.uid.side_effect = uid_handler

        with patch("imaplib.IMAP4_SSL", return_value=poll_imap):
            results = adapter._fetch_new_messages()

        # 103 was seeded at connect → not re-delivered (no duplicate).
        # 104 is genuinely new → delivered (no suppression).
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["message_id"], "<104@test.com>")
        self.assertIn(b"104", adapter._seen_uids)

    def test_bounded_seed_covers_at_least_the_old_trimmed_window(self):
        """The recent-window seed is a superset of the old trimmed SEARCH ALL.

        Old path: SEARCH ALL then _trim_seen_uids() → the top ``_seen_uids_max
        // 2`` UIDs survive. New path seeds the most recent ``_seen_uids_max``.
        Any UID the old path would have suppressed, the new path also seeds, so
        the change cannot newly re-dispatch a recent message.
        """
        adapter = self._make_adapter()
        cap = adapter._seen_uids_max
        count = cap * 3  # mailbox far larger than the window

        seed_imap = MagicMock()
        seeded = tuple(str(500_000 + i).encode() for i in range(cap))
        _configure_seed_imap(seed_imap, count=count, uids=seeded)
        import asyncio
        with patch("imaplib.IMAP4_SSL", return_value=seed_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            self.assertTrue(asyncio.run(adapter.connect()))
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

        new_seed = adapter._seen_uids
        old_trimmed = set(sorted(seeded, key=int)[-(cap // 2):])  # what SEARCH ALL+trim kept
        self.assertTrue(
            old_trimmed.issubset(new_seed),
            "new bounded seed must cover every UID the old trimmed path retained",
        )
        self.assertGreaterEqual(len(new_seed), len(old_trimmed))


class TestPollLoop(unittest.TestCase):
    """Test the async polling loop."""

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
            "EMAIL_POLL_INTERVAL": "1",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter

    def test_check_inbox_dispatches_messages(self):
        """_check_inbox should fetch and dispatch new messages."""
        import asyncio
        adapter = self._make_adapter()
        dispatched = []

        async def mock_dispatch(msg_data):
            dispatched.append(msg_data)

        adapter._dispatch_message = mock_dispatch

        raw_email = MIMEText("Test body", "plain", "utf-8")
        raw_email["From"] = "sender@test.com"
        raw_email["Subject"] = "Inbox Test"
        raw_email["Message-ID"] = "<inbox@test.com>"

        mock_imap = MagicMock()

        def uid_handler(command, *args):
            if command == "search":
                return ("OK", [b"1"])
            if command == "fetch":
                return ("OK", [(b"1", raw_email.as_bytes())])
            return ("NO", [])

        mock_imap.uid.side_effect = uid_handler

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            asyncio.run(adapter._check_inbox())

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["subject"], "Inbox Test")


class TestSendEmailStandalone(unittest.TestCase):
    """Test the standalone _send_email function in send_message_tool."""

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_SMTP_PORT": "587",
    })
    def test_send_email_tool_success(self):
        """_send_email should use verified STARTTLS when sending."""
        import asyncio
        import ssl
        from plugins.platforms.email.adapter import _standalone_send as _email_send
        from types import SimpleNamespace
        async def _send_email(extra, chat_id, message):
            return await _email_send(SimpleNamespace(token=None, api_key=None, extra=extra or {}), chat_id, message)

        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server

            result = asyncio.run(
                _send_email({"address": "hermes@test.com", "smtp_host": "smtp.test.com"}, "user@test.com", "Hello")
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["platform"], "email")
            _, kwargs = mock_server.starttls.call_args
            self.assertIsInstance(kwargs["context"], ssl.SSLContext)
            send_call = mock_server.send_message.call_args[0][0]
            self.assertEqual(send_call["Subject"], "Hermes Agent")
            self.assertIn("Date", send_call)
            self.assertEqual(send_call["To"], "user@test.com")
            self.assertEqual(send_call["From"], "hermes@test.com")


class TestSmtpConnectionCleanup(unittest.TestCase):
    """Verify SMTP connections are closed even when send_message raises."""

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_SMTP_PORT": "587",
    }, clear=False)
    def _make_adapter(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.email.adapter import EmailAdapter
        return EmailAdapter(PlatformConfig(enabled=True))


    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_SMTP_PORT": "587",
    }, clear=False)
    def test_smtp_close_called_when_quit_also_fails(self):
        """If both send_message() and quit() fail, close() is the fallback."""
        adapter = self._make_adapter()
        mock_smtp = MagicMock()
        mock_smtp.send_message.side_effect = Exception("send failed")
        mock_smtp.quit.side_effect = Exception("quit failed")

        with patch("smtplib.SMTP", return_value=mock_smtp):
            with self.assertRaises(Exception):
                adapter._send_email("user@test.com", "Hello")

        mock_smtp.close.assert_called_once()


class TestImapConnectionCleanup(unittest.TestCase):
    """Verify IMAP connections are closed even when fetch raises."""

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_IMAP_PORT": "993",
        "EMAIL_SMTP_HOST": "smtp.test.com",
    }, clear=False)
    def _make_adapter(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.email.adapter import EmailAdapter
        return EmailAdapter(PlatformConfig(enabled=True))

    @patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_IMAP_PORT": "993",
        "EMAIL_SMTP_HOST": "smtp.test.com",
    }, clear=False)
    def test_imap_logout_called_on_uid_fetch_failure(self):
        """IMAP logout() must be called even when uid fetch raises."""
        adapter = self._make_adapter()
        mock_imap = MagicMock()

        def uid_handler(command, *args):
            if command == "search":
                return ("OK", [b"1"])
            if command == "fetch":
                raise Exception("fetch failed")
            return ("NO", [])

        mock_imap.uid.side_effect = uid_handler

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            results = adapter._fetch_new_messages()

        self.assertEqual(results, [])
        mock_imap.logout.assert_called_once()


class TestImapIdExtensionForNetEase(unittest.TestCase):
    """Regression for #22271: 163/NetEase mailbox requires the RFC 2971
    IMAP ID command after LOGIN, otherwise it returns ``BYE Unsafe Login``
    on every UID SEARCH.  We send ID best-effort after every login so that
    163 works while non-supporting servers stay unaffected.
    """

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@163.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.163.com",
            "EMAIL_SMTP_HOST": "smtp.163.com",
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            adapter = EmailAdapter(PlatformConfig(enabled=True))
        return adapter

    def test_connect_sends_imap_id_after_login(self):
        """connect() must call xatom('ID', ...) after LOGIN for 163 support."""
        import asyncio
        adapter = self._make_adapter()

        mock_imap = MagicMock()
        # Let the IMAP phase run to completion so ordering is exercised fully.
        _configure_seed_imap(mock_imap, count=0)

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap), \
             patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            asyncio.run(adapter.connect())
            adapter._running = False
            if adapter._poll_task:
                adapter._poll_task.cancel()

        id_calls = [c for c in mock_imap.xatom.call_args_list if c.args and c.args[0] == "ID"]
        self.assertTrue(
            id_calls,
            "EmailAdapter.connect() must call imap.xatom('ID', ...) after "
            "LOGIN so 163/NetEase mailbox does not return 'Unsafe Login'.",
        )
        payload = id_calls[0].args[1]
        self.assertIn("hermes-agent", payload)

        names = [c[0] for c in mock_imap.method_calls]
        self.assertIn("login", names)
        self.assertLess(names.index("login"), names.index("xatom"))


class TestConnectSmtp(unittest.TestCase):
    """Test _connect_smtp() helper: protocol selection and IPv6 fallback."""

    def _make_adapter(self, port="587"):
        from gateway.config import PlatformConfig
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com",
            "EMAIL_SMTP_PORT": port,
        }):
            from plugins.platforms.email.adapter import EmailAdapter
            return EmailAdapter(PlatformConfig(enabled=True))


    def test_ipv6_timeout_falls_back_to_ipv4(self):
        """When default connection times out, retry with an IPv4-only SMTP path."""
        import socket as _socket
        import plugins.platforms.email.adapter as email_mod

        adapter = self._make_adapter("587")

        with patch("smtplib.SMTP", side_effect=_socket.timeout("timed out")), \
             patch.object(email_mod, "_IPv4SMTP") as mock_ipv4_smtp:
            mock_server = MagicMock()
            mock_ipv4_smtp.return_value = mock_server

            result = adapter._connect_smtp()

            self.assertIs(result, mock_server)
            mock_ipv4_smtp.assert_called_once_with("smtp.test.com", 587, timeout=30)
            mock_server.starttls.assert_called_once()

    def test_port_465_ipv6_fallback(self):
        """Port 465 IPv6 timeout falls back to IPv4 with SMTP_SSL."""
        import socket as _socket
        import plugins.platforms.email.adapter as email_mod

        adapter = self._make_adapter("465")

        with patch("smtplib.SMTP_SSL", side_effect=_socket.timeout("timed out")), \
             patch.object(email_mod, "_IPv4SMTP_SSL") as mock_ipv4_smtp_ssl:
            mock_server = MagicMock()
            mock_ipv4_smtp_ssl.return_value = mock_server

            result = adapter._connect_smtp()

            self.assertIs(result, mock_server)
            mock_ipv4_smtp_ssl.assert_called_once_with(
                "smtp.test.com", 465, timeout=30, context=ANY,
            )


class TestConnectionConfigResolution(unittest.TestCase):
    """Host/address resolution and pre-connect validation (#49736)."""


    def test_connect_aborts_without_attempting_imap_when_host_missing(self):
        """A missing host returns False without the cryptic DNS error, and marks
        the failure non-retryable so the gateway stops reconnecting (#40715)."""
        import asyncio
        from gateway.config import PlatformConfig
        from plugins.platforms.email.adapter import EmailAdapter
        with patch.dict(os.environ, {
            "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_IMAP_HOST": "",
            "EMAIL_SMTP_HOST": "smtp.test.com",
        }, clear=False):
            adapter = EmailAdapter(PlatformConfig(enabled=True))

        with patch("imaplib.IMAP4_SSL") as mock_imap:
            result = asyncio.run(adapter.connect())

        self.assertFalse(result)
        mock_imap.assert_not_called()
        # The OOM fix (#40715): a blank host must NOT leave the platform in the
        # retryable reconnect loop — it is a permanent config error.
        self.assertTrue(adapter.has_fatal_error)
        self.assertEqual(adapter.fatal_error_code, "email_missing_configuration")
        self.assertFalse(adapter.fatal_error_retryable)
        self.assertIn("EMAIL_IMAP_HOST", adapter.fatal_error_message or "")

    def test_blank_present_env_vars_are_not_required(self):
        """Blank/whitespace EMAIL_* values must read as missing (#40715) — an
        abandoned setup with empty keys must not enable the platform."""
        from plugins.platforms.email.adapter import check_email_requirements
        for blank in ("", "   ", "\n"):
            with patch.dict(os.environ, {
                "EMAIL_ADDRESS": blank, "EMAIL_PASSWORD": blank,
                "EMAIL_IMAP_HOST": blank, "EMAIL_SMTP_HOST": blank,
            }, clear=False):
                self.assertFalse(check_email_requirements())


class TestSenderAuthentication(unittest.TestCase):
    """Verify _verify_sender_authentication parses Authentication-Results
    correctly and resists From: spoofing (GHSA-rxqh-5572-8m77)."""

    def _msg(self, from_addr, auth_results=None):
        """Build an email.message.Message with the given From: and
        zero or more Authentication-Results headers (first = topmost/trusted)."""
        msg = MIMEText("body")
        msg["From"] = from_addr
        for ar in auth_results or []:
            msg["Authentication-Results"] = ar
        return msg

    def _verify(self, from_addr, auth_results=None, authserv_id=""):
        from plugins.platforms.email.adapter import (
            _verify_sender_authentication,
            _extract_email_address,
        )
        msg = self._msg(from_addr, auth_results)
        addr = _extract_email_address(from_addr)
        return _verify_sender_authentication(msg, addr, authserv_id=authserv_id)

    def test_dmarc_pass_authenticates(self):
        ok, reason = self._verify(
            "Admin <admin@example.com>",
            ["mx.google.com; dmarc=pass header.from=example.com; spf=pass"],
        )
        self.assertTrue(ok, reason)


    def test_dkim_pass_aligned_authenticates(self):
        ok, reason = self._verify(
            "admin@example.com",
            ["mx.google.com; dkim=pass header.d=example.com"],
        )
        self.assertTrue(ok, reason)

    def test_spf_pass_misaligned_rejected(self):
        # SPF passes for the envelope domain, but it doesn't match From: domain.
        ok, reason = self._verify(
            "admin@example.com",
            ["mx.google.com; spf=pass smtp.mailfrom=bounce@evil.com"],
        )
        self.assertFalse(ok, reason)


    def test_injected_header_below_trusted_does_not_authenticate(self):
        """An attacker-injected Authentication-Results sorts BELOW the receiving
        server's. With authserv-id pinning, only the trusted (first) header is
        consulted, so a forged 'dmarc=pass' lower in the stack is ignored."""
        ok, reason = self._verify(
            "admin@example.com",
            [
                # Trusted: stamped by our server, real verdict = fail
                "mx.ourserver.com; dmarc=fail header.from=example.com",
                # Forged by attacker, claims pass
                "mx.ourserver.com; dmarc=pass header.from=example.com",
            ],
            authserv_id="mx.ourserver.com",
        )
        self.assertFalse(ok, reason)


if __name__ == "__main__":
    unittest.main()
