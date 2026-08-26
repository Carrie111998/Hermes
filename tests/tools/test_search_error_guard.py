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
import shutil

import pytest

from tools.file_operations import (
    ShellFileOperations,
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
        # ``re:`` opts the pattern out of the default ``re.escape`` so the
        # malformed regex reaches rg/grep unchanged and exercises the
        # error-surfacing path.
        res = _search(_ops(match_tree), method, "re:[unclosed", match_tree)
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


class TestSafeSearchPattern:
    """The default literal escape + ``re:`` opt-in for callers that want
    real regex semantics. Regression coverage for the unescaped-pattern
    injection bug — see _safe_search_pattern's docstring."""

    @pytest.fixture
    def literal_tree(self, tmp_path):
        """A tree whose files contain literal regex metacharacters."""
        (tmp_path / "a.txt").write_text("host.com? yes\n")
        (tmp_path / "b.txt").write_text("c++ is great\n")
        (tmp_path / "c.txt").write_text("[bracket] literal\n")
        (tmp_path / "d.txt").write_text("literal [frx here\n")
        return tmp_path

    def test_literal_metachars_find_matches(self, literal_tree):
        for pattern, expected_file in [
            ("host.com?", "a.txt"),
            ("c++",      "b.txt"),
            ("[bracket]","c.txt"),
            ("[frx",     "d.txt"),  # original repro pattern — must not crash
        ]:
            res = _ops(literal_tree).search(
                pattern=pattern, path=str(literal_tree), target="content",
            )
            assert res.error is None, f"{pattern!r} errored: {res.error}"
            assert any(m.path.endswith(expected_file) for m in res.matches), (
                f"{pattern!r} did not match {expected_file}; "
                f"got {[m.path for m in res.matches]}"
            )

    def test_plain_alphanumeric_unchanged(self, literal_tree):
        # No-metachar pattern: re.escape is a no-op, behavior is identical
        # to the pre-fix path.
        res = _ops(literal_tree).search(
            pattern="bracket", path=str(literal_tree), target="content",
        )
        assert res.error is None
        assert any(m.path.endswith("c.txt") for m in res.matches)

    def test_regex_opt_in_still_works(self, literal_tree):
        # ``re:`` prefix opts out of literal-mode; real regex semantics land.
        (literal_tree / "def.txt").write_text("def hello():\n    pass\n")
        res = _ops(literal_tree).search(
            pattern=r"re:def\s+\w+", path=str(literal_tree), target="content",
        )
        assert res.error is None
        assert any(m.path.endswith("def.txt") for m in res.matches)

    def test_regex_opt_in_malformed_still_surfaces_error(self, literal_tree):
        # Opt-in + malformed regex must still surface the parse error.
        res = _ops(literal_tree).search(
            pattern="re:[unclosed", path=str(literal_tree), target="content",
        )
        assert res.error is not None
        assert "Search failed" in res.error

    def test_unit_escape_and_opt_in(self):
        from tools.file_operations import _safe_search_pattern
        # Plain string: escaped.
        assert _safe_search_pattern("frx") == "frx"
        assert _safe_search_pattern("[frx") == r"\[frx"
        assert _safe_search_pattern("host.com?") == r"host\.com\?"
        # Opt-in: prefix stripped, body NOT escaped.
        assert _safe_search_pattern("re:def\\s+\\w+") == "def\\s+\\w+"
        assert _safe_search_pattern("re:[frx") == "[frx"
