"""Guard: no test may anchor a path by escaping the checkout root with a
hardcoded ``Path(__file__).parents[N]`` hop count.

WHY THIS IS AUTOMATED. The defect has been introduced three times and
"fully enumerated" once — the census went stale in two days:

* 2026-08-11 — three sites fixed (``tests/cron/test_critic_skill_review_contract.py``,
  ``tests/devflow_delegation/test_roadmap_intake_migration.py``,
  ``tests/events/subscribers/test_cron_stale_monitor.py``) and the class was
  declared "fully enumerated for tests/".
* 2026-08-13 — ``0162e24718`` added ``tests/cron/test_jobflow_researcher_wake_gate.py``
  with the identical bug written from scratch.
* 2026-08-16 — fixed again (``704581c54b``, landed as ``5c4fb4223b``).

WHY IT KEEPS SLIPPING THROUGH. A hardcoded hop count is correct in exactly
one layout. From the main checkout ``agent-src/tests/cron/x.py``,
``parents[3]`` is ``~/.hermes`` — right. From a worktree at
``agent-src/.claude/worktrees/<name>/tests/cron/x.py`` the same expression is
``agent-src/.claude/worktrees`` — wrong, and the target artifact is absent.
So it PASSES in the main checkout where it was authored, and fails only for
whoever next works in a worktree, as a stable reproducible red that survives
a stash-and-compare and therefore gets written off as "pre-existing, not
mine". That is exactly what happened on 2026-08-16 on branch
``claude/intelligent-lamarr-ee488d``.

THE RULE. For a test at repo-relative path ``p``,
``Path(__file__).resolve().parents[N]`` is the checkout root when
``N == len(p.parts) - 1``. Anything LARGER escapes the checkout. Escaping is
not wrong per se — it is wrong *combined with a hardcoded count*, because the
distance to ``~/.hermes`` differs by 3 between the two layouts.

THE SANCTIONED FIX for a genuine hit is the in-repo ``_find_hermes_root()``
pattern: search upward from ``__file__`` for the ancestor that actually
contains the target artifact, plus a skip when it is absent. Use a
module-level ``pytest.skip(allow_module_level=True)`` only when EVERY test in
the file needs the artifact; if only some do, use ``pytest.mark.skipif`` on
those (see ``tests/events/subscribers/test_cron_stale_monitor.py``, where 8 of
9 tests are hermetic). Do NOT reach for ``git rev-parse --show-toplevel`` (it
returns the *worktree*, reproducing the bug) or
``hermes_constants.get_default_hermes_root()`` (it reads ``HERMES_HOME``,
which ``tests/conftest.py`` redirects to a per-test tempdir).

KNOWN LIMITATION — deliberately narrow to stay false-positive free. The
detector only matches a hop count taken directly off a literal
``Path(__file__)``. It does NOT follow an alias
(``_THIS = Path(__file__).resolve()`` … ``_THIS.parents[2]``, as in
``tests/stress/test_atypical_scenarios.py``), and it does not look at hop
counts off an *imported module's* ``__file__`` (``test_gateway_diag.py``,
``test_update_autostash.py``, ``test_terminal_exit_scratch_binding.py``) —
those are depth-stable and a genuinely different class. Widening the pattern
was measured against the tree and rejected: it flags correct code. See
``test_alias_bound_dunder_file_is_a_documented_blind_spot``.

FALSIFICATION (do not trust an empty result that was never proven to run).
Against a checkout of ``4b779c40e5`` this detector reports exactly one
violation — ``tests/cron/test_jobflow_researcher_wake_gate.py: parents[3]``
against a root-hop limit of 2 — out of 196 ``parents[N]`` occurrences
examined. Against current HEAD it reports zero out of the same 196.
``test_scan_is_not_vacuous`` keeps that second number honest.
"""

from __future__ import annotations

import pathlib
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

