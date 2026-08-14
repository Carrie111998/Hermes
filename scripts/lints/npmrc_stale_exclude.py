"""Stale ``min-release-age-exclude[]`` entries in ``.npmrc`` files.

The ``min-release-age`` gate blocks npm from *resolving* package versions
younger than N days (supply-chain quarantine). An exclude exists to let
resolution pick a specific younger release — usually a vuln fix whose
comment literally says "remove when > 2wks old". Once every locked
version of the excluded package is itself older than N days, resolution
succeeds without the exclude: it is dead weight and a standing hole in
the age gate for that package. This lint automates exactly the removal
instruction those comments carry.

Semantics per exclude pattern (exact name, ``@scope/*``, or trailing
glob), matched against the sibling lockfile's locked packages:

- pattern matches no locked package  -> stale (nothing to exempt)
- every matched locked version was published more than
  ``min-release-age`` (+1 day boundary grace) days ago -> stale
- any registry fetch fails -> pattern skipped (fail open: never remove
  an exclude on partial data; worst case it survives one merge cycle)

Every exclude is temporary by construction. An exclude admits one
release that the age gate holds back, and it expires when that release
grows older than the gate. A permanent exclude is a permanent hole.
This lint has no opt-out marker for that reason. To keep an entry, give
the package a reason to need it.

The fixer deletes the stale exclude lines. It deletes the comment
block of a group only when it removed every exclude in that group.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from lints import REPO_ROOT, Finding, Lint

REGISTRY = "https://registry.npmjs.org"
# One day of grace above min-release-age so a package hovering at the
# boundary doesn't get its exclude removed and re-added across runs.
GRACE = timedelta(days=1)

_EXCLUDE_RE = re.compile(r"^min-release-age-exclude\[\]=(?P<pattern>\S+)\s*$")
_MIN_AGE_RE = re.compile(r"^min-release-age=(?P<days>\d+)\s*$")


@dataclass
class ExcludeGroup:
    """A contiguous comment block plus the exclude lines under it."""

    comment_idxs: list[int] = field(default_factory=list)
    # (line_idx, pattern) pairs
    entries: list[tuple[int, str]] = field(default_factory=list)


def parse_npmrc(text: str) -> tuple[int | None, list[ExcludeGroup]]:
    """Extract ``min-release-age`` and exclude groups from .npmrc text.

    A group is the contiguous run of ``#`` comment lines directly above
    an exclude (no blank line or other directive in between) plus every
    exclude line contiguous below it.
    """
    lines = text.splitlines()
    min_age: int | None = None
    groups: list[ExcludeGroup] = []
    pending_comments: list[int] = []
    current: ExcludeGroup | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if m := _MIN_AGE_RE.match(stripped):
            min_age = int(m.group("days"))
            pending_comments = []
            current = None
        elif m := _EXCLUDE_RE.match(stripped):
            if current is None:
                current = ExcludeGroup(comment_idxs=pending_comments)
                groups.append(current)
                pending_comments = []
            current.entries.append((i, m.group("pattern")))
        elif stripped.startswith("#"):
            if current is not None:
                # Comment directly after excludes starts a new block.
                current = None
            pending_comments.append(i)
        else:
            # Blank line or other directive breaks both runs.
            pending_comments = []
            current = None

    return min_age, groups


def locked_versions(lockfile: Path) -> dict[str, set[str]]:
    """``name -> {versions}`` from a v2/v3 package-lock.json.

    Keys in ``packages`` are install paths (``node_modules/<name>``,
    possibly nested); the package name is everything after the last
    ``node_modules/`` segment.
    """
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for path, meta in data.get("packages", {}).items():
        if not path or "node_modules/" not in path:
            continue
        name = path.rsplit("node_modules/", 1)[1]
        version = meta.get("version")
        if name and version:
            out.setdefault(name, set()).add(version)
    return out


def _fetch_times(name: str) -> dict[str, str]:
    """``version -> ISO publish date`` from the npm registry."""
    url = f"{REGISTRY}/{quote(name, safe='@/')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("time", {})


def _parse_iso(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate_file(
    npmrc: Path,
    lockfile: Path,
    fetch_times: Callable[[str], dict[str, str]],
    now: datetime | None = None,
) -> list[tuple[int, str, str]]:
    """Return stale entries as ``(line_idx, pattern, reason)``."""
    now = now or datetime.now(timezone.utc)
    min_age, groups = parse_npmrc(npmrc.read_text(encoding="utf-8"))
    if min_age is None or not groups:
        return []
    if not lockfile.exists():
        return []
    locked = locked_versions(lockfile)
    threshold = now - timedelta(days=min_age) - GRACE

    times_cache: dict[str, dict[str, str] | None] = {}

    def times_for(name: str) -> dict[str, str] | None:
        if name not in times_cache:
            try:
                times_cache[name] = fetch_times(name)
            except Exception:  # noqa: BLE001 — fail open on any fetch error
                times_cache[name] = None
        return times_cache[name]

    stale: list[tuple[int, str, str]] = []
    for group in groups:
        for line_idx, pattern in group.entries:
            matched = {
                name: versions
                for name, versions in locked.items()
                if fnmatch(name, pattern)
            }
            if not matched:
                stale.append(
                    (
                        line_idx,
                        pattern,
                        "matches no package in the lockfile — nothing to exempt",
                    )
                )
                continue

            all_old = True
            fetch_failed = False
            for name, versions in matched.items():
                times = times_for(name)
                if times is None:
                    fetch_failed = True
                    break
                for version in versions:
                    published = _parse_iso(times.get(version, ""))
                    if published is None or published > threshold:
                        # Unknown date counts as young — fail open.
                        all_old = False
                        break
                if not all_old:
                    break

            if fetch_failed or not all_old:
                continue
            stale.append(
                (
                    line_idx,
                    pattern,
                    f"every locked version is older than min-release-age "
                    f"({min_age}d) — the age gate no longer needs this exclude",
                )
            )
    return stale


def remove_stale_lines(text: str, stale_idxs: set[int]) -> str:
    """Delete the stale exclude lines. Drop the comment block of a
    group only when no exclude in that group survives."""
    _, groups = parse_npmrc(text)
    to_delete = set(stale_idxs)
    for group in groups:
        entry_idxs = {i for i, _ in group.entries}
        if entry_idxs and entry_idxs <= to_delete:
            to_delete.update(group.comment_idxs)

    lines = text.splitlines()
    kept = [line for i, line in enumerate(lines) if i not in to_delete]
    out: list[str] = []
    # Collapse the double blank lines that full-group deletion leaves.
    for line in kept:
        if line.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(line)
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _tracked_npmrc_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.npmrc", "**/.npmrc", ".npmrc"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return [REPO_ROOT / p for p in dict.fromkeys(out.stdout.split())]


def _evaluate_repo() -> list[tuple[Path, list[tuple[int, str, str]]]]:
    results = []
    for npmrc in _tracked_npmrc_files():
        lockfile = npmrc.parent / "package-lock.json"
        stale = evaluate_file(npmrc, lockfile, _fetch_times)
        if stale:
            results.append((npmrc, stale))
    return results


def check() -> list[Finding]:
    findings = []
    for npmrc, stale in _evaluate_repo():
        rel = npmrc.relative_to(REPO_ROOT).as_posix()
        for line_idx, pattern, reason in stale:
            findings.append(
                Finding(
                    lint_id="npmrc-stale-exclude",
                    path=rel,
                    line=line_idx + 1,
                    message=(
                        f"min-release-age-exclude[]={pattern} is stale: {reason}."
                    ),
                    fixable=True,
                )
            )
    return findings


def _verify_only_exclude_deletions(old: str, new: str) -> None:
    """Abort unless ``new`` is ``old`` minus deletions of exclude lines,
    comment lines, and blank lines — the only edits this fixer makes.

    Structural guarantee, not a diff heuristic: every surviving line must
    appear in the original in order (pure-deletion check), and every
    dropped line must be an exclude entry, a comment, or blank. Any other
    delta means a bug in remove_stale_lines, and the file must not be
    written.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()

    i = 0
    dropped: list[str] = []
    for line in old_lines:
        if i < len(new_lines) and new_lines[i] == line:
            i += 1
        else:
            dropped.append(line)
    if i != len(new_lines):
        raise RuntimeError(
            "npmrc-stale-exclude: fixer produced lines not present in the "
            "original file — refusing to write"
        )

    for line in dropped:
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if _EXCLUDE_RE.match(stripped):
            continue
        raise RuntimeError(
            "npmrc-stale-exclude: the fixer would delete the directive "
            f"{stripped!r}, which is not an exclude. Refused the write."
        )


def fix() -> list[str]:
    changed = []
    for npmrc, stale in _evaluate_repo():
        text = npmrc.read_text(encoding="utf-8")
        new_text = remove_stale_lines(text, {idx for idx, _, _ in stale})
        if new_text != text:
            _verify_only_exclude_deletions(text, new_text)
            npmrc.write_text(new_text, encoding="utf-8")
            changed.append(npmrc.relative_to(REPO_ROOT).as_posix())
    return changed


LINT = Lint(
    id="npmrc-stale-exclude",
    description=(
        "min-release-age-exclude[] entries whose locked versions have all "
        "outgrown min-release-age are dead weight in the age gate."
    ),
    severity="blocking",
    autofix=True,
    check=check,
    fix=fix,
    network=True,
    fix_touches=("*.npmrc",),
)
