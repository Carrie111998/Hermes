"""Tests for scripts/ci/check_doc_links.py.

The resolver is exercised as pure logic with an injected ``exists`` predicate —
no filesystem, no reading of real repo files. A test that walked the real tree
would fail every time a doc legitimately changed, which is exactly the
change-detector antipattern AGENTS.md bans.

The bias under test: **resolve generously, report rarely.** A false positive
gets the check disabled; a missed dead link costs one confused reader.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_doc_links.py"
_spec = importlib.util.spec_from_file_location("check_doc_links", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load check_doc_links.py")
_mod = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves its own module out of sys.modules,
# and blows up with AttributeError on None if the module isn't there yet.
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

is_pathlike = _mod.is_pathlike
extract_refs = _mod.extract_refs
section_base_at = _mod.section_base_at
candidates = _mod.candidates
check_doc = _mod.check_doc
in_scope = _mod.in_scope
exemption_reason = _mod.exemption_reason


def _exists(*present: str):
    """Build an ``exists`` predicate over a fixed set of paths."""
    known = set(present)
    return lambda p: p in known


# ── What counts as a repo path ───────────────────────────────────────────


@pytest.mark.parametrize(
    "ref",
    [
        "gateway/run.py",
        "plugins/platforms/telegram/adapter.py",
        ".github/workflows/ci.yml",  # dotdir is a real repo path
        "src/components/markdown.tsx",
    ],
)
def test_pathlike_accepts_repo_paths(ref):
    assert is_pathlike(ref)


@pytest.mark.parametrize(
    "ref,why",
    [
        ("run_agent.py", "bare filename — too ambiguous to resolve"),
        ("/.well-known/agent.json", "absolute/URL path"),
        ("~/.hermes/config.yaml", "home-relative runtime path"),
        (".hermes/security-guidance.md", "runtime path under HERMES_HOME"),
        ("$HERMES_HOME/mem0.json", "env-var interpolation"),
        ("{sessions_dir}/sessions.json", "template placeholder"),
        ("viking://user/memories/profile.md", "custom URL scheme"),
        ("https://example.com/a.json", "http URL"),
        ("@spectrum-ts/imessage/dist/index.js", "npm scoped package specifier"),
        ("ui-tui/dist/entry.js", "build artifact, never tracked"),
        ("web/node_modules/foo/index.js", "vendored dependency"),
        (".md/.txt/.rst/.json/.yaml", "extension list, not a path"),
        ("tools/*.py", "glob"),
        ("gateway/platforms/<platform>.py", "placeholder"),
    ],
)
def test_pathlike_rejects_non_repo_tokens(ref, why):
    assert not is_pathlike(ref), why


# ── Extraction ───────────────────────────────────────────────────────────


def test_extract_skips_fenced_code_blocks():
    text = "\n".join([
        "Real ref: `gateway/run.py`",
        "```python",
        "from a.b import c  # `illustrative/only.py`",
        "```",
        "Another: `tools/registry.py`",
    ])
    assert [r for r, _ in extract_refs(text)] == ["gateway/run.py", "tools/registry.py"]


def test_extract_reports_line_numbers():
    text = "line one\n\nsee `gateway/run.py` here\n"
    assert extract_refs(text) == [("gateway/run.py", 3)]


# ── Section bases ────────────────────────────────────────────────────────


def test_section_base_from_nearest_heading():
    text = "\n".join([
        "# Title",                                    # 1
        "### Slash commands (`src/app/slash/`)",      # 2
        "- `commands/core.ts` — stuff",               # 3
    ])
    assert section_base_at(text, 3) == "src/app/slash"


def test_section_base_is_nearest_preceding_not_any():
    text = "\n".join([
        "## A (`src/one/`)",     # 1
        "- `x.ts`",              # 2
        "## B (`src/two/`)",     # 3
        "- `y.ts`",              # 4
    ])
    assert section_base_at(text, 2) == "src/one"
    assert section_base_at(text, 4) == "src/two"


def test_no_base_before_first_declaring_heading():
    assert section_base_at("## Plain heading\n- `a/b.ts`\n", 2) is None


# ── Resolution ───────────────────────────────────────────────────────────


def test_resolves_from_repo_root():
    found, _, _ = check_doc(
        "docs/x.md", "see `gateway/run.py`", _exists("gateway/run.py")
    )
    assert found == []


def test_resolves_via_ancestor_directory():
    """`app/chat/probe.tsx` cited from apps/desktop/src/debug/README.md."""
    found, _, _ = check_doc(
        "apps/desktop/src/debug/README.md",
        "see `app/chat/probe.tsx`",
        _exists("apps/desktop/src/app/chat/probe.tsx"),
    )
    assert found == []


def test_resolves_via_section_base():
    text = "### Slash (`src/app/slash/`)\n- `commands/core.ts`\n"
    found, _, _ = check_doc(
        "ui-tui/README.md", text, _exists("ui-tui/src/app/slash/commands/core.ts")
    )
    assert found == []


def test_resolves_via_explicit_base_directive():
    text = "<!-- doc-links: base=apps/desktop/src -->\nsee `lib/thing.ts`\n"
    found, _, _ = check_doc(
        "docs/desktop.md", text, _exists("apps/desktop/src/lib/thing.ts")
    )
    assert found == []


def test_reports_a_genuinely_missing_file():
    found, _, _ = check_doc(
        "docs/x.md", "see `gateway/platforms/telegram.py`", _exists("nothing/else.py")
    )
    assert len(found) == 1
    assert found[0].ref == "gateway/platforms/telegram.py"
    assert found[0].line == 1


def test_the_regression_this_check_exists_for():
    """An adapter moves; the doc still names the old path and is not edited."""
    doc = "See `gateway/platforms/telegram.py` for a reference implementation."
    after_move = _exists("plugins/platforms/telegram/adapter.py")
    found, _, _ = check_doc("gateway/platforms/ADDING_A_PLATFORM.md", doc, after_move)
    assert [f.ref for f in found] == ["gateway/platforms/telegram.py"]


# ── Exemptions and scope ─────────────────────────────────────────────────


def test_exempt_ref_is_not_reported_and_is_counted_separately():
    text = "checklist lives at `references/new-skill-pr-salvage.md`"
    found, checked, exempted = check_doc("AGENTS.md", text, _exists())
    assert found == []
    assert (checked, exempted) == (0, 1)


def test_exemption_is_scoped_to_its_doc():
    ref = "references/new-skill-pr-salvage.md"
    assert exemption_reason("AGENTS.md", ref)
    assert exemption_reason("docs/unrelated.md", ref) is None


def test_every_exemption_carries_a_reason():
    for doc_glob, ref, reason in _mod.EXEMPT_REFS:
        assert reason.strip(), f"{doc_glob}:{ref} exempted without a reason"


@pytest.mark.parametrize(
    "doc,expected",
    [
        ("AGENTS.md", True),
        ("gateway/platforms/ADDING_A_PLATFORM.md", True),
        ("docs/plans/2026-06-09-003-fix-telegram.md", False),  # historical record
        (".plans/streaming-support.md", False),                # historical record
        ("website/docs/user-guide/x.md", False),               # docs-site owns this
        ("skills/github/SKILL.md", False),                     # installed-skill layout
    ],
)
def test_doc_scope(doc, expected):
    assert in_scope(doc) is expected


def test_candidates_are_deduplicated():
    got = candidates("a/b.py", "docs/x.md", "a", None)
    assert len(got) == len(set(got))
