"""Fail-closed control guard for Phase-1 managed Kanban tasks.

The durable ``kanban_control_bindings`` row is the authority.  Interactive
surfaces may mutate an ordinary task as before, but a task carrying a non-empty
Phase-1 policy can only be changed by the exact authenticated gateway origin
that created it.  Dashboard, Desktop, TUI and plain CLI calls have no such
origin and therefore remain read-only for managed tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit


CONTROL_IDENTITY_FIELDS = (
    "platform",
    "scope_id",
    "chat_type",
    "chat_id",
    "thread_id",
    "user_id",
    "notifier_profile",
    "session_key",
)
CONTROL_REQUIRED_FIELDS = (
    "platform",
    "chat_type",
    "chat_id",
    "user_id",
    "notifier_profile",
    "session_key",
)

MANAGED_CONTROL_DENIED_MESSAGE = (
    "这个短任务只能从最初创建它的飞书群，由同一位发起人操作；"
    "当前入口没有权限，因此没有做任何更改。"
)


# Pure lexical/path helpers live here because both the model tool producer and
# gateway notifier need the same rule.  This module intentionally has no
# gateway/plugin imports and performs no import-time filesystem access.
_MANAGED_PATH_EXTENSIONS = (
    "png|jpg|jpeg|gif|webp|bmp|tiff|svg|mp4|mov|avi|mkv|webm|mp3|wav|"
    "ogg|opus|m4a|flac|pdf|docx|doc|odt|rtf|txt|md|epub|xlsx|xls|ods|"
    "csv|tsv|json|xml|yaml|yml|pptx|ppt|odp|key|zip|tar|gz|tgz|bz2|"
    "xz|7z|rar|apk|ipa|html|htm|py|pyi|js|jsx|ts|tsx|mjs|cjs|sh|zsh|"
    "bash|toml|ini|cfg|sql|sqlite|sqlite3|db|log"
)
_MANAGED_LOCAL_PATH_RE = re.compile(
    r'''(?P<quoted>[`"'](?:file://)?(?:~/|/|[A-Za-z]:[/\\]|\.\.?[/\\]|'''
    r'''(?:[A-Za-z0-9_.-]+[/\\])+)[^`"'\r\n]+[`"'])|'''
    r'''(?P<rooted>(?<![A-Za-z0-9_:/.-])(?:file://)?'''
    r'''(?:~/|/|[A-Za-z]:[/\\]|\.\.?[/\\])'''
    r'''[^\s<>\[\]{}"'`]+)|'''
    r'''(?P<relative>(?<![A-Za-z0-9_:/.-])'''
    r'''(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+)''',
    re.IGNORECASE,
)
_MANAGED_BARE_FILE_RE = re.compile(
    r'''(?<![A-Za-z0-9_./:-])(?:[A-Za-z0-9_.-]+)\.(?:'''
    + _MANAGED_PATH_EXTENSIONS
    + r''')(?=[\s,;:)}\]，。；：、）】]|$)''',
    re.IGNORECASE,
)
_MANAGED_PATH_TRAILING = "`\"',.;:)}]，。；：、）】"


def extract_managed_local_path_mentions(text: str) -> list[str]:
    """Extract local-path mentions without stat/open or other host I/O."""
    if not isinstance(text, str) or not text:
        return []
    scan = re.sub(r"(?i)\bMEDIA:\s*", " ", text)
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        candidate = str(raw or "").strip()
        if (
            len(candidate) >= 2
            and candidate[0] == candidate[-1]
            and candidate[0] in "`\"'"
        ):
            candidate = candidate[1:-1].strip()
        candidate = candidate.rstrip(_MANAGED_PATH_TRAILING)
        if candidate.lower().startswith(("http://", "https://")):
            return
        # These two control commands appear in normal handoffs and are not host
        # paths. Every other rooted token remains visible to the guard,
        # including a single-component absolute file such as ``/secret``.
        if candidate in {"/stop", "/new"}:
            return
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    for match in _MANAGED_LOCAL_PATH_RE.finditer(scan):
        _add(match.group(0))
    for match in _MANAGED_BARE_FILE_RE.finditer(scan):
        _add(match.group(0))
    return found


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_managed_workspace_path(
    path: str,
    workspace: str,
    *,
    require_file: bool = False,
) -> str | None:
    """Resolve a path inside one frozen workspace, rejecting escapes.

    Obvious absolute and ``..`` escapes are rejected lexically before a strict
    candidate probe.  Inside-looking paths are then realpath-resolved so an
    in-workspace symlink cannot point the producer/notifier back outside.
    """
    raw_workspace = str(workspace or "").strip()
    raw_path = str(path or "").strip()
    if not raw_workspace or not raw_path:
        return None
    if (
        len(raw_path) >= 2
        and raw_path[0] == raw_path[-1]
        and raw_path[0] in "`\"'"
    ):
        raw_path = raw_path[1:-1].strip()
    raw_path = raw_path.rstrip(_MANAGED_PATH_TRAILING)
    if raw_path.lower().startswith("media:"):
        raw_path = raw_path[6:].strip()
    if raw_path.lower().startswith("file://"):
        parsed = urlsplit(raw_path)
        if parsed.scheme.lower() != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        raw_path = unquote(parsed.path)
    if os.name != "nt" and re.match(r"^[A-Za-z]:[/\\]", raw_path):
        return None
    try:
        root = Path(os.path.expanduser(raw_workspace)).resolve(strict=True)
        if not root.is_dir():
            return None
        candidate = Path(os.path.expanduser(raw_path))
        if not candidate.is_absolute():
            candidate = root / candidate
        lexical_candidate = Path(os.path.abspath(str(candidate)))
        raw_root = Path(os.path.abspath(os.path.expanduser(raw_workspace)))
        if not (
            _path_is_within(lexical_candidate, root)
            or _path_is_within(lexical_candidate, raw_root)
        ):
            return None
        resolved = candidate.resolve(strict=require_file)
    except (OSError, RuntimeError, ValueError):
        return None
    if not _path_is_within(resolved, root):
        return None
    if require_file and not resolved.is_file():
        return None
    return str(resolved)


@dataclass(frozen=True)
class ManagedControlDecision:
    allowed: bool
    managed: bool
    task_id: str | None = None
    reason: str = ""


def _clean_identity(identity: Mapping[str, Any] | None) -> dict[str, str]:
    source = identity if isinstance(identity, Mapping) else {}
    values = {
        name: str(source.get(name) or "").strip()
        for name in CONTROL_IDENTITY_FIELDS
    }
    values["platform"] = values["platform"].lower()
    values["chat_type"] = values["chat_type"].lower()
    return values


def _managed_binding_rows(
    conn: sqlite3.Connection,
    task_id: str,
) -> list[sqlite3.Row]:
    """Return every asserted Phase-1 authority row for one task.

    A malformed non-empty value is still authority.  Treating it as ordinary
    would turn corrupt state into permission, so validation happens only after
    the task has already been classified as managed.
    """
    return list(
        conn.execute(
            "SELECT platform, scope_id, chat_type, chat_id, thread_id, "
            "user_id, notifier_profile, session_key, short_handoff_policy "
            "FROM kanban_control_bindings "
            "WHERE task_id = ? AND short_handoff_policy != ''",
            (str(task_id),),
        ).fetchall()
    )


def authorize_managed_task_mutation(
    conn: sqlite3.Connection,
    task_id: str,
    control_identity: Mapping[str, Any] | None,
) -> ManagedControlDecision:
    """Authorize a mutation without changing ordinary Kanban behavior."""
    rows = _managed_binding_rows(conn, task_id)
    if not rows:
        return ManagedControlDecision(True, False, str(task_id), "ordinary")
    if len(rows) != 1:
        return ManagedControlDecision(False, True, str(task_id), "ambiguous")

    caller = _clean_identity(control_identity)
    if any(not caller[name] for name in CONTROL_REQUIRED_FIELDS):
        return ManagedControlDecision(False, True, str(task_id), "identity_missing")

    row = rows[0]
    binding = _clean_identity(dict(row))
    if any(caller[name] != binding[name] for name in CONTROL_IDENTITY_FIELDS):
        return ManagedControlDecision(False, True, str(task_id), "identity_mismatch")

    # Validate the frozen proof after identity matching.  Corrupt or forged
    # policy text remains protected and cannot be revived by a matching caller.
    try:
        from agent.kanban_handoff_scope import canonical_task_policy

        canonical_task_policy(
            row["short_handoff_policy"],
            control_identity=binding,
        )
    except Exception:
        return ManagedControlDecision(False, True, str(task_id), "binding_invalid")
    return ManagedControlDecision(True, True, str(task_id), "origin_allowed")


def authorize_managed_task_mutations(
    conn: sqlite3.Connection,
    task_ids: Sequence[str],
    control_identity: Mapping[str, Any] | None,
) -> ManagedControlDecision:
    """Require authority for every distinct target before any write begins."""
    seen: set[str] = set()
    managed = False
    for raw_task_id in task_ids:
        task_id = str(raw_task_id or "").strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        decision = authorize_managed_task_mutation(
            conn,
            task_id,
            control_identity,
        )
        managed = managed or decision.managed
        if not decision.allowed:
            return decision
    return ManagedControlDecision(True, managed, reason="all_allowed")


def all_managed_task_ids(conn: sqlite3.Connection) -> list[str]:
    """Return protected task ids, including malformed asserted bindings."""
    return [
        str(row["task_id"])
        for row in conn.execute(
            "SELECT DISTINCT task_id FROM kanban_control_bindings "
            "WHERE short_handoff_policy != '' ORDER BY task_id"
        ).fetchall()
    ]


def managed_dispatch_write_target_ids(conn: sqlite3.Connection) -> list[str]:
    """Return managed tasks a dispatcher tick can write in its current state.

    Completed or archived history is deliberately excluded once it is fully
    drained.  A still-open exit gate remains active evidence: dispatch can
    release the gate, clear run identity, append events, promote a successor,
    or clean a workspace even when the parent task already says ``done``.
    """
    rows = conn.execute(
        "SELECT DISTINCT b.task_id "
        "FROM kanban_control_bindings b "
        "JOIN tasks t ON t.id = b.task_id "
        "WHERE b.short_handoff_policy != '' AND ("
        "  t.status IN ('todo', 'blocked', 'ready', 'running', 'review') "
        "  OR EXISTS ("
        "    SELECT 1 FROM task_exit_gates g "
        "    WHERE g.released_at IS NULL "
        "      AND (g.parent_task_id = t.id OR g.child_task_id = t.id)"
        "  )"
        ") ORDER BY b.task_id"
    ).fetchall()
    return [str(row["task_id"]) for row in rows]


__all__ = [
    "CONTROL_IDENTITY_FIELDS",
    "CONTROL_REQUIRED_FIELDS",
    "MANAGED_CONTROL_DENIED_MESSAGE",
    "ManagedControlDecision",
    "all_managed_task_ids",
    "authorize_managed_task_mutation",
    "authorize_managed_task_mutations",
    "extract_managed_local_path_mentions",
    "managed_dispatch_write_target_ids",
    "resolve_managed_workspace_path",
]
