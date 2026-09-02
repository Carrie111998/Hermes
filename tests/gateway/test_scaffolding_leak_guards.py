"""Guards against the 2026-08-19 compaction-echo leak.

Two independent layers, tested separately:
1. is_leaked_scaffolding_response — withholds a reply that reproduces the
   injected compaction handoff block.
2. extract_local_files — refuses to auto-attach harness-internal files whose
   bare paths appear in reply text (the echoed handoff mentioned 6 of them).
"""

import sys
from pathlib import Path

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.response_filters import is_leaked_scaffolding_response  # noqa: E402


HANDOFF = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context window."
)


class TestLeakedScaffoldingFilter:
    def test_verbatim_echo_is_flagged(self):
        assert is_leaked_scaffolding_response(HANDOFF)

    def test_echo_after_preamble_line_is_flagged(self):
        assert is_leaked_scaffolding_response("Here's where we are:\n" + HANDOFF)

    def test_quoted_echo_is_flagged(self):
        assert is_leaked_scaffolding_response("> " + HANDOFF)

    def test_discussion_of_marker_is_delivered(self):
        prose = (
            "Compaction ran at 20:41; the summary is stamped with the "
            "`[CONTEXT COMPACTION` header by context_compressor.py — that's "
            "internal and shouldn't be delivered."
        )
        assert not is_leaked_scaffolding_response(prose)

    def test_marker_deep_in_reply_is_delivered(self):
        prose = "line\n" * 10 + HANDOFF
        assert not is_leaked_scaffolding_response(prose)

    def test_non_strings_pass(self):
        assert not is_leaked_scaffolding_response(None)
        assert not is_leaked_scaffolding_response(42)
        assert not is_leaked_scaffolding_response("")


class TestProtectedFileAttachment:
    def _extract(self, content):
        from gateway.platforms.base import BasePlatformAdapter
        return BasePlatformAdapter.extract_local_files(content)

    def test_hermes_dir_file_not_attached(self, tmp_path, monkeypatch):
        import os
        from gateway.platforms import base as base_mod
        fake_home = tmp_path
        hermes = fake_home / ".hermes"
        hermes.mkdir()
        f = hermes / "notes.md"
        f.write_text("internal")
        monkeypatch.setenv("HOME", str(fake_home))
        files, _ = self._extract(f"see {f} for details")
        assert files == []

    def test_identity_files_not_attached_anywhere(self, tmp_path):
        f = tmp_path / "AGENTS.md"
        f.write_text("rules")
        files, _ = self._extract(f"the rules live at {f}")
        assert files == []

    def test_ordinary_artifact_still_attached(self, tmp_path):
        f = tmp_path / "report.md"
        f.write_text("# report")
        files, cleaned = self._extract(f"wrote the report to {f}")
        assert files == [str(f)]
        assert str(f) not in cleaned
