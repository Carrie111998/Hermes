# Link-Aware Memory Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make deletion of a `~/.claude` agent-memory file link-aware, so a consolidation pass cannot silently orphan inbound wikilinks the way auto-snapshot `486258b` did to six links on 2026-08-14.

**Architecture:** One stdlib-only module (`hooks/memory_links.py`) builds a cross-root reverse-link index and answers "who points at these files?". Three consumers use it: a `PreToolUse` gate that blocks an orphaning deletion, a `PostToolUse` detector that catches deletion routes the gate's patterns miss, and a report inside the existing `SessionEnd` snapshot hook that covers deletions made by other agents between sessions.

**Tech Stack:** Python 3.11 stdlib only (no third-party imports). pytest 9.0.2 for tests. PowerShell 5.1 for the acceptance lint.

**Spec:** `~/.hermes/agent-src/docs/superpowers/specs/2026-08-17-link-aware-memory-deletion-design.md`

## Global Constraints

- **Stdlib only.** Hooks run under `C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe`, which has **no pytest and no third-party packages**. Any `import` outside the standard library breaks every hook.
- **Python 3.11-compatible syntax.** Tests run on 3.12; the hooks run on 3.11. Do not use 3.12-only syntax.
- **Test interpreter:** `C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe` (3.12.13, pytest 9.0.2). Do **not** use bare `python` — it resolves to the WindowsApps MSIX shim, which has documented file-visibility virtualization on this box.
- **Hook contracts, verbatim from the existing hooks:** `PreToolUse` — exit **2** blocks and shows stderr to the model; any other exit code allows. `PostToolUse` — `print(json.dumps({"systemMessage": msg}))`, exit **0** always.
- **Fail open everywhere except the deliberate block.** Malformed event, unreadable root, missing git, budget exceeded → exit 0 and log.
- **Never block the SessionEnd snapshot.** `auto-commit-claude-memory.py`'s contract is *"exit 0 ALWAYS. Never block session end."*
- **Escape marker:** `memory-orphan: approved` — mirrors the existing `cross-session-kill: approved` idiom.
- **Budget env var:** `CLAUDE_MEMORY_LINKS_BUDGET_SEC`, default `6`.
- **`~/.claude` commit rules:** stage explicit paths, then commit **bare**. Never `git add -A`, never `git commit -- <paths>`, never `--no-verify`. (`~/.claude` has no pre-commit hooks; the rule still stands.)
- **Do not normalize the linter's non-DEAD categories.** `CONVENTION`/`RENAMED`/`SUFFIX`/`PROSE`/`CROSSROOT` are deliberate (Diego, 2026-08-10).
- **Acceptance baseline** (measured 2026-08-17, must be unchanged at the end): DEAD 0; NEARMISS 2/2, CROSSROOT 15/14, RENAMED 31/17, SUFFIX 26/21, CONVENTION 75/50, GBRAIN 7/7, PATH 2/2, PROSE 65/30.

---

## File Structure

| File | Responsibility |
|---|---|
| `~/.claude/hooks/memory_links.py` | **Create.** The only unit that knows what a link is: normalization, extraction, identities, root derivation, budget, reverse index, `referrers_of`. |
| `~/.claude/hooks/test_memory_links.py` | **Create.** Unit tests for the module, using temp-dir fixture roots. |
| `~/.claude/hooks/block-memory-file-orphan.py` | **Create.** Layer 1 — `PreToolUse` gate. |
| `~/.claude/hooks/test_block_memory_file_orphan.py` | **Create.** Layer 1 tests. |
| `~/.claude/hooks/detect-memory-file-orphan.py` | **Create.** Layer 2 — `PostToolUse` rolling-inventory detector. |
| `~/.claude/hooks/test_detect_memory_file_orphan.py` | **Create.** Layer 2 tests. |
| `~/.claude/hooks/auto-commit-claude-memory.py` | **Modify.** Layer 3 — orphan report after the commit. |
| `~/.claude/hooks/test_auto_commit_orphan_report.py` | **Create.** Layer 3 tests. |
| `~/.claude/hooks/memory-index-size-guard.py` | **Modify.** Append the link-safety rule to the message that summons the consolidation pass. |
| `~/.claude/settings.json` | **Modify.** Register Layer 1 and Layer 2. |

---

### Task 1: The link module — extraction, normalization, identities

**Files:**
- Create: `C:\Users\diego\.claude\hooks\memory_links.py`
- Test: `C:\Users\diego\.claude\hooks\test_memory_links.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize(target: str) -> str`; `root_label(root: Path) -> str`; `iter_links(lines: Iterable[str]) -> Iterator[tuple[int, str, str]]` yielding `(lineno, target, kind)` with `kind` in `{"wikilink", "mdlink"}`; `identities_from_text(basename: str, text: str) -> set[str]`; `identities_for_file(path: Path) -> set[str]`; `Budget(seconds: float)` with `.check()` raising `BudgetExceeded`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_memory_links.py`:

```python
"""Tests for memory_links.py (shared cross-root link index).

Guards the 2026-08-14 orphaning: auto-snapshot 486258b deleted 118 memory files
and orphaned six inbound wikilinks, found only weeks later by a hand-run linter.

Run:  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_links.py -q
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent / "memory_links.py"


def _load():
    spec = importlib.util.spec_from_file_location("memory_links", MODULE)
    assert spec and spec.loader, f"cannot load {MODULE}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ml = _load()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("reference_ps_eq_coerces", "reference_ps_eq_coerces"),
        ("reference-ps-eq-coerces", "reference_ps_eq_coerces"),
        ("reference ps eq coerces", "reference_ps_eq_coerces"),
        ("reference_ps_eq_coerces.md", "reference_ps_eq_coerces"),
        ("Reference_PS_Eq_Coerces", "reference_ps_eq_coerces"),
        ("  spaced_out  ", "spaced_out"),
    ],
)
def test_normalize_collapses_separators_suffix_and_case(raw, expected):
    assert ml.normalize(raw) == expected


def test_iter_links_finds_wikilinks_and_mdlinks():
    lines = [
        "See [[alpha]] and [[beta-two]].",
        "Also [label](gamma.md) here.",
    ]
    found = [(t, k) for _, t, k in ml.iter_links(lines)]
    assert ("alpha", "wikilink") in found
    assert ("beta-two", "wikilink") in found
    assert ("gamma", "mdlink") in found


def test_iter_links_reports_one_indexed_line_numbers():
    lines = ["no link here", "now [[target]]"]
    assert [n for n, _, _ in ml.iter_links(lines)] == [2]


def test_iter_links_skips_fenced_blocks():
    """A POSIX bracket class inside a bash fence is not a link.

    The PowerShell linter shipped this bug and reported four false DEAD links
    for `sed 's/^[[:space:]]*//'` in a ```bash block.
    """
    lines = [
        "real [[outside_fence]]",
        "```bash",
        "sed 's/^[[:space:]]*//'",
        "[[inside_fence]]",
        "```",
        "real [[after_fence]]",
    ]
    targets = [t for _, t, _ in ml.iter_links(lines)]
    assert targets == ["outside_fence", "after_fence"]


def test_iter_links_skips_tilde_fences_and_indented_fences():
    lines = ["~~~", "[[in_tilde]]", "~~~", "   ```", "[[in_indented]]", "   ```", "[[free]]"]
    assert [t for _, t, _ in ml.iter_links(lines)] == ["free"]


def test_iter_links_skips_inline_backtick_spans():
    lines = ["prose `[[not_a_link]]` and [[a_real_link]]"]
    assert [t for _, t, _ in ml.iter_links(lines)] == ["a_real_link"]


def test_iter_links_ignores_urls_and_absolute_paths_in_mdlinks():
    lines = [
        "[x](https://example.com/page.md)",
        r"[y](C:\Users\diego\notes\thing.md)",
        "[z](local.md)",
    ]
    assert [t for _, t, _ in ml.iter_links(lines)] == ["local"]


def test_identities_include_basename_and_frontmatter_name():
    text = "---\nname: kebab-slug-form\ndescription: x\n---\n\nbody\n"
    ids = ml.identities_from_text("underscore_file_name", text)
    assert ml.normalize("underscore_file_name") in ids
    assert ml.normalize("kebab-slug-form") in ids


def test_identities_ignore_a_name_line_far_below_frontmatter():
    text = "\n".join(["filler"] * 20 + ["name: sneaky-late-name"])
    ids = ml.identities_from_text("real_file", text)
    assert ids == {ml.normalize("real_file")}


