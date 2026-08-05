"""A NUL byte in a path or in a binary's contents must never crash the guard.

Two sibling holes in the #76762 family (fixed 2026-08-04, #78811):

1. ``_read_referenced_script`` catches ``OSError`` around ``os.open``, but a
   NUL byte *inside the path* (a candidate tokenized out of a binary's decoded
   contents) makes ``os.open`` raise ``ValueError`` — the guard crashed with
   ``ValueError: embedded null byte`` instead of treating the path as "nothing
   to scan". A guarded path must never crash the guard.

2. ``terminal_tool._read_script_in_env`` decodes a referenced file without the
   binary-NUL check that ``_read_referenced_script`` performs, so a referenced
   binary (ELF/SQLite/etc.) fed NUL-laced text into the recursion, which then
   produced the NUL-containing paths that crashed ``os.open`` in hole 1.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cron.lifecycle_guard import (
    _read_referenced_script,
    contains_gateway_lifecycle_command_or_referenced_script,
)


def test_nul_byte_in_path_never_crashes_read():
    """os.open(path) raises ValueError on embedded NUL — treat as not-a-script."""
    nul_path = Path("/tmp/foo\x00bar.sh")
    assert _read_referenced_script(nul_path) == (None, False)


def test_nul_byte_path_token_never_crashes_full_guard():
    """A NUL-containing token must fall through the whole guard as non-blocking."""
    cmd = "bash /tmp/foo\x00bar.sh"
    assert contains_gateway_lifecycle_command_or_referenced_script(cmd) is False


def test_nul_byte_path_in_quoted_arg_never_crashes_full_guard():
    cmd = "sh -c 'cat /tmp/foo\x00bar.sh'"
    assert contains_gateway_lifecycle_command_or_referenced_script(cmd) is False


def test_referenced_binary_is_skipped_not_scanned(tmp_path):
    """A binary referenced as a script must read as nothing-to-scan, not text."""
    bin_path = tmp_path / "helper.sh"
    bin_path.write_bytes(b"\x7fELF\x00\x00\x00binary\x00junk\x00more")
    assert _read_referenced_script(bin_path) == (None, False)


def test_referenced_text_script_still_reads():
    """The NUL guard must not change behavior for legitimate text scripts."""
    with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", delete=False) as f:
        f.write("echo hello\n")
        path = f.name
    try:
        text, unsafe = _read_referenced_script(Path(path))
        assert unsafe is False
        assert text == "echo hello\n"
    finally:
        os.unlink(path)


def test_empty_and_missing_paths_do_not_crash():
    assert _read_referenced_script(Path("/nonexistent/definitely/missing.sh")) == (None, False)
    # Path("") resolves to cwd, which is a directory — non-regular-file reports
    # unsafe=True without crashing (the point is: no ValueError escape).
    assert _read_referenced_script(Path(""))[0] is None


def test_recursive_scan_of_binary_contents_does_not_crash(tmp_path):
    """Feed binary junk as a script's decoded contents — recursion must skip."""
    # The guard reads a referenced script and recursively scans its text; a
    # binary decoded with errors="replace" yields NUL characters which then
    # tokenize into NUL-containing paths. Write a tiny wrapper that the guard
    # would actually read (a .sh with a NUL inside is binary → skipped).
    bin_path = tmp_path / "payload.sh"
    bin_path.write_bytes(b"\x00\x00\x00\x00")
    # Direct call never raises even with NUL-laden content.
    assert _read_referenced_script(bin_path) == (None, False)
