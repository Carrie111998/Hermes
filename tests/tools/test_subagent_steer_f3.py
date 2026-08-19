"""F3-complete tests — user-facing targeted subagent steering (prime-agent port).

These exercise the new name-addressed helpers and the peer/broadcast fan-out
that together form the *user entry point* for F3 (the partial PR shipped only
plumbing). All run without booting the gateway — they use the in-process
delegation registry directly, mirroring tests/tools/test_subagent_steer.py.

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

    def __init__(self, name: str = "fake", accept: bool = True, boom: bool = False):
        self.name = name
        self.accept = accept
        self.boom = boom
        self.steer_calls: list[str] = []

    def steer(self, text: str) -> bool:
        if self.boom:
            raise RuntimeError("nope")
        if not text or not text.strip():
            return False
        self.steer_calls.append(text.strip())
        return self.accept


def _owner_parent(session_id: str = "owner-session") -> SimpleNamespace:
    """A fake parent agent whose session_id owns the child records."""
    return SimpleNamespace(session_id=session_id)


def _register_named(sid: str, name: str | None, owner: SimpleNamespace,
                    *, accept: bool = True, agent: FakeAgent | None = None):
    ag = agent or FakeAgent(name=sid, accept=accept)
    dt._register_subagent(
        {
            "subagent_id": sid,
            "parent_id": "root",
            "depth": 1,
            "goal": "test goal",
            "status": "running",
            "agent": ag,
            "owner_agent_session_id": owner.session_id,
            "name": name,
        }
    )
    return ag


# ──────────────────────────────────────────────────────────────────────
# name-addressed control plane
# ──────────────────────────────────────────────────────────────────────


class TestSteerSubagentByName:
    def test_resolves_name_and_queues(self):
        owner = _owner_parent()
        ag = _register_named("sid-f3-a", "worker", owner)
        try:
            res = dt.steer_subagent_by_name("worker", "focus on pricing", owner)
            assert res["status"] == "queued"
            assert res["subagent_id"] == "sid-f3-a"
            assert ag.steer_calls == ["focus on pricing"]
        finally:
            dt._unregister_subagent("sid-f3-a")

    def test_unknown_name_is_no_such_subagent(self):
        owner = _owner_parent()
        res = dt.steer_subagent_by_name("ghost", "hi", owner)
        assert res["status"] == "no_such_subagent"
        assert res.get("name") == "ghost"

    def test_empty_text_is_empty_text(self):
        res = dt.steer_subagent_by_name("worker", "   ", _owner_parent())
        assert res["status"] == "empty_text"

    def test_known_but_not_accepting_is_distinct_error(self):
        owner = _owner_parent()
        ag = _register_named("sid-f3-b", "done", owner, accept=False)
        try:
            res = dt.steer_subagent_by_name("done", "hi", owner)
            assert res["status"] == "not_accepting"
            assert res["subagent_id"] == "sid-f3-b"
            assert ag.steer_calls == []
        finally:
            dt._unregister_subagent("sid-f3-b")

    def test_name_scoped_to_owner_spawn_tree(self):
        owner = _owner_parent("owner-1")
        other = _owner_parent("owner-2")
        _register_named("sid-f3-c", "shared", owner)
        _register_named("sid-f3-d", "shared", other)
        try:
            # owner-1 steering "shared" must resolve to its OWN child only.
            res = dt.steer_subagent_by_name("shared", "x", owner)
            assert res["status"] == "queued"
            assert res["subagent_id"] == "sid-f3-c"
        finally:
            dt._unregister_subagent("sid-f3-c")
            dt._unregister_subagent("sid-f3-d")


class TestListSubagents:
    def test_filters_by_owner_and_name(self):
        owner = _owner_parent("o1")
        other = _owner_parent("o2")
        _register_named("sid-l1", "w", owner)
        _register_named("sid-l2", "w", other)
        _register_named("sid-l3", "z", owner)
        try:
            assert len(dt.list_subagents(owner)) == 2
            assert len(dt.list_subagents(owner, name="w")) == 1
            assert len(dt.list_subagents(other, name="w")) == 1
            assert len(dt.list_subagents(owner, name="nope")) == 0
            # list_active_subagents (unscoped) still sees all three.
            assert len(dt.list_active_subagents()) == 3
        finally:
            for s in ("sid-l1", "sid-l2", "sid-l3"):
                dt._unregister_subagent(s)

    def test_entry_carries_name(self):
        owner = _owner_parent()
        _register_named("sid-l4", "named-child", owner)
        try:
            entries = dt.list_subagents(owner, name="named-child")
            assert entries and entries[0]["name"] == "named-child"
            assert entries[0]["subagent_id"] == "sid-l4"
        finally:
            dt._unregister_subagent("sid-l4")


# ──────────────────────────────────────────────────────────────────────
# control-action handler: name resolution + new actions
# ──────────────────────────────────────────────────────────────────────


def _call_control(action, *, subagent_id=None, name=None, message=None,
                  parent=None):
    return dt._handle_control_action(
        action, subagent_id, message, parent or _owner_parent(), name=name
    )


class TestControlActionNameResolution:
    def test_steer_by_name(self):
        owner = _owner_parent()
        ag = _register_named("sid-ctl-1", "harvester", owner)
        try:
            out = _call_control("steer", name="harvester", message="crank it")
            import json
            payload = json.loads(out)
            assert payload["action"] == "steer"
            assert payload["subagent_id"] == "sid-ctl-1"
            assert payload["name"] == "harvester"
            assert ag.steer_calls == ["crank it"]
        finally:
            dt._unregister_subagent("sid-ctl-1")

    def test_stop_by_name_unknown(self):
        owner = _owner_parent()
        out = _call_control("stop", name="ghost")
        assert "No live subagent named 'ghost'" in out

    def test_list_includes_name_field(self):
        owner = _owner_parent()
        _register_named("sid-ctl-2", "visible", owner)
        try:
            import json
            payload = json.loads(_call_control("list"))
            assert payload["count"] == 1
            assert payload["subagents"][0]["name"] == "visible"
        finally:
            dt._unregister_subagent("sid-ctl-2")


# ──────────────────────────────────────────────────────────────────────
# peer / broadcast plumbing (reuse of AIAgent.steer, no role violation)
# ──────────────────────────────────────────────────────────────────────


def _resolver(map_: dict):
    def _r(target: str):
        if target == "__list__":
            return list(map_.keys())
        return map_.get(target)
    return _r


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


class TestSteerBroadcast:
    def test_broadcasts_to_peers_and_children(self):
        peer_a = FakeAgent("a")
        peer_b = FakeAgent("b")
        resolve = _resolver({"a": peer_a, "b": peer_b, "self": FakeAgent("self")})
        child_agent = FakeAgent("child")
        child_record = {
            "accepting_steer": True,
            "owner_session_id": "self",
            "owner_transport": object(),
            "owner_session_record": object(),
            "agent": child_agent,
            "subagent_id": "child-1",
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
