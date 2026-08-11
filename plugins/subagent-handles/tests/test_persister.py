import os
import sys
import tempfile

import pytest

from src.persister import SessionPersister
from src.registry import SubagentHandle, SubagentRegistry


def test_checkpoint_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = SessionPersister(os.path.join(tmp, "sessions"))
        h = SubagentHandle(subagent_id="a1", session_id="s1", goal="g1", state="running")
        p.checkpoint(h)
        loaded = p.load("a1")
        assert loaded is not None
        assert loaded.subagent_id == "a1"
        assert loaded.session_id == "s1"
        assert loaded.state == "running"


def test_remove():
    with tempfile.TemporaryDirectory() as tmp:
        p = SessionPersister(os.path.join(tmp, "sessions"))
        p.checkpoint(SubagentHandle(subagent_id="a1", session_id="s1", goal="g1"))
        assert p.remove("a1") is True
        assert p.load("a1") is None
        assert p.remove("a1") is False


def test_restore_into_registry():
    with tempfile.TemporaryDirectory() as tmp:
        p = SessionPersister(os.path.join(tmp, "sessions"))
        p.checkpoint(SubagentHandle(subagent_id="a1", session_id="s1", goal="g1"))
        p.checkpoint(SubagentHandle(subagent_id="a2", session_id="s2", goal="g2", state="done"))
        registry = SubagentRegistry()
        restored = p.restore(registry)
        assert set(restored.keys()) == {"a1", "a2"}
        assert registry.resolve("a1").state == "running"
        assert registry.resolve("a2").state == "done"


def test_restore_skips_bad_file():
    import tempfile as _temp
    with _temp.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "a1.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(root, "bad.json"), "w", encoding="utf-8") as f:
            f.write("not-json")
        p = SessionPersister(root)
        registry = SubagentRegistry()
        restored = p.restore(registry)
        assert restored == {}


def test_checkpoint_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "sessions")
        p = SessionPersister(root)
        p.checkpoint(SubagentHandle(subagent_id="a1", session_id="s1", goal="g1"))
        assert os.listdir(root) == ["a1.json"]
        assert not os.path.exists(os.path.join(root, "a1.json.tmp"))
