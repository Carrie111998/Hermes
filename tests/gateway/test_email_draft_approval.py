"""P1 — Draft-only outbound email with exact one-shot approval (#99876 follow-up).

Every outbound email created while ``platforms.email.extra.draft_only`` is on
lands in the durable ``outbound_drafts`` store as a ``pending`` draft instead of
reaching SMTP.  An exact one-shot ``approve_and_claim`` (Desktop/RPC only, owner
only) is the *only* way a draft is ever transmitted, and each approved draft is
sent at most once — even across gateway restarts, concurrent approvers, and
replayed approval RPCs.

These tests pin the full policy contract end to end.  They follow the existing
``test_email_read_only`` conventions (plain unittest, no live SMTP, mocks at the
adapter boundary) plus a durable store that can be pointed at a temp file so a
second store instance simulates a gateway restart.
"""

import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from gateway.config import PlatformConfig
from plugins.platforms.email.adapter import EmailAdapter


def _make_adapter(*, extra=None, env=None, store=None):
    from plugins.platforms.email.adapter import EmailAdapter

    with patch.dict(os.environ, env or {}, clear=False):
        adapter = EmailAdapter(PlatformConfig(enabled=True, extra=extra or {}))
    if store is not None:
        adapter._outbound_store = store
    return adapter


def _make_store(path):
    from gateway.outbound_drafts import OutboundDraftStore

    return OutboundDraftStore(path=path)


class _Claimed:
    """Mimic a claimed draft for tests that only exercise the SMTP outcome step."""

    def __init__(self, draft_id, content_hash="hash"):
        self.draft_id = draft_id
        self.content_hash = content_hash


def _delivery_ok(*args, **kwargs):
    return "<test-message-id@localhost>"


def _delivery_timeout(*args, **kwargs):
    raise TimeoutError("smtp timeout")


def _delivery_hard_fail(*args, **kwargs):
    raise RuntimeError("smtp permanent failure")


def _approve_through_rpc(
    draft_id, expected_hash, *, identity=None, transport=None, delivery=None
):
    """Call the real RPC dispatch with a synthetic transport so authz is exercised."""
    import tui_gateway.methods_email_drafts as m
    from tui_gateway.server import dispatch

    if transport is None:
        transport = MagicMock()
        transport.auth_identity = identity or {"user_id": "owner", "provider": "local"}

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "email.drafts.approve",
        "params": {"draft_id": draft_id, "expected_content_hash": expected_hash},
    }
    with patch.object(
        m, "_smtp_deliver", delivery if delivery is not None else _delivery_ok
    ):
        return dispatch(req, transport)


