#!/usr/bin/env python3
"""Repository lint for agent context-file budgets.

Hermes loads project context in two distinct ways, with two distinct budgets:

* **Startup context** — one *selected* chain per session. A single context
  type wins for the whole load (``.hermes.md`` first, then the ``AGENTS.md``
  chain from git root down to the working directory, then ``CLAUDE.md``, then
  ``.cursorrules``), and each source is head/tail truncated above
  ``context_file_max_chars``. Truncation is silent to the reader: the middle
  of the file simply stops governing the session.
* **Progressive subdirectory hints** — every context file in a directory the
  agent later visits, appended to the tool result and truncated above 8,000
  characters.

This lint models that *selection*: it never sums every candidate file, because
most candidates are shadowed and never load at all. It stays repository-only —
no project registry, no home-directory traversal, no portfolio policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# Hermes' shipped ``context_file_max_chars`` fallback for one startup context
# source. Repositories that pin a different value pass ``--startup-cap``.
DEFAULT_STARTUP_CAP = 40_000

# ── Pinned fallbacks ────────────────────────────────────────────────────────
# These mirror Hermes' loader constants for a standalone CI checkout that has
# no importable ``agent`` package. Inside a Hermes checkout the live constants
# win, so the lint cannot drift away from the loader it guards.
_FALLBACK_HERMES_MD_NAMES = (".hermes.md", "HERMES.md")
_FALLBACK_HINT_FILENAMES = (
    "AGENTS.override.md",
    "AGENTS.md", "agents.md",
    "CLAUDE.md", "claude.md",
    ".cursorrules",
)
_FALLBACK_EXCLUDED_DIR_NAMES = frozenset({
    "node_modules", "venv", ".venv", "__pycache__",
    ".git", ".hg", ".svn",
    ".Trash", ".cache", ".tox", ".mypy_cache", ".pytest_cache",
    "site-packages", "dist-packages",
    "backups", "backup", ".backups",
    "vendor", "third_party",
})
_FALLBACK_NESTED_MAX = 8_000


def _import_hermes_constants():
    """Read the live loader constants. Raises when Hermes is not importable."""
    from agent.prompt_builder import _HERMES_MD_NAMES
    from agent.subdirectory_hints import (
        _EXCLUDED_DIR_NAMES,
        _HINT_FILENAMES,
        _MAX_HINT_CHARS,
    )

    return {
        "source": "hermes",
        "hermes_md_names": tuple(_HERMES_MD_NAMES),
        "hint_filenames": tuple(_HINT_FILENAMES),
        "excluded_dir_names": frozenset(_EXCLUDED_DIR_NAMES),
        "nested_max": _MAX_HINT_CHARS,
    }


def resolve_constants(importer=None):
    """Prefer Hermes' own loader constants; fall back to the pinned literals."""
    try:
        return (importer or _import_hermes_constants)()
    except Exception:
        return {
            "source": "fallback",
            "hermes_md_names": _FALLBACK_HERMES_MD_NAMES,
            "hint_filenames": _FALLBACK_HINT_FILENAMES,
            "excluded_dir_names": _FALLBACK_EXCLUDED_DIR_NAMES,
            "nested_max": _FALLBACK_NESTED_MAX,
        }


_CONSTANTS = resolve_constants()

_HERMES_MD_NAMES = _CONSTANTS["hermes_md_names"]

# Progressively-discovered subdirectory context. Unlike startup loading, every
# matching filename in a visited directory is appended (no first-wins), so the
# lint must consider all of them.
_HINT_FILENAMES = _CONSTANTS["hint_filenames"]

# Directories that hold copies, caches, or vendored trees rather than
# authoritative context.
_EXCLUDED_DIR_NAMES = _CONSTANTS["excluded_dir_names"]

# SubdirectoryHintTracker's per-file hint budget. Above this a nested file is
# truncated to the first N characters and an explicit marker is appended.
DEFAULT_NESTED_MAX = _CONSTANTS["nested_max"]

# The startup chain merges AGENTS-family files; CLAUDE.md and .cursorrules
# load from the working directory only. Derived from the hint filenames so a
# new upstream name is picked up in the right bucket.
_AGENTS_MD_NAMES = tuple(n for n in _HINT_FILENAMES if n.lower().startswith("agents"))
_CLAUDE_MD_NAMES = tuple(n for n in _HINT_FILENAMES if n.lower().startswith("claude"))
_CURSORRULES_NAMES = tuple(
    n for n in _HINT_FILENAMES if n not in _AGENTS_MD_NAMES + _CLAUDE_MD_NAMES
)

