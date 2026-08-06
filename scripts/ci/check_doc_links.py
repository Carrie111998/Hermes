#!/usr/bin/env python3
"""Verify that repo file paths cited in tracked Markdown still exist.

Why this exists
---------------
Docs rot silently when *code* moves, not when docs change. When the messaging
platforms migrated from ``gateway/platforms/<name>.py`` to
``plugins/platforms/<name>/adapter.py``, `ADDING_A_PLATFORM.md` kept pointing
new contributors at files that no longer existed, and
`tests/e2e/matrix_xsign_bootstrap/README.md` kept telling readers to diff
against a symbol that had been deleted. Nothing failed, because nothing checks.

That is also why this check is **not** gated on "markdown changed" — the diff
that breaks a doc reference usually does not touch the doc at all.

Resolution model
----------------
A reference resolves if ANY of these lands on a real tracked path:

1. repo-root relative                    — ``gateway/run.py``
2. relative to the doc's own directory, or any ancestor of it up to the repo
   root. This is what makes ``app/chat/perf-probe.tsx`` resolve from
   ``apps/desktop/src/debug/README.md``.
3. relative to a **section base** — the nearest preceding heading that names a
   directory in backticks, e.g.::

       ### Slash command subsystem (`src/app/slash/`)

       - `commands/core.ts` — general TUI commands

   The base itself is resolved with rules 1 and 2, so both repo-root-anchored
   and doc-relative section bases work.
4. relative to an explicit per-file override::

       <!-- doc-links: base=apps/desktop/src -->

The model is deliberately generous. A false positive costs a contributor real
time and gets the check disabled; a missed dead link costs the next reader one
confused search. Tune toward silence.

Exemptions live in the tables at the top of this file, each with a reason —
if you add one, say why.

Usage
-----
    python3 scripts/ci/check_doc_links.py            # check the repo
    python3 scripts/ci/check_doc_links.py --stats    # + coverage summary
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# ── Which docs are in scope ──────────────────────────────────────────────
#
# Excluded trees are excluded for a *reason*, not for convenience. Dated plan
# documents and investigation logs are historical records: they describe the
# tree as it was, and "fixing" their paths to match today's code destroys the
# thing that makes them worth keeping.

EXCLUDED_DOC_GLOBS: tuple[str, ...] = (
    # Historical records — accurate as of their date, deliberately not updated.
    "docs/plans/*",
    ".plans/*",
    "apps/desktop/scripts/profile-typing-lag.md",  # self-declared investigation log
    # The Docusaurus site has its own link checking in docs-site-checks.yml.
    "website/*",
    # Skill docs address paths relative to an installed skill directory, which
    # is not this repo's layout. Out of scope until that's modeled properly.
    "skills/*",
    "optional-skills/*",
    "locales/*",
    # Vendored / generated.
    "node_modules/*",
)

# ── Reference exemptions ─────────────────────────────────────────────────
#
# (doc_glob, ref, reason). doc_glob "*" applies everywhere.
# These are NOT dead links — they are references that correctly point outside
# this repo's working tree.

EXEMPT_REFS: tuple[tuple[str, str, str], ...] = (
    (
        "AGENTS.md",
        "tools/your_tool.py",
        "tutorial placeholder — the file the reader is being told to create",
    ),
    (
        "AGENTS.md",
        "references/new-skill-pr-salvage.md",
        "lives in the hermes-agent-dev skill, not this repo (stated in context)",
    ),
    (
        "docs/relay-connector-contract.md",
        "src/core/relayBus.ts",
        "connector repo: NousResearch/gateway-gateway",
    ),
    (
        "docs/relay-connector-contract.md",
        "src/core/relayAuthToken.ts",
        "connector repo: NousResearch/gateway-gateway",
    ),
    (
        "docs/relay-connector-contract.md",
        "docs/capability-trust-boundary.md",
        "connector repo — the doc says so inline",
    ),
    (
        "docs/relay-connector-contract.md",
        "docs/connector-gateway-auth-design.md",
        "connector repo — the doc says so inline",
    ),
    (
        "docs/chronos-managed-cron-contract.md",
        "src/server/agent-cron/instance-auth.ts",
        "NAS repo (nous-account-service) — this doc's audience is its implementer",
    ),
    (
        "docs/observability/relay-shared-metrics.md",
        "skills/.usage.json",
        "runtime path under HERMES_HOME, not a repo file",
    ),
    (
        "plugins/hermes-achievements/docs/*",
        "dashboard/perf_scan_coordinator.py",
        "forward-looking spec — file is to be created by the described refactor",
    ),
    (
        "plugins/hermes-achievements/docs/*",
        "dashboard/perf_snapshot.py",
        "forward-looking spec — file is to be created by the described refactor",
    ),
)

# Paths under these prefixes are runtime locations (user's HERMES_HOME, editor
# config), never repo files.
RUNTIME_PREFIXES: tuple[str, ...] = (".hermes/", ".claude/", "~/")

# Path segments that mean "generated output" — never tracked, so a reference
# through one is describing a build artifact, not a source file.
_GENERATED_SEGMENTS = frozenset({"dist", "build", "web_dist", "node_modules", "__pycache__"})

# Extensions we treat as "a path to a file in this repo".
CODE_EXTS = (
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs",
    "yaml", "yml", "json", "toml", "sh", "ps1", "md", "rs",
)

_REF_RE = re.compile(r"`([^`\s]+\.(?:" + "|".join(CODE_EXTS) + r"))`")
_HEADING_BASE_RE = re.compile(r"^#{1,6}\s.*?`([A-Za-z0-9_./-]+/)`")
_BASE_DIRECTIVE_RE = re.compile(r"<!--\s*doc-links:\s*base=([A-Za-z0-9_./-]+)\s*-->")
_EXT_LIST_SEG_RE = re.compile(r"^\.[a-z0-9]+$")


@dataclass(frozen=True)
class Finding:
    doc: str
    line: int
    ref: str


def is_pathlike(ref: str) -> bool:
    """Filter out backticked tokens that look like paths but aren't.

    Each rejection below is a *class* of non-path token seen in this repo's
    docs, not a one-off. Keeping them here rather than in EXEMPT_REFS means a
    new doc using the same idiom doesn't have to re-litigate it.
    """
    if not ref or "/" not in ref:  # bare filenames are too ambiguous to resolve
        return False
    if ref.startswith(("/", "~", "@")):
        # absolute path, home-relative path, or an npm scoped package
        # specifier (`@spectrum-ts/imessage/dist/index.js`)
        return False
    if "://" in ref:  # any URL scheme — http, viking://, etc.
        return False
    if ref.startswith(RUNTIME_PREFIXES):
        return False
    if "$" in ref or "{" in ref or "}" in ref:
        # env-var or template interpolation: `$HERMES_HOME/mem0.json`,
        # `{sessions_dir}/sessions.json` — a runtime path, not a repo file
        return False
    if "*" in ref or "<" in ref or ">" in ref:  # globs / placeholders
        return False
    segs = ref.split("/")
    if all(_EXT_LIST_SEG_RE.match(s) for s in segs):
        return False  # ".md/.txt/.rst" — an extension list, not a path
    if any(s in _GENERATED_SEGMENTS for s in segs):
        return False  # build output isn't tracked; `ui-tui/dist/entry.js`
    return True


def extract_refs(text: str) -> list[tuple[str, int]]:
    """Return ``(ref, line_number)`` for every path-like backticked token."""
    out: list[tuple[str, int]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue  # code blocks are illustrative, not assertions about the tree
        for m in _REF_RE.finditer(line):
            ref = m.group(1)
            if is_pathlike(ref):
                out.append((ref, lineno))
    return out


def section_base_at(text: str, line: int) -> str | None:
    """Directory declared by the nearest heading at or above ``line``."""
    base = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if lineno > line:
            break
        m = _HEADING_BASE_RE.match(raw)
        if m:
            base = m.group(1).rstrip("/")
    return base


def explicit_base(text: str) -> str | None:
    m = _BASE_DIRECTIVE_RE.search(text)
    return m.group(1).rstrip("/") if m else None


def ancestors(doc: str) -> list[str]:
    """The doc's own directory and every ancestor, nearest first, then root."""
    out = []
    d = os.path.dirname(doc)
    while d:
        out.append(d)
        d = os.path.dirname(d)
    out.append("")
    return out