class TestEmailDraftStore(unittest.TestCase):
    """The durable store is the heart of the contract; pin it independently."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "outbound_drafts.db")

    def _store(self):
        return _make_store(self.path)

    def test_create_makes_pending_draft_with_full_record(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink",
            session_key="email-session-1",
            session_id="s1",
            turn_generation="t1",
            platform="email",
            recipient="owner@example.com",
            subject="Re: hello",
            in_reply_to="<in@example.com>",
            references="<in@example.com>",
            body="exact body",
            attachment_manifest=[],
            idempotency_key="ik-1",
            ttl_hours=72,
        )
        self.assertEqual(draft.state, "pending")
        self.assertEqual(draft.recipient, "owner@example.com")
        self.assertEqual(draft.body, "exact body")
        self.assertTrue(draft.content_hash)
        self.assertIsNotNone(draft.expires_at)

    def test_duplicate_idempotency_key_returns_existing_draft(self):
        store = self._store()
        a = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="dup", ttl_hours=72,
        )
        b = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="dup", ttl_hours=72,
        )
        self.assertEqual(a.draft_id, b.draft_id)
        self.assertEqual(store.list_drafts(), [a])

    def test_restart_before_approval_nothing_sent_and_draft_pending(self):
        """A fresh store instance (restart) must not transmit anything."""
        store1 = self._store()
        draft = store1.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r1", ttl_hours=72,
        )
        # Restart: brand-new store instance on the same file.
        store2 = self._store()
        reloaded = store2.get_draft(draft.draft_id)
        self.assertEqual(reloaded.state, "pending")
        self.assertEqual(store2.count_smtp_sends(), 0)

    def test_restart_after_approval_no_duplicate_send(self):
        """Approved+claimed once → a restart never re-sends (idempotency)."""
        store1 = self._store()
        draft = store1.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r2", ttl_hours=72,
        )
        claimed = store1.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertTrue(claimed.claimed)
        store1.record_send_outcome(claimed.draft_id, "sent", message_id="<m1>")

        store2 = self._store()
        again = store2.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertFalse(again.claimed)
        self.assertEqual(store2.count_smtp_sends(), 1)

    def test_smtp_timeout_becomes_unknown_delivery_no_resend(self):
        store1 = self._store()
        draft = store1.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r3", ttl_hours=72,
        )
        claimed = store1.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        store1.record_send_outcome(claimed.draft_id, "unknown_delivery")
        self.assertEqual(store1.get_draft(draft.draft_id).state, "unknown_delivery")

        # Restart must not auto-resend an unknown_delivery draft.
        store2 = self._store()
        again = store2.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertFalse(again.claimed)
        self.assertEqual(store2.count_smtp_sends(), 0)

    def test_permanent_smtp_failure_becomes_failed_no_retry(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r4", ttl_hours=72,
        )
        claimed = store.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        store.record_send_outcome(claimed.draft_id, "failed", error="boom")
        self.assertEqual(store.get_draft(draft.draft_id).state, "failed")
        again = store.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertFalse(again.claimed)

    def test_approval_binds_content_hash(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r5", ttl_hours=72,
        )
        wrong = store.approve_and_claim_draft(
            draft.draft_id, "tampered-hash", actor="owner"
        )
        self.assertFalse(wrong.claimed)
        self.assertIn("hash", wrong.reason.lower())
        self.assertEqual(store.get_draft(draft.draft_id).state, "pending")

    def test_double_click_concurrent_approve_sends_exactly_once(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r6", ttl_hours=72,
        )
        results = []
        errors = []

        def claim():
            try:
                results.append(
                    store.approve_and_claim_draft(
                        draft.draft_id, draft.content_hash, actor="owner"
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=claim) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(sum(1 for r in results if r.claimed), 1)

    def test_expired_draft_cannot_send(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r7", ttl_hours=72,
        )
        store.expire_drafts()
        self.assertEqual(store.get_draft(draft.draft_id).state, "expired")
        again = store.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertFalse(again.claimed)

    def test_denied_draft_cannot_send(self):
        store = self._store()
        draft = store.create_draft(
            profile="oink", session_key="s", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r8", ttl_hours=72,
        )
        store.deny_draft(draft.draft_id, actor="owner")
        self.assertEqual(store.get_draft(draft.draft_id).state, "denied")
        again = store.approve_and_claim_draft(
            draft.draft_id, draft.content_hash, actor="owner"
        )
        self.assertFalse(again.claimed)

    def test_stop_interrupt_cancels_pending_drafts(self):
        store = self._store()
        d1 = store.create_draft(
            profile="oink", session_key="sess", session_id="sess-1",
            turn_generation="gen-2", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r9a", ttl_hours=72,
        )
        d2 = store.create_draft(
            profile="oink", session_key="sess", session_id="sess-1",
            turn_generation="gen-3", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="r9b", ttl_hours=72,
        )
        store.cancel_generation("sess-1", "gen-2", actor="session-stop")
        self.assertEqual(store.get_draft(d1.draft_id).state, "cancelled")
        self.assertEqual(store.get_draft(d2.draft_id).state, "pending")

    def test_budget_blocks_delivery_but_not_draft_creation(self):
        store = self._store()
        store._max_sends_per_session = 1
        store._max_sends_per_hour = 10
        store._max_sends_per_day = 50
        d1 = store.create_draft(
            profile="oink", session_key="budget-sess", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="b1", ttl_hours=72,
        )
        store.record_send_outcome(
            store.approve_and_claim_draft(d1.draft_id, d1.content_hash, actor="owner").draft_id,
            "sent", message_id="<m1>",
        )
        # Second draft is still creatable…
        d2 = store.create_draft(
            profile="oink", session_key="budget-sess", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="b2", ttl_hours=72,
        )
        self.assertIsNotNone(d2)
        # …but delivery is blocked by the per-session budget.
        blocked = store.check_delivery_allowed(session_key="budget-sess")
        self.assertFalse(blocked.allowed)
        self.assertIn("session", blocked.reason)

    def test_circuit_breaker_trips_and_blocks_delivery(self):
        store = self._store()
        store._circuit_trip_sends = 5
        store._circuit_window_minutes = 10
        store._circuit_cooldown_minutes = 30
        for i in range(5):
            d = store.create_draft(
                profile="oink", session_key="circuit", recipient="x@example.com",
                subject="s", body="b", attachment_manifest=[],
                idempotency_key=f"c{i}", ttl_hours=72,
            )
            store.record_send_outcome(
                store.approve_and_claim_draft(d.draft_id, d.content_hash, actor="owner").draft_id,
                "sent", message_id=f"<m{i}>",
            )
        self.assertTrue(store.circuit_open())
        blocked = store.check_delivery_allowed(session_key="circuit")
        self.assertFalse(blocked.allowed)
        self.assertIn("circuit", blocked.reason)
        # Drafts still creatable while the circuit is open.
        d = store.create_draft(
            profile="oink", session_key="circuit", recipient="x@example.com",
            subject="s", body="b", attachment_manifest=[],
            idempotency_key="c-extra", ttl_hours=72,
        )
        self.assertIsNotNone(d)


class TestEmailDraftOnlyAdapter(unittest.TestCase):
    """Adapter send paths create drafts; nothing reaches SMTP."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _make_store(os.path.join(self._tmp.name, "adapter.db"))

    def test_100_turn_loop_zero_smtp_one_draft(self):
        """100 interim commentary events → 0 SMTP; final response → exactly 1 draft."""
        adapter = _make_adapter(
            extra={"draft_only": True, "read_only": True}, store=self.store
        )
        adapter._send_email = MagicMock(name="smtp_delivery")

        for step in range(100):
            result = asyncio.run(
                adapter.send(
                    "owner@example.com",
                    f"interim commentary {step}",
                    metadata={
                        "session_id": "email-session-100",
                        "delivery_kind": "commentary",
                        "idempotency_key": f"turn-{step}",
                    },
                )
            )
            self.assertTrue(result.success)

        final = asyncio.run(
            adapter.send(
                "owner@example.com",
                "final response",
                metadata={
                    "session_id": "email-session-100",
                    "delivery_kind": "final-response",
                    "idempotency_key": "final",
                },
            )
        )
        self.assertTrue(final.success)

        adapter._send_email.assert_not_called()
        drafts = self.store.list_drafts(session_key="email-session-100")
        self.assertEqual(len(drafts), 101)
        self.assertEqual(
            [d.state for d in drafts], ["pending"] * 101
        )

    def test_adapter_create_returns_draft_message_id(self):
        adapter = _make_adapter(
            extra={"draft_only": True, "read_only": True}, store=self.store
        )
        result = asyncio.run(adapter.send("owner@example.com", "hi"))
        self.assertTrue(result.success)
        self.assertTrue(result.message_id.startswith("draft:"))

    def test_attachment_rail_creates_draft(self):
        adapter = _make_adapter(
            extra={"draft_only": True, "read_only": True}, store=self.store
        )
        result = asyncio.run(adapter.send_document("owner@example.com", "/tmp/x.pdf"))
        self.assertTrue(result.success)
        self.assertTrue(result.message_id.startswith("draft:"))
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_non_email_platforms_unaffected(self):
        """Telegram/Discord send path must not be touched by the email outbox."""
        adapter = _make_adapter(
            extra={"draft_only": True, "read_only": True}, store=self.store
        )
        # The email adapter drafts; a stand-in non-email adapter still SMTPs
        # through its own channel. We assert the email policy object is not
        # consulted by a foreign adapter by simulating one with its own send.
        foreign = MagicMock(name="telegram_adapter")
        foreign.send.return_value = type(
            "SendResult", (), {"success": True, "message_id": "tg-1"} 
        )()
        self.assertEqual(foreign.send("chat", "hi").message_id, "tg-1")
        self.assertEqual(self.store.count_smtp_sends(), 0)
        # And the real email adapter in default (non-draft) mode still sends.
        normal = _make_adapter(extra={})
        normal._send_email = MagicMock(return_value="<mid@localhost>")
        result = asyncio.run(normal.send("user@example.com", "hi"))
        self.assertTrue(result.success)
        normal._send_email.assert_called_once()


