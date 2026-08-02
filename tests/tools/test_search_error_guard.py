"""Regression tests for the rg/grep error guard in content search.

The guard in ``_search_with_rg`` / ``_search_with_grep`` had two defects on
``origin/main`` (see PR replacing #39710):

1. **Unreachable on a hard error.** Both methods pipe the search through
   ``| head`` with no ``pipefail``, so the pipeline reported head's exit code
   (0), masking rg/grep's error code (2). The guard never fired, and the
   error text — merged into stdout by ``_exec`` (``stderr=subprocess.STDOUT``)
   — was parsed as bogus match lines instead of being surfaced.

2. **Would have nuked partial results if it ever did fire.** A broad
   ``exit_code == 2`` check discards real matches whenever rg/grep also hit a
   non-fatal error (e.g. one unreadable file in a tree that otherwise
   matched), which both tools signal with exit 2.

The fix adds ``set -o pipefail`` so the real exit code propagates, splits
tool diagnostics from match output by *shape*, and only surfaces an error
when exit==2 AND no usable match payload remains.

These tests drive the real methods through the real local terminal backend.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from tools.file_operations import (
    ShellFileOperations,
    _is_line_oriented_newline_error,
    _pattern_has_regex_newline,
    _split_tool_diagnostics,
)
from tools.environments.local import LocalEnvironment


def _ops(root):
    return ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))


@pytest.fixture
def match_tree(tmp_path):
    """A tree with several files all containing 'needle'."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"needle line {i}\n")
    return tmp_path


