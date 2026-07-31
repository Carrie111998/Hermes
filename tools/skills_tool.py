#!/usr/bin/env python3
"""
Skills Tool Module

This module provides tools for listing and viewing skill documents.
Skills are organized as directories containing a SKILL.md file (the main instructions)
and optional supporting files like references, templates, and examples.

Inspired by Anthropic's Claude Skills system with progressive disclosure architecture:
- Metadata (name ≤64 chars, description ≤1024 chars) - shown in skills_list
- Full Instructions - loaded via skill_view when needed
- Linked Files (references, templates) - loaded on demand

Directory Structure:
    skills/
    ├── my-skill/
    │   ├── SKILL.md           # Main instructions (required)
    │   ├── references/        # Supporting documentation
    │   │   ├── api.md
    │   │   └── examples.md
    │   ├── templates/         # Templates for output
    │   │   └── template.md
    │   └── assets/            # Supplementary files (agentskills.io standard)
    └── category/              # Category folder for organization
        └── another-skill/
            └── SKILL.md

SKILL.md Format (YAML Frontmatter, agentskills.io compatible):
    ---
    name: skill-name              # Required, max 64 chars
    description: Brief description # Required, max 1024 chars
    version: 1.0.0                # Optional
    license: MIT                  # Optional (agentskills.io)
    platforms: [macos]            # Optional — restrict to specific OS platforms
                                  #   Valid: macos, linux, windows
                                  #   Omit to load on all platforms (default)
    prerequisites:                # Optional — legacy runtime requirements
      env_vars: [API_KEY]         #   Legacy env var names are normalized into
                                  #   required_environment_variables on load.
      commands: [curl, jq]        #   Command checks remain advisory only.
    compatibility: Requires X     # Optional (agentskills.io)
    metadata:                     # Optional, arbitrary key-value (agentskills.io)
      hermes:
        tags: [fine-tuning, llm]
        related_skills: [peft, lora]
    ---

    # Skill Title

    Full instructions and content here...

Available tools:
- skills_list: List skills with metadata (progressive disclosure tier 1)
- skill_view: Load full skill content (progressive disclosure tier 2-3)

Usage:
    from tools.skills_tool import skills_list, skill_view, check_skills_requirements

    # List all skills (returns metadata only - token efficient)
    result = skills_list()

    # View a skill's main content (loads full instructions)
    content = skill_view("axolotl")

    # View a reference file within a skill (loads linked file)
    content = skill_view("axolotl", "references/dataset-formats.md")
"""

import errno
import json
import logging
import shlex
import stat
import tempfile
import time
from contextvars import ContextVar, Token

from hermes_constants import get_hermes_home, display_hermes_home
import os
import re
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Any, List, Optional, Set, Tuple

from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get
from utils import env_var_enabled
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS as _EXCLUDED_SKILL_DIRS,
    is_skill_support_path as _is_skill_support_path,
    parse_strict_fenced_frontmatter,
    read_strict_skill_index_file,
)

