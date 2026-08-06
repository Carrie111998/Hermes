#!/usr/bin/env python3
"""Validate repository-owned AGENTS context budgets and navigable links."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit

MAX_ROOT_CHARS = 18_000
GOVERNING_FILES = ("AGENTS.md", "apps/desktop/AGENTS.md")
REQUIRED_REFERENCES = (
    "CONTRIBUTING.md",
    "docs/development/contribution-rubric.md",
    "docs/development/architecture-core.md",
    "docs/development/architecture-tui.md",
    "docs/development/configuration.md",
    "docs/development/plugins.md",
    "docs/development/skills-authoring.md",
    "docs/development/skins.md",
    "docs/development/subsystems.md",
    "docs/development/testing.md",
    "apps/desktop/AGENTS.md",
    "apps/desktop/DESIGN.md",
    "website/docs/developer-guide/gateway-internals.md",
    "gateway/platforms/ADDING_A_PLATFORM.md",
    "website/docs/developer-guide/adding-tools.md",
)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _mask_excluded_markdown(markdown: str, *, source: str) -> str:
    """Mask fenced/inline code and HTML comments while preserving positions."""
    output: list[str] = []
    fence_char: str | None = None
    fence_len = 0
    in_comment = False
    inline_ticks = 0

    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        marker_char = stripped[:1]
        marker_len = 0
        if indent <= 3 and marker_char in {"`", "~"}:
            marker_len = len(stripped) - len(stripped.lstrip(marker_char))

        if fence_char is not None:
            output.append("\n" if line.endswith("\n") else "")
            if marker_char == fence_char and marker_len >= fence_len:
                fence_char = None
                fence_len = 0
            continue
        if not in_comment and inline_ticks == 0 and marker_len >= 3:
            fence_char = marker_char
            fence_len = marker_len
            output.append("\n" if line.endswith("\n") else "")
            continue

        i = 0
        masked = list(line)
        while i < len(line):
            if in_comment:
                end = line.find("-->", i)
                if end < 0:
                    for j in range(i, len(line)):
                        if line[j] != "\n":
                            masked[j] = " "
                    i = len(line)
                    continue
                for j in range(i, end + 3):
                    masked[j] = " "
                in_comment = False
                i = end + 3
                continue

            if inline_ticks:
                delimiter = "`" * inline_ticks
                end = line.find(delimiter, i)
                if end < 0:
                    for j in range(i, len(line)):
                        if line[j] != "\n":
                            masked[j] = " "
                    i = len(line)
                    continue
                for j in range(i, end + inline_ticks):
                    masked[j] = " "
                inline_ticks = 0
                i = end + len(delimiter)
                continue

            if line.startswith("<!--", i):
                end = line.find("-->", i + 4)
                if end < 0:
                    for j in range(i, len(line)):
                        if line[j] != "\n":
                            masked[j] = " "
                    in_comment = True
                    i = len(line)
                    continue
                for j in range(i, end + 3):
                    masked[j] = " "
                i = end + 3
                continue

            if line[i] == "`" and not _is_escaped(line, i):
                ticks = len(line[i:]) - len(line[i:].lstrip("`"))
                inline_ticks = ticks
                for j in range(i, i + ticks):
                    masked[j] = " "
                i += ticks
                continue
            i += 1
        output.append("".join(masked))

    if fence_char is not None:
        raise ValueError(f"{source}: unterminated fenced code block")
    if in_comment:
        raise ValueError(f"{source}: unterminated HTML comment")
    if inline_ticks:
        raise ValueError(f"{source}: unterminated inline code span")
    return "".join(output)


def extract_navigable_links(markdown: str, *, source: str) -> list[str]:
    """Return ordinary inline Markdown link destinations.

    Images, escaped syntax, code spans/blocks, and HTML comments are excluded.
    Malformed candidate links fail closed instead of being silently ignored.
    """
    text = _mask_excluded_markdown(markdown, source=source)
    links: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "[" or _is_escaped(text, i):
            i += 1
            continue
        if i > 0 and text[i - 1] == "!" and not _is_escaped(text, i - 1):
            i += 1
            continue

        close_label = i + 1
        while close_label < len(text):
            if text[close_label] == "]" and not _is_escaped(text, close_label):
                break
            close_label += 1
        if close_label >= len(text) or close_label + 1 >= len(text) or text[close_label + 1] != "(":
            i += 1
            continue

        cursor = close_label + 2
        depth = 1
        destination_end = cursor
        while destination_end < len(text):
            char = text[destination_end]
            if char == "(" and not _is_escaped(text, destination_end):
                depth += 1
            elif char == ")" and not _is_escaped(text, destination_end):
                depth -= 1
                if depth == 0:
                    break
            destination_end += 1
        if depth:
            raise ValueError(f"{source}: unterminated Markdown link at character {i}")

        raw = text[cursor:destination_end].strip()
        if raw.startswith("<"):
            angle_end = raw.find(">")
            if angle_end < 0:
                raise ValueError(f"{source}: malformed angle-bracket link at character {i}")
            destination = raw[1:angle_end]
        else:
            destination = raw.split(maxsplit=1)[0] if raw else ""
        if not destination:
            raise ValueError(f"{source}: empty Markdown link at character {i}")
        links.append(destination)
        i = destination_end + 1
    return links


def _tracked_files(repo_root: Path) -> tuple[set[str], str | None]:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        return {
            os.fsdecode(item) for item in completed.stdout.split(b"\0") if item
        }, None
    except (OSError, subprocess.CalledProcessError) as exc:
        return set(), f"could not enumerate tracked files: {exc}"


def _resolve_relative_target(
    repo_root: Path,
    source_path: Path,
    destination: str,
) -> tuple[str | None, str | None]:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or destination.startswith("#"):
        return None, None
    relative = unquote(parsed.path)
    if not relative:
        return None, None
    candidate = (source_path.parent / relative).resolve()
    try:
        normalized = candidate.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None, f"{source_path.relative_to(repo_root)} link {destination!r} escapes repository root"
    return normalized, None


def validate_repository(
    repo_root: Path,
    *,
    max_root_chars: int = MAX_ROOT_CHARS,
    required_references: Sequence[str] = REQUIRED_REFERENCES,
) -> list[str]:
    """Return all repository AGENTS context contract violations."""
    repo_root = repo_root.resolve()
    errors: list[str] = []
    tracked, git_error = _tracked_files(repo_root)
    if git_error:
        return [git_error]

    contents: dict[str, str] = {}
    for relative in GOVERNING_FILES:
        path = repo_root / relative
        if relative not in tracked or not path.is_file() or path.is_symlink():
            errors.append(f"required governing file is missing: {relative}")
            continue
        try:
            contents[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"could not read {relative}: {exc}")

    root = contents.get("AGENTS.md")
    if root is not None and len(root) > max_root_chars:
        errors.append(
            f"AGENTS.md has {len(root):,} characters; exceeds {max_root_chars:,}-character contract"
        )

    resolved_root_targets: set[str] = set()
    for relative, markdown in contents.items():
        source_path = repo_root / relative
        try:
            links = extract_navigable_links(markdown, source=relative)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for destination in links:
            normalized, resolution_error = _resolve_relative_target(
                repo_root, source_path, destination
            )
            if resolution_error:
                errors.append(resolution_error)
                continue
            if normalized is None:
                continue
            target = repo_root / normalized
            if (
                normalized not in tracked
                or not target.is_file()
                or target.is_symlink()
            ):
                errors.append(
                    f"{relative} link {destination!r} does not resolve to a tracked regular file"
                )
                continue
            if relative == "AGENTS.md":
                resolved_root_targets.add(normalized)

    for required in required_references:
        if required not in resolved_root_targets:
            errors.append(f"required navigable link is missing: {required}")
    return errors


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of scripts/)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    repo_root = _parse_args(argv).repo_root.resolve()
    errors = validate_repository(repo_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    root_chars = len((repo_root / "AGENTS.md").read_text(encoding="utf-8"))
    print(
        f"PASS: AGENTS context contract ({root_chars:,}/{MAX_ROOT_CHARS:,} root characters; "
        f"{len(REQUIRED_REFERENCES)} required links resolved)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
