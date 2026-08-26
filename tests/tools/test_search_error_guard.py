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

import json
import os
import shutil
from unittest import mock

import pytest

from tools.file_operations import (
    ShellFileOperations,
    _pattern_has_regex_newline,
    _split_tool_diagnostics,
)
from tools.environments.local import LocalEnvironment
from tools.file_tools import search_tool


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


class TestSearchContentNewlineWarning:
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


    def test_context_lines_and_separator_are_payload(self):
        out = "a.py:5:hit\na.py-6-after\n--\nb.py:9:hit\n"
        diagnostics, payload = _split_tool_diagnostics(out)
        assert diagnostics == ""
        assert "--" in payload
        assert "a.py-6-after" in payload


class TestHostArchConflation:
    """Pins the host-arch hint added for the Apple-Silicon "Path not found"
    conflation on /usr/local/bin (see FIX-DESIGN.md).

    Three tests:

    1. The verbatim-host case pins the user-visible contract: the error
       string the model sees must include ``host=<system> <machine>``.
    2. The sibling-hint case exercises the new helper with a mock so the
       test is hermetic — it does not depend on whether the host has
       Homebrew installed.
    3. The typo case pins the negative side of the helper: the mapping only
       fires for the architecture mismatch, not for arbitrary typos.
       Without this pin a future widening of ``_HOST_TYPICAL_BIN_PARENTS``
       could quietly expand the hint surface.
    """

    def test_error_includes_host_arch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        # /usr/local/bin does not exist on this Apple-Silicon host, so the
        # call against the verbatim path triggers the not-found branch.
        r = json.loads(search_tool(
            "bash", path="/usr/local/bin", target="files",
            task_id="t-arch-conflation",
        ))
        assert r["total_count"] == 0
        err = r.get("error", "")
        # The original "Path not found:" prefix is preserved so existing
        # pattern-matchers keep working.
        assert "Path not found: /usr/local/bin" in err
        # NEW: the fix surfaces the host arch so a model can recognize an
        # arch mismatch instead of treating it as a guard misfire.
        assert "host=" in err
        assert any(token in err for token in ("Darwin", "Linux"))

    def test_host_typical_sibling_hint_when_parent_exists(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        # Simulate Apple Silicon: /usr/local exists, /usr/local/bin does
        # not, /opt/homebrew/bin does. We can't create real /usr/local in
        # tests, so patch the helper to point at a tmp_path mirror.
        homebrew = tmp_path / "homebrew_bin"
        homebrew.mkdir()
        (homebrew / "bash").write_text("#!/bin/sh\n")
        usr_local = tmp_path / "usr_local"
        usr_local.mkdir()  # parent exists, leaf (usr_local/bin) does not.

        # Patch the sibling resolver so the test doesn't depend on the
        # developer's actual host having Homebrew at /opt/homebrew/bin.
        with mock.patch.object(
            ShellFileOperations, "_host_typical_sibling",
            return_value=str(homebrew),
        ):
            r = json.loads(search_tool(
                "bash", path=str(usr_local / "bin"), target="files",
                task_id="t-sibling-hint",
            ))

        err = r.get("error", "")
        assert "Path not found" in err
        assert "Host-typical sibling exists:" in err
        assert str(homebrew) in err

    def test_sibling_hint_absent_on_typo(self, tmp_path, monkeypatch):
        """A typo'd leaf must NOT trigger the host-typical sibling hint;
        the sibling mapping only fires for known-architecture mismatches.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)),
                                  cwd=str(tmp_path))
        # /usr/lcoal/bin is a typo — no mapping should fire.
        assert ops._host_typical_sibling("/usr/lcoal/bin") is None
        # The host-typical mapping either matches (Apple Silicon, with
        # /opt/homebrew/bin installed) or doesn't (any other host, or no
        # Homebrew). Both outcomes are correct, but the result must be a
        # string in the mapping or None — never an unrelated sibling.
        result = ops._host_typical_sibling("/usr/local/bin")
        assert result in (None, "/opt/homebrew/bin")