# Startup priority: the first *type* with any match anywhere on the chain wins
# the entire load. Types below it never contribute, however large they are.
_STARTUP_TYPES = (
    _HERMES_MD_NAMES,
    _AGENTS_MD_NAMES,
    _CLAUDE_MD_NAMES,
    _CURSORRULES_NAMES,
)


def _identity(path: Path):
    """Filesystem identity of *path*, robust to case-insensitive volumes.

    macOS and Windows resolve ``AGENTS.md`` and ``agents.md`` to the same
    inode; without this a single real file would report as shadowing itself.
    """
    try:
        info = path.stat()
        return (info.st_dev, info.st_ino)
    except OSError:
        return (None, os.path.normcase(os.fspath(path)))


def _char_count(path: Path) -> int:
    """Decoded Unicode character count — the unit Hermes' caps are measured in."""
    return len(path.read_text(encoding="utf-8"))


def _read_nonempty(path: Path):
    """Return loader-visible content, or ``None`` for empty/unreadable files."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return content or None


def _digest_content(content: str) -> str:
    """SHA-256 of stripped content — Hermes' progressive-hint dedupe key."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _present_nonempty(directory: Path, names) -> list:
    """Distinct nonempty files matching *names*, in loader priority order."""
    found = []
    seen = set()
    for name in names:
        candidate = directory / name
        if not candidate.is_file():
            continue
        content = _read_nonempty(candidate)
        if content is None:
            continue
        key = _identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        found.append((candidate, content))
    return found


def _cursor_rule_candidates(cwd: Path) -> list:
    """Nonempty Cursor rules in the collective order used at startup."""
    found = _present_nonempty(cwd, _CURSORRULES_NAMES)
    seen = {_identity(path) for path, _content in found}
    rules_dir = cwd / ".cursor" / "rules"
    if not rules_dir.is_dir():
        return found
    for candidate in sorted(rules_dir.glob("*.mdc")):
        if not candidate.is_file():
            continue
        content = _read_nonempty(candidate)
        key = _identity(candidate)
        if content is None or key in seen:
            continue
        seen.add(key)
        found.append((candidate, content))
    return found


def _chain_directories(root: Path, cwd: Path) -> list:
    """Directories from *root* down to *cwd*, inclusive."""
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return [root]
    directories = [root]
    accumulated = root
    for part in rel.parts:
        accumulated = accumulated / part
        directories.append(accumulated)
    return directories


def _new_entry(path: Path, content: str, label: str):
    return {
        "path": path,
        "content": content,
        "label": label,
        "duplicates": [],
        "shadowed": [],
    }


def _agents_label(path: Path, cwd: Path) -> str:
    if path.parent == cwd:
        return path.name
    return os.path.relpath(path, cwd)


def _startup_selection(root: Path, cwd: Path):
    """Resolve the selected startup chain.

    Empty files do not win. AGENTS files merge root-to-cwd and dedupe exact
    content; Claude variants use the first nonempty file; Cursor rules load as
    one collective source. Returns entries, off-chain shadowed candidates, the
    winning kind, and the directory chain.
    """
    directories = _chain_directories(root, cwd)
    candidates = {}
    for directory in directories:
        candidates[directory] = _present_nonempty(
            directory, sum(_STARTUP_TYPES, ())
        )
    candidates[cwd].extend(
        pair for pair in _cursor_rule_candidates(cwd)
        if pair[0].name != ".cursorrules"
    )

    hermes_entries = []
    for directory in reversed(directories):
        matches = _present_nonempty(directory, _HERMES_MD_NAMES)
        if matches:
            path, content = matches[0]
            hermes_entries = [_new_entry(path, content, path.name)]
            break

    agents_entries = []
    agents_by_content = {}
    agents_by_identity = {}
    for directory in directories:
        matches = _present_nonempty(directory, _AGENTS_MD_NAMES)
        if not matches:
            continue
        path, content = matches[0]
        target = agents_by_identity.get(_identity(path)) or agents_by_content.get(
            content
        )
        if target is not None:
            target["duplicates"].append(path)
            continue
        entry = _new_entry(path, content, _agents_label(path, cwd))
        agents_entries.append(entry)
        agents_by_content[content] = entry
        agents_by_identity[_identity(path)] = entry

    claude_entries = []
    if matches := _present_nonempty(cwd, _CLAUDE_MD_NAMES):
        path, content = matches[0]
        claude_entries = [_new_entry(path, content, path.name)]

    cursor_entries = [
        _new_entry(path, content, path.relative_to(cwd).as_posix())
        for path, content in _cursor_rule_candidates(cwd)
    ]

    choices = (
        ("hermes", hermes_entries),
        ("agents", agents_entries),
        ("claude", claude_entries),
        ("cursor", cursor_entries),
    )
    winning_kind = None
    entries = []
    for kind, choice in choices:
        if choice:
            winning_kind = kind
            entries = choice
            break
    if winning_kind is None:
        return [], [], None, directories

    accounted_paths = {
        os.fspath(path)
        for entry in entries
        for path in [entry["path"], *entry["duplicates"]]
    }
    remaining = [
        path
        for directory in directories
        for path, _content in candidates[directory]
        if os.fspath(path) not in accounted_paths
    ]
    off_chain = []
    for path in remaining:
        same_directory = next(
            (entry for entry in entries if entry["path"].parent == path.parent),
            None,
        )
        if same_directory is None:
            off_chain.append(path)
        else:
            same_directory["shadowed"].append(path)
    return entries, off_chain, winning_kind, directories


