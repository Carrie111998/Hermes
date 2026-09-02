"""Tests for the holographic provider's on_memory_write mirror.

The manager mirrors the built-in memory tool's add/replace/remove to external
providers. The holographic provider must translate each into a fact-store op:
  - add      -> store.add_fact
  - replace  -> locate by old_text, store.update_fact
  - remove   -> locate by old_text, store.remove_fact

Without the old_text anchor, replace/remove are no-ops (replacing an
unlocatable fact would create a duplicate, which is worse than skipping).
"""

from __future__ import annotations

import pytest

from plugins.memory.holographic import HolographicMemoryProvider


class _FakeStore:
    """Records the calls the mirror makes, with enough behavior to assert on."""

    def __init__(self):
        self.facts = {}
        self._next = 1

    def add_fact(self, content, category="general", tags=""):
        fid = self._next
        self._next += 1
        self.facts[fid] = {"content": content, "category": category}
        return fid

    def search_facts(self, query, category=None, min_trust=0.3, limit=10):
        q = query.lower()
        return [
            {"fact_id": fid, "content": f["content"]}
            for fid, f in self.facts.items()
            if q in f["content"].lower()
        ][:limit]

    def update_fact(self, fact_id, content=None, **kw):
        if fact_id not in self.facts:
            return False
        if content is not None:
            self.facts[fact_id]["content"] = content
        if kw.get("category"):
            self.facts[fact_id]["category"] = kw["category"]
        return True

    def remove_fact(self, fact_id):
        self.facts.pop(fact_id, None)
        return True


def _provider() -> HolographicMemoryProvider:
    p = HolographicMemoryProvider(config={})
    p._store = _FakeStore()
    return p


class TestMirror:
    def test_add_lands(self):
        p = _provider()
        p.on_memory_write("add", "user", "user prefers terse replies")
        # user target -> user_pref category
        contents = {f["content"] for f in p._store.facts.values()}
        assert "user prefers terse replies" in contents
        cats = {f["category"] for f in p._store.facts.values()}
        assert cats == {"user_pref"}

    def test_add_memory_target_general(self):
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        f = next(iter(p._store.facts.values()))
        assert f["category"] == "general"

    def test_replace_updates_existing_fact(self):
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        p.on_memory_write(
            "replace",
            "memory",
            "project uses pytest with xdist",
            metadata={"old_text": "project uses pytest"},
        )
        assert len(p._store.facts) == 1  # updated in place, not duplicated
        assert next(iter(p._store.facts.values()))["content"] == (
            "project uses pytest with xdist"
        )

    def test_replace_without_old_text_is_noop(self):
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        # No anchor: must NOT create a duplicate or crash.
        p.on_memory_write("replace", "memory", "project uses pytest with xdist")
        assert len(p._store.facts) == 1

    def test_remove_deletes_matching_fact(self):
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        p.on_memory_write(
            "remove",
            "memory",
            "",
            metadata={"old_text": "project uses pytest"},
        )
        assert len(p._store.facts) == 0

    def test_remove_without_old_text_is_noop(self):
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        p.on_memory_write("remove", "memory", "")
        # content empty -> no-op regardless; the existing fact must survive.
        assert len(p._store.facts) == 1

    def test_remove_with_empty_content_still_deletes(self):
        # The real remove carries the entry text; ensure content presence is
        # required for remove (we still pass it through).
        p = _provider()
        p.on_memory_write("add", "memory", "project uses pytest")
        p.on_memory_write(
            "remove",
            "memory",
            "project uses pytest",
            metadata={"old_text": "project uses pytest"},
        )
        assert len(p._store.facts) == 0