def test_identities_for_file_reads_disk(tmp_path):
    p = tmp_path / "some_file.md"
    p.write_text("---\nname: other-identity\n---\n", encoding="utf-8")
    ids = ml.identities_for_file(p)
    assert ml.normalize("some_file") in ids
    assert ml.normalize("other-identity") in ids


def test_identities_for_file_on_missing_file_returns_basename_only(tmp_path):
    ids = ml.identities_for_file(tmp_path / "gone.md")
    assert ids == {ml.normalize("gone")}


def test_root_label_strips_the_profile_prefix():
    assert ml.root_label(Path(r"C:\x\projects\C--Users-diego--hermes\memory")) == "hermes"
    assert ml.root_label(Path(r"C:\x\projects\C--Users-diego\memory")) == "~"


def test_budget_raises_once_expired():
    b = ml.Budget(seconds=0.0)
    time.sleep(0.01)
    with pytest.raises(ml.BudgetExceeded):
        b.check()


def test_budget_does_not_raise_while_time_remains():
    ml.Budget(seconds=30).check()  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_links.py -q
```

Expected: collection error — `cannot load .../memory_links.py` (the file does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `C:\Users\diego\.claude\hooks\memory_links.py`:

```python
#!/usr/bin/env python3
"""Cross-root agent-memory link index.

Shared by the three orphan guards: block-memory-file-orphan.py (PreToolUse),
detect-memory-file-orphan.py (PostToolUse), and the report inside
auto-commit-claude-memory.py (SessionEnd).

Why this exists
---------------
2026-08-14: auto-snapshot 486258b removed 118 files from the
hermes-agent-src memory root. The merge was content-correct, but six inbound
wikilinks were orphaned and stayed broken until a hand-run lint found them on
2026-08-17. Nothing in the write path was link-aware.

This module answers one question -- "who points at these files?" -- across
EVERY versioned memory root, which is the part a consolidation pass working
inside one root structurally cannot do.

Resolution semantics deliberately mirror memory-link-lint.ps1: a memory's
identity is its FILENAME *and* its frontmatter `name:` slug (links are written
both ways), targets normalise by collapsing [\\s\\-_]+ and dropping a trailing
".md", and fenced blocks / inline backtick spans are code rather than links.
Divergence from the linter would mean the gate blocks on something the linter
calls fine, or vice versa.

CONSTRAINT: stdlib only. These hooks run under the uv python 3.11.9, which has
no third-party packages installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

HOME = Path(os.path.expanduser("~"))
REPO = HOME / ".claude"

WIKI_RE = re.compile(r"\[\[([^\]\[]+)\]\]")
MDLINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$")
SEP_RE = re.compile(r"[\s\-_]+")
URL_RE = re.compile(r"^[a-z]+://", re.IGNORECASE)
ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})")

# Frontmatter must appear at the top of the file. Scanning further would let a
# stray "name:" line deep in prose masquerade as an identity.
FRONTMATTER_SCAN_LINES = 12

DEFAULT_BUDGET_SEC = 6.0


def _default_budget_seconds() -> float:
    raw = os.environ.get("CLAUDE_MEMORY_LINKS_BUDGET_SEC", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SEC
    return value if value > 0 else DEFAULT_BUDGET_SEC


class BudgetExceeded(Exception):
    """The wall-clock budget ran out mid-scan. Callers MUST fail open.

    This box has documented saturation storms in which a seconds-scale probe
    takes minutes. Without an internal deadline the gate becomes a hang under
    exactly the pressure that makes people kill things.
    """


class Budget:
    def __init__(self, seconds: float | None = None) -> None:
        if seconds is None:
            seconds = _default_budget_seconds()
        self._deadline = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._deadline:
            raise BudgetExceeded("memory_links budget exhausted")


class Referrer(NamedTuple):
    root: str   # short label: "hermes", "~", ...
    file: str   # basename, e.g. "feedback_x.md"
    line: int   # 1-indexed
    kind: str   # "wikilink" | "mdlink"


def normalize(target: str) -> str:
    """Collapse a link target onto its comparison key."""
    t = target.strip()
    if t[-3:].lower() == ".md":
        t = t[:-3]
    return SEP_RE.sub("_", t).lower()


def root_label(root: Path) -> str:
    """'.../projects/C--Users-diego--hermes/memory' -> 'hermes'; bare root -> '~'."""
    leaf = root.parent.name
    label = re.sub(r"^C--Users-diego", "", leaf).lstrip("-")
    return label or "~"


def _in_code_span(line: str, offset: int) -> bool:
    """True if `offset` sits inside an inline backtick span on this line."""
    return line.count("`", 0, offset) % 2 == 1


def iter_links(lines: Iterable[str]) -> Iterator[tuple[int, str, str]]:
    """Yield (lineno, target, kind) for every real link. Lines are 1-indexed.

    Fenced blocks are source, not prose: the PowerShell linter reported four
    false DEAD links for a POSIX `[[:space:]]` class inside a ```bash block
    before it learned to track fences across lines. Fence state is per-call, so
    an unclosed fence cannot leak into the next document.
    """
    in_fence = False
    for i, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in WIKI_RE.finditer(line):
            target = m.group(1).strip()
            if not target or _in_code_span(line, m.start()):
                continue
            yield i, target, "wikilink"
        for m in MDLINK_RE.finditer(line):
            href = m.group(1).strip()
            if URL_RE.match(href) or ABS_RE.match(href):
                continue
            if _in_code_span(line, m.start()):
                continue
            yield i, os.path.splitext(os.path.basename(href))[0], "mdlink"


def identities_from_text(basename: str, text: str) -> set[str]:
    """Every normalized name this file can be linked by."""
    ids = {normalize(basename)}
    for line in text.splitlines()[:FRONTMATTER_SCAN_LINES]:
        m = NAME_RE.match(line)
        if m:
            ids.add(normalize(m.group(1).strip().strip('"').strip("'")))
            break
    return ids


def identities_for_file(path: Path) -> set[str]:
    """Identities read from disk. A missing file degrades to basename only."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return identities_from_text(path.stem, text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_links.py -q
```

Expected: `16 passed`.

- [ ] **Step 5: Verify only your paths are staged, then commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory_links.py hooks/test_memory_links.py
git -C "C:/Users/diego/.claude" diff --cached --name-only
```

Expected output — exactly these two lines and nothing else:

```
hooks/memory_links.py
hooks/test_memory_links.py
```

If any other path appears, a sibling session staged it: unstage with `git -C "C:/Users/diego/.claude" restore --staged -- <that path>` and re-check before committing.

```bash
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): memory_links extraction, normalization, identities"
```

---

### Task 2: Root derivation and the reverse index

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory_links.py` (append)
- Test: `C:\Users\diego\.claude\hooks\test_memory_links.py` (append)

**Interfaces:**
- Consumes: Task 1's `normalize`, `root_label`, `iter_links`, `identities_for_file`, `Budget`, `Referrer`.
- Produces: `derive_roots(repo: Path = REPO) -> list[Path]`; `Index` with `.lookup(identity: str) -> list[Referrer]` and `.files_scanned: int`; `build_index(roots: list[Path] | None = None, budget: Budget | None = None) -> Index`; `referrers_of(paths: Iterable[Path | str], roots: list[Path] | None = None, budget: Budget | None = None) -> dict[str, list[Referrer]]`.

- [ ] **Step 1: Write the failing test**

Append to `C:\Users\diego\.claude\hooks\test_memory_links.py`:

