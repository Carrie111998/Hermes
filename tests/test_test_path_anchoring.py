"""Guard: no file in this repo may anchor a path by escaping the checkout
root with a hardcoded ``Path(__file__)`` hop count.

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

SCOPE — 2026-08-16: widened from ``tests/`` to the whole checkout. The
gateway is an editable install that imports the working tree, and tests
import those modules *from a worktree*, so a production module resolving
``~/.hermes`` by a hardcoded hop count breaks in exactly the same
layout-dependent way. The audit that motivated the widening found the
non-test tree clean (0 escapes out of 142 anchoring occurrences across 1148
files), so this half starts green and stays that way by assertion rather
than by census.

TWO SPELLINGS, because the widened scope changed which one dominates. Under
``tests/`` the subscript form (``parents[N]``) outnumbers the ``.parent``
chain 196:97. Under non-test code the ratio *inverts* to 21:121 — production
code overwhelmingly writes ``Path(__file__).parent.parent``. A subscript-only
detector would therefore have covered barely 15% of the anchoring surface it
was just pointed at, which is close enough to theatre to be worth the second
pattern. ``N`` chained ``.parent`` hops is ``parents[N-1]``; both forms are
normalised to a subscript index before the rule is applied, so there is one
rule and one boundary, not two.

KNOWN LIMITATION — deliberately narrow to stay false-positive free. The
detector only matches a hop count taken directly off a literal
``Path(__file__)``. It does NOT follow an alias
(``_THIS = Path(__file__).resolve()`` … ``_THIS.parents[2]``, as in
``tests/stress/test_atypical_scenarios.py``), and it does not look at hop
counts off an *imported module's* ``__file__`` (``test_gateway_diag.py``,
``test_update_autostash.py``, ``test_terminal_exit_scratch_binding.py``) —
those are depth-stable and a genuinely different class. Widening the pattern
was measured against the tree and rejected: it flags correct code. See
``test_alias_bound_dunder_file_is_a_documented_blind_spot``. Two further
spellings were swept by hand during the 2026-08-16 widening and left out of
the automation because the tree contains no instance of either: an
``os.path.dirname()`` nest around ``__file__`` (7 occurrences, max depth 3,
all landing at or inside the root) and a ``__file__``-adjacent ``".."``
literal (0 occurrences outside ``tests/``).

THE SCAN MUST PRUNE ``.claude``. From the main checkout, ``.claude/worktrees``
holds a sibling worktree per active session — each a full copy of this tree.
Walking them would (a) cost a multiple of the real tree and (b) measure their
files against the *outer* root, inflating every root-hop limit by 3 and
silently masking real violations. Both checkouts were verified to hold zero
``*.py`` under ``.claude`` outside ``worktrees``, so pruning the whole
directory loses nothing. See ``test_sibling_worktrees_are_pruned``.

FALSIFICATION (do not trust an empty result that was never proven to run).
Against a checkout of ``4b779c40e5`` this detector reports exactly one
violation — ``tests/cron/test_jobflow_researcher_wake_gate.py: parents[3]``
against a root-hop limit of 2. Against current HEAD both halves report zero,
out of 2511 test files (196 subscript + 97 chain occurrences) and 1148
non-test files (21 subscript + 121 chain). Every one of those six numbers has
a floor under it in ``TestScanIsNotVacuous``, so a broken glob, a wrong root
or a regex that stops matching cannot masquerade as a clean tree. The
widening was itself falsified by planting a violating module under
``gateway/`` and confirming this file goes red — not merely by observing that
it stays green.
"""

from __future__ import annotations

import os
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

# The chain spelling of the same thing, which dominates non-test code.
# ``\b`` after ``parent`` is what keeps this from eating ``parents[`` — there
# is no word boundary between ``t`` and ``s``, so the subscript form stays the
# exclusive business of the pattern above and nothing is counted twice.
_PARENT_CHAIN_OFF_DUNDER_FILE = re.compile(
    r"Path\s*\(\s*__file__\s*\)"
    r"(?:\s*\.\s*resolve\s*\(\s*\))?"
    r"((?:\s*\.\s*parent\b)+)"
)

# Counts the hops inside a matched chain. Needed instead of ``str.count``
# because flattening may leave whitespace around each dot (``. parent``).
_ONE_PARENT_HOP = re.compile(r"\.\s*parent\b")

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


