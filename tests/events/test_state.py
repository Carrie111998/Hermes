import json
from pathlib import Path

from events.state import load_state, save_state


def test_load_state_returns_default_when_file_missing(tmp_path):
    assert load_state(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"foo": "bar", "n": 42})
    assert load_state(path, default={}) == {"foo": "bar", "n": 42}


def test_load_state_falls_back_on_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_state(path, default={"fallback": True}) == {"fallback": True}


def test_save_state_creates_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "state.json"
    save_state(nested, {"ok": True})
    assert nested.exists()


def test_save_state_is_atomic(tmp_path):
    """Writing should use a tmp file + rename to avoid partial writes."""
    path = tmp_path / "state.json"
    save_state(path, {"count": 1})
    save_state(path, {"count": 2})
    assert json.loads(path.read_text()) == {"count": 2}
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