def candidates(ref: str, doc: str, base: str | None, override: str | None) -> list[str]:
    """Every path this reference could legitimately mean, most likely first."""
    anchors = ancestors(doc)
    out = [ref]  # repo-root relative
    out += [os.path.normpath(os.path.join(a, ref)) for a in anchors if a]
    for b in (override, base):
        if not b:
            continue
        # The section base itself may be repo-root- or doc-relative.
        for a in anchors:
            out.append(os.path.normpath(os.path.join(a, b, ref)) if a
                       else os.path.normpath(os.path.join(b, ref)))
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def exemption_reason(doc: str, ref: str) -> str | None:
    for doc_glob, exempt_ref, reason in EXEMPT_REFS:
        if ref == exempt_ref and (doc_glob == "*" or fnmatch.fnmatch(doc, doc_glob)):
            return reason
    return None


def in_scope(doc: str) -> bool:
    return not any(fnmatch.fnmatch(doc, g) for g in EXCLUDED_DOC_GLOBS)


def check_doc(doc: str, text: str, exists) -> tuple[list[Finding], int, int]:
    """Check one doc. Returns ``(findings, checked, exempted)``.

    ``exists`` is injected so this is testable without a filesystem.
    """
    override = explicit_base(text)
    findings: list[Finding] = []
    checked = exempted = 0
    for ref, lineno in extract_refs(text):
        if exemption_reason(doc, ref):
            exempted += 1
            continue
        checked += 1
        base = section_base_at(text, lineno)
        if not any(exists(c) for c in candidates(ref, doc, base, override)):
            findings.append(Finding(doc, lineno, ref))
    return findings, checked, exempted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", action="store_true", help="print coverage summary")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = ap.parse_args()

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    tracked_set = {p for p in tracked if p}
    docs = sorted(p for p in tracked_set if p.endswith(".md") and in_scope(p))

    def exists(p: str) -> bool:
        return p in tracked_set or os.path.exists(p)

    findings: list[Finding] = []
    total_checked = total_exempt = 0
    for doc in docs:
        try:
            text = open(doc, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        f, c, e = check_doc(doc, text, exists)
        findings += f
        total_checked += c
        total_exempt += e

    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))

    if args.stats or not findings:
        print(
            f"doc-links: {total_checked} references checked across {len(docs)} docs "
            f"({total_exempt} exempted, {len(EXCLUDED_DOC_GLOBS)} doc globs out of scope)"
        )

    if findings:
        print()
        print(f"::error::{len(findings)} documentation reference(s) point at files that do not exist.")
        print()
        by_doc: dict[str, list[Finding]] = {}
        for f in findings:
            by_doc.setdefault(f.doc, []).append(f)
        for doc, items in sorted(by_doc.items()):
            print(f"  {doc}")
            for f in items:
                print(f"    line {f.line}: `{f.ref}`")
        print()
        print("A reference resolves against the repo root, the doc's own directory or any")
        print("ancestor of it, or a directory named in backticks by the nearest heading.")
        print()
        print("To fix, in order of preference:")
        print("  1. The file moved   -> update the path (that is the whole point of this check).")
        print("  2. The path is right but addressed from somewhere else -> add a section base:")
        print("       ## Section title (`some/dir/`)")
        print("     or a per-file override near the top of the doc:")
        print("       <!-- doc-links: base=some/dir -->")
        print("  3. It genuinely lives outside this repo (another repo, a runtime path under")
        print("     HERMES_HOME, a placeholder the reader will create) -> add it to")
        print("     EXEMPT_REFS in scripts/ci/check_doc_links.py *with a reason*.")
        print()
        print("Run locally: python3 scripts/ci/check_doc_links.py --stats")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