def _hop_counts(source: str) -> tuple[list[int], list[int]]:
    """Every anchoring hop in ``source``, as ``(subscript, chain)`` indices.

    Both lists are ``parents[N]``-equivalent indices, so the single boundary
    in ``_root_hop_count`` applies to either without a special case. A chain
    of ``N`` ``.parent`` hops is ``parents[N-1]``: ``Path(p).parent`` is
    ``Path(p).parents[0]``.
    """
    flat = _flatten(source)
    subscript = [int(m.group(1)) for m in _PARENTS_OFF_DUNDER_FILE.finditer(flat)]
    chain = [
        len(_ONE_PARENT_HOP.findall(m.group(1))) - 1
        for m in _PARENT_CHAIN_OFF_DUNDER_FILE.finditer(flat)
    ]
    return subscript, chain


def _escaping_hops(rel: pathlib.PurePath, source: str) -> list[int]:
    """Return every hop count in ``source`` that escapes the checkout root."""
    limit = _root_hop_count(rel)
    subscript, chain = _hop_counts(source)
    return [n for n in subscript + chain if n > limit]


def _read(path: pathlib.Path) -> tuple[pathlib.Path, str | None]:
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path, None


class _Scan:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.files_read = 0
        self.subscript_occurrences = 0
        self.chain_occurrences = 0
        self.unreadable: list[str] = []
        self.violations: list[tuple[str, int, int]] = []

    @property
    def occurrences(self) -> int:
        return self.subscript_occurrences + self.chain_occurrences


# Never descend into these. ``.claude`` is the load-bearing one — see the
# docstring: from the main checkout it holds one full sibling copy of this
# tree per active session.
_PRUNED_DIRS = frozenset({
    ".claude",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
})


def _walk_py(
    start: pathlib.Path, *, skip_top_level: frozenset[str]
) -> list[pathlib.Path]:
    """Collect ``*.py`` under ``start``, pruning as we go.

    Pruning during the walk rather than filtering after it is not a
    micro-optimisation: ``rglob`` would enumerate every sibling worktree in
    full before anything got a chance to reject it.
    """
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(start):
        here = pathlib.Path(dirpath)
        rel_dir = here.relative_to(_REPO_ROOT)
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        if rel_dir == pathlib.Path("."):
            dirnames[:] = [d for d in dirnames if d not in skip_top_level]
        found.extend(here / f for f in filenames if f.endswith(".py"))
    return sorted(found)


def _scan(scope: str) -> _Scan:
    scan = _Scan(scope)
    if scope == "tests":
        paths = _walk_py(_TESTS_DIR, skip_top_level=frozenset())
    else:
        paths = _walk_py(_REPO_ROOT, skip_top_level=frozenset({"tests"}))
    paths = [p for p in paths if p.resolve() != _SELF]
    with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        for path, source in pool.map(_read, paths):
            rel = path.relative_to(_REPO_ROOT)
            if source is None:
                scan.unreadable.append(rel.as_posix())
                continue
            scan.files_read += 1
            subscript, chain = _hop_counts(source)
            scan.subscript_occurrences += len(subscript)
            scan.chain_occurrences += len(chain)
            limit = _root_hop_count(rel)
            scan.violations.extend(
                (rel.as_posix(), n, limit) for n in subscript + chain if n > limit
            )
    return scan


def _scan_tests_tree() -> _Scan:
    return _scan("tests")


@pytest.fixture(scope="module")
def scan() -> _Scan:
    return _scan("tests")


@pytest.fixture(scope="module")
def non_tests_scan() -> _Scan:
    return _scan("non-tests")


def _fail_on(scan: _Scan) -> None:
    """Fail with the remediation text if ``scan`` found anything."""
    if not scan.violations:
        return
    lines = [
        f"  {rel}: parents[{n}] escapes the checkout root "
        f"(root is parents[{limit}] from this depth)"
        for rel, n, limit in scan.violations
    ]
    pytest.fail(
        f"Hardcoded parents[N] escaping the checkout root in {scan.scope} "
        f"code, {len(scan.violations)} place(s):\n"
        + "\n".join(lines)
        + "\n\n(A `.parent.parent...` chain is reported as its equivalent "
        "parents[N-1] index.) This passes from the main checkout and fails "
        "from a git worktree, where the file sits 3 levels deeper. Replace "
        "the fixed hop count with the in-repo `_find_hermes_root()` pattern: "
        "walk `Path(__file__).resolve().parents` for the ancestor that "
        "contains the artifact, and skip when it is absent (in a test: "
        "module-level `pytest.skip(allow_module_level=True)` only if EVERY "
        "test needs it, otherwise `pytest.mark.skipif` on the ones that do). "
        "Do NOT use `git rev-parse --show-toplevel` (returns the worktree) or "
        "`hermes_constants.get_default_hermes_root()` (reads HERMES_HOME, "
        "which tests/conftest.py redirects to a tempdir). Worked examples: "
        "tests/events/subscribers/test_cron_stale_monitor.py, "
        "tests/cron/test_jobflow_researcher_wake_gate.py. Full rationale: "
        f"{_SELF.name} module docstring."
    )


