"""Safety guards for trajectory_compressor: input/output identity + atomic write.

The compressor used to accept ``--output`` equal to ``--input`` (or an
``output_dir`` equal to the input dir) and wrote with a bare ``open(..., 'w')``,
truncating the source JSONL. These guards make that impossible and make every
final write atomic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_compressor import (
    TrajectoryCompressor,
    _atomic_write_text,
    _reject_same_path,
)


def test_reject_same_path_identical_raises(tmp_path):
    p = tmp_path / "in.jsonl"
    with pytest.raises(ValueError):
        _reject_same_path(p, p)


def test_reject_same_path_alias_raises(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text("orig", encoding="utf-8")
    alias = tmp_path / "sub" / ".." / "in.jsonl"  # resolves to the same file
    with pytest.raises(ValueError):
        _reject_same_path(p, alias)


def test_reject_same_path_distinct_is_allowed(tmp_path):
    _reject_same_path(tmp_path / "a.jsonl", tmp_path / "b.jsonl")  # no raise


def test_atomic_write_text_writes_content_and_leaves_no_temp(tmp_path):
    out = tmp_path / "out.jsonl"
    _atomic_write_text(out, lambda f: f.write("line1\nline2\n"))
    assert out.read_text(encoding="utf-8") == "line1\nline2\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_preserves_prior_file_on_error(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text("ORIGINAL", encoding="utf-8")

    def _boom(f):
        f.write("partial")
        raise RuntimeError("mid-write crash")

    with pytest.raises(RuntimeError):
        _atomic_write_text(out, _boom)

    # The previous content is intact and no temp file is left behind.
    assert out.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []


def test_process_directory_rejects_in_place(tmp_path):
    # __new__ skips tokenizer init (which would hit the network); the guard is
    # the first thing process_directory does, so no tokenizer is needed.
    compressor = TrajectoryCompressor.__new__(TrajectoryCompressor)
    with pytest.raises(ValueError):
        compressor.process_directory(tmp_path, tmp_path)