```python
# --- Task 2: roots and the reverse index -----------------------------------


def _mkroot(tmp_path, root_name, files):
    """Build a fake projects/<root_name>/memory/ dir. files: {name: text}."""
    root = tmp_path / "projects" / root_name / "memory"
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_build_index_maps_targets_to_referrers(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego--hermes", {
        "referrer_one.md": "points at [[target_file]]\n",
        "referrer_two.md": "also [[target-file]] here\n",
        "target_file.md": "the target\n",
    })
    idx = ml.build_index(roots=[root])
    hits = idx.lookup(ml.normalize("target_file"))
    assert {h.file for h in hits} == {"referrer_one.md", "referrer_two.md"}
    assert idx.files_scanned == 3


def test_referrers_of_finds_a_same_root_referrer(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego--hermes", {
        "referrer.md": "see [[doomed]]\n",
        "doomed.md": "content\n",
    })
    out = ml.referrers_of([root / "doomed.md"], roots=[root])
    assert list(out) == [str(root / "doomed.md")]
    assert [(r.file, r.line) for r in out[str(root / "doomed.md")]] == [("referrer.md", 1)]


def test_referrers_of_finds_a_CROSS_ROOT_referrer(tmp_path):
    """The case a consolidation pass structurally cannot see.

    Five of the six links orphaned by 486258b were same-root; the sixth was
    cross-root and is the one that needed inlining.
    """
    doomed_root = _mkroot(tmp_path, "C--Users-diego--hermes-agent-src", {
        "doomed.md": "content\n",
    })
    other_root = _mkroot(tmp_path, "C--Users-diego--hermes", {
        "far_referrer.md": "see [[doomed]]\n",
    })
    out = ml.referrers_of([doomed_root / "doomed.md"], roots=[doomed_root, other_root])
    hits = out[str(doomed_root / "doomed.md")]
    assert [(h.root, h.file) for h in hits] == [("hermes", "far_referrer.md")]


def test_referrers_of_matches_via_the_frontmatter_slug(tmp_path):
    """Links are written to the `name:` slug as often as to the filename."""
    root = _mkroot(tmp_path, "C--Users-diego", {
        "underscore_name.md": "---\nname: kebab-slug\n---\nbody\n",
        "referrer.md": "see [[kebab-slug]]\n",
    })
    out = ml.referrers_of([root / "underscore_name.md"], roots=[root])
    assert [r.file for r in out[str(root / "underscore_name.md")]] == ["referrer.md"]


def test_referrers_of_excludes_referrers_in_the_same_delete_set(tmp_path):
    """Mutually-referencing files deleted together are not an orphan risk."""
    root = _mkroot(tmp_path, "C--Users-diego", {
        "a.md": "see [[b]]\n",
        "b.md": "see [[a]]\n",
    })
    out = ml.referrers_of([root / "a.md", root / "b.md"], roots=[root])
    assert out == {}


def test_referrers_of_returns_empty_when_nothing_points_at_the_file(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego", {
        "lonely.md": "content\n",
        "unrelated.md": "see [[something_else]]\n",
    })
    assert ml.referrers_of([root / "lonely.md"], roots=[root]) == {}


def test_referrers_of_ignores_a_mention_inside_a_code_fence(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego", {
        "doomed.md": "content\n",
        "documenter.md": "```\n[[doomed]]\n```\n",
    })
    assert ml.referrers_of([root / "doomed.md"], roots=[root]) == {}


def test_referrers_of_deduplicates_when_basename_and_slug_both_match(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego", {
        "dup_target.md": "---\nname: dup-target\n---\n",
        "referrer.md": "[[dup_target]]\n",
    })
    out = ml.referrers_of([root / "dup_target.md"], roots=[root])
    assert len(out[str(root / "dup_target.md")]) == 1


def test_build_index_propagates_budget_exceeded(tmp_path):
    root = _mkroot(tmp_path, "C--Users-diego", {"a.md": "[[x]]\n"})
    with pytest.raises(ml.BudgetExceeded):
        ml.build_index(roots=[root], budget=ml.Budget(seconds=-1))


def test_build_index_skips_an_unreadable_root(tmp_path):
    good = _mkroot(tmp_path, "C--Users-diego", {"a.md": "[[x]]\n"})
    missing = tmp_path / "projects" / "C--Users-diego-nope" / "memory"
    idx = ml.build_index(roots=[missing, good])
    assert idx.files_scanned == 1


def test_derive_roots_returns_memory_dirs_for_the_live_repo():
    roots = ml.derive_roots()
    assert roots, "expected at least one derived memory root"
    assert all(r.name == "memory" for r in roots)
    assert all(r.is_dir() for r in roots)


def test_derive_roots_excludes_the_gitignored_roots():
    """The four deliberately-unversioned roots must not be linted."""
    labels = {ml.root_label(r) for r in ml.derive_roots()}
    assert "OneDrive-Documents-BolaoWC2022" not in labels
    assert "-openclaw" not in labels
    assert "hermes" in labels
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_links.py -q
```

Expected: the 16 Task 1 tests pass; the 12 new ones fail with `AttributeError: module 'memory_links' has no attribute 'build_index'` (and `derive_roots`, `referrers_of`).

- [ ] **Step 3: Write the implementation**

Append to `C:\Users\diego\.claude\hooks\memory_links.py`:

```python
# --- roots ------------------------------------------------------------------

# Git vars that hijack even an explicit `git -C <repo>`: they name the dir or
# index directly, while -C only changes cwd. Cleared for the child only.
_GIT_ENV_HIJACKERS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def derive_roots(repo: Path = REPO, timeout: float = 10.0) -> list[Path]:
    """Every projects/<root>/memory the ~/.claude repo actually versions.

    Derived, never hardcoded: the PowerShell linter's history records a pinned
    two-root list that was blind to eight of the ten live roots, including the
    one every cwd=~ session reads and writes.

    `--no-index` is mandatory. Without it git silently reports already-tracked
    paths as not-ignored, which reads identically to a genuinely trackable path;
    with it the answer comes from the .gitignore negation blocks alone, so an
    allowlisted-but-not-yet-committed root is covered on the day its block lands.

    On any git failure this returns ALL candidate roots. Over-scanning costs a
    few hundred ms; under-scanning silently misses a referrer, which is the bug
    this module exists to prevent.
    """
    projects = repo / "projects"
    if not projects.is_dir():
        return []
    try:
        candidates = sorted(
            d.name for d in projects.iterdir() if (d / "memory").is_dir()
        )
    except OSError:
        return []
    if not candidates:
        return []

    def _all() -> list[Path]:
        return [projects / n / "memory" for n in candidates]

    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_HIJACKERS}
    probe = [f"projects/{n}/memory/MEMORY.md" for n in candidates]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--no-index", "--", *probe],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return _all()
    # rc 1 means "none are ignored" -- a valid answer, not a failure.
    if proc.returncode not in (0, 1):
        return _all()

    ignored = set()
    for line in proc.stdout.splitlines():
        p = line.replace("\\", "/").strip().strip('"')
        m = re.match(r"^projects/([^/]+)/memory/", p)
        if m:
            ignored.add(m.group(1))
    keep = [n for n in candidates if n not in ignored]
    return [projects / n / "memory" for n in keep] if keep else _all()


# --- index ------------------------------------------------------------------


class Index:
    """Reverse map: normalized target -> the referrers pointing at it."""

    def __init__(self) -> None:
        self._rev: dict[str, list[Referrer]] = {}
        self.files_scanned = 0

    def add_document(self, label: str, filename: str, lines: Iterable[str]) -> None:
        for lineno, target, kind in iter_links(lines):
            self._rev.setdefault(normalize(target), []).append(
                Referrer(label, filename, lineno, kind)
            )
        self.files_scanned += 1

    def lookup(self, identity: str) -> list[Referrer]:
        return list(self._rev.get(identity, ()))


def build_index(
    roots: list[Path] | None = None, budget: Budget | None = None
) -> Index:
    """Scan every root once. Raises BudgetExceeded if the clock runs out."""
    roots = derive_roots() if roots is None else roots
    budget = Budget() if budget is None else budget
    idx = Index()
    for root in roots:
        label = root_label(root)
        try:
            entries = sorted(root.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            budget.check()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            idx.add_document(label, path.name, text.splitlines())
    return idx


def referrers_of(
    paths: Iterable[Path | str],
    roots: list[Path] | None = None,
    budget: Budget | None = None,
) -> dict[str, list[Referrer]]:
    """Who still points at each of `paths`, once `paths` are gone?

    Referrers that are themselves in `paths` are excluded -- mutually
    referencing files deleted together orphan nothing.

    Returns only paths WITH at least one live referrer, so an empty dict means
    "safe to delete". Raises BudgetExceeded; callers must fail open.
    """
    targets = [Path(p) for p in paths]
    deleting = {(root_label(p.parent), p.name) for p in targets}
    idx = build_index(roots=roots, budget=budget)

    out: dict[str, list[Referrer]] = {}
    for p in targets:
        hits: list[Referrer] = []
        seen: set[tuple[str, str, int]] = set()
        for identity in sorted(identities_for_file(p)):
            for r in idx.lookup(identity):
                if (r.root, r.file) in deleting:
                    continue
                key = (r.root, r.file, r.line)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(r)
        if hits:
            out[str(p)] = sorted(hits)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_links.py -q
```

Expected: `28 passed`.

- [ ] **Step 5: Sanity-check cost against the live corpus**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -c "import importlib.util,time; s=importlib.util.spec_from_file_location('ml',r'C:\Users\diego\.claude\hooks\memory_links.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); t=time.time(); i=m.build_index(); print('files',i.files_scanned,'secs',round(time.time()-t,2))"
```

Expected: roughly `files 1150-1200 secs 1.5-3.0`. If it exceeds the 6s default budget, stop and reduce cost before continuing — a gate that trips its own deadline on a healthy box is useless.

- [ ] **Step 6: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory_links.py hooks/test_memory_links.py
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): cross-root reverse-link index and referrers_of"
```