class TestTreeIsClean:
    def test_no_test_escapes_the_checkout_root(self, scan: _Scan) -> None:
        _fail_on(scan)

    def test_no_non_test_module_escapes_the_checkout_root(
        self, non_tests_scan: _Scan
    ) -> None:
        """The gateway is an editable install; tests import it from a worktree.

        So a production module with a hardcoded hop count fails in exactly the
        same layout-dependent way a test does — it just fails somebody else's
        test run instead of its own.
        """
        _fail_on(non_tests_scan)

    def test_every_test_file_was_readable(self, scan: _Scan) -> None:
        assert not scan.unreadable, (
            "unreadable test files were silently skipped by the scan: "
            f"{scan.unreadable}"
        )

    def test_every_non_test_file_was_readable(self, non_tests_scan: _Scan) -> None:
        assert not non_tests_scan.unreadable, (
            "unreadable non-test files were silently skipped by the scan: "
            f"{non_tests_scan.unreadable}"
        )


class TestScanIsNotVacuous:
    """An empty result is only meaningful if the scan provably ran.

    This whole defect class keeps surviving censuses that returned nothing
    because they never actually looked. Every floor sits well below the
    observed 2026-08-16 number, so ordinary churn will not trip it — but a
    broken walk, a wrong root, or a regex that stops matching drops one of
    them to zero. The floors are per scope AND per pattern on purpose: a
    combined total would let the non-test half collapse to nothing while the
    much larger tests half held the sum above the line.
    """

    def test_tests_tree_was_walked(self, scan: _Scan) -> None:
        assert scan.files_read > 500, (  # observed: 2511
            f"only {scan.files_read} test files read from {_TESTS_DIR} — "
            "the tree walk is broken, so a clean result proves nothing"
        )

    def test_non_tests_tree_was_walked(self, non_tests_scan: _Scan) -> None:
        assert non_tests_scan.files_read > 400, (  # observed: 1148
            f"only {non_tests_scan.files_read} non-test files read from "
            f"{_REPO_ROOT} — the tree walk is broken, so a clean result "
            "proves nothing"
        )

    @pytest.mark.parametrize(
        ("scope", "attr", "floor", "observed"),
        [
            # tests/ favours the subscript form; non-test code inverts it.
            ("tests", "subscript_occurrences", 50, 196),
            ("tests", "chain_occurrences", 25, 97),
            ("non-tests", "subscript_occurrences", 8, 21),
            ("non-tests", "chain_occurrences", 40, 121),
        ],
    )
    def test_each_pattern_still_matches_in_each_scope(
        self,
        scan: _Scan,
        non_tests_scan: _Scan,
        scope: str,
        attr: str,
        floor: int,
        observed: int,
    ) -> None:
        target = scan if scope == "tests" else non_tests_scan
        found = getattr(target, attr)
        assert found > floor, (
            f"only {found} `{attr}` matched across {scope} (floor {floor}, "
            f"{observed} observed on 2026-08-16) — that pattern has stopped "
            "matching, so a clean result proves nothing"
        )

    def test_the_two_scopes_are_disjoint_and_both_nonempty(
        self, scan: _Scan, non_tests_scan: _Scan
    ) -> None:
        """Neither half may silently swallow the other's files."""
        assert scan.files_read and non_tests_scan.files_read
        assert scan.scope != non_tests_scan.scope


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

    def test_flags_an_escaping_parent_chain_in_a_production_module(self) -> None:
        """The non-test spelling of the same defect.

        ``gateway/x.py`` is depth 1, so ``.parent.parent`` is the checkout
        root and a third hop leaves it — which from a worktree lands in
        ``.claude/worktrees`` rather than ``~/.hermes``.
        """
        rel = pathlib.PurePosixPath("gateway/x.py")
        source = 'ROOT = Path(__file__).resolve().parent.parent.parent / "profiles"'
        assert _escaping_hops(rel, source) == [2]

    @pytest.mark.parametrize(
        ("hops", "escapes"),
        [(1, False), (2, False), (3, True), (4, True)],
    )
    def test_chain_of_n_parents_is_parents_n_minus_one(
        self, hops: int, escapes: bool
    ) -> None:
        """``Path(p).parent`` is ``Path(p).parents[0]`` — the off-by-one that
        makes the chain form easy to get wrong by hand."""
        rel = pathlib.PurePosixPath("hermes_cli/x.py")  # depth 1, root = parents[1]
        source = "ROOT = Path(__file__)" + ".parent" * hops
        assert _escaping_hops(rel, source) == ([hops - 1] if escapes else [])

    def test_flags_a_whitespace_split_parent_chain(self) -> None:
        rel = pathlib.PurePosixPath("gateway/x.py")
        split = (
            "ROOT = (\n    Path(__file__)\n    .parent\n    .parent\n    .parent\n)\n"
        )
        assert _escaping_hops(rel, split) == [2]

    def test_resolve_after_the_chain_still_matches(self) -> None:
        """``hermes_cli/main.py:510`` puts ``.resolve()`` last, not first."""
        rel = pathlib.PurePosixPath("hermes_cli/main.py")
        assert _escaping_hops(rel, "R = Path(__file__).parent.parent.resolve()") == []
        assert _escaping_hops(
            rel, "R = Path(__file__).parent.parent.parent.resolve()"
        ) == [2]


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

    def test_chain_pattern_does_not_double_count_the_subscript_form(self) -> None:
        """``\\b`` after ``parent`` is what keeps ``parents[`` out of the chain.

        Without it, ``Path(__file__).parents[3]`` would be counted once as a
        subscript and again as a one-hop chain, and every occurrence floor
        would be inflated by the wrong pattern.
        """
        subscript, chain = _hop_counts("R = Path(__file__).resolve().parents[3]")
        assert subscript == [3]
        assert chain == []

    def test_hermes_cli_main_app_bundle_hop_is_off_a_local_variable(self) -> None:
        """``hermes_cli/main.py:5766`` — ``exe.parents[2]`` walks a macOS app
        bundle, not the checkout. Depth 1, so a naive scan would call it an
        escape; it never touches ``__file__``."""
        rel = pathlib.PurePosixPath("hermes_cli/main.py")
        assert _escaping_hops(rel, "app = exe.parents[2]") == []

    def test_deep_skill_script_reaching_its_own_root_is_correct(self) -> None:
        """``skills/creative/comfyui/scripts/x.py`` is depth 4, so ``parents[4]``
        IS the checkout root — a large hop count is not by itself a defect."""
        rel = pathlib.PurePosixPath("skills/creative/comfyui/scripts/check_deps.py")
        assert _escaping_hops(rel, "R = Path(__file__).resolve().parents[4]") == []
        assert _escaping_hops(rel, "R = Path(__file__).resolve().parents[5]") == [5]

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