@pytest.fixture
def partial_error_tree(tmp_path):
    """A tree with matches plus one unreadable file (forces exit 2 + matches)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip(
            "root bypasses mode-bit permission errors; diagnostic splitting has "
            "deterministic unit coverage below"
        )
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text(f"needle line {i}\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    locked = sub / "locked.txt"
    locked.write_text("needle in locked\n")
    os.chmod(locked, 0o000)
    yield tmp_path
    os.chmod(locked, 0o755)  # let pytest clean up tmp_path


# Run every test once per available backend method.
_METHODS = ["_search_with_grep"]
if shutil.which("rg"):
    _METHODS.append("_search_with_rg")


def _search(ops, method, pattern, path, **kw):
    fn = getattr(ops, method)
    return fn(pattern, str(path), kw.get("file_glob"), kw.get("limit", 50),
              kw.get("offset", 0), kw.get("output_mode", "content"),
              kw.get("context", 0))


@pytest.mark.parametrize("method", _METHODS)
class TestSearchErrorGuard:
    def test_happy_path_returns_matches(self, method, match_tree):
        res = _search(_ops(match_tree), method, "needle", match_tree)
        assert res.error is None
        assert len(res.matches) == 5

    def test_hard_error_is_surfaced(self, method, match_tree):
        # An invalid regex makes rg/grep exit 2 with only diagnostics in
        # stdout. The guard MUST surface it — not return empty matches.
        res = _search(_ops(match_tree), method, "[", match_tree)
        assert res.error is not None, "search error was silently swallowed"
        assert "Search failed" in res.error
        assert not res.matches


    def test_count_mode_with_partial_error(self, method, partial_error_tree):
        res = _search(_ops(partial_error_tree), method, "needle",
                      partial_error_tree, output_mode="count")
        assert res.error is None
        assert res.total_count >= 4


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="non-root runs the parametrized partial-error integration directly",
)
@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("output_mode", ["content", "files_only", "count"])
def test_partial_error_integration_demotes_root_to_unprivileged_uid(
    method,
    output_mode,
):
    """Exercise real EACCES branches for every result shape under root."""
    import pwd

    try:
        account = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("host has no nobody account for unprivileged integration")

    root = Path(tempfile.mkdtemp(prefix="hermes-search-unprivileged-", dir="/tmp"))
    root.chmod(0o755)
    locked = root / "locked.txt"
    try:
        for index in range(4):
            path = root / f"f{index}.txt"
            path.write_text(f"needle line {index}\n")
            path.chmod(0o644)
        locked.write_text("needle in locked\n")
        locked.chmod(0o000)

        class _UnprivilegedEnvironment:
            cwd = str(root)

            def execute(self, command, cwd=None, timeout=None, stdin_data=None):
                def _demote():
                    os.setgroups([])
                    os.setgid(account.pw_gid)
                    os.setuid(account.pw_uid)

                completed = subprocess.run(
                    ["/bin/bash", "-c", command],
                    cwd=cwd or self.cwd,
                    env=os.environ.copy(),
                    input=stdin_data,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                    preexec_fn=_demote,
                )
                return {
                    "output": completed.stdout,
                    "returncode": completed.returncode,
                }

        ops = ShellFileOperations(_UnprivilegedEnvironment(), cwd=str(root))
        result = _search(ops, method, "needle", root, output_mode=output_mode)
        assert result.error is None
        if output_mode == "content":
            assert len(result.matches) == 4
            assert all(match.path != str(locked) for match in result.matches)
        elif output_mode == "files_only":
            assert len(result.files) == 4
            assert all("locked.txt" not in path for path in result.files)
        else:
            assert result.total_count == 4
            assert all("locked.txt" not in path for path in result.counts)
    finally:
        locked.chmod(0o644)
        shutil.rmtree(root)


class TestSearchContentNewlineWarning:
    def test_newline_error_classifier_requires_real_rg_diagnostic_shape(self):
        actual = (
            "Search failed: the literal '\"\\n\"' is not allowed in a regex\n\n"
            "Consider enabling multiline mode with the --multiline flag "
            "(or -U for short).\n"
            "When multiline mode is enabled, new line characters can be matched."
        )
        assert _is_line_oriented_newline_error(actual)

    def test_newline_error_classifier_rejects_keyword_coincidence(self):
        unrelated = (
            "Search failed: docs.txt:1: ordinary content mentions literal \\n "
            "not allowed while discussing multiline parsing"
        )
        assert not _is_line_oriented_newline_error(unrelated)

    def test_odd_backslash_n_is_detected_as_regex_newline(self):
        assert _pattern_has_regex_newline(r"needle\n")
        assert _pattern_has_regex_newline(r"needle\\\n")


    def test_literal_backslash_n_pattern_does_not_warn(self, match_tree):
        res = _ops(match_tree).search(
            r"absent\\npattern",
            path=str(match_tree),
            target="content",
        )

        assert res.error is None
        assert res.total_count == 0
        assert res.warning is None


class TestSplitToolDiagnostics:
    """Unit coverage for the shape-based diagnostic/payload splitter."""

    def test_pure_error_has_empty_payload(self):
        out = "rg: regex parse error:\n    (?:[)\n       ^\nerror: unclosed character class\n"
        diagnostics, payload = _split_tool_diagnostics(out)
        assert payload.strip() == ""
        assert "regex parse error" in diagnostics

    def test_partial_error_separates_matches(self):
        out = ("rg: sub/locked.txt: Permission denied (os error 13)\n"
               "a.txt:1:needle here\nb.txt:2:needle there\n")
        diagnostics, payload = _split_tool_diagnostics(out)
        assert "Permission denied" in diagnostics
        assert "a.txt:1:needle here" in payload
        assert "b.txt:2:needle there" in payload
        assert "Permission denied" not in payload

    def test_unprefixed_diagnostic_with_hyphen_digits_is_not_a_file(self):
        out = (
            "/tmp/hermes-search-3285_case/locked.txt: Permission denied "
            "(os error 13)\n"
            "/tmp/hermes-search-3285_case/f0.txt\n"
        )
        diagnostics, payload = _split_tool_diagnostics(out)
        assert "Permission denied" in diagnostics
        assert "locked.txt" not in payload
        assert payload == "/tmp/hermes-search-3285_case/f0.txt"

    def test_files_only_is_payload(self):
        diagnostics, payload = _split_tool_diagnostics("src/a.py\nsrc/b.py\n")
        assert diagnostics == ""
        assert payload == "src/a.py\nsrc/b.py"

    def test_count_lines_are_payload(self):
        diagnostics, payload = _split_tool_diagnostics("src/a.py:3\nsrc/b.py:1\n")
        assert diagnostics == ""
        assert "src/a.py:3" in payload

    def test_context_lines_and_separator_are_payload(self):
        out = "a.py:5:hit\na.py-6-after\n--\nb.py:9:hit\n"
        diagnostics, payload = _split_tool_diagnostics(out)
        assert diagnostics == ""
        assert "--" in payload
        assert "a.py-6-after" in payload