---

### Task 3: Layer 1 — the PreToolUse gate

**Files:**
- Create: `C:\Users\diego\.claude\hooks\block-memory-file-orphan.py`
- Test: `C:\Users\diego\.claude\hooks\test_block_memory_file_orphan.py`
- Modify: `C:\Users\diego\.claude\settings.json`

**Interfaces:**
- Consumes: `memory_links.referrers_of`, `memory_links.derive_roots`, `memory_links.BudgetExceeded`.
- Produces: `RULES: list[tuple[str, Pattern]]`; `ESCAPE_MARKER: str`; `extract_memory_paths(command: str, roots: list[Path]) -> list[Path]`; `verdict(tool_name: str, command: str, roots: list[Path] | None = None) -> str | None` returning a block reason or `None`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_block_memory_file_orphan.py`:

```python
"""Tests for block-memory-file-orphan.py (PreToolUse gate).

Guards the 2026-08-14 orphaning (auto-snapshot 486258b, 118 files deleted, six
inbound wikilinks orphaned).

Run:  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_block_memory_file_orphan.py -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "block-memory-file-orphan.py"


def _load():
    spec = importlib.util.spec_from_file_location("block_memory_file_orphan", HOOK)
    assert spec and spec.loader, f"cannot load {HOOK}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


@pytest.fixture
def corpus(tmp_path):
    """A doomed file with one same-root and one cross-root referrer."""
    src = tmp_path / "projects" / "C--Users-diego--hermes-agent-src" / "memory"
    src.mkdir(parents=True)
    (src / "doomed.md").write_text("content\n", encoding="utf-8")
    (src / "near_referrer.md").write_text("see [[doomed]]\n", encoding="utf-8")
    other = tmp_path / "projects" / "C--Users-diego--hermes" / "memory"
    other.mkdir(parents=True)
    (other / "far_referrer.md").write_text("see [[doomed]]\n", encoding="utf-8")
    (src / "unlinked.md").write_text("nobody points here\n", encoding="utf-8")
    return {"roots": [src, other], "src": src, "other": other}


def test_blocks_remove_item_on_a_referenced_file(corpus):
    cmd = f'Remove-Item "{corpus["src"] / "doomed.md"}"'
    reason = hook.verdict("PowerShell", cmd, roots=corpus["roots"])
    assert reason is not None
    assert "near_referrer.md" in reason
    assert "far_referrer.md" in reason


def test_block_message_names_the_root_of_each_referrer(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    reason = hook.verdict("Bash", cmd, roots=corpus["roots"])
    assert "hermes-agent-src" in reason
    assert "hermes|" in reason or "hermes |" in reason


def test_block_message_states_the_inline_by_default_rule(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    reason = hook.verdict("Bash", cmd, roots=corpus["roots"])
    assert "inline" in reason.lower()


def test_block_message_advertises_the_escape_marker(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.ESCAPE_MARKER in hook.verdict("Bash", cmd, roots=corpus["roots"])


def test_allows_deletion_of_a_file_nothing_points_at(corpus):
    cmd = f'rm {corpus["src"] / "unlinked.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_allows_when_the_referrer_is_deleted_by_the_same_command(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"} {corpus["src"] / "near_referrer.md"} {corpus["other"] / "far_referrer.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_escape_marker_allows_a_blocked_deletion(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"}  # {hook.ESCAPE_MARKER}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_is_self_clearing_once_the_referrers_are_repointed(corpus):
    """The whole escape-hatch design: fix, retry, it passes."""
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is not None
    (corpus["src"] / "near_referrer.md").write_text("see [[survivor]]\n", encoding="utf-8")
    (corpus["other"] / "far_referrer.md").write_text("fact inlined, no link\n", encoding="utf-8")
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_ignores_non_shell_tools(corpus):
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Edit", cmd, roots=corpus["roots"]) is None


def test_ignores_a_command_with_no_deletion_verb(corpus):
    cmd = f'cat {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_ignores_a_deletion_outside_any_memory_root(corpus, tmp_path):
    other = tmp_path / "scratch" / "notes.md"
    other.parent.mkdir(parents=True)
    other.write_text("x\n", encoding="utf-8")
    assert hook.verdict("Bash", f"rm {other}", roots=corpus["roots"]) is None


def test_a_rename_within_one_root_is_not_blocked(corpus):
    """Two memory paths on a move = a rename. Layer 2 reports it instead."""
    cmd = f'Move-Item "{corpus["src"] / "doomed.md"}" "{corpus["src"] / "renamed.md"}"'
    assert hook.verdict("PowerShell", cmd, roots=corpus["roots"]) is None


def test_a_move_out_of_a_memory_root_is_blocked(corpus):
    cmd = f'Move-Item "{corpus["src"] / "doomed.md"}" "C:\\\\tmp\\\\doomed.md"'
    assert hook.verdict("PowerShell", cmd, roots=corpus["roots"]) is not None


def test_empty_rules_table_blocks_nothing(corpus, monkeypatch):
    """Proves the RULES table drives the verdict rather than passing vacuously."""
    monkeypatch.setattr(hook, "RULES", [])
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_fails_open_when_the_index_budget_is_exhausted(corpus, monkeypatch):
    def boom(*a, **k):
        raise hook.ml.BudgetExceeded("simulated")

    monkeypatch.setattr(hook.ml, "referrers_of", boom)
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


def test_fails_open_on_an_unexpected_error(corpus, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(hook.ml, "referrers_of", boom)
    cmd = f'rm {corpus["src"] / "doomed.md"}'
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None


ORDINARY_COMMANDS = [
    "git status",
    "python -m pytest tests/ -q",
    "ls -la",
    "Get-ChildItem C:\\Users\\diego",
    "docker version",
    "rm -rf node_modules",
    "Remove-Item C:\\temp\\build.log",
    "git rm --cached secrets.env",
]


@pytest.mark.parametrize("cmd", ORDINARY_COMMANDS)
def test_ordinary_commands_are_never_blocked(cmd, corpus):
    assert hook.verdict("Bash", cmd, roots=corpus["roots"]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_block_memory_file_orphan.py -q
```

Expected: collection error — `cannot load .../block-memory-file-orphan.py`.

- [ ] **Step 3: Write the implementation**

Create `C:\Users\diego\.claude\hooks\block-memory-file-orphan.py`:

```python
#!/usr/bin/env python3
"""PreToolUse hook: block a memory-file deletion that would orphan a live link.

Why
---
2026-08-14: auto-snapshot 486258b removed 118 files from the hermes-agent-src
memory root. The content merged correctly, but six inbound wikilinks were left
pointing at filenames that no longer existed, and stayed broken until a
hand-run lint found them on 2026-08-17. Five were same-root; the sixth was
cross-root -- the case a consolidation pass working inside one root
structurally cannot see.

This hook resolves inbound links BEFORE the deletion runs, while the agent
still holds the merge context and repair is free.

Self-clearing: repoint or inline the referrers, re-issue the identical command,
and it passes. There is no state to reset and nothing to lie to.

What it does NOT do
-------------------
Contain a determined caller. The escape marker below is deliberate, and a
deletion can always be spelled in a way these patterns miss -- that is what
detect-memory-file-orphan.py (PostToolUse) and the SessionEnd report are for.
The goal is to make orphaning a DELIBERATE, VISIBLE act rather than an accident.

Contract: PreToolUse hooks read a JSON event on stdin; exit 2 blocks the call
and shows stderr to the model. Any other exit code allows it.

Tests: test_block_memory_file_orphan.py (same directory).
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
LOG = HOME / ".claude" / "logs" / "memory-orphan-guard.log"

SHELL_TOOLS = {"bash", "powershell", "shell"}

# Deliberate override. Present in the command => allowed, and logged.
ESCAPE_MARKER = "memory-orphan: approved"


def _load_memory_links():
    path = Path(__file__).resolve().parent / "memory_links.py"
    spec = importlib.util.spec_from_file_location("memory_links", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_links"] = mod
    spec.loader.exec_module(mod)
    return mod


ml = _load_memory_links()


def _log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts}  block  {line}\n")
    except Exception:
        pass


# A path under a memory root, quoted or bare. Stops at shell separators.
_MEM_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:)?[^\s"',;|]*[\\/]memory[\\/][^\s"',;|]+\.md"""
)

_MOVE_RE = re.compile(r"\b(?:Move-Item|mv)\b", re.IGNORECASE)