class TestScanScoping:
    """The widened walk has to stay inside THIS checkout."""

    def test_sibling_worktrees_are_pruned(self, non_tests_scan: _Scan) -> None:
        """From the main checkout, ``.claude/worktrees`` holds a full sibling
        copy of this tree per active session.

        Scanning them would measure their files against the OUTER root — every
        root-hop limit inflated by 3 — which turns the boundary into nonsense
        in the permissive direction and masks real violations. It also costs a
        multiple of the real tree. Both checkouts were verified to contain no
        ``*.py`` under ``.claude`` outside ``worktrees``, so nothing is lost.
        """
        assert ".claude" in _PRUNED_DIRS
        stray = [
            rel
            for rel, _n, _limit in non_tests_scan.violations
            if ".claude" in pathlib.PurePosixPath(rel).parts
        ]
        assert not stray, f"scan escaped into .claude: {stray}"

    def test_walk_reaches_the_real_production_packages(
        self, non_tests_scan: _Scan
    ) -> None:
        """A floor on the file count alone would pass on the wrong subtree."""
        assert non_tests_scan.files_read > 400
        for pkg in ("gateway", "hermes_cli", "events", "cron", "tools"):
            assert (_REPO_ROOT / pkg).is_dir(), f"{pkg}/ vanished — fix the scope"

    def test_tests_dir_is_excluded_from_the_non_tests_scan(self) -> None:
        paths = _walk_py(_REPO_ROOT, skip_top_level=frozenset({"tests"}))
        leaked = [
            p.relative_to(_REPO_ROOT).as_posix()
            for p in paths
            if p.relative_to(_REPO_ROOT).parts[0] == "tests"
        ]
        assert not leaked, f"tests/ leaked into the non-test scan: {leaked[:5]}"

    def test_pruned_dirs_are_not_walked(self) -> None:
        paths = _walk_py(_REPO_ROOT, skip_top_level=frozenset({"tests"}))
        offenders = [
            p.relative_to(_REPO_ROOT).as_posix()
            for p in paths
            if _PRUNED_DIRS.intersection(p.relative_to(_REPO_ROOT).parts)
        ]
        assert not offenders, f"walk entered a pruned dir: {offenders[:5]}"


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
