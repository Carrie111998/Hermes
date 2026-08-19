"""F3 tests — peer/child/broadcast steering via steer_subagent/steer_session.

These exercise the new ``steer_session`` and ``steer_broadcast`` helpers with
fake agents and a fake resolver, so they run without booting the gateway.
The broadcast test also stubs the delegation registry so child subagents are
exercised. This is the F3 acceptance gate (W11/Phase F3-3).

Key invariant checked: steering reuses ``AIAgent.steer`` (appends to last
tool result) — it never creates a new user turn or violates role alternation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import delegate_tool as dt


class FakeAgent:
    """Minimal stand-in for AIAgent with a steer() that records text."""

    def __init__(self, name: str):
        self.name = name
        self.steer_calls: list[str] = []

    def steer(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        self.steer_calls.append(text.strip())
        return True


def _resolver(map_: dict):
    def _r(target: str):
        if target == "__list__":
            return list(map_.keys())
        return map_.get(target)
    return _r


# ──────────────────────────────────────────────────────────────────────
# steer_session (peer)
# ──────────────────────────────────────────────────────────────────────


class TestSteerSession:
    def test_steers_resolved_peer(self):
        peer = FakeAgent("peer")
        resolve = _resolver({"peer-sid": peer})
        assert dt.steer_session("peer-sid", "look left", resolve_agent=resolve) is True
        assert peer.steer_calls == ["look left"]

    def test_empty_text_rejected(self):
        peer = FakeAgent("peer")
        resolve = _resolver({"peer-sid": peer})
        assert dt.steer_session("peer-sid", "   ", resolve_agent=resolve) is False
        assert peer.steer_calls == []

    def test_unknown_target_rejected(self):
        resolve = _resolver({})
        assert dt.steer_session("ghost", "hi", resolve_agent=resolve) is False

    def test_no_resolver_rejected(self):
        assert dt.steer_session("x", "hi") is False

    def test_resolver_exception_is_safe(self):
        def boom(target):
            raise RuntimeError("nope")
        assert dt.steer_session("x", "hi", resolve_agent=boom) is False


# ──────────────────────────────────────────────────────────────────────
# steer_broadcast (peer + child)
# ──────────────────────────────────────────────────────────────────────


class TestSteerBroadcast:
    def test_broadcasts_to_peers_and_children(self):
        peer_a = FakeAgent("a")
        peer_b = FakeAgent("b")
        resolve = _resolver({"a": peer_a, "b": peer_b, "self": FakeAgent("self")})
        # Stub the delegation registry so one child subagent exists.
        child_agent = FakeAgent("child")
        child_record = {
            "accepting_steer": True,
            "owner_session_id": "self",
            "owner_transport": object(),
            "owner_session_record": object(),
            "agent": child_agent,
        }
        fake_registry = {"child-1": child_record}
        with patch.object(dt, "_active_subagents", fake_registry), patch.object(
            dt, "_active_subagents_lock", __import__("threading").Lock()
        ):
            counts = dt.steer_broadcast(
                "everyone: freeze",
                resolve_agent=resolve,
                exclude_session_id="self",
            )
        # self excluded → 2 peer steers + 1 child steer.
        assert counts["sessions"] == 2
        assert counts["subagents"] == 1
        assert counts["failed"] == 0
        assert peer_a.steer_calls == ["everyone: freeze"]
        assert peer_b.steer_calls == ["everyone: freeze"]
        assert child_agent.steer_calls == ["everyone: freeze"]

    def test_broadcast_excludes_sender(self):
        self_agent = FakeAgent("self")
        resolve = _resolver({"self": self_agent})
        counts = dt.steer_broadcast(
            "ping", resolve_agent=resolve, exclude_session_id="self"
        )
        assert counts["sessions"] == 0
        assert self_agent.steer_calls == []

    def test_broadcast_empty_text_is_noop(self):
        resolve = _resolver({"a": FakeAgent("a")})
        counts = dt.steer_broadcast("  ", resolve_agent=resolve)
        assert counts["sessions"] == 0
        assert counts["subagents"] == 0