# (label, pattern). The whole block decision lives in this table so the suite
# can empty it and prove the hook is not passing vacuously.
RULES = [
    ("rm", re.compile(r"\brm\b")),
    ("Remove-Item / del / erase", re.compile(r"\b(?:Remove-Item|del|erase)\b", re.I)),
    ("git rm", re.compile(r"\bgit\s+rm\b")),
    ("python unlink/remove", re.compile(r"os\.remove|os\.unlink|\.unlink\s*\(|shutil\.move")),
    ("Move-Item / mv out of a memory root", _MOVE_RE),
]


def extract_memory_paths(command: str, roots: list[Path]) -> list[Path]:
    """Existing *.md paths named in the command that sit in one of `roots`."""
    resolved = {str(r.resolve()).lower() for r in roots}
    found: list[Path] = []
    seen: set[str] = set()
    for raw in _MEM_PATH_RE.findall(command or ""):
        p = Path(raw.strip().strip("\"'"))
        try:
            parent = str(p.parent.resolve()).lower()
        except OSError:
            continue
        if parent not in resolved or not p.is_file():
            continue
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            found.append(p)
    return found


def _advice(orphans: dict) -> str:
    lines = [
        "Blocked: this deletion would orphan inbound memory links.",
        "",
    ]
    for path, refs in orphans.items():
        lines.append(f"  {Path(path).name} is still referenced by:")
        for r in refs:
            lines.append(f"      {r.root}|{r.file}:{r.line}  ({r.kind})")
    lines += [
        "",
        "Repair before deleting. For a referrer in ANOTHER root, INLINE the fact",
        "into the referrer by default -- repointing it only converts a dead link",
        "into a cross-root one, which is a trade rather than a fix (Diego's",
        "2026-08-17 precedent). Repoint instead only when the fact is too large",
        "to inline, and say so.",
        "For a referrer in the SAME root, repoint it at the merge target.",
        "",
        "Then re-issue this exact command -- the check is self-clearing.",
        "",
        "Deleting the referrers too? Name them in the SAME command and they are",
        "excluded automatically.",
        "",
        f"To override deliberately, include the marker: {ESCAPE_MARKER}",
    ]
    return "\n".join(lines)


def verdict(tool_name: str, command: str, roots: list[Path] | None = None) -> str | None:
    """Return a block reason, or None to allow. Never raises."""
    if (tool_name or "").strip().lower() not in SHELL_TOOLS:
        return None
    if not command:
        return None
    if ESCAPE_MARKER in command:
        _log(f"override-used cmd={command[:120]!r}")
        return None

    matched = None
    for label, pattern in RULES:
        try:
            if pattern.search(command):
                matched = label
                break
        except Exception:
            continue
    if matched is None:
        return None

    try:
        roots = ml.derive_roots() if roots is None else roots
        paths = extract_memory_paths(command, roots)
        if not paths:
            return None
        # A move naming TWO memory paths is a rename within the memory tree.
        # Layer 2 reports that; blocking it here would be a false positive.
        if matched.startswith("Move-Item") and len(paths) > 1:
            return None
        orphans = ml.referrers_of(paths, roots=roots)
    except ml.BudgetExceeded:
        _log("fail-open budget-exceeded")
        return None
    except Exception as e:  # fail open, always
        _log(f"fail-open error={e!r}")
        return None

    if not orphans:
        return None
    _log(f"BLOCKED rule={matched} files={len(orphans)}")
    return _advice(orphans)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # never block on a malformed event

    tool_name = event.get("tool_name") or ""
    command = (event.get("tool_input") or {}).get("command") or ""

    reason = verdict(tool_name, command)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_block_memory_file_orphan.py -q
```

Expected: `24 passed`.

- [ ] **Step 5: Verify the real hook blocks end-to-end via stdin**

Run (PowerShell):

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"rm C:/Users/diego/.claude/projects/C--Users-diego/memory/feedback_verify_your_own_write_survived.md"}}' | C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe C:/Users/diego/.claude/hooks/block-memory-file-orphan.py; echo "EXIT=$?"
```

Expected: the block message listing referrers (that file has 20 inbound links), then `EXIT=2`. This also proves the module imports cleanly under the 3.11 hook interpreter with no third-party packages.

- [ ] **Step 6: Register the hook in settings.json**

Modify `C:\Users\diego\.claude\settings.json`. In `hooks.PreToolUse`, the existing entry has `"matcher": "Bash|PowerShell"` with one hook in its `hooks` array. Append a second element to **that same array**:

```json
{
  "type": "command",
  "command": "\"C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe\" \"C:/Users/diego/.claude/hooks/block-memory-file-orphan.py\"",
  "timeout": 15
}
```

- [ ] **Step 7: Verify settings.json is still valid JSON**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -c "import json;d=json.load(open(r'C:\Users\diego\.claude\settings.json',encoding='utf-8'));print(len(d['hooks']['PreToolUse'][0]['hooks']),'PreToolUse hooks')"
```

Expected: `2 PreToolUse hooks`.

- [ ] **Step 8: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/block-memory-file-orphan.py hooks/test_block_memory_file_orphan.py settings.json
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): PreToolUse gate blocking orphaning memory deletions"
```

---

### Task 4: Layer 2 — the PostToolUse inventory detector

**Files:**
- Create: `C:\Users\diego\.claude\hooks\detect-memory-file-orphan.py`
- Test: `C:\Users\diego\.claude\hooks\test_detect_memory_file_orphan.py`
- Modify: `C:\Users\diego\.claude\settings.json`

**Interfaces:**
- Consumes: `memory_links.derive_roots`, `memory_links.build_index`, `memory_links.normalize`, `memory_links.root_label`, `memory_links.BudgetExceeded`.
- Produces: `inventory(roots: list[Path]) -> dict[str, list[str]]` mapping root label to sorted filenames; `state_path(session_id: str) -> Path`; `vanished(previous: dict, current: dict) -> list[tuple[str, str]]` of `(root_label, filename)`; `report(vanished_entries, roots) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_detect_memory_file_orphan.py`:

```python
"""Tests for detect-memory-file-orphan.py (PostToolUse detector).

Layer 1 pattern-matches commands and can be spelled around. This layer diffs a
rolling inventory, so it sees a deletion by ANY route -- and it fires seconds
after the command, while the same agent still holds the merge context.

Run:  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_detect_memory_file_orphan.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "detect-memory-file-orphan.py"


def _load():
    spec = importlib.util.spec_from_file_location("detect_memory_file_orphan", HOOK)
    assert spec and spec.loader, f"cannot load {HOOK}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "projects" / "C--Users-diego--hermes" / "memory"
    root.mkdir(parents=True)
    (root / "doomed.md").write_text("content\n", encoding="utf-8")
    (root / "referrer.md").write_text("see [[doomed]]\n", encoding="utf-8")
    (root / "lonely.md").write_text("nobody\n", encoding="utf-8")
    return root


def test_inventory_lists_md_files_by_root_label(corpus):
    inv = hook.inventory([corpus])
    assert inv == {"hermes": ["doomed.md", "lonely.md", "referrer.md"]}


def test_vanished_detects_a_removed_file():
    prev = {"hermes": ["a.md", "b.md"]}
    cur = {"hermes": ["a.md"]}
    assert hook.vanished(prev, cur) == [("hermes", "b.md")]


def test_vanished_ignores_added_files():
    prev = {"hermes": ["a.md"]}
    cur = {"hermes": ["a.md", "new.md"]}
    assert hook.vanished(prev, cur) == []


def test_vanished_on_a_first_run_reports_nothing():
    assert hook.vanished({}, {"hermes": ["a.md"]}) == []


def test_report_names_the_referrer_of_a_vanished_file(corpus):
    (corpus / "doomed.md").unlink()
    msg = hook.report([("hermes", "doomed.md")], roots=[corpus])
    assert msg is not None
    assert "doomed.md" in msg
    assert "referrer.md" in msg


def test_report_is_silent_when_nothing_pointed_at_the_vanished_file(corpus):
    (corpus / "lonely.md").unlink()
    assert hook.report([("hermes", "lonely.md")], roots=[corpus]) is None


def test_report_is_silent_when_nothing_vanished(corpus):
    assert hook.report([], roots=[corpus]) is None


def test_state_path_is_per_session():
    a = hook.state_path("session-aaa")
    b = hook.state_path("session-bbb")
    assert a != b
    assert "session-aaa" in a.name


def test_state_path_sanitizes_a_hostile_session_id():
    p = hook.state_path("../../evil")
    assert ".." not in p.name and "/" not in p.name and "\\" not in p.name


def test_report_fails_open_on_budget_exceeded(corpus, monkeypatch):
    def boom(*a, **k):
        raise hook.ml.BudgetExceeded("simulated")

    monkeypatch.setattr(hook.ml, "build_index", boom)
    (corpus / "doomed.md").unlink()
    assert hook.report([("hermes", "doomed.md")], roots=[corpus]) is None


def test_main_emits_a_system_message_and_exits_zero(corpus, tmp_path, monkeypatch, capsys):
    """End-to-end: first call records, deletion happens, second call reports."""
    monkeypatch.setattr(hook.ml, "derive_roots", lambda *a, **k: [corpus])
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")

    payload = json.dumps({"session_id": "s1", "tool_name": "Bash"})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    assert hook.main() == 0
    capsys.readouterr()

    (corpus / "doomed.md").unlink()

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    assert hook.main() == 0
    out = capsys.readouterr().out
    assert "referrer.md" in json.loads(out)["systemMessage"]


def test_main_exits_zero_on_a_malformed_event(monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    assert hook.main() == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_detect_memory_file_orphan.py -q
```

