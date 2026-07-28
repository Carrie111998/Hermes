"""files_written must not be permanently empty (H-21).

delegate_task built every subagent.complete event's `files_written` from
writes_since("", wall_start, []) with the comment "# all writes since
wall_start". But an empty path filter matched nothing, so the field was empty
in every event ever emitted — the parent was told a child wrote no files
regardless of what it did.

Fixed with an explicit sentinel rather than by making [] mean "everything":
the OTHER caller passes the parent's read set, and flipping empty's meaning
would have made the sibling-write reminder list files the parent never read.
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from tools.file_state import FileStateRegistry


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.delenv("HERMES_DISABLE_FILE_STATE_GUARD", raising=False)
    return FileStateRegistry()


@pytest.fixture
def files():
    with tempfile.TemporaryDirectory() as d:
        made = {}
        for name in ("a.py", "b.py", "c.py"):
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            made[name] = os.path.realpath(path)
        yield made


def _seed(registry, files):
    registry.note_write("child-1", files["a.py"])
    registry.note_write("child-1", files["b.py"])
    registry.note_write("parent", files["c.py"])
    return time.time() - 5


def test_none_means_no_filter(registry, files):
    since = _seed(registry, files)
    result = registry.writes_since("", since, None)
    assert set(result) == {"child-1", "parent"}
    assert len(result["child-1"]) == 2


def test_empty_list_still_means_match_nothing(registry, files):
    """Unchanged on purpose — the sibling-write caller depends on it."""
    since = _seed(registry, files)
    assert registry.writes_since("", since, []) == {}


def test_explicit_paths_still_filter(registry, files):
    since = _seed(registry, files)
    result = registry.writes_since("", since, [files["a.py"]])
    assert result == {"child-1": [files["a.py"]]}


def test_exclude_task_id_still_applies_with_no_filter(registry, files):
    since = _seed(registry, files)
    assert set(registry.writes_since("child-1", since, None)) == {"parent"}


def test_since_timestamp_still_applies_with_no_filter(registry, files):
    _seed(registry, files)
    assert registry.writes_since("", time.time() + 60, None) == {}


def test_default_argument_is_no_filter(registry, files):
    """delegate_task relies on the default; a change to [] would silently
    restore the empty-forever behaviour."""
    since = _seed(registry, files)
    assert registry.writes_since("", since) != {}


def test_delegate_tool_no_longer_passes_an_empty_filter():
    """Structural: the call site is what was wrong, not the helper."""
    import inspect

    import tools.delegate_tool as dt

    src = inspect.getsource(dt)
    assert 'writes_since(\n                "", wall_start, None\n            )' in src \
        or 'writes_since("", wall_start, None)' in src, (
        "delegate_tool is passing a path filter again — files_written will be empty"
    )