# This file sits at ``tests/<name>.py`` — depth 1 — so ``parents[1]`` IS the
# checkout root and the derivation never leaves it. That is the very rule
# enforced below (``N == len(rel.parts) - 1``), applied to the guard itself;
# ``test_this_guard_is_itself_layout_independent`` proves it resolved.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"

# Matches a hop count taken directly off a literal ``Path(__file__)``. The
# unanchored ``Path(`` also picks up the qualified ``pathlib.Path(__file__)``
# spelling, which is how the 2026-08-13 regression was written. An imported
# module's ``__file__`` (``Path(mod.__file__)``) does not match: the ``mod.``
# sits inside the parentheses.
#
# ``\s*`` at every join is load-bearing. Flattening turns a newline into a
# space, so a chain the formatter split at the dots arrives as
# ``Path(__file__) .resolve() .parents[3]``. Without the tolerance that form
# reads as clean — a silent evasion, caught by
# ``test_flags_a_multi_line_split_expression``. Tolerating whitespace cannot
# introduce a false positive: it only matches source that Python parses the
# same way.
_PARENTS_OFF_DUNDER_FILE = re.compile(
    r"Path\s*\(\s*__file__\s*\)"
    r"(?:\s*\.\s*resolve\s*\(\s*\))?"
    r"\s*\.\s*parents\s*\[\s*(\d+)\s*\]"
)

# The scan must skip this file: the fixtures below embed violating source as
# string literals, and a self-scan would flag them. Nothing is lost — this
# file's own anchoring is checked directly, see the test named above.
_SELF = pathlib.Path(__file__).resolve()

# Read the tree on threads. Serial reads of ~2.5k files cost 15-30s on this
# box (Defender, not decode); 16 threads bring the same bytes back in ~1.7s.
_READ_WORKERS = 16


def _flatten(source: str) -> str:
    """Collapse all whitespace so a multi-line expression matches as one unit.

    The 2026-08-13 site was written across two lines, with the ``parents[3]``
    and the ``/ "profiles" / ...`` continuation split apart.
    """
    return " ".join(source.split())


def _root_hop_count(rel: pathlib.PurePath) -> int:
    """Hops from a repo-relative file up to the checkout root.

    ``tests/x.py`` -> 1; ``tests/cron/x.py`` -> 2; ``tests/gateway/relay/x.py``
    -> 3. A ``parents[N]`` with this exact N *is* the root and is correct.
    """
    return len(rel.parts) - 1


def _escaping_hops(rel: pathlib.PurePath, source: str) -> list[int]:
    """Return every hop count in ``source`` that escapes the checkout root."""
    limit = _root_hop_count(rel)
    return [
        int(match.group(1))
        for match in _PARENTS_OFF_DUNDER_FILE.finditer(_flatten(source))
        if int(match.group(1)) > limit
    ]


def _read(path: pathlib.Path) -> tuple[pathlib.Path, str | None]:
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path, None


class _Scan:
    def __init__(self) -> None:
        self.files_read = 0
        self.occurrences = 0
        self.unreadable: list[str] = []
        self.violations: list[tuple[str, int, int]] = []


def _scan_tests_tree() -> _Scan:
    scan = _Scan()
    paths = [p for p in sorted(_TESTS_DIR.rglob("*.py")) if p.resolve() != _SELF]
    with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        for path, source in pool.map(_read, paths):
            rel = path.relative_to(_REPO_ROOT)
            if source is None:
                scan.unreadable.append(rel.as_posix())
                continue
            scan.files_read += 1
            flat = _flatten(source)
            hops = [int(m.group(1)) for m in _PARENTS_OFF_DUNDER_FILE.finditer(flat)]
            scan.occurrences += len(hops)
            limit = _root_hop_count(rel)
            scan.violations.extend(
                (rel.as_posix(), n, limit) for n in hops if n > limit
            )
    return scan


@pytest.fixture(scope="module")
def scan() -> _Scan:
    return _scan_tests_tree()