Expected: collection error — `cannot load .../detect-memory-file-orphan.py`.

- [ ] **Step 3: Write the implementation**

Create `C:\Users\diego\.claude\hooks\detect-memory-file-orphan.py`:

```python
#!/usr/bin/env python3
"""PostToolUse hook: report memory files that vanished with links still pointing at them.

Why this layer exists
---------------------
block-memory-file-orphan.py pattern-matches shell commands, and a deletion can
always be spelled a way its patterns miss -- a glob, a variable-expanded path,
an unrecognised verb. This hook parses nothing: it keeps a rolling inventory of
memory-root filenames and diffs it, so ANY route that unlinks a file is seen.

The gain over a session-end report is timing. This fires seconds after the
deleting command, so the agent that caused the orphan is still live and still
holds the merge context -- repair is an immediate correction rather than a
follow-up discovered weeks later, which is how the 2026-08-14 case went.

Matched to Bash|PowerShell only: Edit/Write/MultiEdit can empty a file but
never unlink one, so matching them would add cost on the hottest tool path and
detect nothing.

A vanished file whose frontmatter `name:` slug differed from its basename is
matched by basename only here -- the file is gone, so its slug is unreadable.
The SessionEnd report recovers the slug from git and closes that gap.

Contract: exit 0 ALWAYS. Never block. Errors are swallowed to the log.

Tests: test_detect_memory_file_orphan.py (same directory).
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
LOG = HOME / ".claude" / "logs" / "memory-orphan-guard.log"
STATE_DIR = HOME / ".claude" / "logs" / "memory-inventory"
STATE_TTL_SEC = 7 * 24 * 3600

SHELL_TOOLS = {"Bash", "PowerShell", "Shell"}


def _load_memory_links():
    path = Path(__file__).resolve().parent / "memory_links.py"
    spec = importlib.util.spec_from_file_location("memory_links", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_links"] = mod
    spec.loader.exec_module(mod)
    return mod


ml = _load_memory_links()


def _log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts}  detect  {line}\n")
    except Exception:
        pass


def inventory(roots: list[Path]) -> dict[str, list[str]]:
    """Root label -> sorted *.md filenames currently on disk."""
    out: dict[str, list[str]] = {}
    for root in roots:
        try:
            out[ml.root_label(root)] = sorted(p.name for p in root.glob("*.md"))
        except OSError:
            continue
    return out


def state_path(session_id: str) -> Path:
    """Per-session state file. Concurrent sessions must not see each other's
    deletions, so the session id is part of the name -- sanitized, because it
    arrives from the event payload."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")[:64]
    return STATE_DIR / f"inv-{safe}.json"


def vanished(previous: dict, current: dict) -> list[tuple[str, str]]:
    """(root_label, filename) present before and absent now.

    An empty `previous` is a first run, not a mass deletion.
    """
    if not previous:
        return []
    gone: list[tuple[str, str]] = []
    for label, names in previous.items():
        now = set(current.get(label, ()))
        for name in names:
            if name not in now:
                gone.append((label, name))
    return sorted(gone)


def report(vanished_entries, roots: list[Path]) -> str | None:
    """A systemMessage naming live referrers of vanished files, or None."""
    if not vanished_entries:
        return None
    try:
        idx = ml.build_index(roots=roots)
    except ml.BudgetExceeded:
        _log("fail-open budget-exceeded")
        return None
    except Exception as e:
        _log(f"fail-open error={e!r}")
        return None

    gone = {(label, name) for label, name in vanished_entries}
    lines: list[str] = []
    for label, name in vanished_entries:
        identity = ml.normalize(Path(name).stem)
        refs = [r for r in idx.lookup(identity) if (r.root, r.file) not in gone]
        if not refs:
            continue
        lines.append(f"  {label}|{name} is still referenced by:")
        for r in refs:
            lines.append(f"      {r.root}|{r.file}:{r.line}  ({r.kind})")
    if not lines:
        return None

    return (
        "Memory files were deleted with inbound links still pointing at them:\n"
        + "\n".join(lines)
        + "\n\nRepair now, while the merge context is still in this session. For a"
        "\nreferrer in ANOTHER root, inline the fact by default; repointing only"
        "\nconverts a dead link into a cross-root one. For a same-root referrer,"
        "\nrepoint it at the merge target."
    )


def _prune_state() -> None:
    try:
        cutoff = time.time() - STATE_TTL_SEC
        for p in STATE_DIR.glob("inv-*.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except OSError:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read() or "{}"
        payload = json.loads(raw.lstrip("\ufeff"))
    except Exception:
        return 0

    if payload.get("tool_name") not in SHELL_TOOLS:
        return 0

    try:
        roots = ml.derive_roots()
        current = inventory(roots)
        path = state_path(payload.get("session_id") or "")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _prune_state()

        previous = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                previous = {}

        path.write_text(json.dumps(current), encoding="utf-8")

        gone = vanished(previous, current)
        if gone:
            _log(f"vanished={len(gone)}")
            msg = report(gone, roots)
            if msg:
                print(json.dumps({"systemMessage": msg}))
                _log("REPORTED orphaned links")
    except Exception as e:
        _log(f"error {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_detect_memory_file_orphan.py -q
```

Expected: `13 passed`.

- [ ] **Step 5: Register the hook in settings.json**

Modify `C:\Users\diego\.claude\settings.json`. Add a **new object** to the `hooks.PostToolUse` array (the existing object matches `Edit|Write|MultiEdit`; this one must not disturb it):

```json
{
  "matcher": "Bash|PowerShell",
  "hooks": [
    {
      "type": "command",
      "command": "\"C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe\" \"C:/Users/diego/.claude/hooks/detect-memory-file-orphan.py\"",
      "timeout": 15
    }
  ]
}
```

- [ ] **Step 6: Verify settings.json is still valid JSON**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -c "import json;d=json.load(open(r'C:\Users\diego\.claude\settings.json',encoding='utf-8'));print([h['matcher'] for h in d['hooks']['PostToolUse']])"
```

Expected: `['Edit|Write|MultiEdit', 'Bash|PowerShell']`.

- [ ] **Step 7: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/detect-memory-file-orphan.py hooks/test_detect_memory_file_orphan.py settings.json
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): PostToolUse inventory diff detecting orphaned memory links"
```

---

### Task 5: Layer 3 — the SessionEnd orphan report

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\auto-commit-claude-memory.py`
- Test: `C:\Users\diego\.claude\hooks\test_auto_commit_orphan_report.py`

**Interfaces:**
- Consumes: `memory_links.build_index`, `memory_links.identities_from_text`, `memory_links.normalize`, `memory_links.derive_roots`.
- Produces: `_deleted_memory_paths(commit: str = "HEAD") -> list[str]` (repo-relative POSIX paths); `_orphan_report(commit: str = "HEAD") -> str | None`; a report file at `~/.claude/logs/memory-orphans-<UTC timestamp>.md`.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_auto_commit_orphan_report.py`:

