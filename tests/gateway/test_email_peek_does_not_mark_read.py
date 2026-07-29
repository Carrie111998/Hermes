"""The email poller must not mark a mailbox read, and must not re-download it.

`_fetch_new_messages` searched UNSEEN and fetched every hit with `(RFC822)`, a
form that implicitly sets \\Seen (RFC 3501 6.4.5), before the sender allowlist is
applied at dispatch — so it marked mail read that the gateway then refused to
act on.

The fix is two changes that only work together. `BODY.PEEK[]` stops the flag
write; a UID watermark stops the re-download that removing the flag would
otherwise cause. `_trim_seen_uids` evicts old UIDs and says in its own docstring
that this is safe "because IMAP's UNSEEN flag prevents re-delivery regardless" —
PEEK alone deletes that guarantee, so every evicted UID would be re-fetched in
full on every poll. `test_backlog_is_not_refetched_every_poll` is the regression
for that, and it fails on a PEEK-only patch.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch


BASE_ENV = {
    "EMAIL_ADDRESS": "agent@test.com",
    "EMAIL_PASSWORD": "secret",
    "EMAIL_IMAP_HOST": "imap.test.com",
    "EMAIL_IMAP_PORT": "993",
    "EMAIL_SMTP_HOST": "smtp.test.com",
    "EMAIL_SMTP_PORT": "587",
    "EMAIL_POLL_INTERVAL": "15",
}


def _raw(sender: str = "someone@elsewhere.com") -> bytes:
    msg = MIMEText("body text")
    msg["From"] = sender
    msg["Subject"] = "hello"
    msg["Message-ID"] = "<m1@test.com>"
    return msg.as_bytes()


class _FakeIMAP:
    """An IMAP fake that models the \\Seen flag the way RFC 3501 6.4.5 does.

    Modelling the flag is the point: a fake that only records command strings
    cannot tell a fetch that mutates the mailbox from one that does not, so a
    test written against it passes with the bug fully restored.
    """

    def __init__(self, uids, raw=None):
        self.uids = [u if isinstance(u, bytes) else str(u).encode() for u in uids]
        self.raw = raw or _raw()
        self.flags: dict[bytes, set[str]] = {u: set() for u in self.uids}
        self.fetched: list[bytes] = []
        self.searches: list[tuple] = []

    def login(self, *a):
        return ("OK", [b""])

    def select(self, *a, **k):
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])

    def xatom(self, *a, **k):
        return ("OK", [b""])

    def _unseen(self) -> list[bytes]:
        return [u for u in self.uids if "\\Seen" not in self.flags[u]]

    def uid(self, command, *args):
        cmd = command.lower()
        if cmd == "search":
            self.searches.append(args)
            hits = self._unseen() if "UNSEEN" in args else list(self.uids)
            # Honour a `UID <lo>:*` range the way a real server would.
            for a in args:
                if isinstance(a, str) and ":" in a and a.split(":")[0].isdigit():
                    lo = int(a.split(":")[0])
                    hits = [u for u in hits if int(u) >= lo]
            return ("OK", [b" ".join(hits)])
        if cmd == "fetch":
            uid = args[0]
            spec = " ".join(str(a) for a in args[1:])
            self.fetched.append(uid)
            # THE RFC: a non-PEEK body fetch implicitly sets \Seen.
            if "PEEK" not in spec.upper():
                self.flags.setdefault(uid, set()).add("\\Seen")
            return ("OK", [(b"1 (BODY[] {N}", self.raw)])
        if cmd == "store":
            uid = args[0]
            if "+FLAGS" in " ".join(str(a) for a in args[1:]):
                self.flags.setdefault(uid, set()).add("\\Seen")
            return ("OK", [b""])
        return ("OK", [b""])


def _adapter(env=None):
    from gateway.config import PlatformConfig

    merged = dict(BASE_ENV)
    merged.update(env or {})
    with patch.dict(os.environ, merged, clear=False):
        from plugins.platforms.email.adapter import EmailAdapter

        return EmailAdapter(PlatformConfig(enabled=True))


def _run(adapter, fake, env=None):
    merged = dict(BASE_ENV)
    merged.update(env or {})
    with patch.dict(os.environ, merged, clear=False):
        with patch("imaplib.IMAP4_SSL", return_value=fake):
            with patch("plugins.platforms.email.adapter._send_imap_id", MagicMock()):
                return adapter._fetch_new_messages()


def _connect(adapter, fake):
    """`connect()` is a coroutine; it establishes the UID watermark."""
    with patch.dict(os.environ, BASE_ENV, clear=False):
        with patch("imaplib.IMAP4_SSL", return_value=fake):
            with patch("plugins.platforms.email.adapter._send_imap_id", MagicMock()):
                return asyncio.run(adapter.connect())


class TestPollDoesNotMutateTheMailbox(unittest.TestCase):
    def test_polling_leaves_every_flag_untouched(self):
        """The bug, asserted on mailbox STATE rather than on a command string."""
        adapter = _adapter()
        fake = _FakeIMAP([101, 102, 103])

        _run(adapter, fake)

        self.assertEqual(
            [u for u, f in fake.flags.items() if f],
            [],
            "polling marked mail read: %r" % (fake.flags,),
        )

    def test_messages_are_still_delivered(self):
        """Not mutating the mailbox must not mean not reading it."""
        adapter = _adapter()
        fake = _FakeIMAP([201])

        results = _run(adapter, fake)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sender_addr"], "someone@elsewhere.com")


class TestBacklogIsBounded(unittest.TestCase):
    def test_backlog_is_not_refetched_every_poll(self):
        """The regression a PEEK-only fix introduces.

        `_seen_uids` is capped and trimmed, and `_trim_seen_uids` documents that
        eviction is safe only because \\Seen keeps evicted mail out of an UNSEEN
        search. With PEEK and no watermark, every evicted UID comes back on the
        next poll — a full re-download of the backlog, every interval, forever.
        """
        adapter = _adapter()
        fake = _FakeIMAP(list(range(1, 51)))

        # connect() establishes the watermark over the existing mailbox.
        _connect(adapter, fake)

        # Simulate eviction: a long-running process trims its in-memory set.
        adapter._seen_uids = set()

        first = _run(adapter, fake)
        second = _run(adapter, fake)

        self.assertEqual(first, [], "the pre-existing backlog is not new mail")
        self.assertEqual(second, [], "and it must not come back on the next poll")
        self.assertEqual(
            fake.fetched, [], "no message body should have been downloaded at all"
        )

    def test_mail_arriving_after_connect_is_still_delivered(self):
        """The watermark must bound the backlog WITHOUT muting new mail."""
        adapter = _adapter()
        fake = _FakeIMAP([10, 11, 12])

        _connect(adapter, fake)

        # A new message arrives above the watermark.
        fake.uids.append(b"13")
        fake.flags[b"13"] = set()

        results = _run(adapter, fake)

        self.assertEqual(len(results), 1, "new mail must still be delivered")
        self.assertEqual(fake.fetched, [b"13"])

    def test_search_is_bounded_by_the_watermark(self):
        adapter = _adapter()
        fake = _FakeIMAP([7, 8, 9])

        _connect(adapter, fake)
        fake.searches.clear()
        _run(adapter, fake)

        self.assertTrue(fake.searches, "no search was issued")
        joined = " ".join(str(a) for a in fake.searches[-1])
        self.assertIn("UNSEEN", joined)
        self.assertIn("10:*", joined, "search must start above the highest known UID")

    def test_an_empty_mailbox_still_polls(self):
        """Watermark 0 must not degenerate into a search that matches nothing."""
        adapter = _adapter()
        fake = _FakeIMAP([])

        _connect(adapter, fake)

        fake.uids.append(b"1")
        fake.flags[b"1"] = set()

        self.assertEqual(len(_run(adapter, fake)), 1)


if __name__ == "__main__":
    unittest.main()
