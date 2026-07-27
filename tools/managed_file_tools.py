"""Pure-Python file tools for verified managed short-task workers.

The managed lane shares a checkout with a sequence of fresh worker processes.
Its file surface therefore cannot reuse the ordinary shell-backed file layer:
that layer imports Terminal/LSP machinery and may launch executables from the
workspace.  This module performs bounded local filesystem operations directly
with Python and confines every resolved path to the dispatcher-frozen root.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
from typing import Any

from agent.managed_short_task import (
    managed_short_task_lane,
    verified_managed_short_task_lane,
)
from tools.registry import registry, tool_error


_MAX_READ_CHARS = 100_000
_MAX_READ_FILE_BYTES = 5_000_000
_MAX_WRITE_FILE_BYTES = 5_000_000
_MAX_SEARCH_FILE_BYTES = 2_000_000
_MAX_SEARCH_CANDIDATES = 10_000
_MAX_SEARCH_RESULTS = 2_000
_BLOCKED_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    ".envrc",
    "auth.json",
    ".anthropic_oauth.json",
    ".netrc",
    ".pgpass",
    ".git-credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
_REVIEW_READ_LOCK = threading.Lock()
_REVIEW_READ_EVIDENCE: dict[
    tuple[str, int, str], dict[str, dict[str, Any]]
] = {}


def _workspace_root() -> tuple[Path | None, str | None]:
    if not verified_managed_short_task_lane():
        return None, "Managed file access requires a verified CLI bootstrap."
    raw = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    try:
        root = Path(raw)
        if not root.is_absolute():
            raise ValueError("workspace is not absolute")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace is not a directory")
        return root, None
    except (OSError, RuntimeError, ValueError):
        return None, "Managed file access requires a verified workspace directory."


def _resolve_path(
    raw_path: Any,
    *,
    require_exists: bool = False,
    _allow_internal_absolute: bool = False,
) -> tuple[Path | None, str | None]:
    root, error = _workspace_root()
    if error:
        return None, error
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "Managed file access requires a concrete path."
    if raw_path.startswith("~"):
        return None, "Managed file paths cannot use home-directory expansion."
    try:
        supplied = Path(raw_path)
        if supplied.is_absolute() and not _allow_internal_absolute:
            return None, "Managed tool paths must be relative to the assigned workspace."
        if not supplied.is_absolute() and any(part == ".." for part in supplied.parts):
            return None, "Managed tool paths cannot contain parent traversal."
        candidate = supplied if supplied.is_absolute() else root / supplied
        relative_candidate = candidate.relative_to(root)
        cursor = root
        for component in relative_candidate.parts:
            cursor = cursor / component
            if cursor.exists() or cursor.is_symlink():
                if cursor.is_symlink():
                    return None, "Managed file paths cannot traverse symbolic links."
        resolved = candidate.resolve(strict=require_exists)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, f"Managed file access is limited to the assigned workspace; refused {raw_path!r}."
    return resolved, None


def _read_denied(path: Path) -> str | None:
    name = path.name.casefold()
    blocked_names = {item.casefold() for item in _BLOCKED_SECRET_NAMES}
    if name in blocked_names or name.startswith(".env."):
        return f"Read denied: {path.name!r} is a secret-bearing file."
    parts = {part.casefold() for part in path.parts}
    if "mcp-tokens" in parts or ".ssh" in parts or ".gnupg" in parts:
        return "Read denied: protected credential path."
    return None


def _write_denied(path: Path) -> str | None:
    denied = _read_denied(path)
    if denied:
        return denied.replace("Read denied", "Write denied", 1)
    return None


def _read_text_bytes(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[str | None, bytes | None, str | None]:
    denied = _read_denied(path)
    if denied:
        return None, None, denied
    checked, error = _resolve_path(
        str(path), require_exists=True, _allow_internal_absolute=True
    )
    if error or checked != path:
        return None, None, error or "Managed read target changed during validation."
    try:
        preopen_metadata = path.lstat()
        if not stat.S_ISREG(preopen_metadata.st_mode):
            return None, None, f"Only regular files may be read: {path}"
        if preopen_metadata.st_nlink != 1:
            return None, None, f"Hard-linked files are refused in managed mode: {path}"
        if max_bytes is not None and preopen_metadata.st_size > max_bytes:
            return None, None, f"File exceeds managed read limit: {path}"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None, None, f"Only regular files may be read: {path}"
            if metadata.st_nlink != 1:
                return None, None, f"Hard-linked files are refused in managed mode: {path}"
            if max_bytes is not None and metadata.st_size > max_bytes:
                return None, None, f"File exceeds managed read limit: {path}"
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        return None, None, f"Could not read {path}: {exc}"
    checked_after, error = _resolve_path(
        str(path), require_exists=True, _allow_internal_absolute=True
    )
    if error or checked_after != path:
        return None, None, error or "Managed read target changed during access."
    if b"\x00" in data[:4096]:
        return None, None, f"Binary file cannot be read as text: {path}"
    try:
        return data.decode("utf-8-sig"), data, None
    except UnicodeDecodeError:
        return None, None, f"File is not valid UTF-8 text: {path}"


def _read_text(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[str | None, str | None]:
    text, _data, error = _read_text_bytes(path, max_bytes=max_bytes)
    return text, error


def _atomic_write(path: Path, content: str) -> str | None:
    denied = _write_denied(path)
    if denied:
        return denied
    try:
        if path.exists():
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return f"Only regular files may be replaced: {path}"
            if metadata.st_nlink != 1:
                return f"Hard-linked files are refused in managed mode: {path}"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after mkdir so a concurrently introduced symlink cannot
        # redirect the final replace outside the frozen workspace.
        checked, error = _resolve_path(
            str(path), require_exists=False, _allow_internal_absolute=True
        )
        if error or checked != path.resolve(strict=False):
            return error or "Managed write target changed during validation."
        checked_parent, error = _resolve_path(
            str(path.parent), require_exists=True, _allow_internal_absolute=True
        )
        if error or checked_parent != path.parent.resolve(strict=True):
            return error or "Managed write parent changed during validation."
        previous_mode = path.stat().st_mode & 0o777 if path.exists() else None
        fd, temporary = tempfile.mkstemp(
            prefix=".hermes-managed-", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if previous_mode is not None:
                os.chmod(temporary, previous_mode)
            checked_before_replace, error = _resolve_path(
                str(path), require_exists=False, _allow_internal_absolute=True
            )
            if error or checked_before_replace != path.resolve(strict=False):
                return error or "Managed write target changed before replace."
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        checked_after, error = _resolve_path(
            str(path), require_exists=True, _allow_internal_absolute=True
        )
        if error or checked_after != path:
            return error or "Managed write target changed after replace."
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return "Managed write did not produce one ordinary workspace file."
        return None
    except OSError as exc:
        return f"Could not write {path}: {exc}"


def _line_numbered(text: str, start: int) -> str:
    return "\n".join(
        f"{number}|{line}" for number, line in enumerate(text.splitlines(), start)
    )


def _review_evidence_bucket_key(
    root: Path | None = None,
) -> tuple[str, int, str] | None:
    if managed_short_task_lane() != "review":
        return None
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    raw_run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        run_id = int(raw_run_id)
    except (TypeError, ValueError):
        return None
    if run_id <= 0:
        return None
    if root is None:
        root, error = _workspace_root()
        if error or root is None:
            return None
    return task_id, run_id, str(root)


def read_file_tool(path: str, offset: int = 1, limit: int = 500, **_kw) -> str:
    resolved, error = _resolve_path(path, require_exists=True)
    if error:
        return tool_error(error)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        return tool_error("offset must be a positive integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return tool_error("limit must be a positive integer")
    limit = min(limit, 2000)
    content, exact_bytes, error = _read_text_bytes(
        resolved, max_bytes=_MAX_READ_FILE_BYTES
    )
    if error:
        return tool_error(error)
    if content is None or exact_bytes is None:
        return tool_error("Managed read returned no verified text.")
    lines = content.splitlines()
    page = lines[offset - 1 : offset - 1 + limit]
    rendered = _line_numbered("\n".join(page), offset)
    truncated_by_chars = len(rendered) > _MAX_READ_CHARS
    if truncated_by_chars:
        rendered = rendered[:_MAX_READ_CHARS]
    truncated = offset - 1 + len(page) < len(lines) or truncated_by_chars
    if managed_short_task_lane() == "review" and offset == 1 and not truncated:
        root, root_error = _workspace_root()
        if root_error:
            return tool_error(root_error)
        relative = resolved.relative_to(root).as_posix()
        bucket_key = _review_evidence_bucket_key(root)
        if bucket_key is None:
            return tool_error("Review read evidence could not be bound to this task.")
        with _REVIEW_READ_LOCK:
            bucket = _REVIEW_READ_EVIDENCE.setdefault(bucket_key, {})
            bucket[relative] = {
                "path": relative,
                "sha256": hashlib.sha256(exact_bytes).hexdigest(),
                "size": len(exact_bytes),
            }
    return json.dumps(
        {
            "content": rendered,
            "total_lines": len(lines),
            "file_size": resolved.stat().st_size,
            "truncated": truncated,
            "resolved_path": str(resolved),
        },
        ensure_ascii=False,
    )


def managed_review_read_evidence() -> list[dict[str, Any]]:
    """Return current review-task evidence, isolated by task and workspace."""
    bucket_key = _review_evidence_bucket_key()
    if bucket_key is None:
        return []
    with _REVIEW_READ_LOCK:
        bucket = _REVIEW_READ_EVIDENCE.get(bucket_key, {})
        return [
            dict(bucket[path])
            for path in sorted(bucket)
        ]


def write_file_tool(path: str, content: str, **_kw) -> str:
    if managed_short_task_lane() == "review":
        return tool_error(
            "独立复核阶段只能读取文件；如需修改，请提交问题，由新的实现短任务处理。"
        )
    if not isinstance(content, str):
        return tool_error("write_file content must be a string")
    if len(content.encode("utf-8")) > _MAX_WRITE_FILE_BYTES:
        return tool_error("write_file content exceeds the managed write limit")
    resolved, error = _resolve_path(path)
    if error:
        return tool_error(error)
    error = _atomic_write(resolved, content)
    if error:
        return tool_error(error)
    return json.dumps(
        {
            "success": True,
            "bytes_written": len(content.encode("utf-8")),
            "resolved_path": str(resolved),
            "files_modified": [str(resolved)],
            "lint": {"skipped": True, "message": "Deferred to isolated verification"},
        },
        ensure_ascii=False,
    )


def _replace_text(
    path: Path,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
) -> tuple[str | None, str | None]:
    content, error = _read_text(path, max_bytes=_MAX_READ_FILE_BYTES)
    if error:
        return None, error
    count = content.count(old_string)
    if not old_string:
        return None, "old_string must not be empty"
    if count == 0:
        return None, "old_string was not found"
    if count > 1 and not replace_all:
        return None, f"old_string is not unique ({count} matches)"
    updated = content.replace(old_string, new_string, -1 if replace_all else 1)
    return updated, None


def _apply_v4a(patch_text: str) -> str:
    from tools.patch_parser import OperationType, parse_v4a_patch

    operations, error = parse_v4a_patch(patch_text)
    if error:
        return tool_error(error)
    if not operations:
        return tool_error("Patch contains no operations")
    if len(operations) != 1:
        return tool_error(
            "Managed V4A patches are limited to one file per call so a failure cannot half-apply a multi-file change."
        )
    if operations[0].operation in {OperationType.DELETE, OperationType.MOVE}:
        return tool_error(
            "Managed Phase-1 patches do not permit delete or move operations."
        )

    planned: dict[Path, str | None] = {}
    original: dict[Path, str | None] = {}
    display_paths: list[str] = []
    for operation in operations:
        source, path_error = _resolve_path(
            operation.file_path,
            require_exists=operation.operation
            in {OperationType.UPDATE, OperationType.DELETE, OperationType.MOVE},
        )
        if path_error:
            return tool_error(path_error)
        destination = None
        if operation.new_path:
            destination, path_error = _resolve_path(operation.new_path)
            if path_error:
                return tool_error(path_error)

        current = planned.get(source)
        if source not in planned:
            if source.exists():
                current, path_error = _read_text(
                    source, max_bytes=_MAX_READ_FILE_BYTES
                )
                if path_error:
                    return tool_error(path_error)
            else:
                current = None
            original[source] = current

        if operation.operation == OperationType.ADD:
            if current is not None:
                return tool_error(f"Add target already exists: {operation.file_path}")
            current = "\n".join(
                line.content
                for hunk in operation.hunks
                for line in hunk.lines
                if line.prefix == "+"
            )
            planned[source] = current
        elif operation.operation == OperationType.UPDATE:
            if current is None:
                return tool_error(f"Update target does not exist: {operation.file_path}")
            for hunk in operation.hunks:
                search = "\n".join(
                    line.content for line in hunk.lines if line.prefix in {" ", "-"}
                )
                replacement = "\n".join(
                    line.content for line in hunk.lines if line.prefix in {" ", "+"}
                )
                if not search:
                    hint = hunk.context_hint or ""
                    if not hint or current.count(hint) != 1:
                        return tool_error("Addition-only hunk requires one unique context hint")
                    replacement = hint + ("\n" + replacement if replacement else "")
                    search = hint
                updated, replace_error = _replace_in_memory(current, search, replacement)
                if replace_error:
                    return tool_error(f"{operation.file_path}: {replace_error}")
                current = updated
            planned[source] = current
        display_paths.append(operation.file_path)

    if any(
        updated is not None
        and len(updated.encode("utf-8")) > _MAX_WRITE_FILE_BYTES
        for updated in planned.values()
    ):
        return tool_error("Patched content exceeds the managed write limit")

    diffs: list[str] = []
    for path, updated in planned.items():
        before = original.get(path)
        diffs.append(
            "".join(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (updated or "").splitlines(keepends=True),
                    fromfile=f"a/{path.name}" if before is not None else "/dev/null",
                    tofile=f"b/{path.name}" if updated is not None else "/dev/null",
                )
            )
        )
    for path, updated in planned.items():
        write_error = _atomic_write(path, updated)
        if write_error:
            return tool_error(write_error)
    return json.dumps(
        {"success": True, "diff": "\n".join(diffs), "files_modified": display_paths},
        ensure_ascii=False,
    )


def _replace_in_memory(content: str, old: str, new: str) -> tuple[str | None, str | None]:
    count = content.count(old)
    if count == 0:
        return None, "patch context was not found"
    if count > 1:
        return None, f"patch context is ambiguous ({count} matches)"
    return content.replace(old, new, 1), None


def patch_tool(
    mode: str = "replace",
    path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    **_kw,
) -> str:
    if managed_short_task_lane() == "review":
        return tool_error(
            "独立复核阶段只能读取文件；如需修改，请提交问题，由新的实现短任务处理。"
        )
    if mode == "patch":
        if not isinstance(patch, str):
            return tool_error("patch mode requires patch text")
        return _apply_v4a(patch)
    if mode != "replace" or not isinstance(path, str):
        return tool_error("replace mode requires a path")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return tool_error("replace mode requires string old_string and new_string")
    resolved, error = _resolve_path(path, require_exists=True)
    if error:
        return tool_error(error)
    original, error = _read_text(resolved, max_bytes=_MAX_READ_FILE_BYTES)
    if error:
        return tool_error(error)
    updated, error = _replace_text(
        resolved, old_string, new_string, replace_all=bool(replace_all)
    )
    if error:
        return tool_error(error)
    if len(updated.encode("utf-8")) > _MAX_WRITE_FILE_BYTES:
        return tool_error("Patched content exceeds the managed write limit")
    error = _atomic_write(resolved, updated)
    if error:
        return tool_error(error)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return json.dumps(
        {"success": True, "diff": diff, "files_modified": [str(resolved)]},
        ensure_ascii=False,
    )


def _safe_glob(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalized = raw.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        normalized.startswith("~")
        or pure.is_absolute()
        or any(part == ".." for part in pure.parts)
    ):
        raise ValueError("search glob must remain relative to the workspace")
    return normalized


def _candidate_files(base: Path):
    if base.is_file():
        yield base
        return
    seen = 0
    for current, directories, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            seen += 1
            if seen > _MAX_SEARCH_CANDIDATES:
                return
            path = current_path / filename
            checked, error = _resolve_path(
                str(path),
                require_exists=True,
                _allow_internal_absolute=True,
            )
            if not error:
                try:
                    metadata = checked.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    continue
                yield checked


def search_files_tool(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
    **_kw,
) -> str:
    base, error = _resolve_path(path, require_exists=True)
    if error:
        return tool_error(error)
    try:
        glob_filter = _safe_glob(file_glob)
        if target == "files":
            _safe_glob(pattern)
    except ValueError as exc:
        return tool_error(str(exc))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return tool_error("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return tool_error("offset must be a non-negative integer")
    limit = min(limit, 500)
    results: list[dict[str, Any]] = []
    if target == "files":
        for candidate in _candidate_files(base):
            if _read_denied(candidate):
                continue
            relative = candidate.relative_to(base if base.is_dir() else base.parent)
            if fnmatch.fnmatch(relative.as_posix(), pattern) or fnmatch.fnmatch(candidate.name, pattern):
                results.append({"path": str(candidate)})
                if len(results) >= _MAX_SEARCH_RESULTS:
                    break
    elif target == "content":
        if not isinstance(pattern, str) or not pattern:
            return tool_error("content search requires a non-empty literal string")
        if len(pattern) > 1_000:
            return tool_error("literal search pattern is too large")
        for candidate in _candidate_files(base):
            relative = candidate.relative_to(base if base.is_dir() else base.parent)
            if glob_filter and not (
                fnmatch.fnmatch(relative.as_posix(), glob_filter)
                or fnmatch.fnmatch(candidate.name, glob_filter)
            ):
                continue
            text, read_error = _read_text(
                candidate, max_bytes=_MAX_SEARCH_FILE_BYTES
            )
            if read_error:
                continue
            matches = []
            for line_number, line in enumerate(text.splitlines(), 1):
                if pattern in line[:10_000]:
                    matches.append((line_number, line))
                    if len(matches) >= _MAX_SEARCH_RESULTS:
                        break
            if not matches:
                continue
            if output_mode == "files_only":
                results.append({"path": str(candidate)})
            elif output_mode == "count":
                results.append({"path": str(candidate), "count": len(matches)})
            else:
                for line_number, line in matches:
                    results.append(
                        {"path": str(candidate), "line": line_number, "content": line}
                    )
                    if len(results) >= _MAX_SEARCH_RESULTS:
                        break
            if len(results) >= _MAX_SEARCH_RESULTS:
                break
    else:
        return tool_error("target must be 'content' or 'files'")
    page = results[offset : offset + limit]
    return json.dumps(
        {
            "matches": page,
            "total_count": len(results),
            "truncated": offset + len(page) < len(results),
        },
        ensure_ascii=False,
    )


READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read UTF-8 text within the assigned workspace with line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 1},
            "limit": {"type": "integer", "default": 500},
        },
        "required": ["path"],
    },
}
WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Atomically write UTF-8 text within the assigned workspace.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
}
PATCH_SCHEMA = {
    "name": "patch",
    "description": "Apply a targeted replacement or V4A patch within the assigned workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["replace", "patch"], "default": "replace"},
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "patch": {"type": "string"},
        },
        "required": ["mode"],
    },
}
SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search UTF-8 files by literal content or file-name glob within the assigned workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "path": {"type": "string", "default": "."},
            "file_glob": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "default": "content"},
            "context": {"type": "integer", "default": 0},
        },
        "required": ["pattern"],
    },
}


def _handle_read(args, **kwargs):
    return read_file_tool(
        args.get("path", ""), args.get("offset", 1), args.get("limit", 500)
    )


def _handle_write(args, **kwargs):
    return write_file_tool(args.get("path", ""), args.get("content"))


def _handle_patch(args, **kwargs):
    return patch_tool(
        mode=args.get("mode", "replace"),
        path=args.get("path"),
        old_string=args.get("old_string"),
        new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False),
        patch=args.get("patch"),
    )


def _handle_search(args, **kwargs):
    return search_files_tool(
        pattern=args.get("pattern", ""),
        target=args.get("target", "content"),
        path=args.get("path", "."),
        file_glob=args.get("file_glob"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"),
        context=args.get("context", 0),
    )


def _register() -> None:
    registry.register("read_file", "file", READ_FILE_SCHEMA, _handle_read, lambda: True, emoji="📖", max_result_size_chars=100_000)
    registry.register("write_file", "file", WRITE_FILE_SCHEMA, _handle_write, lambda: True, emoji="✍️", max_result_size_chars=100_000)
    registry.register("patch", "file", PATCH_SCHEMA, _handle_patch, lambda: True, emoji="🔧", max_result_size_chars=100_000)
    registry.register("search_files", "file", SEARCH_FILES_SCHEMA, _handle_search, lambda: True, emoji="🔎", max_result_size_chars=100_000)


_register()