```python
"""Tests for the orphan report inside auto-commit-claude-memory.py (SessionEnd).

This layer is the ONLY one that sees deletions made by other agents (Codex,
crons) between sessions -- `git add -A` sweeps those in, and they never pass
through any PreToolUse or PostToolUse hook.

Its ordering is load-bearing: the report runs AFTER the commit, reading
`git show HEAD`, never `git diff --cached` before it. That makes it
structurally impossible for a reporter bug to cost a snapshot.

Run:  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_auto_commit_orphan_report.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "auto-commit-claude-memory.py"


def _load():
    spec = importlib.util.spec_from_file_location("auto_commit_claude_memory", HOOK)
    assert spec and spec.loader, f"cannot load {HOOK}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repo shaped like ~/.claude, with one commit that deletes a
    referenced memory file."""
    root = tmp_path / "claude"
    mem = root / "projects" / "C--Users-diego--hermes" / "memory"
    mem.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "t@t.local")
    _git(root, "config", "user.name", "t")

    (mem / "doomed.md").write_text("---\nname: doomed-slug\n---\nbody\n", encoding="utf-8")
    (mem / "referrer.md").write_text("see [[doomed-slug]]\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")

    (mem / "doomed.md").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "delete doomed")

    monkeypatch.setattr(hook, "REPO", str(root))
    monkeypatch.setattr(hook.ml, "derive_roots", lambda *a, **k: [mem])
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    return root


def test_deleted_memory_paths_reads_the_commit(repo):
    paths = hook._deleted_memory_paths("HEAD")
    assert paths == ["projects/C--Users-diego--hermes/memory/doomed.md"]


def test_orphan_report_names_the_referrer(repo):
    msg = hook._orphan_report("HEAD")
    assert msg is not None
    assert "doomed.md" in msg
    assert "referrer.md" in msg


def test_orphan_report_resolves_the_frontmatter_slug_from_git(repo):
    """The file is gone from disk; its `name:` slug is recoverable only from
    the parent commit's blob. Without that, a slug-written link is missed."""
    msg = hook._orphan_report("HEAD")
    assert "referrer.md:1" in msg


def test_orphan_report_is_none_when_nothing_was_deleted(repo):
    assert hook._orphan_report("HEAD~1") is None


def test_orphan_report_is_none_when_the_deleted_file_had_no_referrers(repo, tmp_path):
    mem = Path(repo) / "projects" / "C--Users-diego--hermes" / "memory"
    (mem / "lonely.md").write_text("nobody\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add lonely")
    (mem / "lonely.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete lonely")
    assert hook._orphan_report("HEAD") is None


def test_orphan_report_swallows_errors_and_returns_none(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(hook.ml, "build_index", boom)
    assert hook._orphan_report("HEAD") is None


def test_main_still_returns_zero_when_the_reporter_raises(repo, monkeypatch):
    """The exit-0-always contract must survive a broken reporter."""
    def boom(*a, **k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(hook, "_orphan_report", boom)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id":"x"}'))
    mem = Path(repo) / "projects" / "C--Users-diego--hermes" / "memory"
    (mem / "new.md").write_text("x\n", encoding="utf-8")
    assert hook.main() == 0
    head = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "auto-snapshot" in head, "the commit must survive a reporter failure"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_auto_commit_orphan_report.py -q
```

Expected: 7 failures — `AttributeError: module 'auto_commit_claude_memory' has no attribute 'ml'` / `_deleted_memory_paths`.

- [ ] **Step 3: Add the module import and the reporter**

In `C:\Users\diego\.claude\hooks\auto-commit-claude-memory.py`, after the existing `import time` line (currently line 25), add:

```python
import importlib.util
from pathlib import Path as _Path


def _load_memory_links():
    path = _Path(__file__).resolve().parent / "memory_links.py"
    spec = importlib.util.spec_from_file_location("memory_links", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_links"] = mod
    spec.loader.exec_module(mod)
    return mod


ml = _load_memory_links()
```

Then, immediately before `def main()`, add:

```python
_MEM_PATH_RE = __import__("re").compile(r"^projects/[^/]+/memory/[^/]+\.md$")


def _deleted_memory_paths(commit: str = "HEAD") -> list[str]:
    """Repo-relative paths this commit DELETED from a memory root."""
    rc, out, _ = _git("show", "--name-status", "--format=", commit)
    if rc != 0:
        return []
    paths = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().startswith("D"):
            p = parts[1].replace("\\", "/").strip()
            if _MEM_PATH_RE.match(p):
                paths.append(p)
    return paths


def _orphan_report(commit: str = "HEAD") -> str | None:
    """Links that still point at files this commit deleted.

    This is the only layer that sees deletions made by OTHER agents (Codex,
    crons) between sessions -- `git add -A` sweeps those in, and they never
    pass through a PreToolUse or PostToolUse hook.

    The deleted file is gone from disk, so its frontmatter `name:` slug is read
    back from the PARENT commit's blob. Basename-only matching would miss every
    link written to the slug, which is most of them in the `~` root.
    """
    try:
        deleted = _deleted_memory_paths(commit)
        if not deleted:
            return None
        idx = ml.build_index(roots=ml.derive_roots())
        gone = {(ml.root_label(_Path(p).parent), _Path(p).name) for p in deleted}

        lines = []
        for path in deleted:
            name = _Path(path).name
            rc, blob, _ = _git("show", f"{commit}^:{path}")
            identities = ml.identities_from_text(
                _Path(path).stem, blob if rc == 0 else ""
            )
            refs, seen = [], set()
            for identity in sorted(identities):
                for r in idx.lookup(identity):
                    if (r.root, r.file) in gone:
                        continue
                    key = (r.root, r.file, r.line)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(r)
            if refs:
                lines.append(f"## {path}")
                for r in sorted(refs):
                    lines.append(f"- {r.root}|{r.file}:{r.line} ({r.kind})")
        if not lines:
            return None
        return (
            f"# Orphaned memory links in {commit}\n\n"
            "These links point at files this commit deleted. Inline the fact for a\n"
            "cross-root referrer; repoint a same-root referrer at the merge target.\n\n"
            + "\n".join(lines)
            + "\n"
        )
    except Exception as e:
        _log(f"orphan-report-failed {e!r}")
        return None
```

- [ ] **Step 4: Call the reporter AFTER the commit succeeds**

In `main()`, the block that logs the commit currently reads:

```python
        rc, head, _ = _git("rev-parse", "--short", "HEAD")
        _log(f"committed {n} file(s) head={head} session={sid}")
```

Replace it with:

```python
        rc, head, _ = _git("rev-parse", "--short", "HEAD")
        _log(f"committed {n} file(s) head={head} session={sid}")

        # AFTER the commit, never before. Reading `git show HEAD` rather than
        # `git diff --cached` makes it structurally impossible for a bug in the
        # reporter to cost a snapshot -- the exit-0-always contract holds by
        # construction rather than by care.
        try:
            report = _orphan_report("HEAD")
        except Exception as e:
            _log(f"orphan-report-raised {e!r}")
            report = None
        if report:
            ts_file = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = os.path.join(REPO, "logs", f"memory-orphans-{ts_file}.md")
            try:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "w", encoding="utf-8") as f:
                    f.write(report)
                _log(f"ORPHANED LINKS detected -- report at {out}")
            except Exception as e:
                _log(f"orphan-report-write-failed {e!r}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_auto_commit_orphan_report.py -q
```

Expected: `7 passed`.

- [ ] **Step 6: Verify the modified hook still imports under the 3.11 hook interpreter**

Run:

```bash
echo '{}' | C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe C:/Users/diego/.claude/hooks/auto-commit-claude-memory.py; echo "EXIT=$?"
```

Expected: `EXIT=0`. Check `~/.claude/logs/auto-commit-claude-memory.log` — the last line should be a normal `no-op`, `committed`, or `skip` entry, **not** an import traceback.

- [ ] **Step 7: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/auto-commit-claude-memory.py hooks/test_auto_commit_orphan_report.py
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): SessionEnd orphan report after the snapshot commit"
```

---

### Task 6: Instruction injection into the summoning message

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory-index-size-guard.py:117-122`
- Test: `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: no new callable — the existing `main()` emits an extended `systemMessage`.

This is the spec's substitute for editing the vendor `consolidate-memory` skill, which is not on disk. The guard's message is what tells an agent to invoke that skill, so the link-safety rule arrives exactly when the pass begins.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py`:

```python
"""The consolidation-summoning message must carry the link-safety rule.

The vendor `consolidate-memory` skill is not on disk and cannot be edited. This
hook's message is what summons it, so it is the injection point for the rule
whose absence orphaned six links on 2026-08-14.

Run:  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "memory-index-size-guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("memory_index_size_guard", HOOK)
    assert spec and spec.loader, f"cannot load {HOOK}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def _emit(tmp_path, monkeypatch, capsys, lines=500):
    index = tmp_path / ".claude" / "projects" / "C--Users-diego" / "memory" / "MEMORY.md"
    index.parent.mkdir(parents=True)
    index.write_text("x\n" * lines, encoding="utf-8")
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": str(index)}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    assert hook.main() == 0
    return json.loads(capsys.readouterr().out)["systemMessage"]


def test_message_still_summons_the_consolidation_skill(tmp_path, monkeypatch, capsys):
    msg = _emit(tmp_path, monkeypatch, capsys)
    assert "consolidate-memory" in msg


def test_message_carries_the_link_safety_rule(tmp_path, monkeypatch, capsys):
    msg = _emit(tmp_path, monkeypatch, capsys).lower()
    assert "inbound" in msg and "link" in msg
    assert "inline" in msg


def test_message_warns_that_deletion_is_gated(tmp_path, monkeypatch, capsys):
    msg = _emit(tmp_path, monkeypatch, capsys)
    assert "block" in msg.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: `1 passed, 2 failed` — the summons test passes; the link-safety and gating assertions fail.

- [ ] **Step 3: Extend the message**

In `C:\Users\diego\.claude\hooks\memory-index-size-guard.py`, replace the `msg = (...)` assignment (currently lines 117-122) with:

```python
    msg = (
        f"MEMORY.md index is {lines} lines (early-warning threshold {threshold}; "
        f"harness auto-memory read cap ~{HARNESS_CAP}). Consider a consolidation "
        f"pass: invoke the /consolidate-memory skill to merge related entries and "
        f"relocate detail into topic files before the index gets truncated.\n"
        f"LINK SAFETY: before deleting a file whose content you merged, resolve "
        f"its inbound [[wikilinks]] and repoint them at the merge target. If a "
        f"referrer lives in ANOTHER memory root, inline the fact into that "
        f"referrer instead -- repointing only converts a dead link into a "
        f"cross-root one. Deletions that would orphan a link are blocked by the "
        f"PreToolUse guard; fix the referrers and re-issue the same command."
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory-index-size-guard.py hooks/test_memory_index_size_guard_message.py
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): carry the link-safety rule in the consolidation summons"
```

---

### Task 7: Linter conformance and acceptance

**Files:**
- Test: `C:\Users\diego\.claude\hooks\test_linter_conformance.py` (create)

**Interfaces:**
- Consumes: `memory_links.build_index`, `memory_links.derive_roots`, `memory_links.root_label`; the PowerShell linter at `C:\Users\diego\memory-link-lint.ps1`.
- Produces: no callable — a guard that the Python module and the PowerShell linter agree.

Without this, the gate and the linter that adjudicates its result can drift apart, and a divergence would look like a hook bug rather than a semantics mismatch.

- [ ] **Step 1: Write the failing test**

Create `C:\Users\diego\.claude\hooks\test_linter_conformance.py`:

```python
"""memory_links.py must agree with memory-link-lint.ps1.

The linter is what adjudicates whether a link is dead. If the gate's notion of
"a link" drifts from the linter's, the gate blocks on things the linter calls
fine (or waves through things it does not), and the disagreement reads as a
hook bug rather than a semantics mismatch.

These tests are SLOW (the linter takes ~25s). Run:
  C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_linter_conformance.py -q
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent / "memory_links.py"
LINTER = Path(r"C:\Users\diego\memory-link-lint.ps1")


def _load():
    spec = importlib.util.spec_from_file_location("memory_links", MODULE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ml = _load()

pytestmark = pytest.mark.skipif(not LINTER.exists(), reason="linter not present")


@pytest.fixture(scope="module")
def lint_output():
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(LINTER)],
        capture_output=True, text=True, timeout=300,
    )
    return proc.stdout


def test_root_set_matches_the_linter(lint_output):
    """Both must derive the same versioned roots from the same allowlist."""
    m = re.search(r"roots:\s*(\d+)", lint_output)
    assert m, f"could not parse root count from:\n{lint_output[:400]}"
    assert len(ml.derive_roots()) == int(m.group(1))


def test_root_labels_match_the_linter(lint_output):
    listed = set(re.findall(r"^\s{4}(\S+)\s+\d+ files", lint_output, re.MULTILINE))
    assert listed, "could not parse root labels from linter output"
    assert {ml.root_label(r) for r in ml.derive_roots()} == listed


def test_file_counts_match_the_linter(lint_output):
    """A count mismatch means one side is scanning files the other is not."""
    listed = {
        label: int(n)
        for label, n in re.findall(r"^\s{4}(\S+)\s+(\d+) files", lint_output, re.MULTILINE)
    }
    for root in ml.derive_roots():
        label = ml.root_label(root)
        assert len(list(root.glob("*.md"))) == listed[label], f"file count mismatch for {label}"


def test_no_dead_links_remain(lint_output):
    """The acceptance gate: DEAD must still be 0 after all four layers land."""
    assert "No genuinely dead links" in lint_output, lint_output[-800:]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_linter_conformance.py -q
```

Expected: collection succeeds; failures depend on drift. Fix `memory_links.py` — not the linter — if roots, labels, or counts disagree. **The linter is read-only and must not be edited** (Diego's 2026-08-10 lint-only call).

- [ ] **Step 3: Fix any divergence in the Python module**

If root counts or labels differ, the likely cause is `derive_roots` falling back to all-candidates because git failed. Verify by running:

```bash
git -C "C:/Users/diego/.claude" check-ignore --no-index -- projects/C--Users-diego--openclaw/memory/MEMORY.md; echo "EXIT=$?"
```

Expected: prints the matching `.gitignore` rule and `EXIT=0` (that root IS ignored). If it exits 1, the allowlist changed and both tools will pick it up — update the test's expectation, not the derivation.

- [ ] **Step 4: Run the full hook suite**

Run:

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/ -q
```

Expected: all suites pass, including the two pre-existing ones (`test_block_unscoped_process_kill.py` 24 tests, `test_mcp_registration_check.py`). A regression in either means a new hook broke a shared assumption.

- [ ] **Step 5: Run the acceptance lint and compare against the baseline**

Run:

```bash
powershell -NoProfile -File C:\Users\diego\memory-link-lint.ps1
```

Expected — **exactly** the Global Constraints baseline:

```
NEARMISS       2 / 2
CROSSROOT     15 / 14
RENAMED       31 / 17
SUFFIX        26 / 21
CONVENTION    75 / 50
GBRAIN         7 / 7
PATH           2 / 2
PROSE         65 / 30

No genuinely dead links. Remaining categories are convention/expected.
```

A changed non-DEAD count means something normalized links that were deliberately left alone — investigate before proceeding.

- [ ] **Step 6: Commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/test_linter_conformance.py
git -C "C:/Users/diego/.claude" diff --cached --name-only
git -C "C:/Users/diego/.claude" commit -m "test(hooks): conformance between memory_links and the PowerShell linter"
```

- [ ] **Step 7: Live end-to-end rehearsal**

Create a throwaway memory file and a referrer in the `~` root, then attempt to delete it through a real shell call so the registered `PreToolUse` hook fires:

```bash
powershell -NoProfile -Command "Set-Content -Path 'C:\Users\diego\.claude\projects\C--Users-diego\memory\zz_orphan_rehearsal_target.md' -Value 'rehearsal target' -Encoding utf8; Set-Content -Path 'C:\Users\diego\.claude\projects\C--Users-diego\memory\zz_orphan_rehearsal_referrer.md' -Value 'see [[zz_orphan_rehearsal_target]]' -Encoding utf8"
```

Then, in a normal shell tool call, attempt:

```bash
rm C:/Users/diego/.claude/projects/C--Users-diego/memory/zz_orphan_rehearsal_target.md
```

Expected: the call is **blocked**, and the message names `~|zz_orphan_rehearsal_referrer.md:1`.

Clean up both rehearsal files (delete the referrer first, so the gate correctly allows the target):

```bash
powershell -NoProfile -Command "Remove-Item 'C:\Users\diego\.claude\projects\C--Users-diego\memory\zz_orphan_rehearsal_referrer.md'; Remove-Item 'C:\Users\diego\.claude\projects\C--Users-diego\memory\zz_orphan_rehearsal_target.md'"
```

Confirm the lint is still clean:

```bash
powershell -NoProfile -File C:\Users\diego\memory-link-lint.ps1
```

Expected: `No genuinely dead links`, exit 0.

---

## Self-Review Notes

**Spec coverage** — every section maps to a task: shared module → Tasks 1-2; Layer 1 → Task 3; Layer 2 → Task 4; Layer 3 → Task 5; instruction injection → Task 6; conformance + acceptance → Task 7. Error-handling requirements (fail-open, internal deadline, commit-before-report ordering, per-session state) each have a named test.

**Known gap, deliberately accepted:** Layer 2 matches a vanished file by basename only, because the file is gone and its frontmatter slug is unreadable from disk. Layer 3 closes this by recovering the slug from the parent commit's blob (`test_orphan_report_resolves_the_frontmatter_slug_from_git`). Documented in the Layer 2 docstring rather than silently left.

**Ordering constraint:** Tasks 1 and 2 must land before 3, 4, and 5 — all three consumers import `memory_links.py`. Tasks 3-6 are independent of each other. Task 7 must run last.