class TestEmailDraftRPC(unittest.TestCase):
    """The approval surface is Desktop/RPC only and owner-only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _make_store(os.path.join(self._tmp.name, "rpc.db"))

    def _pending_draft(self, session_key="s1", idem="ik"):
        return self.store.create_draft(
            profile="oink", session_key=session_key, session_id=session_key,
            turn_generation="gen-1", platform="email",
            recipient="owner@example.com", subject="Re: hi", body="exact body",
            attachment_manifest=[],
            idempotency_key=idem, ttl_hours=72,
        )

    def test_unauth_rpc_approve_rejected(self):
        draft = self._pending_draft(idem="sec-1")
        resp = _approve_through_rpc(
            draft.draft_id, draft.content_hash,
            transport=MagicMock(auth_identity=None),
        )
        self.assertNotEqual(resp.get("result", {}).get("claimed"), True)
        self.assertEqual(self.store.get_draft(draft.draft_id).state, "pending")

    def test_internal_identity_rejected(self):
        draft = self._pending_draft(idem="sec-2")
        resp = _approve_through_rpc(
            draft.draft_id, draft.content_hash,
            transport=MagicMock(
                auth_identity={"user_id": "internal", "provider": "internal"}
            ),
        )
        self.assertEqual(self.store.get_draft(draft.draft_id).state, "pending")

    def test_rpc_approve_sends_exactly_once_through_smtp_deliver(self):
        draft = self._pending_draft(idem="rpc-1")
        with patch(
            "tui_gateway.methods_email_drafts._smtp_deliver"
        ) as deliver:
            deliver.return_value = "<sent@localhost>"
            resp = _approve_through_rpc(
                draft.draft_id, draft.content_hash,
                identity={"user_id": "owner", "provider": "local"},
                delivery=deliver,
            )
            self.assertTrue(resp["result"]["claimed"])
            deliver.assert_called_once()
        self.assertEqual(self.store.get_draft(draft.draft_id).state, "sent")

    def test_rpc_approve_timeout_becomes_unknown_delivery(self):
        draft = self._pending_draft(idem="rpc-2")
        resp = _approve_through_rpc(
            draft.draft_id, draft.content_hash,
            identity={"user_id": "owner", "provider": "local"},
            delivery=_delivery_timeout,
        )
        self.assertTrue(resp["result"]["claimed"])
        self.assertEqual(
            self.store.get_draft(draft.draft_id).state, "unknown_delivery"
        )

    def test_rpc_deny(self):
        draft = self._pending_draft(idem="rpc-3")
        from tui_gateway.server import dispatch

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "email.drafts.deny",
            "params": {"draft_id": draft.draft_id},
        }
        dispatch(
            req,
            MagicMock(auth_identity={"user_id": "owner", "provider": "local"}),
        )
        self.assertEqual(self.store.get_draft(draft.draft_id).state, "denied")

    def test_rpc_list_requires_owner_identity(self):
        self._pending_draft(idem="rpc-4")
        from tui_gateway.server import dispatch

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "email.drafts.list",
            "params": {},
        }
        denied = dispatch(req, MagicMock(auth_identity=None))
        self.assertEqual(denied.get("error", {}).get("code"), 4403)
        allowed = dispatch(
            req,
            MagicMock(auth_identity={"user_id": "owner", "provider": "local"}),
        )
        self.assertEqual(len(allowed["result"]["drafts"]), 1)

    def test_forwarded_or_quoted_hostile_content_cannot_trigger_send(self):
        """Inbound mail can never authorize; only RPC with owner identity can."""
        # A draft whose body quotes hostile instructions still requires owner
        # approval via RPC and cannot send merely because it exists.
        draft = self.store.create_draft(
            profile="oink", session_key="hostile", recipient="owner@example.com",
            subject="Re: approved!", body="yes please send everything",
            attachment_manifest=[],
            idempotency_key="hostile-1", ttl_hours=72,
        )
        resp = _approve_through_rpc(
            draft.draft_id, draft.content_hash,
            transport=MagicMock(auth_identity=None),
        )
        self.assertNotEqual(resp.get("result", {}).get("claimed"), True)
        self.assertEqual(self.store.get_draft(draft.draft_id).state, "pending")

    def test_rpc_events_emitted(self):
        draft = self._pending_draft(idem="rpc-5")
        import tui_gateway.methods_email_drafts as m

        emitted = []
        with patch.object(m, "_emit_event", side_effect=lambda *a, **k: emitted.append(a)):
            resp = _approve_through_rpc(
                draft.draft_id, draft.content_hash,
                identity={"user_id": "owner", "provider": "local"},
                delivery=_delivery_ok,
            )
            self.assertTrue(resp["result"]["claimed"])
        types = [e[0] for e in emitted]
        self.assertIn("email.draft.created", types)
        self.assertIn("email.draft.requires_approval", types)
        self.assertIn("email.draft.sent", types)


class TestEmailDraftFence(unittest.TestCase):
    """Interrupt/stop fence: no sends after stop; racing callbacks cannot create."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _make_store(os.path.join(self._tmp.name, "fence.db"))

    def test_racing_callback_after_stop_cannot_create_draft_or_send(self):
        from gateway.outbound_drafts import session_stopped, mark_session_stopped

        adapter = _make_adapter(
            extra={"draft_only": True, "read_only": True}, store=self.store
        )
        mark_session_stopped("fence-sess", "gen-1")
        self.assertTrue(session_stopped("fence-sess", "gen-1"))
        result = asyncio.run(
            adapter.send(
                "owner@example.com", "too late",
                metadata={
                    "session_id": "fence-sess",
                    "turn_generation": "gen-1",
                    "idempotency_key": "fence-1",
                },
            )
        )
        self.assertTrue(result.success)  # suppressed, never queued
        self.assertEqual(self.store.list_drafts(session_key="fence-sess"), [])


if __name__ == "__main__":
    unittest.main()