class TestTreeIsClean:
    def test_no_test_escapes_the_checkout_root(self, scan: _Scan) -> None:
        if not scan.violations:
            return
        lines = [
            f"  {rel}: parents[{n}] escapes the checkout root "
            f"(root is parents[{limit}] from this depth)"
            for rel, n, limit in scan.violations
        ]
        pytest.fail(
            "Hardcoded parents[N] escaping the checkout root in "
            f"{len(scan.violations)} place(s):\n"
            + "\n".join(lines)
            + "\n\nThis passes from the main checkout and fails from a git "
            "worktree, where the file sits 3 levels deeper. Replace the fixed "
            "hop count with the in-repo `_find_hermes_root()` pattern: walk "
            "`Path(__file__).resolve().parents` for the ancestor that contains "
            "the artifact, and skip when it is absent (module-level "
            "`pytest.skip(allow_module_level=True)` only if EVERY test needs "
            "it, otherwise `pytest.mark.skipif` on the ones that do). Do NOT "
            "use `git rev-parse --show-toplevel` (returns the worktree) or "
            "`hermes_constants.get_default_hermes_root()` (reads HERMES_HOME, "
            "which tests/conftest.py redirects to a tempdir). Worked examples: "
            "tests/events/subscribers/test_cron_stale_monitor.py, "
            "tests/cron/test_jobflow_researcher_wake_gate.py. Full rationale: "
            f"{_SELF.name} module docstring."
        )

    def test_scan_is_not_vacuous(self, scan: _Scan) -> None:
        """An empty result is only meaningful if the scan provably ran.

        This whole defect class keeps surviving censuses that returned
        nothing because they never actually looked. Both floors sit far below
        the observed 2026-08-16 numbers (2510 files, 196 occurrences), so
        ordinary churn will not trip them — but a broken glob, a wrong root,
        or a regex that stops matching drops one of them to zero.
        """
        assert scan.files_read > 500, (
            f"only {scan.files_read} test files read from {_TESTS_DIR} — "
            "the tree walk is broken, so a clean result proves nothing"
        )
        assert scan.occurrences > 50, (
            f"only {scan.occurrences} `Path(__file__)...parents[N]` occurrences "
            "matched across the tree — the pattern has stopped matching, so a "
            "clean result proves nothing"
        )

    def test_every_test_file_was_readable(self, scan: _Scan) -> None:
        assert not scan.unreadable, (
            "unreadable test files were silently skipped by the scan: "
            f"{scan.unreadable}"
        )


class TestDetectorPositiveControl:
    def test_flags_the_2026_08_13_regression_verbatim(self) -> None:
        """The exact source that shipped in ``0162e24718``, at its real path.

        Multi-line, and using the qualified ``pathlib.Path`` spelling — both
        of which the detector has to survive.
        """
        source = (
            "GATE = (\n"
            "    pathlib.Path(__file__).resolve().parents[3]\n"
            '    / "profiles" / "main" / "scripts" '
            '/ "jobflow_researcher_wake_gate.py"\n'
            ")\n"
        )
        rel = pathlib.PurePosixPath(
            "tests/cron/test_jobflow_researcher_wake_gate.py"
        )
        assert _escaping_hops(rel, source) == [3]

    def test_flags_a_multi_line_split_expression(self) -> None:
        """Whitespace-flattening is load-bearing, not decoration."""
        rel = pathlib.PurePosixPath("tests/cron/x.py")
        split = "ROOT = (\n    Path(__file__)\n    .resolve()\n    .parents[3]\n)\n"
        assert _escaping_hops(rel, split) == [3]

    @pytest.mark.parametrize(
        ("rel", "hop", "escapes"),
        [
            ("tests/x.py", 1, False),  # depth 1: parents[1] IS the root
            ("tests/x.py", 2, True),
            ("tests/cron/x.py", 2, False),  # depth 2: parents[2] IS the root
            ("tests/cron/x.py", 3, True),
            ("tests/gateway/relay/x.py", 3, False),  # depth 3 -> parents[3]
            ("tests/gateway/relay/x.py", 4, True),
        ],
    )
    def test_boundary_is_exactly_len_parts_minus_one(
        self, rel: str, hop: int, escapes: bool
    ) -> None:
        source = f"ROOT = Path(__file__).resolve().parents[{hop}]"
        found = _escaping_hops(pathlib.PurePosixPath(rel), source)
        assert found == ([hop] if escapes else [])