def _working_dir_hint_seed(cwd: Path):
    """Return the first existing CWD hint and its nonempty content.

    SubdirectoryHintTracker stops at the first existing filename even when that
    file is empty. Startup selection instead falls through empty files, so this
    seed must be computed independently.
    """
    seen = set()
    for name in _HINT_FILENAMES:
        candidate = cwd / name
        if not candidate.is_file():
            continue
        key = _identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        return candidate, _read_nonempty(candidate)
    return None, None


def _nested_candidates(root: Path, skip_dirs, seen_by_digest, seen_by_identity):
    """Progressively-discoverable context files below *root*.

    Directories already accounted for by the startup chain are skipped. Files
    whose content Hermes already injected fold into the original entry's
    ``duplicates`` provenance rather than disappearing from the report.
    """
    found = []
    for directory, dirnames, _filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
        directory = Path(directory)
        if directory in skip_dirs:
            continue
        matches = _present_nonempty(directory, _HINT_FILENAMES)
        if not matches:
            continue
        candidate, content = matches[0]
        target = seen_by_identity.get(_identity(candidate))
        digest = _digest_content(content)
        if target is None:
            target = seen_by_digest.get(digest)
        if target is not None:
            target["duplicates"].append(candidate)
            continue
        entry = _new_entry(candidate, content, candidate.name)
        entry["shadowed"] = [path for path, _content in matches[1:]]
        seen_by_identity[_identity(candidate)] = entry
        seen_by_digest[digest] = entry
        found.append(entry)
    return found


def _startup_loaded_chars(kind: str, entries: list) -> int:
    section_lengths = [
        len(f"## {entry['label']}\n\n{entry['content']}") for entry in entries
    ]
    if kind == "cursor":
        return sum(length + 2 for length in section_lengths)
    return sum(section_lengths) + max(0, len(section_lengths) - 1) * 2


def scan(
    root,
    cwd=None,
    startup_cap: int = DEFAULT_STARTUP_CAP,
    nested_max: int = DEFAULT_NESTED_MAX,
):
    """Return a JSON-serialisable context-budget report for *root*."""
    root = Path(root).resolve()
    cwd = root if cwd is None else Path(cwd).resolve()

    entries, off_chain, startup_kind, chain_directories = _startup_selection(
        root, cwd
    )

    def rel(path):
        return path.relative_to(root).as_posix()

    skip_dirs = set(chain_directories)
    seed_path, seed_content = _working_dir_hint_seed(cwd)
    seen_by_digest = {}
    seen_by_identity = {}
    if seed_path is not None and seed_content is not None:
        seed_target = next(
            (
                entry
                for entry in entries
                if _identity(entry["path"]) == _identity(seed_path)
                or entry["content"] == seed_content
            ),
            _new_entry(seed_path, seed_content, seed_path.name),
        )
        seen_by_digest[_digest_content(seed_content)] = seed_target
        seen_by_identity[_identity(seed_path)] = seed_target
    nested_entries = _nested_candidates(
        root, skip_dirs, seen_by_digest, seen_by_identity
    )

    startup_chain = [
        {
            "path": rel(entry["path"]),
            "chars": _char_count(entry["path"]),
            "loaded_chars": len(
                f"## {entry['label']}\n\n{entry['content']}"
            ),
            "shadowed": [rel(other) for other in entry["shadowed"]],
            "duplicates": [rel(other) for other in entry["duplicates"]],
        }
        for entry in entries
    ]
    nested = [
        {
            "path": rel(entry["path"]),
            "chars": _char_count(entry["path"]),
            "loaded_chars": len(entry["content"]),
            "shadowed": [rel(path) for path in entry["shadowed"]],
            "duplicates": [rel(dup) for dup in entry["duplicates"]],
        }
        for entry in nested_entries
    ]

    failures = [
        {
            "kind": "startup_over_cap",
            "path": entry["path"],
            "chars": entry["loaded_chars"],
            "source_chars": entry["chars"],
            "limit": startup_cap,
        }
        for entry in startup_chain
        if startup_kind != "cursor" and entry["loaded_chars"] > startup_cap
    ]
    startup_context_chars = (
        _startup_loaded_chars(startup_kind, entries) if startup_kind else 0
    )
    if (
        startup_kind == "cursor"
        or (startup_kind == "agents" and len(startup_chain) > 1)
    ) and startup_context_chars > startup_cap:
        failures.append(
            {
                "kind": "startup_chain_over_cap",
                "path": (
                    ".cursorrules + .cursor/rules/*.mdc"
                    if startup_kind == "cursor"
                    else "AGENTS.md (directory chain)"
                ),
                "chars": startup_context_chars,
                "limit": startup_cap,
            }
        )
    failures.extend([
        {
            "kind": "nested_over_cap",
            "path": entry["path"],
            "chars": entry["loaded_chars"],
            "limit": nested_max,
        }
        for entry in nested
        if entry["loaded_chars"] > nested_max
    ])

    return {
        "root": str(root),
        "cwd": str(cwd),
        "startup_cap": startup_cap,
        "nested_max": nested_max,
        "startup_kind": startup_kind,
        "startup_context_chars": startup_context_chars,
        "startup_chain": startup_chain,
        "shadowed_candidates": [rel(other) for other in off_chain],
        "nested": nested,
        "failures": failures,
        "ok": not failures,
    }


