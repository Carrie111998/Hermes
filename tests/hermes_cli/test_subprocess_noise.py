"""Tests for the benign Darwin malloc-stack-logging stderr filter (#54833).

The libmalloc/MallocStackLogging teardown line is harmless but pollutes
gateway.error.log, desktop.log, terminals, and model context.  The filter must
match ONLY that exact whole line, ONLY on Darwin, and must leave every other
byte of stderr alone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.subprocess_noise import (
    filter_benign_darwin_subprocess_stderr,
    is_benign_darwin_malloc_stack_logging_line,
)

EXACT = (
    "MallocStackLogging: can't turn off malloc stack logging because it was "
    "not enabled."
)
WITH_PID = f"Python(12345) {EXACT}"

# One observed on the reporter's machine and one on a plain launchd child:
# process-name prefix varies ("Python", "python") — both are the same line.
OBSERVED = [
    EXACT,
    WITH_PID,
    f"python(56273) {EXACT}",
]


class TestPredicate:
    def test_observed_variants_match_on_darwin(self):
        for line in OBSERVED:
            assert is_benign_darwin_malloc_stack_logging_line(
                line, platform="darwin"
            ), line

    def test_crlf_and_missing_newline_tolerated(self):
        assert is_benign_darwin_malloc_stack_logging_line(
            EXACT + "\r\n", platform="darwin"
        )
        assert is_benign_darwin_malloc_stack_logging_line(
            EXACT + "\n", platform="darwin"
        )

    def test_not_matched_on_linux_or_windows(self):
        for plat in ("linux", "win32"):
            assert not is_benign_darwin_malloc_stack_logging_line(
                EXACT, platform=plat
            )

    def test_different_malloc_diagnostic_not_matched(self):
        # Other MallocStackLogging diagnostics stay visible on purpose.
        assert not is_benign_darwin_malloc_stack_logging_line(
            "MallocStackLogging: malloc stack logging enabled", platform="darwin"
        )

    def test_embedded_or_prefixed_text_not_matched(self):
        for line in (
            f"ERROR: {EXACT}",
            f"{EXACT} (see docs)",
            f"prefix {EXACT}",
            EXACT[:-1],  # truncated sentence
            EXACT.replace("can't", "can t"),  # near-miss
            "Python(abc) " + EXACT,  # malformed pid
            "",
        ):
            assert not is_benign_darwin_malloc_stack_logging_line(
                line, platform="darwin"
            ), line


class TestFilter:
    def test_empty_input(self):
        assert filter_benign_darwin_subprocess_stderr("", platform="darwin") == ""

    def test_removes_only_the_noise_line_and_keeps_neighbors(self):
        text = f"real error line\n{WITH_PID}\nanother real line\n"
        out = filter_benign_darwin_subprocess_stderr(text, platform="darwin")
        assert out == "real error line\nanother real line\n"

    def test_no_final_newline_is_preserved(self):
        text = f"noise\n{EXACT}"
        out = filter_benign_darwin_subprocess_stderr(text, platform="darwin")
        assert out == "noise\n"

    def test_crlf_neighbors_survive(self):
        text = f"keep me\r\n{EXACT}\r\nkeep me too\r\n"
        out = filter_benign_darwin_subprocess_stderr(text, platform="darwin")
        assert out == "keep me\r\nkeep me too\r\n"

    def test_all_noise_collapses_to_empty(self):
        text = "\n".join(OBSERVED)
        assert (
            filter_benign_darwin_subprocess_stderr(text, platform="darwin") == ""
        )

    def test_non_darwin_is_byte_identical_noop(self):
        text = f"something\n{WITH_PID}\nelse\n"
        assert (
            filter_benign_darwin_subprocess_stderr(text, platform="linux") == text
        )
        assert (
            filter_benign_darwin_subprocess_stderr(text, platform="win32") == text
        )

    def test_identity_fast_path_returns_same_object_off_darwin(self):
        text = f"whatever\n{EXACT}\n"
        out = filter_benign_darwin_subprocess_stderr(text, platform="linux")
        assert out is text

    def test_lines_are_never_joined_by_deletion(self):
        # A removed middle line must not splice surrounding diagnostics.
        text = f"AA\n{EXACT}\nBB\n"
        out = filter_benign_darwin_subprocess_stderr(text, platform="darwin")
        assert out == "AA\nBB\n"