logger = logging.getLogger(__name__)
_OS_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", set())
_SECURE_PACKAGE_OPEN_SUPPORTED = (
    os.name == "nt"
    or (
        _OS_OPEN_SUPPORTS_DIR_FD
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
)

# Per-session skill discovery cache.  _find_all_skills() re-reads every
# SKILL.md on every call; with hundreds of skills this is wasteful.
# Cache validation (mirrors hermes_cli/profiles.py::_count_skills, d5eee133e):
#   - signature = per-dir max mtime of the dir AND its immediate children
#     (one scandir per dir; catches skill add/remove inside categories,
#     which does NOT bump the root dir's mtime), plus the disabled-set
#     (config-driven — changes with no filesystem mtime bump at all)
#   - a short TTL bounds staleness from in-place SKILL.md edits, which
#     bump only the file's mtime, invisible to any directory signature.
# skip_disabled True/False are cached separately.
_SKILLS_CACHE: dict = {}          # {cache_key: (signature, timestamp, skills_list)}
_SKILLS_CACHE_TTL_SECONDS = 30.0
_SKILLS_CACHE_KEY_DISABLED = "with_disabled"
_SKILLS_CACHE_KEY_FILTERED = "filtered"

# Linked-file manifests are discovery hints, not a directory dump. Keep each
# category bounded so a skill with a large assets tree cannot flood a tool
# result or slash-invocation message. Explicit ``skill_view(file_path=...)``
# remains unrestricted by this preview budget.
MAX_LINKED_FILES_PER_CATEGORY = 50
MAX_LINKED_FILE_PATH_CHARS = 8_000
_LINKED_FILE_CATEGORIES = ("references", "templates", "scripts", "assets")


def _skills_scan_signature(dirs_to_scan, disabled, *, on_error=None) -> tuple:
    """Cheap change-signature for the skill scan inputs.

    O(#dirs + #categories) stat calls, not a recursive walk. Includes the
    platform the scan's ``skill_matches_platform`` filter will use (read
    from ``agent.skill_utils``'s ``sys`` so test patches of that module
    are honored) — the scan result is platform-dependent.
    """
    from agent import skill_utils as _skill_utils

    platform = getattr(getattr(_skill_utils, "sys", None), "platform", "")
    sig = []
    for d in dirs_to_scan:
        try:
            m = d.stat().st_mtime
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
            continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            em = entry.stat(follow_symlinks=False).st_mtime
                            if em > m:
                                m = em
                    except OSError as exc:
                        if on_error is not None:
                            on_error(exc)
                        continue
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
        sig.append((str(d), m))
    return (
        tuple(sig),
        frozenset(disabled),
        platform,
        _skill_utils.get_skill_environment_fingerprint(),
    )


def _validate_configured_external_roots() -> tuple[List[Path], Optional[Dict[str, Any]]]:
    """Return scannable configured roots or a structured scope error.

    Configured roots are discovery inputs even when they are unavailable.
    Callers must not silently omit one and publish a local-only catalog.
    """
    from agent.skill_utils import get_external_skills_dirs

    roots = [Path(root) for root in get_external_skills_dirs()]
    for root in roots:
        try:
            root_stat = root.stat()
        except OSError as exc:
            return roots, {
                "success": False,
                "error_code": "skills_discovery_incomplete",
                "error": f"Configured external skills root is unavailable: {root}",
                "detail": str(exc),
            }
        if not stat.S_ISDIR(root_stat.st_mode):
            return roots, {
                "success": False,
                "error_code": "skills_discovery_incomplete",
                "error": f"Configured external skills root is not a directory: {root}",
            }
    return roots, None


def build_linked_files_manifest(
    skill_dir: Path,
) -> tuple[Dict[str, List[str]], Dict[str, Any]]:
    """Build a deterministic, bounded preview of a skill's support files.

    The manifest is intentionally shallow in output size while still walking
    nested support directories. Directory symlinks are not followed. The
    returned summary tells callers when additional files remain discoverable
    through an explicit ``skill_view(..., file_path=...)`` call.
    """
    linked_files: Dict[str, List[str]] = {}
    truncated_categories: List[str] = []
    shown = 0
    path_chars = 0

    for category in _LINKED_FILE_CATEGORIES:
        category_dir = skill_dir / category
        if category_dir.is_symlink() or not category_dir.is_dir():
            continue

        entries: List[str] = []
        category_truncated = False
        stop_category = False
        for root, dirs, files in os.walk(category_dir, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if not (Path(root) / d).is_symlink()
            )
            for filename in sorted(files):
                file_path = Path(root) / filename
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                rel = file_path.relative_to(skill_dir).as_posix()
                if (
                    len(entries) >= MAX_LINKED_FILES_PER_CATEGORY
                    or path_chars + len(rel) > MAX_LINKED_FILE_PATH_CHARS
                ):
                    category_truncated = True
                    stop_category = True
                    break
                entries.append(rel)
                shown += 1
                path_chars += len(rel)
            if stop_category:
                break

        if entries:
            linked_files[category] = entries
        if category_truncated:
            truncated_categories.append(category)

    return linked_files, {
        "shown": shown,
        "truncated": bool(truncated_categories),
        "truncated_categories": truncated_categories,
        "per_category_limit": MAX_LINKED_FILES_PER_CATEGORY,
        "path_char_limit": MAX_LINKED_FILE_PATH_CHARS,
    }


def _stat_signature(value) -> Tuple[int, int, int, int, int]:
    """Stable identity/content metadata used for bound skill-package reads."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _package_signature(path: Path) -> Tuple[int, int, int, int, int]:
    """Return package metadata from the same no-reparse primitive used to open it."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        with open_directory(path, writable=False) as package:
            return package.stat().signature
    return _stat_signature(path.stat())


def _bound_open_flags(*, directory: bool) -> int:
    """Return fail-closed POSIX flags for a package-relative open."""
    if not hasattr(os, "O_NOFOLLOW") or (directory and not hasattr(os, "O_DIRECTORY")):
        raise OSError("secure package-relative opens are unsupported on this platform")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_bound_package_dir(
    skill_dir: Path,
    expected_signature: Tuple[int, int, int, int, int],
) -> Any:
    """Open the already-discovered package and verify its directory identity."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        package_handle = open_directory(skill_dir, writable=False)
        try:
            if package_handle.stat().signature != expected_signature:
                raise RuntimeError("skill package changed during loading")
            return package_handle
        except Exception:
            package_handle.close()
            raise

    package_fd = os.open(skill_dir, _bound_open_flags(directory=True))
    try:
        if _stat_signature(os.fstat(package_fd)) != expected_signature:
            raise RuntimeError("skill package changed during loading")
        if not _OS_OPEN_SUPPORTS_DIR_FD:
            raise OSError("secure package-relative opens are unsupported")
        return package_fd
    except Exception:
        os.close(package_fd)
        raise


def _close_bound_handle(handle: Any) -> None:
    if os.name == "nt":
        handle.close()
    else:
        os.close(handle)


def _bound_handle_signature(handle: Any) -> Tuple[int, int, int, int, int]:
    if os.name == "nt":
        return handle.stat().signature
    return _stat_signature(os.fstat(handle))


def _bound_relative_parts(file_path: str) -> Tuple[str, ...]:
    """Normalize a support path without consulting the mutable package path."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("File path must be a non-empty relative path.")
    windows_path = PureWindowsPath(file_path)
    normalized = file_path.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    if (
        windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
    ):
        raise ValueError("File path must stay within the skill directory.")
    if ".." in posix_path.parts:
        raise ValueError("Path traversal ('..') is not allowed.")
    if any(part in ("", ".") for part in posix_path.parts):
        raise ValueError("File path must stay within the skill directory.")
    return tuple(posix_path.parts)


def _read_bound_support_file(
    package_fd: Any,
    file_path: str,
    expected_package_signature: Tuple[int, int, int, int, int],
) -> tuple[bytes, os.stat_result]:
    """Read a regular support file through openat without following symlinks."""
    parts = _bound_relative_parts(file_path)
    if os.name == "nt":
        from tools.nt_secure_fs_optional import read_regular_file

        current = package_fd
        opened = []
        try:
            for part in parts[:-1]:
                current = current.open_dir(part, writable=False)
                opened.append(current)
            payload, metadata = read_regular_file(current, parts[-1])
            if package_fd.stat().signature != expected_package_signature:
                raise RuntimeError("skill package changed during loading")
            return payload, metadata
        finally:
            for handle in reversed(opened):
                handle.close()

    current_fd = os.dup(package_fd)
    file_fd = None
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                _bound_open_flags(directory=True),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd

        file_fd = os.open(
            parts[-1],
            _bound_open_flags(directory=False),
            dir_fd=current_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Requested support path is not a regular file.")
        chunks = []
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if _stat_signature(before) != _stat_signature(after):
            raise RuntimeError("support file changed during loading")
        if _stat_signature(os.fstat(package_fd)) != expected_package_signature:
            raise RuntimeError("skill package changed during loading")
        return b"".join(chunks), after
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def _build_bound_linked_files_manifest(
    package_fd: Any,
    expected_package_signature: Tuple[int, int, int, int, int],
) -> tuple[Dict[str, List[str]], Dict[str, Any]]:
    """Build the linked-file preview entirely below a bound package dirfd."""
    linked_files: Dict[str, List[str]] = {}
    truncated_categories: List[str] = []
    shown = 0
    path_chars = 0

    if os.name == "nt":
        def walk_nt(directory, relative: str, entries: List[str]) -> bool:
            nonlocal shown, path_chars
            before = directory.stat().signature
            for entry in sorted(
                directory.list_entries(), key=lambda item: item.name.casefold()
            ):
                if entry.is_reparse:
                    continue
                rel = f"{relative}/{entry.name}"
                if entry.is_dir:
                    with directory.open_dir(
                        entry.name, writable=False
                    ) as child:
                        if walk_nt(child, rel, entries):
                            return True
                    continue
                if (
                    len(entries) >= MAX_LINKED_FILES_PER_CATEGORY
                    or path_chars + len(rel) > MAX_LINKED_FILE_PATH_CHARS
                ):
                    return True
                entries.append(rel)
                shown += 1
                path_chars += len(rel)
            if before != directory.stat().signature:
                raise RuntimeError(
                    "support directory changed while building manifest"
                )
            return False

        for category in _LINKED_FILE_CATEGORIES:
            try:
                category_handle = package_fd.open_dir(
                    category, writable=False
                )
            except (FileNotFoundError, NotADirectoryError):
                continue
            try:
                entries: List[str] = []
                category_truncated = walk_nt(
                    category_handle, category, entries
                )
            finally:
                category_handle.close()
            if entries:
                linked_files[category] = entries
            if category_truncated:
                truncated_categories.append(category)
        if package_fd.stat().signature != expected_package_signature:
            raise RuntimeError("skill package changed while building manifest")
        return linked_files, {
            "shown": shown,
            "truncated": bool(truncated_categories),
            "truncated_categories": truncated_categories,
            "per_category_limit": MAX_LINKED_FILES_PER_CATEGORY,
            "path_char_limit": MAX_LINKED_FILE_PATH_CHARS,
        }

    def _walk_bound_dir(
        directory_fd: int,
        prefix: str,
        entries: List[str],
    ) -> bool:
        nonlocal shown, path_chars
        before = _stat_signature(os.fstat(directory_fd))
        truncated = False
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for entry in children:
            entry_stat = entry.stat(follow_symlinks=False)
            rel = f"{prefix}/{entry.name}"
            if stat.S_ISLNK(entry_stat.st_mode):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                child_fd = os.open(
                    entry.name,
                    _bound_open_flags(directory=True),
                    dir_fd=directory_fd,
                )
                try:
                    if _walk_bound_dir(child_fd, rel, entries):
                        truncated = True
                        break
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            if (
                len(entries) >= MAX_LINKED_FILES_PER_CATEGORY
                or path_chars + len(rel) > MAX_LINKED_FILE_PATH_CHARS
            ):
                truncated = True
                break
            entries.append(rel)
            shown += 1
            path_chars += len(rel)
        if before != _stat_signature(os.fstat(directory_fd)):
            raise RuntimeError("skill support directory changed during loading")
        return truncated

    for category in _LINKED_FILE_CATEGORIES:
        try:
            category_fd = os.open(
                category,
                _bound_open_flags(directory=True),
                dir_fd=package_fd,
            )
        except OSError:
            # Missing, inaccessible, and symlinked support categories are not
            # part of the manifest, matching the historical path-based helper.
            continue
        try:
            entries: List[str] = []
            category_truncated = _walk_bound_dir(category_fd, category, entries)
        finally:
            os.close(category_fd)
        if entries:
            linked_files[category] = entries
        if category_truncated:
            truncated_categories.append(category)

    if _stat_signature(os.fstat(package_fd)) != expected_package_signature:
        raise RuntimeError("skill package changed during loading")
    return linked_files, {
        "shown": shown,
        "truncated": bool(truncated_categories),
        "truncated_categories": truncated_categories,
        "per_category_limit": MAX_LINKED_FILES_PER_CATEGORY,
        "path_char_limit": MAX_LINKED_FILE_PATH_CHARS,
    }


def _expand_inline_shell_bound(
    content: str,
    package_fd: Any,
    timeout: int,
    *,
    skill_dir: Path,
    session_id: Optional[str] = None,
    template_vars: bool = True,
) -> str:
    """Expand inline shell without reopening the mutable package path.

    Template variables in prose use the display path, while variables inside
    commands use a stable handle-relative location. This distinction must be
    made before command execution: globally substituting the package path and
    rewriting it afterwards reintroduces a package-swap race.
    """
    from agent import skill_preprocessing

    inline_shell_re = re.compile(r"!`([^`\n]+)`")

    def _substitute_prose(value: str) -> str:
        if not template_vars:
            return value
        pieces: List[str] = []
        cursor = 0
        for match in inline_shell_re.finditer(value):
            pieces.append(
                skill_preprocessing.substitute_template_vars(
                    value[cursor:match.start()], skill_dir, session_id
                )
            )
            pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(
            skill_preprocessing.substitute_template_vars(
                value[cursor:], skill_dir, session_id
            )
        )
        return "".join(pieces)

    content = _substitute_prose(content)

    snapshot_limits = {
        "max_depth": 32,
        "max_entries": 4096,
        "max_file_bytes": 16 * 1024 * 1024,
        "max_total_bytes": 64 * 1024 * 1024,
    }

    def _copy_posix_directory(
        source_fd: int,
        destination: Path,
        *,
        state: Optional[Dict[str, int]] = None,
        depth: int = 0,
    ) -> None:
        if depth > snapshot_limits["max_depth"]:
            raise OSError("skill snapshot exceeds the safe nesting limit")
        if state is None:
            state = {"entries": 0, "bytes": 0}
        before = _stat_signature(os.fstat(source_fd))
        destination.mkdir(mode=0o700)
        for entry in os.scandir(source_fd):
            state["entries"] += 1
            if state["entries"] > snapshot_limits["max_entries"]:
                raise OSError("skill snapshot exceeds the safe entry limit")
            entry_stat = os.stat(
                entry.name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
            target = destination / entry.name
            if stat.S_ISDIR(entry_stat.st_mode):
                child_fd = os.open(
                    entry.name,
                    _bound_open_flags(directory=True),
                    dir_fd=source_fd,
                )
                try:
                    if _stat_signature(os.fstat(child_fd)) != (
                        _stat_signature(entry_stat)
                    ):
                        raise RuntimeError(
                            "skill package changed while snapshotting"
                        )
                    _copy_posix_directory(
                        child_fd,
                        target,
                        state=state,
                        depth=depth + 1,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise OSError(
                    f"refusing non-regular inline-shell input: {entry.name}"
                )
            file_fd = os.open(
                entry.name,
                _bound_open_flags(directory=False)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=source_fd,
            )
            try:
                file_before = os.fstat(file_fd)
                if not stat.S_ISREG(file_before.st_mode):
                    raise OSError(
                        f"refusing non-regular inline-shell input: "
                        f"{entry.name}"
                    )
                if _stat_signature(file_before) != _stat_signature(entry_stat):
                    raise RuntimeError(
                        "skill file changed while snapshotting"
                    )
                if file_before.st_size > snapshot_limits["max_file_bytes"]:
                    raise OSError(
                        f"skill snapshot file is too large: {entry.name}"
                    )
                with target.open("xb") as destination_file:
                    copied = 0
                    while True:
                        chunk = os.read(file_fd, 64 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if (
                            copied > snapshot_limits["max_file_bytes"]
                            or state["bytes"] + copied
                            > snapshot_limits["max_total_bytes"]
                        ):
                            raise OSError(
                                "skill snapshot exceeds the safe byte limit"
                            )
                        destination_file.write(chunk)
                if _stat_signature(os.fstat(file_fd)) != (
                    _stat_signature(file_before)
                ):
                    raise RuntimeError(
                        "skill file changed while snapshotting"
                    )
                state["bytes"] += copied
                target.chmod(stat.S_IMODE(file_before.st_mode))
            finally:
                os.close(file_fd)
        if _stat_signature(os.fstat(source_fd)) != before:
            raise RuntimeError("skill package changed while snapshotting")

    # Both platforms execute from a handle-derived snapshot. Besides avoiding
    # path replacement, this keeps ${HERMES_SKILL_DIR} stable even when a
    # command changes cwd before using it.
    with tempfile.TemporaryDirectory(
        prefix=".skill-inline-"
    ) as temp_dir:
        snapshot = Path(temp_dir) / "skill"
        if os.name == "nt":
            from tools.nt_secure_fs_optional import copy_tree_no_reparse

            copy_tree_no_reparse(package_fd, snapshot)
        else:
            _copy_posix_directory(package_fd, snapshot)

        snapshot_shell_path = (
            str(snapshot).replace("\\", "/")
            if os.name == "nt"
            else str(snapshot)
        )

        def _escape_shell_value(
            value: str,
            *,
            in_single_quote: bool,
            in_double_quote: bool,
        ) -> str:
            if in_single_quote:
                return value.replace("'", "'\"'\"'")
            if in_double_quote:
                return (
                    value.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("$", "\\$")
                    .replace("`", "\\`")
                )
            return shlex.quote(value)

        def _substitute_command_template_vars(command: str) -> str:
            if not template_vars:
                return command
            values = {
                "${HERMES_SKILL_DIR}": snapshot_shell_path,
                "${HERMES_SESSION_ID}": (
                    str(session_id) if session_id is not None else None
                ),
            }
            rendered: List[str] = []
            index = 0
            in_single_quote = False
            in_double_quote = False
            escaped = False
            while index < len(command):
                matched_token = next(
                    (
                        token
                        for token in values
                        if command.startswith(token, index)
                    ),
                    None,
                )
                if matched_token is not None:
                    value = values[matched_token]
                    if value is None:
                        rendered.append(matched_token)
                    else:
                        rendered.append(
                            _escape_shell_value(
                                value,
                                in_single_quote=in_single_quote,
                                in_double_quote=in_double_quote,
                            )
                        )
                    index += len(matched_token)
                    continue

                character = command[index]
                rendered.append(character)
                if escaped:
                    escaped = False
                elif character == "\\" and not in_single_quote:
                    escaped = True
                elif character == "'" and not in_double_quote:
                    in_single_quote = not in_single_quote
                elif character == '"' and not in_single_quote:
                    in_double_quote = not in_double_quote
                index += 1
            return "".join(rendered)

        def _replace_command_literal_path(
            command: str,
            bound_path: str,
        ) -> str:
            rendered: List[str] = []
            index = 0
            in_single_quote = False
            in_double_quote = False
            escaped = False
            bound_length = len(bound_path)
            while index < len(command):
                candidate = command[index:index + bound_length]
                matches = (
                    candidate.casefold() == bound_path.casefold()
                    if os.name == "nt"
                    else candidate == bound_path
                )
                if matches:
                    rendered.append(
                        _escape_shell_value(
                            snapshot_shell_path,
                            in_single_quote=in_single_quote,
                            in_double_quote=in_double_quote,
                        )
                    )
                    index += bound_length
                    continue
                character = command[index]
                rendered.append(character)
                if escaped:
                    escaped = False
                elif character == "\\" and not in_single_quote:
                    escaped = True
                elif character == "'" and not in_double_quote:
                    in_single_quote = not in_single_quote
                elif character == '"' and not in_single_quote:
                    in_double_quote = not in_double_quote
                index += 1
            return "".join(rendered)

        def _bind_command(match: re.Match) -> str:
            command = match.group(1)
            command = _substitute_command_template_vars(command)
            # A skill may predate the template token and embed its own package
            # path literally. Keep that legacy form bound too; otherwise a
            # rename/recreate race can make `cd <skill_dir>` enter an attacker
            # replacement even though cwd initially points at the snapshot.
            bound_path_spellings = {str(skill_dir)}
            if os.name == "nt":
                bound_path_spellings.add(str(package_fd.final_path()))
                bound_path_spellings.update(
                    path.replace("\\", "/")
                    for path in tuple(bound_path_spellings)
                )
            for bound_path in sorted(
                bound_path_spellings, key=len, reverse=True
            ):
                command = _replace_command_literal_path(
                    command, bound_path
                )
            return f"!`{command}`"

        bound_content = inline_shell_re.sub(_bind_command, content)
        expanded_content = skill_preprocessing.expand_inline_shell(
            bound_content, snapshot, timeout
        )
        # Preserve the historical user-facing path in command output (notably
        # `pwd`) without ever exposing that mutable path to command execution.
        snapshot_spellings = {
            str(snapshot),
            str(snapshot.resolve()),
            snapshot.as_posix(),
        }
        if os.name == "nt":
            windows_snapshot = PureWindowsPath(snapshot)
            if windows_snapshot.drive.endswith(":"):
                drive = windows_snapshot.drive[0].lower()
                posix_snapshot = windows_snapshot.as_posix()
                snapshot_spellings.add(
                    f"/{drive}{posix_snapshot[len(windows_snapshot.drive):]}"
                )
        for snapshot_spelling in sorted(
            snapshot_spellings, key=len, reverse=True
        ):
            expanded_content = expanded_content.replace(
                snapshot_spelling, str(skill_dir)
            )
        return expanded_content


# All skills live in ~/.hermes/skills/ (seeded from bundled skills/ on install).
# This is the single source of truth -- agent edits, hub installs, and bundled
# skills all coexist here without polluting the git repo.
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Return the active profile's skills directory at call time.

    Some long-lived runtimes import this module before the active profile has
    set HERMES_HOME. Keep the legacy SKILLS_DIR module attribute for tests and
    external patchers, but when it has not been patched, resolve from the live
    profile-scoped HERMES_HOME on every call.
    """
    configured = Path(SKILLS_DIR)
    if configured != _SKILLS_DIR_AT_IMPORT:
        return configured
    return get_hermes_home() / "skills"


# Anthropic-recommended limits for progressive disclosure efficiency
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

# Platform identifiers for the 'platforms' frontmatter field.
# Maps user-friendly names to sys.platform prefixes.
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_ENV_BACKENDS = frozenset(
    {"docker", "singularity", "modal", "ssh", "daytona", "vercel_sandbox"}
)
_secret_capture_callback = None
# Keep the historical process default for CLI callers, but isolate gateway
# turns: multiple desktop sessions can ask for different secrets concurrently.
_secret_capture_callback_context: ContextVar = ContextVar(
    "skills_secret_capture_callback", default=None
)


def _skill_lookup_path_error(name: str) -> Optional[str]:
    """Return an error if a local skill lookup *name* can escape search roots.

    The skill ``name`` is joined onto each trusted search dir to build the
    on-disk lookup path, so it must stay relative and free of ``..`` segments —
    otherwise ``name="../outside"`` or an absolute path could select a skill
    (and read files) outside the skills directory. Mirrors the ``file_path``
    validation done later via ``tools.path_security``. We also reject Windows
    drive paths (e.g. ``C:\\skills``), whose ``:`` would otherwise be misread as
    a plugin namespace separator.
    """
    from tools.path_security import has_traversal_component

    if not isinstance(name, str):
        return "Skill name must be a string."
    candidate = name.strip()
    if (
        PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
        or PureWindowsPath(candidate).drive
    ):
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def load_env() -> Dict[str, str]:
    """Load profile-scoped environment variables from HERMES_HOME/.env."""
    env_path = get_hermes_home() / ".env"
    env_vars: Dict[str, str] = {}
    if not env_path.exists():
        return env_vars

    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip("\"'")
    return env_vars


class SkillReadinessStatus(str, Enum):
    AVAILABLE = "available"
    SETUP_NEEDED = "setup_needed"
    UNSUPPORTED = "unsupported"


# Prompt injection detection — shared by local-skill and plugin-skill paths.
_INJECTION_PATTERNS: list = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]


def set_secret_capture_callback(callback) -> None:
    """Set the legacy process-default callback (CLI compatibility)."""
    global _secret_capture_callback
    _secret_capture_callback = callback


def set_secret_capture_callback_context(callback) -> Token:
    """Install a secret callback for only the current thread/context."""
    return _secret_capture_callback_context.set(callback)


def reset_secret_capture_callback_context(token: Token) -> None:
    """Restore the prior thread/context-local callback."""
    _secret_capture_callback_context.reset(token)


def _current_secret_capture_callback():
    return _secret_capture_callback_context.get() or _secret_capture_callback


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Check if a skill is compatible with the current OS platform.

    Delegates to ``agent.skill_utils.skill_matches_platform`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import skill_matches_platform as _impl
    return _impl(frontmatter)


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """Check if a skill is relevant to the current runtime environment.

    Delegates to ``agent.skill_utils.skill_matches_environment`` — kept here
    as a public re-export so existing callers don't need updating. This is an
    offer-time relevance gate (kanban/docker/s6), NOT a hard-compatibility gate;
    explicit skill loads bypass it.
    """
    from agent.skill_utils import skill_matches_environment as _impl
    return _impl(frontmatter)


def _normalize_prerequisite_values(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item) for item in value if str(item).strip()]


def _collect_prerequisite_values(
    frontmatter: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    prereqs = frontmatter.get("prerequisites")
    if not prereqs or not isinstance(prereqs, dict):
        return [], []
    return (
        _normalize_prerequisite_values(prereqs.get("env_vars")),
        _normalize_prerequisite_values(prereqs.get("commands")),
    )


def _normalize_setup_metadata(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    setup = frontmatter.get("setup")
    if not isinstance(setup, dict):
        return {"help": None, "collect_secrets": []}

    help_text = setup.get("help")
    normalized_help = (
        str(help_text).strip()
        if isinstance(help_text, str) and help_text.strip()
        else None
    )

    collect_secrets_raw = setup.get("collect_secrets")
    if isinstance(collect_secrets_raw, dict):
        collect_secrets_raw = [collect_secrets_raw]
    if not isinstance(collect_secrets_raw, list):
        collect_secrets_raw = []

    collect_secrets: List[Dict[str, Any]] = []
    for item in collect_secrets_raw:
        if not isinstance(item, dict):
            continue

        env_var = str(item.get("env_var") or "").strip()
        if not env_var:
            continue

        prompt = str(item.get("prompt") or f"Enter value for {env_var}").strip()
        provider_url = str(item.get("provider_url") or item.get("url") or "").strip()

        entry: Dict[str, Any] = {
            "env_var": env_var,
            "prompt": prompt,
            "secret": bool(item.get("secret", True)),
        }
        if provider_url:
            entry["provider_url"] = provider_url
        collect_secrets.append(entry)

    return {
        "help": normalized_help,
        "collect_secrets": collect_secrets,
    }


def _get_required_environment_variables(
    frontmatter: Dict[str, Any],
    legacy_env_vars: List[str] | None = None,
) -> List[Dict[str, Any]]:
    setup = _normalize_setup_metadata(frontmatter)
    required_raw = frontmatter.get("required_environment_variables")
    if isinstance(required_raw, dict):
        required_raw = [required_raw]
    if not isinstance(required_raw, list):
        required_raw = []

    required: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _append_required(entry: Dict[str, Any]) -> None:
        env_name = str(entry.get("name") or entry.get("env_var") or "").strip()
        if not env_name or env_name in seen:
            return
        if not _ENV_VAR_NAME_RE.match(env_name):
            return

        normalized: Dict[str, Any] = {
            "name": env_name,
            "prompt": str(entry.get("prompt") or f"Enter value for {env_name}").strip(),
        }

        help_text = (
            entry.get("help")
            or entry.get("provider_url")
            or entry.get("url")
            or setup.get("help")
        )
        if isinstance(help_text, str) and help_text.strip():
            normalized["help"] = help_text.strip()

        required_for = entry.get("required_for")
        if isinstance(required_for, str) and required_for.strip():
            normalized["required_for"] = required_for.strip()

        if entry.get("optional"):
            normalized["optional"] = True

        seen.add(env_name)
        required.append(normalized)

    for item in required_raw:
        if isinstance(item, str):
            _append_required({"name": item})
            continue
        if isinstance(item, dict):
            _append_required(item)

    for item in setup["collect_secrets"]:
        _append_required(
            {
                "name": item.get("env_var"),
                "prompt": item.get("prompt"),
                "help": item.get("provider_url") or setup.get("help"),
            }
        )

    if legacy_env_vars is None:
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
    for env_var in legacy_env_vars:
        _append_required({"name": env_var})

    return required


def _capture_required_environment_variables(
    skill_name: str,
    missing_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not missing_entries:
        return {
            "missing_names": [],
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    missing_names = [entry["name"] for entry in missing_entries]
    # Most gateway surfaces (messaging platforms) can't prompt for a secret, so
    # they short-circuit to the "unsupported" hint. Interactive gateway surfaces
    # — the desktop app / TUI — set HERMES_INTERACTIVE and register a
    # secret-capture callback that routes to a secure secret.request overlay, so
    # they fall through and actually prompt. (HERMES_INTERACTIVE is the same flag
    # tools/approval.py uses to tell an interactive surface from a messaging one.)
    if _is_gateway_surface() and not env_var_enabled("HERMES_INTERACTIVE"):
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": _gateway_setup_hint(),
        }

    callback = _current_secret_capture_callback()
    if callback is None:
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    setup_skipped = False
    remaining_names: List[str] = []

    for entry in missing_entries:
        metadata = {"skill_name": skill_name}
        if entry.get("help"):
            metadata["help"] = entry["help"]
        if entry.get("required_for"):
            metadata["required_for"] = entry["required_for"]

        try:
            callback_result = callback(
                entry["name"],
                entry["prompt"],
                metadata,
            )
        except Exception:
            logger.warning(
                f"Secret capture callback failed for {entry['name']}", exc_info=True
            )
            callback_result = {
                "success": False,
                "stored_as": entry["name"],
                "validated": False,
                "skipped": True,
            }

        success = isinstance(callback_result, dict) and bool(
            callback_result.get("success")
        )
        skipped = isinstance(callback_result, dict) and bool(
            callback_result.get("skipped")
        )
        if success and not skipped:
            continue

        setup_skipped = True
        remaining_names.append(entry["name"])

    return {
        "missing_names": remaining_names,
        "setup_skipped": setup_skipped,
        "gateway_setup_hint": None,
    }


def _is_gateway_surface() -> bool:
    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return True
    from gateway.session_context import get_session_env
    return bool(get_session_env("HERMES_SESSION_PLATFORM"))


def _get_terminal_backend_name() -> str:
    return str(os.getenv("TERMINAL_ENV", "local")).strip().lower() or "local"


def _is_env_var_persisted(
    var_name: str, env_snapshot: Dict[str, str] | None = None
) -> bool:
    if env_snapshot is None:
        env_snapshot = load_env()
    if var_name in env_snapshot:
        return bool(env_snapshot.get(var_name))
    return bool(os.getenv(var_name))


def _remaining_required_environment_names(
    required_env_vars: List[Dict[str, Any]],
    capture_result: Dict[str, Any],
    *,
    env_snapshot: Dict[str, str] | None = None,
) -> List[str]:
    missing_names = set(capture_result["missing_names"])

    if env_snapshot is None:
        env_snapshot = load_env()
    remaining = []
    for entry in required_env_vars:
        name = entry["name"]
        if entry.get("optional"):
            continue
        if name in missing_names or not _is_env_var_persisted(name, env_snapshot):
            remaining.append(name)
    return remaining


def _gateway_setup_hint() -> str:
    try:
        from gateway.platforms.base import GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE

        return GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE
    except Exception:
        return f"Secure secret entry is not available. Load this skill in the local CLI to be prompted, or add the key to {display_hermes_home()}/.env manually."


def _build_setup_note(
    readiness_status: SkillReadinessStatus,
    missing: List[str],
    setup_help: str | None = None,
) -> str | None:
    if readiness_status == SkillReadinessStatus.SETUP_NEEDED:
        missing_str = ", ".join(missing) if missing else "required prerequisites"
        note = f"Setup needed before using this skill: missing {missing_str}."
        if setup_help:
            return f"{note} {setup_help}"
        return note
    return None


def check_skills_requirements() -> bool:
    """Skills are always available -- the directory is created on first use if needed."""
    return True


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Delegates to ``agent.skill_utils.parse_frontmatter`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import parse_frontmatter
    return parse_frontmatter(content)


def _get_category_from_path(skill_path: Path) -> Optional[str]:
    """
    Extract category from skill path based on directory structure.

    For paths like: ~/.hermes/skills/mlops/axolotl/SKILL.md -> "mlops"
    Also works for external skill dirs configured via skills.external_dirs.
    """
    # Try the active profile skills dir first (respects monkeypatching in tests),
    # then fall back to external dirs from config.
    dirs_to_check = [_skills_dir()]
    try:
        from agent.skill_utils import get_external_skills_dirs
        dirs_to_check.extend(get_external_skills_dirs())
    except Exception:
        pass
    for skills_dir in dirs_to_check:
        try:
            rel_path = skill_path.relative_to(skills_dir)
            parts = rel_path.parts
            if len(parts) >= 3:
                return parts[0]
        except ValueError:
            continue
    return None


def _parse_tags(tags_value) -> List[str]:
    """
    Parse tags from frontmatter value.

    Handles:
    - Already-parsed list (from yaml.safe_load): [tag1, tag2]
    - String with brackets: "[tag1, tag2]"
    - Comma-separated string: "tag1, tag2"

    Args:
        tags_value: Raw tags value — may be a list or string

    Returns:
        List of tag strings
    """
    if not tags_value:
        return []

    # yaml.safe_load already returns a list for [tag1, tag2]
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]

    # String fallback — handle bracket-wrapped or comma-separated
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]

    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]



def _get_disabled_skill_names() -> Set[str]:
    """Load disabled skill names from config.

    Delegates to ``agent.skill_utils.get_disabled_skill_names`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import get_disabled_skill_names
    return get_disabled_skill_names()


def _get_session_platform() -> str:
    """Resolve the current platform from gateway session context.

    Mirrors the platform-resolution logic in
    ``agent.skill_utils.get_disabled_skill_names`` so that
    ``_is_skill_disabled`` respects ``HERMES_SESSION_PLATFORM``.
    """
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM") or ""
    except Exception:
        return ""


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """Check if a skill is disabled in config.

    Resolves the active platform from (in order of precedence):
    1. Explicit ``platform`` argument
    2. ``HERMES_PLATFORM`` environment variable
    3. ``HERMES_SESSION_PLATFORM`` from gateway session context
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        skills_cfg = config.get("skills", {})
        resolved_platform = platform or os.getenv("HERMES_PLATFORM") or _get_session_platform()
        global_disabled = skills_cfg.get("disabled", [])
        if resolved_platform:
            platform_disabled = cfg_get(skills_cfg, "platform_disabled", resolved_platform)
            if platform_disabled is not None:
                # A globally-disabled skill stays disabled on every platform;
                # the platform list adds to it rather than replacing it. Keep
                # in sync with agent.skill_utils.get_disabled_skill_names.
                return name in platform_disabled or name in global_disabled
        return name in global_disabled
    except Exception:
        return False


def _find_all_skills(*, skip_disabled: bool = False) -> List[Dict[str, Any]]:
    """Recursively find all skills in ~/.hermes/skills/ and external dirs.

    Args:
        skip_disabled: If True, return ALL skills regardless of disabled
            state (used by ``hermes skills`` config UI). Default False
            filters out disabled skills.

    Returns:
        List of skill metadata dicts (name, description, category).

    Results are cached per-session; the cache is invalidated when the scan
    signature changes (dir/category mtimes or the disabled-set) and expires
    after a short TTL to bound staleness from in-place SKILL.md edits.
    """
    from agent.skill_utils import get_external_skills_dirs, iter_skill_index_files

    cache_kind = (
        _SKILLS_CACHE_KEY_DISABLED
        if skip_disabled
        else _SKILLS_CACHE_KEY_FILTERED
    )

    # Load disabled set once (not per-skill). Part of the cache signature:
    # disabling a skill is a config change with no filesystem mtime bump.
    disabled = set() if skip_disabled else _get_disabled_skill_names()

    scan_incomplete = False
    external_root_failed = False

    def _mark_scan_incomplete(error) -> None:
        nonlocal scan_incomplete
        scan_incomplete = True
        logger.debug("Skills discovery scan incomplete: %s", error)

    # Collect directories to scan — same resolution as the scan loop below
    # (_skills_dir() resolves the LIVE profile HERMES_HOME; the module-level
    # SKILLS_DIR can be stale in long-lived runtimes). Keep the active root in
    # the cache scope even when it cannot be stat'ed: otherwise an I/O error
    # can silently turn a local+external catalog into an external-only cache.
    dirs_to_scan: list = []
    active_skills_dir = _skills_dir()
    scope_dirs: list = [active_skills_dir]
    try:
        active_stat = active_skills_dir.stat()
    except FileNotFoundError:
        # A missing local skills root is an ordinary empty catalog, not a
        # partial scan. skills_list creates it later when appropriate.
        pass
    except OSError as exc:
        _mark_scan_incomplete(exc)
    else:
        if stat.S_ISDIR(active_stat.st_mode):
            dirs_to_scan.append(active_skills_dir)
        else:
            _mark_scan_incomplete(
                NotADirectoryError(
                    f"Local skills root is not a directory: {active_skills_dir}"
                )
            )

    external_dirs = get_external_skills_dirs()
    scope_dirs.extend(external_dirs)
    for external_dir in external_dirs:
        try:
            external_stat = external_dir.stat()
        except OSError as exc:
            external_root_failed = True
            _mark_scan_incomplete(exc)
            continue
        if not stat.S_ISDIR(external_stat.st_mode):
            external_root_failed = True
            _mark_scan_incomplete(
                NotADirectoryError(
                    f"External skills root is not a directory: {external_dir}"
                )
            )
            continue
        dirs_to_scan.append(external_dir)

    # Keep last-good results scoped to the exact discovery inputs. In
    # particular, a failed scan after a profile/root switch must never return
    # the old profile's skills just because both use the same cache kind.
    from agent import skill_utils as _skill_utils

    platform = getattr(getattr(_skill_utils, "sys", None), "platform", "")
    cache_key = (
        cache_kind,
        tuple(str(directory) for directory in scope_dirs),
        frozenset(disabled),
        platform,
        _skill_utils.get_skill_environment_fingerprint(),
    )
    signature = _skills_scan_signature(
        dirs_to_scan, disabled, on_error=_mark_scan_incomplete
    )
    now = time.monotonic()

    cached = _SKILLS_CACHE.get(cache_key)
    if (
        not scan_incomplete
        and cached is not None
        and cached[0] == signature
        and (now - cached[1]) < _SKILLS_CACHE_TTL_SECONDS
    ):
        # Per-call shallow copies: callers mutate the returned dicts
        # (e.g. web_server annotates s["enabled"]/s["usage"]) — handing
        # out the cached objects would poison the cache for everyone else.
        return [dict(s) for s in cached[2]]

    skills = []
    seen_names: set = set()

    # Scan local dir first, then external dirs (local takes precedence) —
    # dirs_to_scan already resolved above for the signature.
    for scan_dir in dirs_to_scan:
        for skill_md in iter_skill_index_files(
            scan_dir, "SKILL.md", on_error=_mark_scan_incomplete
        ):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue

            skill_dir = skill_md.parent

            try:
                # Do not follow a canonical SKILL.md symlink.  A package may
                # be discovered through a configured category symlink, but
                # its active entry file itself must remain in that package.
                _, frontmatter, body = read_strict_skill_index_file(skill_md)

                if not skill_matches_platform(frontmatter):
                    continue

                if not skill_matches_environment(frontmatter):
                    continue

                name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
                if name in seen_names:
                    continue
                if name in disabled:
                    continue

                description = frontmatter.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break

                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."

                category = _get_category_from_path(skill_md)

                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": description,
                    "category": category,
                })

            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
                _mark_scan_incomplete(e)
                continue
            except Exception as e:
                logger.debug(
                    "Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True
                )
                _mark_scan_incomplete(e)
                continue

    if scan_incomplete:
        # Do not cache a catalog that omitted a directory or file. A same-scope
        # completed catalog is safe to serve until the transient failure clears;
        # otherwise return no listing rather than a misleading partial one.
        # A configured external root that is itself unavailable is stricter:
        # advertising its old entries is not a usable catalog, so fail closed
        # even when an exact-scope cache exists.
        if external_root_failed:
            return []
        if cached is not None:
            return [dict(s) for s in cached[2]]
        return []

    # Store in cache keyed by the scan signature computed BEFORE the scan
    # (a write racing the scan changes the signature, so the next call
    # re-scans rather than serving the torn result past the TTL). Same
    # shallow-copy contract as the hit path — the caller may mutate.
    _SKILLS_CACHE[cache_key] = (signature, now, skills)
    return [dict(s) for s in skills]


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: str = None, task_id: str = None) -> str:
    """
    List all available skills (progressive disclosure tier 1 - minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view() to
    load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops")
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with minimal skill info: name, description, category
    """
    try:
        active_skills_dir = _skills_dir()
        _external_roots, external_root_error = _validate_configured_external_roots()
        if external_root_error is not None:
            return json.dumps(external_root_error, ensure_ascii=False)
        try:
            active_stat = active_skills_dir.stat()
        except FileNotFoundError:
            active_skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": f"Local skills root is unavailable: {active_skills_dir}",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        else:
            if not stat.S_ISDIR(active_stat.st_mode):
                return json.dumps(
                    {
                        "success": False,
                        "error_code": "skills_discovery_incomplete",
                        "error": f"Local skills root is not a directory: {active_skills_dir}",
                    },
                    ensure_ascii=False,
                )

        # Find all skills
        all_skills = _find_all_skills()

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found in skills/ directory.",
                },
                ensure_ascii=False,
            )

        # Filter by category if specified
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        # Sort by category then name
        all_skills = _sort_skills(all_skills)

        # Extract unique categories
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        return json.dumps(
            {
                "success": True,
                "skills": all_skills,
                "categories": categories,
                "count": len(all_skills),
                "hint": "Use skill_view(name) to see full content, tags, and linked files",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return tool_error(str(e), success=False)


# ── Plugin skill serving ──────────────────────────────────────────────────


def _serve_plugin_skill(
    skill_md: Path,
    namespace: str,
    bare: str,
    *,
    file_path: str | None = None,
    preprocess: bool = True,
    session_id: str | None = None,
) -> str:
    """Read a plugin skill through a bound package snapshot.

    Plugin registry entries are paths, not capabilities.  Never use the
    registry path for a later ``read_text``/preprocessor cwd: a directory or
    symlink replacement between those operations could otherwise serve one
    package and execute another.
    """
    from hermes_cli.plugins import _get_disabled_plugins, get_plugin_manager

    if namespace in _get_disabled_plugins():
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Plugin '{namespace}' is disabled. "
                    f"Re-enable with: hermes plugins enable {namespace}"
                ),
            },
            ensure_ascii=False,
        )

    if not _SECURE_PACKAGE_OPEN_SUPPORTED:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Secure plugin-skill package binding is unsupported on "
                    "this platform; refusing inline-capable plugin skill."
                ),
            },
            ensure_ascii=False,
        )

    try:
        # Resolve once, then bind the resolved directory with O_NOFOLLOW and
        # compare the descriptor identity to the pre-open snapshot.  This is
        # the same package-identity contract as local skill_view.
        package_path = skill_md.parent.resolve(strict=True)
        package_signature = _package_signature(package_path)
        package_fd = _open_bound_package_dir(package_path, package_signature)
        try:
            skill_bytes, _ = _read_bound_support_file(
                package_fd, skill_md.name, package_signature
            )
            content = skill_bytes.decode("utf-8")
            linked_files, linked_files_summary = _build_bound_linked_files_manifest(
                package_fd, package_signature
            )
            support_bytes = None
            support_stat = None
            support_error = None
            if file_path:
                try:
                    support_bytes, support_stat = _read_bound_support_file(
                        package_fd,
                        file_path,
                        package_signature,
                    )
                except Exception as exc:
                    support_error = exc
        finally:
            _close_bound_handle(package_fd)
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Failed to bind skill '{namespace}:{bare}' to its "
                    f"package snapshot: {e}"
                ),
            },
            ensure_ascii=False,
        )

    try:
        parsed_frontmatter, _ = parse_strict_fenced_frontmatter(content)
        if parsed_frontmatter:
            # Retain parser compatibility side effects without accepting its
            # permissive malformed-YAML fallback as the authority.
            _parse_frontmatter(content)
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Plugin skill '{namespace}:{bare}' has invalid "
                    f"frontmatter: {e}"
                ),
            },
            ensure_ascii=False,
        )

    if not skill_matches_platform(parsed_frontmatter):
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{namespace}:{bare}' is not supported on this platform.",
                "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
            },
            ensure_ascii=False,
        )

    if file_path:
        if support_error is not None or support_bytes is None or support_stat is None:
            available = [
                entry
                for entries in linked_files.values()
                for entry in entries
            ]
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"File '{file_path}' is not safely readable within "
                        f"plugin skill '{namespace}:{bare}': {support_error}"
                    ),
                    "available_files": available,
                    "linked_files_summary": linked_files_summary,
                },
                ensure_ascii=False,
            )
        try:
            support_content = support_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps(
                {
                    "success": True,
                    "name": f"{namespace}:{bare}",
                    "file": file_path,
                    "content": (
                        f"[Binary file: {PurePosixPath(file_path).name}, "
                        f"size: {support_stat.st_size} bytes]"
                    ),
                    "is_binary": True,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "name": f"{namespace}:{bare}",
                "file": file_path,
                "content": support_content,
                "file_type": PurePosixPath(file_path.replace("\\", "/")).suffix,
            },
            ensure_ascii=False,
        )

    # Injection scan — log but still serve (matches local-skill behaviour)
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        logger.warning(
            "Plugin skill '%s:%s' contains patterns that may indicate prompt injection",
            namespace, bare,
        )

    description = str(parsed_frontmatter.get("description", ""))
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

    # Bundle context banner — tells the agent about sibling skills
    try:
        siblings = [
            s for s in get_plugin_manager().list_plugin_skills(namespace)
            if s != bare
        ]
        if siblings:
            sib_list = ", ".join(siblings)
            banner = (
                f"[Bundle context: This skill is part of the '{namespace}' plugin.\n"
                f"Sibling skills: {sib_list}.\n"
                f"Use qualified form to invoke siblings (e.g. {namespace}:{siblings[0]}).]\n\n"
            )
        else:
            banner = f"[Bundle context: This skill is part of the '{namespace}' plugin.]\n\n"
    except Exception:
        banner = ""

    rendered_content = content
    if preprocess:
        try:
            from agent import skill_preprocessing

            preprocessing_config = skill_preprocessing.load_skills_config()
            (
                template_vars_enabled,
                inline_shell_enabled,
                timeout,
            ) = skill_preprocessing.normalize_preprocessing_config(
                preprocessing_config
            )
            if inline_shell_enabled:
                package_fd = _open_bound_package_dir(package_path, package_signature)
                try:
                    rendered_content = _expand_inline_shell_bound(
                        rendered_content,
                        package_fd,
                        timeout,
                        skill_dir=package_path,
                        session_id=session_id,
                        template_vars=template_vars_enabled,
                    )
                finally:
                    _close_bound_handle(package_fd)
            elif template_vars_enabled:
                rendered_content = skill_preprocessing.substitute_template_vars(
                    rendered_content, package_path, session_id
                )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Could not preprocess plugin skill {namespace}:{bare} "
                        f"from its bound package: {e}"
                    ),
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "success": True,
            "name": f"{namespace}:{bare}",
            "content": f"{banner}{rendered_content}" if banner else rendered_content,
            "description": description,
            "skill_dir": str(package_path),
            "lookup_name": f"{namespace}:{bare}",
            "package_bound": True,
            "preprocessed": bool(preprocess),
            "linked_files": linked_files or None,
            "linked_files_summary": linked_files_summary,
            "readiness_status": SkillReadinessStatus.AVAILABLE.value,
        },
        ensure_ascii=False,
    )


def skill_view(
    name: str,
    file_path: str = None,
    task_id: str = None,
    preprocess: bool = True,
) -> str:
    """
    View the content of a skill or a specific file within a skill directory.

    Args:
        name: Name or path of the skill (e.g., "axolotl" or "03-fine-tuning/axolotl").
            Qualified names like "plugin:skill" resolve to plugin-provided skills.
        file_path: Optional path to a specific file within the skill (e.g., "references/api.md")
        task_id: Optional task identifier used to probe the active backend
        preprocess: Apply configured SKILL.md template and inline shell rendering
            to main skill content while its package snapshot remains bound.

    Returns:
        JSON string with skill content or error message
    """
    try:
        # Validate before the ':' qualified-name dispatch so a Windows drive
        # path (e.g. C:\skills\foo) can't be reinterpreted as a plugin
        # namespace, and so a traversal/absolute name never reaches the
        # search-dir join that builds direct_path below.
        lookup_error = _skill_lookup_path_error(name)
        if lookup_error:
            return json.dumps(
                {
                    "success": False,
                    "error": lookup_error,
                    "hint": "Use a skill name or relative path within the skills directory.",
                },
                ensure_ascii=False,
            )

        local_category_name: str | None = None
        # ── Qualified name dispatch (plugin skills) ──────────────────
        # Names containing ':' are routed to the plugin skill registry.
        # Bare names fall through to the existing flat-tree scan below.
        if ":" in name:
            from agent.skill_utils import is_valid_namespace, parse_qualified_name
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            namespace, bare = parse_qualified_name(name)
            if not is_valid_namespace(namespace):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Invalid namespace '{namespace}' in '{name}'. "
                            f"Namespaces must match [a-zA-Z0-9_-]+."
                        ),
                    },
                    ensure_ascii=False,
                )

            discover_plugins()  # idempotent
            pm = get_plugin_manager()
            plugin_skill_md = pm.find_plugin_skill(name)

            if plugin_skill_md is not None:
                if not plugin_skill_md.exists():
                    # Stale registry entry — file deleted out of band
                    pm.remove_plugin_skill(name)
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Skill '{name}' file no longer exists at "
                                f"{plugin_skill_md}. The registry entry has "
                                f"been cleaned up — try again after the "
                                f"plugin is reloaded."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return _serve_plugin_skill(
                    plugin_skill_md,
                    namespace,
                    bare,
                    file_path=file_path,
                    preprocess=preprocess,
                    session_id=task_id,
                )

            # Plugin exists but this specific skill is missing?
            available = pm.list_plugin_skills(namespace)
            if available:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Skill '{bare}' not found in plugin '{namespace}'.",
                        "available_skills": [f"{namespace}:{s}" for s in available],
                        "hint": f"The '{namespace}' plugin provides {len(available)} skill(s).",
                    },
                    ensure_ascii=False,
                )
            # Plugin itself not found — fall through to flat-tree scan.
            # Categorized local skills also use `category:skill` in config and
            # gateway prompts, so preserve that form and translate it to the
            # on-disk `category/skill` path during the local scan below.
            if bare:
                local_category_name = f"{namespace}/{bare}"

        # The categorized fall-through form (namespace/bare) joins onto each
        # search dir too; re-validate it since `bare` is not namespace-checked.
        if local_category_name:
            lookup_error = _skill_lookup_path_error(local_category_name)
            if lookup_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": lookup_error,
                        "hint": "Use a skill name or relative path within the skills directory.",
                    },
                    ensure_ascii=False,
                )

        # Build list of all skill directories to search. Validate the complete
        # configured external scope before looking for a local match; otherwise
        # a missing external root could hide a collision and load the wrong
        # skill.
        all_dirs = []
        active_skills_dir = _skills_dir()
        try:
            active_stat = active_skills_dir.stat()
        except FileNotFoundError:
            active_stat = None
        except OSError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": f"Local skills root is unavailable: {active_skills_dir}",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        if active_stat is not None and not stat.S_ISDIR(active_stat.st_mode):
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": f"Local skills root is not a directory: {active_skills_dir}",
                },
                ensure_ascii=False,
            )
        if active_stat is not None:
            all_dirs.append(active_skills_dir)
        external_dirs, external_root_error = _validate_configured_external_roots()
        if external_root_error is not None:
            return json.dumps(external_root_error, ensure_ascii=False)
        all_dirs.extend(external_dirs)

        if not all_dirs:
            return json.dumps(
                {
                    "success": False,
                    "error": "Skills directory does not exist yet. It will be created on first install.",
                },
                ensure_ascii=False,
            )

        skill_dir = None
        skill_md = None

        # Collision detection: collect ALL candidates across every dir using
        # every lookup strategy (direct path, recursive by parent dir name,
        # legacy flat <name>.md). If more than one matches, refuse and tell
        # the caller — silent shadowing of a local skill by a same-named
        # external skill is a real bug class (`/skills` shows one, agent
        # loaded the other) so we surface it loudly instead of guessing.
        from agent.skill_utils import iter_skill_index_files

        # Bind candidates to the exact regular-file bytes inspected during
        # discovery. Configured roots may legitimately be directory symlinks,
        # but a symlink/package swap must not redirect the later load.
        candidates: List[Tuple[Optional[Path], Path, Dict[str, Any]]] = []
        seen_file_identities: Dict[Tuple[int, ...], Dict[str, Any]] = {}
        seen_path_identities: Dict[str, Tuple[int, ...]] = {}
        discovery_errors: List[str] = []

        def _record_discovery_error(context: str, exc: BaseException) -> None:
            """Remember a scan failure instead of accepting a partial index."""
            detail = f"{context}: {exc}"
            if detail not in discovery_errors:
                discovery_errors.append(detail)

        def _is_directory_for_discovery(path: Path, context: str) -> bool:
            try:
                return stat.S_ISDIR(path.stat().st_mode)
            except FileNotFoundError:
                return False
            except OSError as exc:
                _record_discovery_error(context, exc)
                return False

        def _is_regular_file_for_discovery(path: Path, context: str) -> bool:
            try:
                return stat.S_ISREG(path.stat().st_mode)
            except FileNotFoundError:
                return False
            except OSError as exc:
                _record_discovery_error(context, exc)
                return False

        def _read_discovery_snapshot(path: Path) -> Optional[Dict[str, Any]]:
            """Open, strictly parse, and identity-bind one skill index file."""
            try:
                if os.name == "nt":
                    from tools.nt_secure_fs_optional import (
                        open_directory,
                        read_regular_file,
                    )

                    package_path = path.parent.resolve(strict=True)
                    with open_directory(
                        package_path, writable=False
                    ) as package:
                        parent_metadata = package.stat()
                        payload, file_metadata = read_regular_file(
                            package, path.name
                        )
                        fm_content = payload.decode("utf-8")
                        fm, _ = parse_strict_fenced_frontmatter(fm_content)
                        if fm:
                            _parse_frontmatter(fm_content)

                        # The supported configured/category alias must still
                        # resolve to the held package after parsing, and its
                        # canonical file must still be the object read above.
                        current_package_path = path.parent.resolve(strict=True)
                        with open_directory(
                            current_package_path, writable=False
                        ) as current_package:
                            if (
                                current_package.identity
                                != parent_metadata.identity
                            ):
                                raise RuntimeError(
                                    "skill package changed during discovery"
                                )
                            with current_package.open_file(
                                path.name, writable=False
                            ) as current_file:
                                if (
                                    current_file.identity
                                    != file_metadata.identity
                                ):
                                    raise RuntimeError(
                                        "skill file changed during discovery"
                                    )
                        return {
                            "content": fm_content,
                            "frontmatter": fm,
                            "identity": file_metadata.identity,
                            "package_signature": parent_metadata.signature,
                            "package_path": str(package_path),
                            "signature": file_metadata.signature,
                        }

                parent_before = path.parent.stat()
                if not stat.S_ISDIR(parent_before.st_mode):
                    raise ValueError("skill package is not a directory")
                if not hasattr(os, "O_NOFOLLOW"):
                    raise OSError("secure SKILL.md opens are unsupported on this platform")
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                fd = os.open(path, flags)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("skill index is not a regular file")
                    with os.fdopen(
                        fd, "r", encoding="utf-8", closefd=False
                    ) as skill_file:
                        fm_content = skill_file.read()
                    after = os.fstat(fd)
                finally:
                    os.close(fd)

                signature = _stat_signature(before)
                if signature != _stat_signature(after):
                    raise RuntimeError("skill file changed during discovery")

                # ``parse_frontmatter`` intentionally has a permissive
                # key/value fallback for non-discovery callers. A malformed
                # fence cannot claim a directory-derived active-skill name.
                fm, _ = parse_strict_fenced_frontmatter(fm_content)
                if fm:
                    _parse_frontmatter(fm_content)

                # Re-resolve after parsing too: compatibility hooks or a racing
                # writer may rename a path while its bytes are being inspected.
                current_parent = path.parent.stat()
                parent_signature = (
                    parent_before.st_dev,
                    parent_before.st_ino,
                    parent_before.st_size,
                    parent_before.st_mtime_ns,
                    parent_before.st_ctime_ns,
                )
                if (
                    signature != _stat_signature(path.stat())
                    or parent_signature != _stat_signature(current_parent)
                ):
                    raise RuntimeError("skill file changed during discovery")
                package_path = path.parent.resolve(strict=True)
                if parent_signature != _stat_signature(package_path.stat()):
                    raise RuntimeError("skill package changed during discovery")

                return {
                    "content": fm_content,
                    "frontmatter": fm,
                    "identity": (before.st_dev, before.st_ino),
                    "package_signature": parent_signature,
                    "package_path": str(package_path),
                    "signature": signature,
                }
            except Exception as exc:
                _record_discovery_error(f"cannot inspect {path}", exc)
                return None

        def _record_direct_candidate(search_dir: Path, relative_name: str) -> None:
            direct_path = search_dir / relative_name
            if not _is_skill_support_path(direct_path) and _is_directory_for_discovery(
                direct_path, f"cannot stat skill directory {direct_path}"
            ):
                direct_skill_md = direct_path / "SKILL.md"
                if _is_regular_file_for_discovery(
                    direct_skill_md, f"cannot stat skill file {direct_skill_md}"
                ):
                    _record(direct_path, direct_skill_md)
                return
            legacy_path = direct_path.with_suffix(".md")
            if not _is_skill_support_path(legacy_path) and _is_regular_file_for_discovery(
                legacy_path, f"cannot stat legacy skill file {legacy_path}"
            ):
                _record(None, legacy_path)

        def _record(
            sd: Optional[Path],
            smd: Path,
            snapshot: Optional[Dict[str, Any]] = None,
        ) -> None:
            if snapshot is None:
                snapshot = _read_discovery_snapshot(smd)
            if snapshot is None:
                return

            identity = snapshot["identity"] + snapshot["package_signature"]
            lexical_path = os.path.abspath(os.fspath(smd))
            previous_path_identity = seen_path_identities.get(lexical_path)
            if (
                previous_path_identity is not None
                and previous_path_identity != identity
            ):
                _record_discovery_error(
                    f"cannot inspect {smd}",
                    RuntimeError("skill file changed during discovery"),
                )
                return
            seen_path_identities[lexical_path] = identity

            previous = seen_file_identities.get(identity)
            if previous is not None:
                if (
                    previous["signature"] != snapshot["signature"]
                    or previous["content"] != snapshot["content"]
                ):
                    _record_discovery_error(
                        f"cannot inspect {smd}",
                        RuntimeError("skill file changed during discovery"),
                    )
                return

            seen_file_identities[identity] = snapshot
            candidates.append((sd, smd, snapshot))

        for search_dir in all_dirs:
            # Strategy 1: direct path (e.g., "mlops/axolotl" or bare "axolotl"
            # at the top of the dir).
            _record_direct_candidate(search_dir, name)

            # Strategy 1b: categorized form for plugin namespace fall-through
            # (e.g., a "myplugin:explore" name with no plugin registered also
            # tries the on-disk path "myplugin/explore").
            if local_category_name:
                _record_direct_candidate(search_dir, local_category_name)

            # Strategy 2: recursive by directory name (catches nested skills
            # like "foundations/runtime/explore-codebase" called by bare name),
            # plus frontmatter `name:` lookup. `skills_list()` exposes the
            # frontmatter name, so `skill_view(name)` must accept it too even
            # when the on-disk directory is a shorter category/alias.
            for found_skill_md in iter_skill_index_files(
                search_dir,
                "SKILL.md",
                on_error=lambda exc, root=search_dir: _record_discovery_error(
                    f"cannot traverse skills root {root}", exc
                ),
            ):
                snapshot = _read_discovery_snapshot(found_skill_md)
                if snapshot is None:
                    continue
                fm = snapshot["frontmatter"]
                if found_skill_md.parent.name == name or fm.get("name") == name:
                    _record(found_skill_md.parent, found_skill_md, snapshot)

            # Strategy 3: legacy flat <name>.md files anywhere under the dir.
            # Exclude skill support docs: references/templates/assets/scripts
            # are loaded through skill_view(skill, file_path=...) and must not
            # shadow or collide with real skills that share the same basename.
            try:
                for found_md in search_dir.rglob(f"{name}.md"):
                    if found_md.name != "SKILL.md" and not _is_skill_support_path(
                        found_md
                    ):
                        _record(None, found_md)
            except OSError as exc:
                _record_discovery_error(
                    f"cannot traverse legacy skills under {search_dir}", exc
                )

        if discovery_errors:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": (
                        "Skill discovery is incomplete; refusing to load a "
                        "partial match."
                    ),
                    "detail": "; ".join(discovery_errors[:3]),
                },
                ensure_ascii=False,
            )

        if len(candidates) > 1:
            paths = [str(smd) for _, smd, _ in candidates]
            logging.getLogger(__name__).warning(
                "Skill name collision for '%s': %d candidates — %s",
                name, len(candidates), "; ".join(paths),
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Ambiguous skill name '{name}': {len(candidates)} skills "
                        "match across your local skills dir and external_dirs. "
                        "Refusing to guess — load one explicitly by its categorized path."
                    ),
                    "matches": paths,
                    "hint": (
                        "Pass the full relative path instead of the bare name "
                        "(e.g., 'category/skill-name'), or rename one of the "
                        "colliding skills so each name is unique."
                    ),
                },
                ensure_ascii=False,
            )

        if candidates:
            skill_dir, skill_md, discovered_snapshot = candidates[0]

        if not skill_md:
            available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skills_list to see all available skills",
                },
                ensure_ascii=False,
            )

        # Re-open once after the complete-root collision scan and require the
        # same identity, metadata, and bytes. The resulting snapshot supplies
        # both metadata and content below; there is no redirectable final read.
        final_snapshot = _read_discovery_snapshot(skill_md)
        if final_snapshot is None:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": (
                        "Skill discovery is incomplete; refusing to load a "
                        "partial match."
                    ),
                    "detail": "; ".join(discovery_errors[-3:]),
                },
                ensure_ascii=False,
            )
        if (
            final_snapshot["identity"] != discovered_snapshot["identity"]
            or final_snapshot["package_signature"]
            != discovered_snapshot["package_signature"]
            or final_snapshot["package_path"] != discovered_snapshot["package_path"]
            or final_snapshot["signature"] != discovered_snapshot["signature"]
            or final_snapshot["content"] != discovered_snapshot["content"]
        ):
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": (
                        "Skill discovery is incomplete; refusing to load a "
                        "partial match."
                    ),
                    "detail": (
                        f"cannot inspect {skill_md}: "
                        "skill file changed during discovery"
                    ),
                },
                ensure_ascii=False,
            )

        content = final_snapshot["content"]
        bound_skill_dir = Path(final_snapshot["package_path"])

        def _post_discovery_incomplete(detail: str) -> str:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "skills_discovery_incomplete",
                    "error": (
                        "Skill discovery is incomplete; refusing to load a "
                        "partial match."
                    ),
                    "detail": detail,
                },
                ensure_ascii=False,
            )

        # Security: warn if skill is loaded from outside trusted directories
        # (local skills dir + configured external_dirs are all trusted)
        _outside_skills_dir = True
        _trusted_dirs = [active_skills_dir.resolve()]
        try:
            _trusted_dirs.extend(d.resolve() for d in all_dirs[1:])
        except Exception:
            pass
        for _td in _trusted_dirs:
            try:
                skill_md.resolve().relative_to(_td)
                _outside_skills_dir = False
                break
            except ValueError:
                continue

        # Security: detect common prompt injection patterns
        # (pattern list at module level as _INJECTION_PATTERNS)
        _content_lower = content.lower()
        _injection_detected = any(p in _content_lower for p in _INJECTION_PATTERNS)

        if _outside_skills_dir or _injection_detected:
            _warnings = []
            if _outside_skills_dir:
                _warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
            if _injection_detected:
                _warnings.append("skill content contains patterns that may indicate prompt injection")
            logging.getLogger(__name__).warning("Skill security warning for '%s': %s", name, "; ".join(_warnings))

        parsed_frontmatter: Dict[str, Any] = final_snapshot["frontmatter"]

        if not skill_matches_platform(parsed_frontmatter):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' is not supported on this platform.",
                    "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
                },
                ensure_ascii=False,
            )

        # Check if the skill is disabled by the user
        resolved_name = parsed_frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{resolved_name}' is disabled. "
                        "Enable it with `hermes skills` or inspect the files directly on disk."
                    ),
                },
                ensure_ascii=False,
            )

        # If a specific file path is requested, read that instead
        if file_path and skill_dir:
            try:
                relative_parts = _bound_relative_parts(file_path)
            except ValueError as exc:
                return json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            try:
                package_fd = _open_bound_package_dir(
                    bound_skill_dir,
                    final_snapshot["package_signature"],
                )
            except Exception as exc:
                return _post_discovery_incomplete(
                    f"cannot bind skill package {bound_skill_dir}: {exc}"
                )
            try:
                try:
                    support_bytes, support_stat = _read_bound_support_file(
                        package_fd,
                        file_path,
                        final_snapshot["package_signature"],
                    )
                except FileNotFoundError:
                    try:
                        available_files, files_summary = (
                            _build_bound_linked_files_manifest(
                                package_fd,
                                final_snapshot["package_signature"],
                            )
                        )
                    except Exception as exc:
                        return _post_discovery_incomplete(
                            f"cannot inspect skill package {bound_skill_dir}: {exc}"
                        )
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"File '{file_path}' not found in skill '{name}'."
                            ),
                            "available_files": available_files or None,
                            "linked_files_summary": files_summary,
                            "hint": "Use one of the available file paths listed above",
                        },
                        ensure_ascii=False,
                    )
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        return json.dumps(
                            {
                                "success": False,
                                "error": (
                                    "Path escapes the allowed directory "
                                    "boundary or traverses a symlink."
                                ),
                                "hint": (
                                    "Use a regular file path within the skill "
                                    "directory"
                                ),
                            },
                            ensure_ascii=False,
                        )
                    return _post_discovery_incomplete(
                        f"cannot read support file '{file_path}': {exc}"
                    )
                except Exception as exc:
                    return _post_discovery_incomplete(
                        f"cannot read support file '{file_path}': {exc}"
                    )
            finally:
                _close_bound_handle(package_fd)

            target_file = bound_skill_dir.joinpath(*relative_parts)
            try:
                support_content = support_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # Binary file - return info about it instead
                return json.dumps(
                    {
                        "success": True,
                        "name": name,
                        "file": file_path,
                        "content": (
                            f"[Binary file: {relative_parts[-1]}, "
                            f"size: {support_stat.st_size} bytes]"
                        ),
                        "is_binary": True,
                    },
                    ensure_ascii=False,
                )

            try:
                from tools.skill_manager_tool import mark_background_review_skill_read

                mark_background_review_skill_read(target_file)
            except Exception:
                logger.debug(
                    "Could not record background-review skill read for %s",
                    target_file,
                    exc_info=True,
                )

            return json.dumps(
                {
                    "success": True,
                    "name": name,
                    "file": file_path,
                    "content": support_content,
                    "file_type": PurePosixPath(file_path.replace("\\", "/")).suffix,
                },
                ensure_ascii=False,
            )

        # Reuse the parse from the platform check above
        frontmatter = parsed_frontmatter

        # Supporting files are a bounded discovery preview. Explicit reads by
        # file_path remain available even when this manifest is truncated.
        linked_files: Dict[str, List[str]] = {}
        linked_files_summary: Dict[str, Any] = {
            "shown": 0,
            "truncated": False,
            "truncated_categories": [],
            "per_category_limit": MAX_LINKED_FILES_PER_CATEGORY,
            "path_char_limit": MAX_LINKED_FILE_PATH_CHARS,
        }
        if skill_dir and _SECURE_PACKAGE_OPEN_SUPPORTED:
            try:
                package_fd = _open_bound_package_dir(
                    bound_skill_dir,
                    final_snapshot["package_signature"],
                )
            except Exception as exc:
                return _post_discovery_incomplete(
                    f"cannot bind skill package {bound_skill_dir}: {exc}"
                )
            try:
                linked_files, linked_files_summary = (
                    _build_bound_linked_files_manifest(
                        package_fd,
                        final_snapshot["package_signature"],
                    )
                )
            except Exception as exc:
                return _post_discovery_incomplete(
                    f"cannot inspect skill package {bound_skill_dir}: {exc}"
                )
            finally:
                _close_bound_handle(package_fd)

        # Read tags/related_skills with backward compat:
        # Check metadata.hermes.* first (agentskills.io convention), fall back to top-level
        hermes_meta = {}
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {}) or {}

        tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
        related_skills = _parse_tags(
            hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
        )

        try:
            rel_path = str(skill_md.relative_to(active_skills_dir))
        except ValueError:
            # External skill — use path relative to the skill's own parent dir
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get(
            "name", skill_md.stem if not skill_dir else skill_dir.name
        )
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
        required_env_vars = _get_required_environment_variables(
            frontmatter, legacy_env_vars
        )
        backend = _get_terminal_backend_name()
        env_snapshot = load_env()
        missing_required_env_vars = [
            e
            for e in required_env_vars
            if not e.get("optional")
            and not _is_env_var_persisted(e["name"], env_snapshot)
        ]
        capture_result = _capture_required_environment_variables(
            skill_name,
            missing_required_env_vars,
        )
        if missing_required_env_vars:
            env_snapshot = load_env()
        remaining_missing_required_envs = _remaining_required_environment_names(
            required_env_vars,
            capture_result,
            env_snapshot=env_snapshot,
        )
        setup_needed = bool(remaining_missing_required_envs)

        # Register available skill env vars so they pass through to sandboxed
        # execution environments (execute_code, terminal).  Only vars that are
        # actually set get registered — missing ones are reported as setup_needed.
        available_env_names = [
            e["name"]
            for e in required_env_vars
            if e["name"] not in remaining_missing_required_envs
        ]
        if available_env_names:
            try:
                from tools.env_passthrough import register_env_passthrough

                register_env_passthrough(available_env_names)
            except Exception:
                logger.debug(
                    "Could not register env passthrough for skill %s",
                    skill_name,
                    exc_info=True,
                )

        # Register credential files for mounting into remote sandboxes
        # (Modal, Docker).  Files that exist on the host are registered;
        # missing ones are added to the setup_needed indicators.
        required_cred_files_raw = frontmatter.get("required_credential_files", [])
        if not isinstance(required_cred_files_raw, list):
            required_cred_files_raw = []
        missing_cred_files: list = []
        if required_cred_files_raw:
            try:
                from tools.credential_files import register_credential_files

                missing_cred_files = register_credential_files(required_cred_files_raw)
                if missing_cred_files:
                    setup_needed = True
            except Exception:
                logger.debug(
                    "Could not register credential files for skill %s",
                    skill_name,
                    exc_info=True,
                )

        rendered_content = content
        if preprocess:
            if skill_dir and not _SECURE_PACKAGE_OPEN_SUPPORTED:
                from agent import skill_preprocessing

                preprocessing_config = skill_preprocessing.load_skills_config()
                (
                    template_vars_enabled,
                    inline_shell_enabled,
                    _timeout,
                ) = skill_preprocessing.normalize_preprocessing_config(
                    preprocessing_config
                )
                if template_vars_enabled:
                    rendered_content = (
                        skill_preprocessing.substitute_template_vars(
                            rendered_content,
                            bound_skill_dir,
                            task_id,
                        )
                    )
                if inline_shell_enabled:
                    return _post_discovery_incomplete(
                        "secure inline-shell package binding is unsupported "
                        "on this platform"
                    )
            elif skill_dir:
                try:
                    package_fd = _open_bound_package_dir(
                        bound_skill_dir,
                        final_snapshot["package_signature"],
                    )
                except Exception as exc:
                    return _post_discovery_incomplete(
                        f"cannot bind skill package {bound_skill_dir}: {exc}"
                    )
                try:
                    from agent import skill_preprocessing

                    preprocessing_config = (
                        skill_preprocessing.load_skills_config()
                    )
                    (
                        template_vars_enabled,
                        inline_shell_enabled,
                        timeout,
                    ) = skill_preprocessing.normalize_preprocessing_config(
                        preprocessing_config
                    )
                    if inline_shell_enabled:
                        rendered_content = _expand_inline_shell_bound(
                            rendered_content,
                            package_fd,
                            timeout,
                            skill_dir=bound_skill_dir,
                            session_id=task_id,
                            template_vars=template_vars_enabled,
                        )
                    elif template_vars_enabled:
                        rendered_content = (
                            skill_preprocessing.substitute_template_vars(
                                rendered_content,
                                bound_skill_dir,
                                task_id,
                            )
                        )
                    if (
                        _bound_handle_signature(package_fd)
                        != final_snapshot["package_signature"]
                    ):
                        raise RuntimeError(
                            "skill package changed during preprocessing"
                        )
                except Exception as exc:
                    return _post_discovery_incomplete(
                        f"cannot preprocess skill package {bound_skill_dir}: {exc}"
                    )
                finally:
                    _close_bound_handle(package_fd)
            else:
                try:
                    from agent.skill_preprocessing import preprocess_skill_content

                    rendered_content = preprocess_skill_content(
                        content,
                        None,
                        session_id=task_id,
                    )
                except Exception:
                    logger.debug(
                        "Could not preprocess legacy skill %s",
                        skill_name,
                        exc_info=True,
                    )

        # ── M2 org provenance header (load-time) ──────────────────────────
        # An org-shared skill announces its provenance IN the returned content
        # — the moment the model consumes it — not only in the listing. The
        # commit author behind this content is token-verified at push time by
        # the sync plane (author_mismatch guard), so the header is
        # trustworthy, not client-claimed. Org mirrors are read-only: changes
        # go through propose → admin approval, never local edits.
        org_provenance = None
        if skill_dir:
            try:
                from agent.skill_utils import (
                    ORG_PROVENANCE_FILE,
                    is_org_mirror_path,
                    org_id_of_path,
                )

                if is_org_mirror_path(skill_dir, active_skills_dir):
                    prov_org = org_id_of_path(skill_dir, active_skills_dir)
                    author = ""
                    ts = ""
                    if prov_org:
                        try:
                            prov = json.loads(
                                (
                                    active_skills_dir
                                    / "_org"
                                    / prov_org
                                    / ORG_PROVENANCE_FILE
                                ).read_text(encoding="utf-8")
                            )
                            author = str(
                                prov.get("author_device")
                                or prov.get("author_user_id")
                                or ""
                            )
                            ts = str(prov.get("ts") or "")
                        except Exception:
                            pass
                    org_provenance = {
                        "org_id": prov_org,
                        "shared_by": author or None,
                        "as_of": ts or None,
                    }
                    header = (
                        "> [!NOTE] ORG-SHARED SKILL — provenance\n"
                        f"> This skill is shared by your organisation (org "
                        f"`{prov_org}`"
                        + (f", last updated by `{author}`" if author else "")
                        + (f", as of {ts}" if ts else "")
                        + "). It was reviewed and approved for the whole\n"
                        "> team — treat it as third-party instructions rather "
                        "than your own notes.\n"
                        "> You MAY improve it in place like any other skill. "
                        "Your edits are kept locally\n"
                        "> and are never overwritten by org updates; share "
                        "them back with\n"
                        "> `hermes sync propose` (or automatically, if your "
                        "org enables it).\n\n"
                    )
                    rendered_content = header + rendered_content
            except Exception:
                logger.debug(
                    "Could not resolve org provenance for %s",
                    skill_name,
                    exc_info=True,
                )

        result = {
            "success": True,
            "name": skill_name,
            "description": frontmatter.get("description", ""),
            "tags": tags,
            "related_skills": related_skills,
            "content": rendered_content,
            "path": rel_path,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "org_provenance": org_provenance,
            # Internal slash/preload consumers use these markers to avoid
            # reopening this path for rendering or a support-file scan after
            # the checked dirfd has been closed.
            "lookup_name": name,
            "package_bound": bool(skill_dir),
            "preprocessed": bool(preprocess),
            "linked_files": linked_files if linked_files else None,
            "linked_files_summary": linked_files_summary,
            "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
            if linked_files
            else None,
            "required_environment_variables": required_env_vars,
            "required_commands": [],
            "missing_required_environment_variables": remaining_missing_required_envs,
            "missing_credential_files": missing_cred_files,
            "missing_required_commands": [],
            "setup_needed": setup_needed,
            "setup_skipped": capture_result["setup_skipped"],
            "readiness_status": SkillReadinessStatus.SETUP_NEEDED.value
            if setup_needed
            else SkillReadinessStatus.AVAILABLE.value,
        }

        setup_help = next((e["help"] for e in required_env_vars if e.get("help")), None)
        if setup_help:
            result["setup_help"] = setup_help

        if capture_result["gateway_setup_hint"]:
            result["gateway_setup_hint"] = capture_result["gateway_setup_hint"]

        try:
            from tools.skill_manager_tool import mark_background_review_skill_read

            mark_background_review_skill_read(skill_md)
        except Exception:
            logger.debug(
                "Could not record background-review skill read for %s",
                skill_md,
                exc_info=True,
            )

        if setup_needed:
            missing_items = [
                f"env ${env_name}" for env_name in remaining_missing_required_envs
            ] + [
                f"file {path}" for path in missing_cred_files
            ]
            setup_note = _build_setup_note(
                SkillReadinessStatus.SETUP_NEEDED,
                missing_items,
                setup_help,
            )
            if backend in _REMOTE_ENV_BACKENDS and setup_note:
                setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
            if setup_note:
                result["setup_note"] = setup_note

        # Surface agentskills.io optional fields when present
        if frontmatter.get("compatibility"):
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return tool_error(str(e), success=False)




if __name__ == "__main__":
    """Test the skills tool"""
    print("🎯 Skills Tool Test")
    print("=" * 60)

    # Test listing skills
    print("\n📋 Listing all skills:")
    result = json.loads(skills_list())
    if result["success"]:
        print(
            f"Found {result['count']} skills in {len(result.get('categories', []))} categories"
        )
        print(f"Categories: {result.get('categories', [])}")
        print("\nFirst 10 skills:")
        for skill in result["skills"][:10]:
            cat = f"[{skill['category']}] " if skill.get("category") else ""
            print(f"  • {cat}{skill['name']}: {skill['description'][:60]}...")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a skill
    print("\n📖 Viewing skill 'axolotl':")
    result = json.loads(skill_view("axolotl"))
    if result["success"]:
        print(f"Name: {result['name']}")
        print(f"Description: {result.get('description', 'N/A')[:100]}...")
        print(f"Content length: {len(result['content'])} chars")
        if result.get("linked_files"):
            print(f"Linked files: {result['linked_files']}")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a reference file
    print("\n📄 Viewing reference file 'axolotl/references/dataset-formats.md':")
    result = json.loads(skill_view("axolotl", "references/dataset-formats.md"))
    if result["success"]:
        print(f"File: {result['file']}")
        print(f"Content length: {len(result['content'])} chars")
        print(f"Preview: {result['content'][:150]}...")
    else:
        print(f"Error: {result['error']}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans').",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(
        category=args.get("category"), task_id=kw.get("task_id")
    ),
    check_fn=check_skills_requirements,
    emoji="📚",
)
def _skill_view_with_bump(args, **kw):
    """Invoke skill_view, then bump view_count on success. Best-effort: a
    telemetry failure never breaks the tool call."""
    name = args.get("name", "")
    result = skill_view(
        name, file_path=args.get("file_path"), task_id=kw.get("task_id")
    )
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            # Use the resolved skill name from the payload when present —
            # qualified forms ("plugin:skill") return with the canonical name.
            resolved = parsed.get("name") or name
            if resolved:
                from tools.skill_usage import bump_use, bump_view
                bump_view(str(resolved))
                # A skill_view tool call is the agent actively loading the skill
                # to act on it — that counts as use, not just a browse/view.
                # Curator's stale timer keys off last_used_at (see agent/curator.py).
                bump_use(str(resolved))
    except Exception:
        pass
    return result


registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=_skill_view_with_bump,
    check_fn=check_skills_requirements,
    emoji="📚",
)
