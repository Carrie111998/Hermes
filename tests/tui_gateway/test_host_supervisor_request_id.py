"""Compute-host ``request_id`` correlation must be collision-proof.

The pending-turn map was keyed on the client-supplied ``request_id``. Two
connections can both send the JSON-RPC id ``"1"``, so the second submit
overwrote the first and the first session's terminal frame was delivered to
the second's callback (cross-session metadata contamination, plus a stuck
first session).

Contract pinned here:

* ``submit_turn`` always mints an internal UUID for the pending-turn key and
  preserves the client id only as ``client_request_id`` for diagnostics.
* Two submits with the same client id register as two distinct pending turns.
* ``_complete_turn`` only delivers a terminal frame when the frame's ``sid``
  matches the submitting session's ``sid``.
"""

from __future__ import annotations

import pytest

from tui_gateway.host_supervisor import HostSupervisor


def _make_supervisor():
    sup = HostSupervisor(autostart=False)
    sent: list[dict] = []

    def _noop_start():
        return None

    sup.start = _noop_start
    sup._send_frame = lambda frame: sent.append(frame)
    return sup, sent


def test_submit_turn_mints_internal_id_and_isolates_sessions():
    sup, _sent = _make_supervisor()
    delivered: dict[str, dict] = {}

    rid_a = sup.submit_turn(
        {"request_id": "1", "sid": "A"},
        on_complete=lambda f: delivered.__setitem__("A", f),
    )
    rid_b = sup.submit_turn(
        {"request_id": "1", "sid": "B"},
        on_complete=lambda f: delivered.__setitem__("B", f),
    )

    # Same client id, but the internal correlation keys must not collide.
    assert rid_a != rid_b
    assert len(sup._pending_turns) == 2

    # Completing A's turn delivers only to A's callback, not B's.
    sup._complete_turn({"request_id": rid_a, "sid": "A", "type": "turn.end"})
    assert "A" in delivered
    assert "B" not in delivered

    sup._complete_turn({"request_id": rid_b, "sid": "B", "type": "turn.end"})
    assert "B" in delivered


def test_complete_turn_rejects_sid_mismatch():
    sup, _sent = _make_supervisor()
    fired: list[dict] = []

    rid = sup.submit_turn(
        {"request_id": "1", "sid": "A"},
        on_complete=lambda f: fired.append(f),
    )

    # A terminal frame for a *different* session must never be delivered.
    sup._complete_turn({"request_id": rid, "sid": "B", "type": "turn.end"})
    assert fired == []
    # And the mismatch was consumed, so no late re-delivery is possible.
    assert len(sup._pending_turns) == 0


def test_client_request_id_preserved_for_diagnostics():
    sup, sent = _make_supervisor()
    sup.submit_turn({"request_id": "1", "sid": "A"})

    assert len(sent) == 1
    frame = sent[0]
    assert frame["request_id"] != "1"  # internal id is not the client's
    assert frame["client_request_id"] == "1"  # client id kept for diagnostics
    assert frame["type"] == "turn.start"
