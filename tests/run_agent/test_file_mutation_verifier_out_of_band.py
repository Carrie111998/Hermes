"""The file-mutation verifier must not report a file that DID change.

``FILE_MUTATING_TOOL_NAMES`` only covers ``write_file`` and ``patch``, but
those are not the only tools that write.  A model whose ``patch`` is denied
(e.g. by the credential-file guard) and which then lands the same edit via
``terminal`` / ``python -c`` has not over-claimed — yet the tool-level
bookkeeping alone still had the path marked failed, so the footer accused it
of lying about a file that was, in fact, modified.

A verifier that cries wolf gets ignored, which defeats the point of having
one.  These tests pin the filesystem-truth check that fixes it, and — just as
importantly — pin that it still fires when the file really is untouched.
"""

import os

import pytest

from agent.tool_result_classification import path_change_fingerprint
from run_agent import AIAgent


def _footer(failed):
    return AIAgent._format_file_mutation_failure_footer(failed)


class TestOutOfBandWriteSuppression:

    def test_untouched_file_still_warns(self, tmp_path):
        """The original contract: a genuinely failed write is still reported."""
        target = tmp_path / "untouched.py"
        target.write_text("original\n")

        failed = {
            str(target): {
                "tool": "patch",
                "error_preview": "Could not find old_string",
                "fingerprint": path_change_fingerprint(str(target)),
            }
        }

        footer = _footer(failed)
        assert "File-mutation verifier" in footer
        assert "untouched.py" in footer

    def test_out_of_band_write_suppresses_the_warning(self, tmp_path):
        """THE BUG: patch denied, terminal wrote it anyway → no false alarm."""
        target = tmp_path / "written_elsewhere.env"
        target.write_text("BEFORE=1\n")

        failed = {
            str(target): {
                "tool": "patch",
                "error_preview": "Write denied: protected system/credential file",
                "fingerprint": path_change_fingerprint(str(target)),
            }
        }

        # Something the verifier does not watch lands the write.
        os.utime(target, ns=(0, 0))
        target.write_text("AFTER=1\nAND_MORE=2\n")

        assert _footer(failed) == ""

    def test_failed_create_then_out_of_band_create_suppresses(self, tmp_path):
        """A path that did not exist is still a comparable state."""
        target = tmp_path / "created_elsewhere.txt"

        failed = {
            str(target): {
                "tool": "write_file",
                "error_preview": "denied",
                "fingerprint": path_change_fingerprint(str(target)),
            }
        }
        assert failed[str(target)]["fingerprint"] == (False, 0, 0)

        target.write_text("landed via terminal\n")
        assert _footer(failed) == ""

    def test_failed_create_that_stayed_missing_still_warns(self, tmp_path):
        target = tmp_path / "never_created.txt"

        failed = {
            str(target): {
                "tool": "write_file",
                "error_preview": "denied",
                "fingerprint": path_change_fingerprint(str(target)),
            }
        }
        assert "never_created.txt" in _footer(failed)

    def test_mixed_batch_reports_only_the_real_failure(self, tmp_path):
        landed = tmp_path / "landed.py"
        stalled = tmp_path / "stalled.py"
        landed.write_text("a\n")
        stalled.write_text("b\n")

        failed = {
            str(p): {
                "tool": "patch",
                "error_preview": "boom",
                "fingerprint": path_change_fingerprint(str(p)),
            }
            for p in (landed, stalled)
        }

        landed.write_text("a changed out of band\n")

        footer = _footer(failed)
        assert "stalled.py" in footer
        assert "landed.py" not in footer
        assert "1 file(s)" in footer


class TestFailsSafeWhenUnknown:
    """Unknown must never mean silent — that is the whole point."""

    def test_entry_without_fingerprint_still_warns(self, tmp_path):
        """Back-compat: older state, and hand-built dicts in other tests."""
        target = tmp_path / "legacy.py"
        target.write_text("x\n")

        failed = {str(target): {"tool": "patch", "error_preview": "boom"}}
        assert "legacy.py" in _footer(failed)

    def test_unstattable_path_still_warns(self, monkeypatch, tmp_path):
        target = tmp_path / "weird.py"
        target.write_text("x\n")

        failed = {
            str(target): {
                "tool": "patch",
                "error_preview": "boom",
                "fingerprint": (True, 1, 1),
            }
        }

        import run_agent as ra

        def _explode(*_a, **_kw):
            raise PermissionError("nope")

        monkeypatch.setattr(ra, "path_change_fingerprint", _explode)
        assert "weird.py" in _footer(failed)

    def test_unreadable_path_reports_unknown_and_still_warns(self, monkeypatch, tmp_path):
        """``path_change_fingerprint`` returning None must not suppress."""
        target = tmp_path / "opaque.py"
        target.write_text("x\n")

        failed = {
            str(target): {
                "tool": "patch",
                "error_preview": "boom",
                "fingerprint": (True, 1, 1),
            }
        }

        import run_agent as ra

        monkeypatch.setattr(ra, "path_change_fingerprint", lambda *_a, **_k: None)
        assert "opaque.py" in _footer(failed)


class TestPathChangeFingerprint:

    def test_detects_content_change_at_identical_mtime(self, tmp_path):
        """Size catches a same-mtime rewrite; mtime_ns catches same-size."""
        target = tmp_path / "f.txt"
        target.write_text("aaa")
        before = path_change_fingerprint(str(target))

        target.write_text("aaaa")
        os.utime(target, ns=(before[1], before[1]))

        assert path_change_fingerprint(str(target)) != before

    def test_relative_path_resolves_against_terminal_cwd(self, tmp_path, monkeypatch):
        (tmp_path / "rel.txt").write_text("hello")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

        fp = path_change_fingerprint("rel.txt")
        assert fp is not None and fp[0] is True and fp[2] == 5

    def test_missing_path_is_a_comparable_state_not_none(self, tmp_path):
        assert path_change_fingerprint(str(tmp_path / "nope")) == (False, 0, 0)