class TestDetectorNegativeControls:
    """Real in-tree code that is CORRECT and must never be flagged.

    Each case was verified against the tree on 2026-08-16.
    """

    def test_relay_tests_parents_3_from_depth_3_is_the_root(self) -> None:
        rel = pathlib.PurePosixPath("tests/gateway/relay/test_no_stub_leak.py")
        source = "_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]"
        assert _escaping_hops(rel, source) == []

    def test_openclaw_migration_parents_2_from_depth_2_is_the_root(self) -> None:
        rel = pathlib.PurePosixPath("tests/skills/test_openclaw_migration.py")
        source = (
            "SCRIPT_PATH = (\n"
            "    Path(__file__).resolve().parents[2]\n"
            '    / "tools" / "skills_guard.py"\n'
            ")\n"
        )
        assert _escaping_hops(rel, source) == []

    def test_hop_count_off_another_path_object_is_out_of_scope(self) -> None:
        """``SCRIPT_PATH.parents[1]`` hangs off SCRIPT_PATH, not ``__file__``."""
        rel = pathlib.PurePosixPath("tests/skills/test_openclaw_migration.py")
        assert _escaping_hops(rel, "assert x == SCRIPT_PATH.parents[1]") == []

    def test_imported_module_dunder_file_is_a_different_class(self) -> None:
        """Anchoring on an imported module's ``__file__`` is depth-stable.

        ``test_gateway_diag.py``, ``test_update_autostash.py`` and
        ``test_terminal_exit_scratch_binding.py`` all do this legitimately.
        """
        rel = pathlib.PurePosixPath("tests/test_gateway_diag.py")
        source = "ROOT = Path(gateway_diag.__file__).resolve().parents[2]"
        assert _escaping_hops(rel, source) == []

    def test_bare_parents_identifier_is_not_a_path(self) -> None:
        """``test_kanban_promote.py:78`` — a local variable named ``parents``."""
        rel = pathlib.PurePosixPath("tests/test_kanban_promote.py")
        assert _escaping_hops(rel, "assert parents[0] in err") == []

    def test_alias_bound_dunder_file_is_a_documented_blind_spot(self) -> None:
        """``tests/stress/test_atypical_scenarios.py`` binds ``__file__`` first.

        It is also explicitly guarded
        (``_THIS.parents[2] if _THIS.parent.name == "stress" else Path.cwd()``),
        so there is nothing to catch here. This test exists to record that the
        alias form is *knowingly* out of scope rather than an oversight — see
        the KNOWN LIMITATION note in the module docstring.
        """
        rel = pathlib.PurePosixPath("tests/stress/test_atypical_scenarios.py")
        source = (
            "_THIS = Path(__file__).resolve()\n"
            'WT = _THIS.parents[2] if _THIS.parent.name == "stress" '
            "else Path.cwd()\n"
        )
        assert _escaping_hops(rel, source) == []


class TestGuardSelfConsistency:
    def test_this_guard_is_itself_layout_independent(self) -> None:
        """``parents[1]`` from ``tests/`` is the root in BOTH layouts.

        If this resolved wrongly, every scan above would be walking the wrong
        directory — and the tree-clean test would pass vacuously.
        """
        assert (_REPO_ROOT / "pyproject.toml").is_file()
        assert (_TESTS_DIR / "conftest.py").is_file()
        assert _root_hop_count(_SELF.relative_to(_REPO_ROOT)) == 1

    def test_self_is_excluded_from_the_tree_scan(self) -> None:
        """The fixtures above embed violating source; a self-scan would flag it."""
        assert _escaping_hops(
            _SELF.relative_to(_REPO_ROOT), _SELF.read_text(encoding="utf-8")
        ), "this file no longer contains sample violations — drop the exclusion"
