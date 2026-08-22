#!/usr/bin/env python3
"""Mechanical weekly vault health scan with a compact Telegram result."""

from __future__ import annotations

import argparse
import collections
import re
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from foundation_cron_common import atomic_write_text, local_now, parse_datetime

LINK_PATTERN = re.compile(r"!?\[\[([^\]]+)\]\]")
VALID_STATUSES = {
    "active",
    "draft",
    "archived",
    "superseded",
    "pending",
    "sent",
    "approved",
    "deprecated",
}
KNOWN_CONTEXT_KEYS = {
    "status",
    "canonical",
    "supersedes",
    "superseded_by",
    "updated",
    "source",
    "purpose",
    "scope",
    "tags",
    "aliases",
    "cssclasses",
    "generated_by",
    "generated_at",
}
EXCLUDED_PREFIXES = (".obsidian/", ".trash/", "Hermes/Briefs/", "Hermes/Reports/")
ORPHAN_EXEMPT_NAMES = {"readme", "index", "inbox", "outbox", "journal", "home"}
MAX_DETAILS = 200


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _notes(root: Path) -> list[Path]:
    notes: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        relative = _relative(path, root)
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        notes.append(path)
    return sorted(notes)


def _frontmatter(path: Path) -> tuple[dict | None, str | None]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None, "opening frontmatter delimiter has no closing delimiter"
    try:
        value = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        return None, f"malformed YAML: {str(exc).splitlines()[0]}"
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "frontmatter must be a mapping"
    return value, None


def _metadata_problems(path: Path) -> tuple[list[str], list[str]]:
    metadata, parse_error = _frontmatter(path)
    if parse_error:
        return [parse_error], []
    if metadata is None:
        return [], []
    problems: list[str] = []
    status = metadata.get("status")
    if status is not None and (not isinstance(status, str) or status.casefold() not in VALID_STATUSES):
        problems.append(f"invalid defined status value: {status!r}")
    canonical = metadata.get("canonical")
    if canonical is not None and not isinstance(canonical, bool):
        problems.append("canonical must be true or false")
    for key in ("supersedes", "superseded_by"):
        value = metadata.get(key)
        if value is not None and not isinstance(value, (str, list)):
            problems.append(f"{key} must be a string or list")
    updated = metadata.get("updated")
    if updated is not None and not isinstance(updated, (str, date, datetime)):
        problems.append("updated must be a date or string")
    unknown = [str(key) for key in metadata if str(key) not in KNOWN_CONTEXT_KEYS]
    return problems, unknown


def scan(root: Path) -> dict[str, object]:
    notes = _notes(root)
    by_relative: dict[str, Path] = {}
    by_stem: dict[str, list[Path]] = collections.defaultdict(list)
    for path in notes:
        relative = _relative(path, root)
        by_relative[relative.casefold()] = path
        by_relative[relative[:-3].casefold()] = path
        by_stem[path.stem.casefold()].append(path)

    inbound: collections.Counter[Path] = collections.Counter()
    outbound: collections.Counter[Path] = collections.Counter()
    broken: list[str] = []
    metadata_issues: list[str] = []
    unknown_counts: collections.Counter[str] = collections.Counter()

    for path in notes:
        relative = _relative(path, root)
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_link in LINK_PATTERN.findall(text):
            target = raw_link.split("|", 1)[0].split("#", 1)[0].strip()
            if not target or "://" in target:
                continue
            normalized = target.replace("\\", "/").removesuffix(".md").casefold()
            candidates: list[Path] = []
            exact = by_relative.get(normalized) or by_relative.get(f"{normalized}.md")
            if exact:
                candidates = [exact]
            elif "/" not in normalized:
                candidates = by_stem.get(normalized, [])
            if not candidates:
                broken.append(f"{relative} -> [[{raw_link}]]")
            else:
                outbound[path] += 1
                for candidate in candidates:
                    inbound[candidate] += 1

        problems, unknown = _metadata_problems(path)
        metadata_issues.extend(f"{relative}: {problem}" for problem in problems)
        unknown_counts.update(unknown)

    orphans = [
        _relative(path, root)
        for path in notes
        if not inbound[path]
        and not outbound[path]
        and path.stem.casefold() not in ORPHAN_EXEMPT_NAMES
    ]
    repeated_unknown = sorted((key, count) for key, count in unknown_counts.items() if count >= 2)
    return {
        "notes_scanned": len(notes),
        "broken_links": sorted(broken),
        "orphans": sorted(orphans),
        "metadata_issues": sorted(metadata_issues),
        "repeated_unknown_keys": repeated_unknown,
    }


def _section(title: str, items: list[str]) -> str:
    if not items:
        return f"## {title}\n\nNone.\n"
    visible = items[:MAX_DETAILS]
    suffix = f"\n- …and {len(items) - MAX_DETAILS} more" if len(items) > MAX_DETAILS else ""
    return f"## {title}\n\n" + "\n".join(f"- {item}" for item in visible) + suffix + "\n"


def render_report(result: dict[str, object], now: datetime) -> str:
    unknown = [f"`{key}` ({count} files)" for key, count in result["repeated_unknown_keys"]]
    return (
        "---\n"
        "status: observational\n"
        "generated_by: hermes\n"
        f"generated_at: {now.isoformat()}\n"
        "purpose: Mechanical weekly vault-health diagnostic; not a source of truth.\n"
        "---\n\n"
        f"# Weekly Vault Health — {now.date().isoformat()}\n\n"
        f"Notes scanned: {result['notes_scanned']}\n\n"
        + _section("Broken wikilinks", result["broken_links"])
        + "\n"
        + _section("Orphan candidates", result["orphans"])
        + "\n"
        + _section("Defined metadata problems", result["metadata_issues"])
        + "\n"
        + _section(
            "Repeated unknown metadata keys (informational candidates, not errors)", unknown
        )
        + "\nUnknown/custom keys are descriptive-only under the canonical glossary and are not counted as failures.\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    report_dir = args.report_dir or args.vault_root / "Hermes/Reports/Vault Health"
    try:
        result = scan(args.vault_root)
        report_path = report_dir / f"{now.date().isoformat()}.md"
        if not args.no_write:
            atomic_write_text(report_path, render_report(result, now))
        print(f"🧹 Weekly Vault Health — {now.date().isoformat()}")
        print(
            f"{result['notes_scanned']} notes scanned; "
            f"{len(result['broken_links'])} broken links; "
            f"{len(result['orphans'])} orphan candidates; "
            f"{len(result['metadata_issues'])} metadata problems."
        )
        print(f"Full diagnostic: {report_path}")
    except Exception as exc:
        print(f"🩺 Operations Alert\n- weekly vault-health scan crashed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
