"""Regression tests: gateway lifecycle guard must not false-positive (or
crash) on large binaries referenced by absolute path (#76510).

The guard reads "referenced scripts" for gateway-lifecycle commands. When a
command executes a large binary by path (e.g. ``/.../venv/bin/python``), two
failure modes existed:

1. Local read path (fixed in #76762): the bounded first-chunk read detects
   NUL bytes and skips the binary.
2. Remote-reader path (this fix): ``tools/terminal_tool.py`` falls back to a
   backend shell read (``env.execute("cat ...")``) when the local read is
   skipped for size, handing the guard the ENTIRE binary as text. Scanning
   decoded machine code feeds junk path tokens into the recursion, which
   crashed the guard with ``ValueError: embedded null byte`` from
   ``os.open`` and fail-closed blocked innocent commands.

The security boundary must be immune to what the reader callback returns:
NUL-byte content is never a shell script, so it is skipped exactly like the
local binary skip.
"""

import os

import pytest

from cron.lifecycle_guard import (
    _read_referenced_script,
    contains_gateway_lifecycle_command_or_referenced_script,
)


def _make_large_binary(path, size=1_200_000):
    """Write a file larger than the 1MB read limit with binary content.

    The content mimics what naive tokenization of a real ELF interpreter
    produces: NUL-laden tokens that contain "/" (so the guard treats them
    as referenced-script paths) — the shape that crashed the guard with
    ``ValueError: embedded null byte`` in production (#76510).
    """
    with open(path, "wb") as fh:
        fh.write(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8)
        # Tokens: "\x00/junk\x00path\x00" carries "/" + NUL bytes, so it is
        # yielded as a script path and makes os.open raise ValueError.
        chunk = b"\x00/junk\x00path\x00 hermes\x00gateway\x00stop\x00" * 64
        while fh.tell() < size:
            fh.write(chunk)


class TestRemoteReaderBinarySkip:
    """Binary content returned by the remote reader must be skipped."""

    def test_large_binary_via_remote_reader_not_blocked(self, tmp_path):
        """#76510: absolute-path binary > 1MB whose content comes back
        through the remote reader must not block and must not raise."""
        binary = tmp_path / "python"
        _make_large_binary(binary)
        raw = binary.read_bytes().decode("utf-8", errors="replace")

        def reader(script_path):
            # Mirrors terminal_tool._read_script_in_env's backend fallback:
            # the whole file content comes back as text.
            return raw if os.path.basename(script_path) == "python" else None

        command = f"{binary} --version"
        # Pre-fix this raised ValueError("embedded null byte") or returned
        # True at the recursion-depth limit; either way the command was
        # unusable inside the gateway.
        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                command, cwd=str(tmp_path), read_remote_script=reader
            )
            is False
        )

    def test_remote_reader_text_script_still_scanned(self, tmp_path):
        """The NUL skip must not weaken the guard: text content from the
        remote reader is still scanned for lifecycle commands."""
        missing = tmp_path / "helper.sh"  # not on disk -> reader fallback

        def reader(script_path):
            return "hermes gateway restart" if "helper.sh" in script_path else None

        command = f"bash {missing}"
        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                command, cwd=str(tmp_path), read_remote_script=reader
            )
            is True
        )

    def test_remote_reader_benign_text_not_blocked(self, tmp_path):
        """Benign text scripts from the remote reader stay allowed."""
        missing = tmp_path / "deploy.sh"

        def reader(script_path):
            return "echo deploying && systemctl restart nginx" if "deploy.sh" in script_path else None

        command = f"bash {missing}"
        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                command, cwd=str(tmp_path), read_remote_script=reader
            )
            is False
        )


class TestReadReferencedScriptHardening:
    """The bounded local reader itself must never crash the guard."""

    def test_oversized_binary_local_read_skipped(self, tmp_path):
        """#76762 regression guard: >1MB binary is skipped, not unsafe."""
        binary = tmp_path / "interpreter"
        _make_large_binary(binary)
        text, unsafe = _read_referenced_script(binary)
        assert text is None
        assert unsafe is False

    def test_null_byte_path_does_not_raise(self, tmp_path):
        """Junk tokens tokenized out of binary content can contain NUL
        bytes; os.open raises ValueError on them. The reader must treat
        that as 'nothing to scan', never crash."""
        text, unsafe = _read_referenced_script(tmp_path / "junk\x00.sh")
        assert text is None
        assert unsafe is False


class TestDirectLifecycleCommandsStillBlocked:
    """Security invariants must hold alongside the false-positive fix."""

    def test_direct_restart_still_blocked(self):
        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                "hermes gateway restart", cwd="/tmp"
            )
            is True
        )

    def test_local_script_with_restart_still_blocked(self, tmp_path):
        script = tmp_path / "cycle.sh"
        script.write_text("#!/bin/sh\nhermes gateway restart\n")
        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                f"bash {script}", cwd=str(tmp_path)
            )
            is True
        )