def _format_report(report) -> str:
    """Human-readable summary of failures and resolution provenance."""
    lines = []
    for entry in report["startup_chain"]:
        line = f"  startup  {entry['chars']:>7,}  {entry['path']}"
        if entry["shadowed"]:
            line += f"  (shadows {', '.join(entry['shadowed'])})"
        if entry["duplicates"]:
            line += f"  (identical copy at {', '.join(entry['duplicates'])})"
        lines.append(line)
    for entry in report["nested"]:
        line = f"  nested   {entry['chars']:>7,}  {entry['path']}"
        if entry["shadowed"]:
            line += f"  (shadows {', '.join(entry['shadowed'])})"
        if entry["duplicates"]:
            line += f"  (identical copy at {', '.join(entry['duplicates'])})"
        lines.append(line)
    if report["shadowed_candidates"]:
        lines.append(
            "  never loaded at startup: "
            + ", ".join(report["shadowed_candidates"])
        )
    return "\n".join(lines)


def _format_failures(report) -> str:
    lines = []
    for failure in report["failures"]:
        if failure["kind"] in {"startup_over_cap", "startup_chain_over_cap"}:
            reason = (
                "exceeds the startup context cap — Hermes preserves the head "
                "and tail with an explicit truncation marker; the middle is omitted"
            )
        else:
            reason = (
                "exceeds the subdirectory hint budget — Hermes preserves the "
                f"first {failure['limit']:,} characters and adds an explicit "
                "truncation marker; the remainder is omitted"
            )
        lines.append(
            f"  {failure['path']}: {failure['chars']:,} chars > "
            f"{failure['limit']:,} — {reason}"
        )
    return "\n".join(lines)


def _has_provenance(report) -> bool:
    return bool(
        report["shadowed_candidates"]
        or any(
            entry["shadowed"] or entry["duplicates"]
            for entry in [*report["startup_chain"], *report["nested"]]
        )
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check agent context files against Hermes' load budgets.",
    )
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument(
        "--cwd",
        default=None,
        help="model a startup working directory below the root (default: root)",
    )
    parser.add_argument(
        "--startup-cap",
        type=int,
        default=DEFAULT_STARTUP_CAP,
        help=f"per-source startup cap (default: {DEFAULT_STARTUP_CAP})",
    )
    parser.add_argument(
        "--nested-max",
        type=int,
        default=DEFAULT_NESTED_MAX,
        help=f"per-file subdirectory hint cap (default: {DEFAULT_NESTED_MAX})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON on stdout"
    )
    args = parser.parse_args(argv)

    report = scan(
        args.root,
        cwd=args.cwd,
        startup_cap=args.startup_cap,
        nested_max=args.nested_max,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["failures"] or _has_provenance(report):
        print(_format_report(report))

    if report["failures"]:
        print("context file budget exceeded:", file=sys.stderr)
        print(_format_failures(report), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
