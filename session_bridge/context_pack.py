from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

from hermes_state import SessionDB

from .models import ContextPack, Provider
from .store import SessionBridgeStore, _native_session_snapshot_identity


@dataclass(frozen=True)
class ContextPackRequest:
    source_session_id: str
    target_provider: Provider
    bridge_id: str
    source_cursor: str
    source_hash: str
    budget_chars: int
    stale: bool = False
    diverged: bool = False
    exact_cwd: str | None = None
    worktree_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SectionItem:
    section: str
    text: str
    priority: int
    order: int


@dataclass(frozen=True)
class _RecentItem:
    text: str
    tool_noise: bool = False


@dataclass(frozen=True)
class _SourceSnapshot:
    existing_pack: Mapping[str, Any] | None
    session: Mapping[str, Any] | None
    messages: Sequence[Mapping[str, Any]]
    external: Mapping[str, Any] | None
    native_identity: Mapping[str, str] | None
    activity_value_json: str | None
    target_session_id: str | None
    target_external: Mapping[str, Any] | None


_SECTION_ORDER = (
    "Identity / Snapshot",
    "Goal / Latest Intent",
    "Decisions and Constraints",
    "Unresolved Work",
    "Recent Turns",
    "Files",
    "Repository State",
    "Referenced MemPalace / GBrain Links",
    "Warnings",
)
_VARIABLE_SECTIONS = (
    "Goal / Latest Intent",
    "Decisions and Constraints",
    "Unresolved Work",
    "Recent Turns",
    "Files",
    "Repository State",
    "Referenced MemPalace / GBrain Links",
)

_DECISION_RE = re.compile(
    r"\b(?:decision|decided|we will|chose|chosen|approved|agreed)\b", re.IGNORECASE
)
_CONSTRAINT_RE = re.compile(
    r"\b(?:constraints?|must|must not|never|always|required?|only|do not|don't|cannot|can't)\b",
    re.IGNORECASE,
)
_OPEN_WORK_RE = re.compile(
    r"\b(?:todo|to-do|fixme|open question|unresolved|remaining|next steps?|follow[- ]?up|pending|not yet|needs? to)\b",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"(?<![\w:/\\])(?:[A-Za-z]:[\\/])?(?:[A-Za-z0-9_.-]+[\\/])+"
    r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}"
    r"|(?<![\w/\\])[A-Za-z0-9_-]+\.(?:py|pyi|js|jsx|ts|tsx|md|mdx|toml|yaml|yml|json|jsonl|sql|sh|ps1|css|scss|html|txt)\b",
    re.IGNORECASE,
)
_MEMORY_URI_RE = re.compile(
    r"(?:mempalace|gbrain)://[^\s<>{}\[\]]+",
    re.IGNORECASE,
)
_MEMPALACE_DRAWER_RE = re.compile(
    r"\bdrawer_[A-Za-z0-9][A-Za-z0-9_.-]*_[A-Fa-f0-9]{24}\b"
)
_MEMPALACE_CONTEXT_RE = re.compile(
    r"\bmempalace(?:\s+(?:drawer|record))?\s*(?::|=|at)?\s+"
    r"(?P<reference>drawer_[A-Za-z0-9][A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)
_GBRAIN_CONTEXT_RE = re.compile(
    r"\bgbrain(?:\s+(?:page|wiki))?\s*(?::|=|at)?\s+"
    r"(?P<reference>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)",
    re.IGNORECASE,
)
_GBRAIN_EXPLICIT_WIKI_RE = re.compile(
    r"\bgbrain\s+wiki\s+"
    r"\[\[(?P<reference>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)\]\]",
    re.IGNORECASE,
)
_GBRAIN_WIKI_RE = re.compile(
    r"\[\[(?P<reference>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)\]\]"
)
_GBRAIN_WIKI_NAMESPACES = frozenset({
    "companies",
    "concepts",
    "decisions",
    "events",
    "hermes",
    "people",
    "projects",
    "sessions",
    "systems",
    "tools",
})

_BEARER_RE = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/-]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_ASSIGNMENT_START_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:\\?[\"'])?(?:password|token)(?:\\?[\"'])?[ \t]*(?:=|:)[ \t]*"
)
_YAML_BLOCK_HEADER_RE = re.compile(r"^[|>](?:[+-]?[1-9]?|[1-9][+-]?)[ \t]*(?:#.*)?$")
_HEREDOC_HEADER_RE = re.compile(
    r"<<(?P<strip_tabs>-?)[ \t]*(?P<quote>[\"']?)"
    r"(?P<delimiter>[A-Za-z0-9_.-]+)(?P=quote)"
)
_POWERSHELL_HERE_STRING_HEADER_RE = re.compile(r"^@(?P<quote>[\"'])[ \t]*$")
_YAML_SEQUENCE_ITEM_PREFIX_RE = re.compile(r"^[ \t]*-[ \t]+$")
_PEER_KEY_RE = re.compile(
    r"^(?:-[ \t]+)?(?:"
    r"(?P<quote>[\"'])[A-Za-z_][A-Za-z0-9_.-]*(?P=quote)"
    r"|[A-Za-z_][A-Za-z0-9_.-]*)[ \t]*(?:=|:)"
)
_YAML_DOCUMENT_BOUNDARY_RE = re.compile(r"^(?:---|\.\.\.)(?:[ \t]+#.*)?$")
_GIT_STATUS_MAX_LINES = 200
_GIT_STATUS_MAX_CHARS = 32_768
_STALE_WARNING = (
    "- [stale source] The source refresh did not reach a confirmed current snapshot."
)
_DIVERGED_WARNING = (
    "- [diverged] Both linked descendants advanced; this pack does not merge them."
)


class ContextPackBuilder:
    """Build and persist a deterministic, bounded source snapshot handoff."""

    def __init__(self, db: SessionDB, store: SessionBridgeStore) -> None:
        self.db = db
        self.store = store

    def build(self, request: ContextPackRequest) -> ContextPack:
        self._validate_request(request)
        snapshot = self._read_source_snapshot(request)
        session = snapshot.session
        if session is None:
            raise KeyError(request.source_session_id)

        external = snapshot.external
        if external is not None:
            snapshot_mismatch = (
                external["last_native_cursor"] is not None
                and external["last_native_cursor"] != request.source_cursor
            ) or (
                external["last_native_hash"] is not None
                and external["last_native_hash"] != request.source_hash
            )
        elif snapshot.native_identity is not None:
            snapshot_mismatch = (
                snapshot.native_identity["cursor"] != request.source_cursor
                or snapshot.native_identity["source_hash"] != request.source_hash
            )
        else:
            snapshot_mismatch = False
        if snapshot_mismatch and not request.stale:
            raise ValueError("source snapshot identity mismatch")

        expected_pack_id = _stable_pack_id(request)
        existing = snapshot.existing_pack
        if existing is not None:
            self._validate_persisted_identity(
                existing,
                request=request,
                expected_pack_id=expected_pack_id,
                expected_target_session_id=snapshot.target_session_id,
            )
            return _context_pack_from_row(existing)

        messages = snapshot.messages
        target_session_id = snapshot.target_session_id
        target_external = snapshot.target_external
        snapshot_timestamp, warnings = self._snapshot_timestamp(
            session,
            messages,
            snapshot.activity_value_json,
        )
        if request.stale:
            warnings.append(_STALE_WARNING)
        if request.diverged:
            warnings.append(_DIVERGED_WARNING)
        if snapshot_mismatch:
            warnings.append(
                "- [snapshot identity mismatch] The requested cursor/hash differs from the latest indexed identity."
            )
        if request.exact_cwd is not None:
            warnings.append(_exact_cwd_instruction(request.exact_cwd))
        warnings.extend(f"- [{warning}]" for warning in request.worktree_warnings)

        repository_items, repository_warnings = self._repository_state(session)
        warnings.extend(repository_warnings)
        identity = self._identity_section(
            request,
            session,
            external,
            target_session_id,
            target_external,
            snapshot_timestamp,
        )
        other_items = self._extract_items(messages, repository_items)
        recent_items = self._recent_turns(messages)
        payload = self._render_bounded(
            request.budget_chars,
            identity=identity,
            other_items=other_items,
            recent_items=recent_items,
            warnings=warnings,
        )

        pack = ContextPack(
            id=expected_pack_id,
            bridge_id=request.bridge_id,
            source_session_id=request.source_session_id,
            target_session_id=target_session_id,
            source_cursor=request.source_cursor,
            source_hash=request.source_hash,
            budget_chars=request.budget_chars,
            payload=payload,
            created_at=snapshot_timestamp,
            immutable_at=None,
        )
        persisted = self._persist_pack_once(pack, request=request)
        return _context_pack_from_row(persisted)

    @staticmethod
    def _validate_request(request: ContextPackRequest) -> None:
        if not isinstance(request, ContextPackRequest):
            raise TypeError("request must be a ContextPackRequest")
        for label, value in (
            ("source session ID", request.source_session_id),
            ("bridge ID", request.bridge_id),
            ("source cursor", request.source_cursor),
            ("source hash", request.source_hash),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
        if request.target_provider not in (Provider.CLAUDE, Provider.CODEX):
            raise ValueError("target provider must be Claude or Codex")
        if (
            not isinstance(request.budget_chars, int)
            or isinstance(request.budget_chars, bool)
            or request.budget_chars <= 0
        ):
            raise ValueError("context budget must be a positive integer")
        if request.exact_cwd is not None and (
            not isinstance(request.exact_cwd, str)
            or not request.exact_cwd
            or not Path(request.exact_cwd).is_absolute()
            or any(character in request.exact_cwd for character in "\x00\r\n")
            or _redact(request.exact_cwd) != request.exact_cwd
        ):
            raise ValueError("exact cwd must be a safe absolute path")
        if not isinstance(request.worktree_warnings, tuple) or any(
            not isinstance(warning, str)
            or not warning
            or len(warning) > 1024
            or any(character in warning for character in "\x00\r\n")
            or not warning.startswith((
                "worktree_branch_drift: recorded=",
                "worktree_head_drift: recorded=",
            ))
            for warning in request.worktree_warnings
        ):
            raise ValueError("worktree warnings are malformed")

    def _read_source_snapshot(self, request: ContextPackRequest) -> _SourceSnapshot:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            conn.execute("BEGIN")
            try:
                existing_row = conn.execute(
                    """SELECT * FROM session_context_packs
                       WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                         AND budget_chars = ?""",
                    (
                        request.bridge_id,
                        request.source_cursor,
                        request.source_hash,
                        request.budget_chars,
                    ),
                ).fetchone()
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (request.source_session_id,),
                ).fetchone()
                message_rows = conn.execute(
                    """SELECT * FROM messages
                       WHERE session_id = ? ORDER BY id""",
                    (request.source_session_id,),
                ).fetchall()
                external_row = conn.execute(
                    "SELECT * FROM external_sessions WHERE session_id = ?",
                    (request.source_session_id,),
                ).fetchone()
                activity_row = conn.execute(
                    "SELECT value_json FROM session_bridge_state WHERE key = ?",
                    (f"session-bridge:external-activity:{request.source_session_id}",),
                ).fetchone()
                target_session_id = self._find_target_session_in_connection(
                    conn, request
                )
                target_external_row = (
                    conn.execute(
                        "SELECT * FROM external_sessions WHERE session_id = ?",
                        (target_session_id,),
                    ).fetchone()
                    if target_session_id is not None
                    else None
                )
            finally:
                conn.rollback()

        session = dict(session_row) if session_row is not None else None
        message_records = [dict(row) for row in message_rows]
        decode_content = self.db._decode_content
        if session is not None and session.get("source") == "session_bridge_profile":
            profile_matches: list[tuple[dict[str, Any], list[dict[str, Any]], Any]] = []
            with self.store._native_hermes_databases() as databases:
                for _profile, database, owned in databases:
                    if not owned or not self.store._profile_catalog_compatible(
                        database
                    ):
                        continue
                    with database._lock:
                        profile_conn = database._conn
                        assert profile_conn is not None
                        profile_session = profile_conn.execute(
                            "SELECT * FROM sessions WHERE id = ?",
                            (request.source_session_id,),
                        ).fetchone()
                        if profile_session is None:
                            continue
                        profile_messages = profile_conn.execute(
                            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                            (request.source_session_id,),
                        ).fetchall()
                    profile_matches.append((
                        dict(profile_session),
                        [dict(row) for row in profile_messages],
                        database._decode_content,
                    ))
            if len(profile_matches) != 1:
                raise ValueError("profile-native source identity is ambiguous")
            session, message_records, decode_content = profile_matches[0]
        native_identity = (
            _native_session_snapshot_identity(
                session,
                message_records,
                decode_content=decode_content,
            )
            if session is not None and external_row is None
            else None
        )
        messages: list[dict[str, Any]] = []
        for message in message_records:
            message["content"] = decode_content(message.get("content"))
            if message.get("tool_calls"):
                try:
                    message["tool_calls"] = json.loads(message["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    message["tool_calls"] = []
            if message.get("active") == 1:
                messages.append(message)
        return _SourceSnapshot(
            existing_pack=(dict(existing_row) if existing_row is not None else None),
            session=session,
            messages=messages,
            external=dict(external_row) if external_row is not None else None,
            native_identity=native_identity,
            activity_value_json=(
                activity_row["value_json"] if activity_row is not None else None
            ),
            target_session_id=target_session_id,
            target_external=(
                dict(target_external_row) if target_external_row is not None else None
            ),
        )

    def _validate_persisted_identity(
        self,
        row: Mapping[str, Any],
        *,
        request: ContextPackRequest,
        expected_pack_id: str,
        expected_target_session_id: str | None,
    ) -> None:
        if row["source_session_id"] != request.source_session_id:
            raise ValueError("context pack source identity mismatch")
        if row["id"] != expected_pack_id:
            safety_variant_ids = {
                _stable_pack_id_with_safety(request, stale, diverged)
                for stale in (False, True)
                for diverged in (False, True)
            }
            if row["id"] in safety_variant_ids:
                raise ValueError("context pack safety/snapshot identity mismatch")
            raise ValueError("context pack target-provider/snapshot identity mismatch")
        if row["target_session_id"] != expected_target_session_id:
            raise ValueError("context pack target identity mismatch")
        warnings = _rendered_section_body(row["payload"], "Warnings")
        warning_lines = set(warnings.splitlines()) if warnings is not None else set()
        if request.stale and _STALE_WARNING not in warning_lines:
            raise ValueError("context pack stale source warning missing")
        if request.diverged and _DIVERGED_WARNING not in warning_lines:
            raise ValueError("context pack diverged warning missing")
        if (
            request.exact_cwd is not None
            and _exact_cwd_instruction(request.exact_cwd) not in warning_lines
        ):
            raise ValueError("context pack exact cwd instruction missing")
        if any(
            f"- [{warning}]" not in warning_lines
            for warning in request.worktree_warnings
        ):
            raise ValueError("context pack worktree warning missing")

    def _persist_pack_once(
        self,
        pack: ContextPack,
        *,
        request: ContextPackRequest,
    ) -> dict[str, Any]:
        def _write(conn):
            conn.execute(
                """INSERT INTO session_context_packs (
                   id, bridge_id, source_session_id, target_session_id,
                   source_cursor, source_hash, budget_chars, payload, created_at,
                   immutable_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bridge_id, source_cursor, source_hash, budget_chars)
                   DO NOTHING""",
                (
                    pack.id,
                    pack.bridge_id,
                    pack.source_session_id,
                    pack.target_session_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                    pack.payload,
                    pack.created_at,
                    pack.immutable_at,
                ),
            )
            row = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND budget_chars = ?""",
                (
                    pack.bridge_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("context pack persistence conflict")
            persisted = dict(row)
            self._validate_persisted_identity(
                persisted,
                request=request,
                expected_pack_id=pack.id,
                expected_target_session_id=self._find_target_session_in_connection(
                    conn, request
                ),
            )
            return persisted

        return self.db._execute_write(_write)

    def _find_target_session(self, request: ContextPackRequest) -> str | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            return self._find_target_session_in_connection(conn, request)

    @staticmethod
    def _find_target_session_in_connection(
        conn: Any, request: ContextPackRequest
    ) -> str | None:
        row = conn.execute(
            """SELECT CASE
                          WHEN links.from_session_id = ? THEN links.to_session_id
                          ELSE links.from_session_id
                       END AS target_session_id
               FROM session_links AS links
               JOIN external_sessions AS target
                 ON target.session_id = CASE
                     WHEN links.from_session_id = ? THEN links.to_session_id
                     ELSE links.from_session_id
                    END
               WHERE links.bridge_id = ?
                 AND (? = links.from_session_id OR ? = links.to_session_id)
                 AND target.provider = ?
               ORDER BY CASE links.relation
                            WHEN 'continues' THEN 0
                            WHEN 'mirrors' THEN 1
                            ELSE 2
                        END,
                        links.created_at DESC,
                        links.id
               LIMIT 1""",
            (
                request.source_session_id,
                request.source_session_id,
                request.bridge_id,
                request.source_session_id,
                request.source_session_id,
                request.target_provider.value,
            ),
        ).fetchone()
        return row["target_session_id"] if row is not None else None

    def _snapshot_timestamp(
        self,
        session: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        activity_value_json: str | None,
    ) -> tuple[float, list[str]]:
        candidates: list[float] = []
        warnings: list[str] = []

        invalid_source_timestamp = False
        for value in (session.get("started_at"), session.get("ended_at")):
            if value is None:
                continue
            timestamp = _finite_timestamp(value)
            if timestamp is None:
                invalid_source_timestamp = True
            else:
                candidates.append(timestamp)
        for row in messages:
            timestamp = _finite_timestamp(row.get("timestamp"))
            if timestamp is None:
                invalid_source_timestamp = True
            else:
                candidates.append(timestamp)
        if invalid_source_timestamp:
            warnings.append(
                "- [invalid timestamp] Malformed or non-finite source timestamps were ignored; affected turns display @unknown."
            )

        activity: Mapping[str, Any] | None = None
        if activity_value_json is not None:
            try:
                decoded_activity = json.loads(activity_value_json)
                if not isinstance(decoded_activity, dict):
                    raise ValueError("activity state is not an object")
                activity = decoded_activity
            except (json.JSONDecodeError, TypeError, ValueError):
                warnings.append(
                    "- [invalid activity watermark] The persisted source activity state is malformed and was ignored."
                )
        if activity is not None:
            last_active = activity.get("last_active")
            activity_timestamp = (
                _finite_timestamp(last_active)
                if isinstance(last_active, (int, float))
                and not isinstance(last_active, bool)
                else None
            )
            if activity_timestamp is None:
                warnings.append(
                    "- [invalid activity watermark] The persisted source activity timestamp is not finite numeric data and was ignored."
                )
            else:
                candidates.append(activity_timestamp)
        if not candidates:
            warnings.append(
                "- [invalid timestamp fallback] No finite source timestamp was available; the deterministic epoch fallback was used."
            )
            return 0.0, warnings
        return max(candidates), warnings

    @staticmethod
    def _identity_section(
        request: ContextPackRequest,
        session: Mapping[str, Any],
        external: Mapping[str, Any] | None,
        target_session_id: str | None,
        target_external: Mapping[str, Any] | None,
        snapshot_timestamp: float,
    ) -> str:
        source_native_id = (
            external["native_id"] if external is not None else session["id"]
        )
        target_native_id = (
            target_external["native_id"] if target_external is not None else "(pending)"
        )
        values = (
            f"- Bridge ID: {request.bridge_id}",
            f"- Source canonical ID: {request.source_session_id}",
            f"- Source provider: {session['source']}",
            f"- Source native ID: {source_native_id}",
            f"- Target provider: {request.target_provider.value}",
            f"- Target canonical ID: {target_session_id or '(pending)'}",
            f"- Target native ID: {target_native_id}",
            f"- Source cursor: {request.source_cursor}",
            f"- Source hash: {request.source_hash}",
            f"- Snapshot timestamp: {snapshot_timestamp:.6f}",
        )
        return "\n".join(_redact(value) for value in values)

    def _repository_state(
        self, session: Mapping[str, Any]
    ) -> tuple[list[_SectionItem], list[str]]:
        cwd_value = session.get("cwd")
        cwd = str(cwd_value).strip() if cwd_value is not None else ""
        repo_root = session.get("git_repo_root")
        branch = session.get("git_branch")
        values: list[str] = [f"- Cwd: {cwd or '(missing)'}"]
        values.append(f"- Repository root: {repo_root or '(unknown)'}")
        values.append(f"- Recorded branch: {branch or '(unknown)'}")
        warnings: list[str] = []

        if not cwd:
            warnings.append(
                "- [missing cwd] The source session has no working directory; git state is unavailable."
            )
        elif not Path(cwd).is_dir():
            warnings.append(
                "- [missing cwd] The recorded working directory is unavailable; git state was not read."
            )
        else:
            try:
                completed = subprocess.run(
                    ["git", "-C", cwd, "status", "--short", "--branch"],
                    timeout=3,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                warnings.append(
                    "- [repository unavailable] Git status timed out after 3 seconds."
                )
            except OSError:
                warnings.append(
                    "- [repository unavailable] Git status could not be executed."
                )
            else:
                returncode = completed.returncode
                full_stdout = (
                    completed.stdout if isinstance(completed.stdout, str) else ""
                )
                bounded_stdout = full_stdout[: _GIT_STATUS_MAX_CHARS + 1]
                output_truncated = len(full_stdout) > _GIT_STATUS_MAX_CHARS
                # The mandated subprocess contract captures complete output. Drop
                # those full strings immediately after retaining the bounded copy.
                try:
                    completed.stdout = ""
                    completed.stderr = ""
                except (AttributeError, TypeError):
                    pass
                del full_stdout
                del completed
                if returncode != 0:
                    warnings.append(
                        "- [repository unavailable] The recorded cwd is not an accessible git repository."
                    )
                else:
                    status_lines = bounded_stdout.splitlines()
                    if len(status_lines) > _GIT_STATUS_MAX_LINES:
                        output_truncated = True
                    status_lines = status_lines[:_GIT_STATUS_MAX_LINES]
                    if status_lines:
                        values.extend(f"- Git status: {line}" for line in status_lines)
                    else:
                        values.append("- Git status: clean")
                    if output_truncated:
                        warnings.append(
                            "- [git output truncated] Git status exceeded the bounded line/character limit."
                        )

        return (
            [
                _SectionItem(
                    section="Repository State",
                    text=_redact(value),
                    priority=84 if index < 3 else 62,
                    order=index,
                )
                for index, value in enumerate(values)
            ],
            warnings,
        )

    @staticmethod
    def _extract_items(
        messages: Sequence[Mapping[str, Any]],
        repository_items: list[_SectionItem],
    ) -> list[_SectionItem]:
        items: list[_SectionItem] = []
        user_contents = [
            _compact(_redact(str(row["content"])))
            for row in messages
            if row.get("role") == "user"
            and isinstance(row.get("content"), str)
            and str(row["content"]).strip()
        ]
        if user_contents:
            items.append(
                _SectionItem(
                    "Goal / Latest Intent",
                    f"- Original goal: {_redact(user_contents[0])}",
                    94,
                    0,
                )
            )
            items.append(
                _SectionItem(
                    "Goal / Latest Intent",
                    f"- Latest user intent: {_redact(user_contents[-1])}",
                    100,
                    1,
                )
            )

        decision_lines: dict[str, tuple[int, str]] = {}
        open_lines: dict[str, tuple[int, str]] = {}
        file_stats: dict[str, list[int]] = {}
        memory_links: dict[str, int] = {}
        occurrence = 0
        for message_index, row in enumerate(messages):
            content = row.get("content")
            if isinstance(content, str):
                redacted_content = _redact(content)
                if row.get("role") in ("user", "assistant"):
                    for raw_line in redacted_content.splitlines():
                        line = _compact(raw_line)
                        if not line:
                            continue
                        if _DECISION_RE.search(line) or _CONSTRAINT_RE.search(line):
                            decision_lines[line] = (message_index, line)
                        if _OPEN_WORK_RE.search(line) or (
                            row.get("role") == "user" and line.endswith("?")
                        ):
                            open_lines[line] = (message_index, line)
                searchable = redacted_content
            else:
                searchable = ""
            tool_calls = row.get("tool_calls")
            if tool_calls:
                searchable += "\n" + json.dumps(
                    tool_calls,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                searchable = _redact(searchable)
            for match in _FILE_RE.finditer(searchable):
                path = match.group(0).replace("\\", "/")
                occurrence += 1
                stats = file_stats.setdefault(path, [0, 0])
                stats[0] += 1
                stats[1] = occurrence
            for link in _memory_references(searchable):
                occurrence += 1
                memory_links[link] = occurrence

        for order, (_, line) in enumerate(
            sorted(decision_lines.values(), key=lambda value: value[0])
        ):
            items.append(
                _SectionItem(
                    "Decisions and Constraints",
                    f"- {_redact(line)}",
                    78 + min(order, 8),
                    order,
                )
            )
        for order, (_, line) in enumerate(
            sorted(open_lines.values(), key=lambda value: value[0])
        ):
            items.append(
                _SectionItem(
                    "Unresolved Work",
                    f"- {_redact(line)}",
                    82 + min(order, 8),
                    order,
                )
            )

        ranked_files = sorted(
            file_stats.items(),
            key=lambda item: (-item[1][0], -item[1][1], item[0].casefold()),
        )
        for order, (path, (count, _)) in enumerate(ranked_files):
            items.append(
                _SectionItem(
                    "Files",
                    f"- {_redact(path)} (references: {count})",
                    max(45, 72 - order),
                    order,
                )
            )
        for order, (link, _) in enumerate(
            sorted(memory_links.items(), key=lambda item: item[1])
        ):
            items.append(
                _SectionItem(
                    "Referenced MemPalace / GBrain Links",
                    f"- {_redact(link)}",
                    55,
                    order,
                )
            )
        items.extend(repository_items)
        return items

    @staticmethod
    def _recent_turns(
        messages: Sequence[Mapping[str, Any]],
    ) -> list[_RecentItem]:
        result: list[_RecentItem] = []
        noise_count = 0

        def flush_noise() -> None:
            nonlocal noise_count
            if noise_count:
                suffix = "event" if noise_count == 1 else "events"
                result.append(
                    _RecentItem(
                        f"- [tool activity collapsed: {noise_count} {suffix}]",
                        tool_noise=True,
                    )
                )
                noise_count = 0

        for row in messages:
            role = str(row.get("role") or "").lower()
            content = row.get("content")
            noisy = bool(
                role == "tool"
                or row.get("tool_name")
                or row.get("tool_call_id")
                or row.get("tool_calls")
            )
            if noisy:
                if role == "assistant" and isinstance(content, str) and content.strip():
                    flush_noise()
                    result.append(
                        _RecentItem(_format_turn(role, content, row["timestamp"]))
                    )
                noise_count += 1
                continue

            flush_noise()
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            result.append(_RecentItem(_format_turn(role, content, row["timestamp"])))
        flush_noise()
        return result

    @staticmethod
    def _render_bounded(
        budget: int,
        *,
        identity: str,
        other_items: list[_SectionItem],
        recent_items: list[_RecentItem],
        warnings: list[str],
    ) -> str:
        bodies = {section: "" for section in _SECTION_ORDER}
        bodies["Identity / Snapshot"] = identity
        grouped = _group_section_items(other_items)
        for section, values in grouped.items():
            bodies[section] = "\n".join(item.text for item in values)
        bodies["Recent Turns"] = "\n".join(item.text for item in recent_items)
        bodies["Warnings"] = "\n".join(_redact(value) for value in warnings)
        full = _render_sections(bodies)
        if len(full) <= budget:
            return full

        bounded_warnings = [
            *warnings,
            "- [context truncated] Lower-priority context was omitted.",
        ]
        fixed_bodies = {section: "" for section in _SECTION_ORDER}
        fixed_bodies["Identity / Snapshot"] = identity
        fixed_bodies["Warnings"] = "\n".join(
            _redact(value) for value in bounded_warnings
        )
        fixed = _render_sections(fixed_bodies)
        if len(fixed) > budget:
            raise ValueError(
                "context budget is too small for mandatory snapshot identity and warnings"
            )

        available = budget - len(fixed)
        recent_reserve = math.ceil(available * 0.45)
        selected_recent = _select_recent(recent_items, recent_reserve)
        recent_body = "\n".join(item.text for item in selected_recent)
        selected_other = _select_other(other_items, available - len(recent_body))
        other_bodies = _selected_other_bodies(selected_other)
        other_length = sum(len(value) for value in other_bodies.values())

        # Unused non-transcript capacity flows back to recent raw turns. The first
        # pass reserves 45%; this pass can only increase that allocation.
        selected_recent = _select_recent(recent_items, available - other_length)
        recent_body = "\n".join(item.text for item in selected_recent)
        selected_other = _select_other(other_items, available - len(recent_body))
        other_bodies = _selected_other_bodies(selected_other)

        fixed_bodies.update(other_bodies)
        fixed_bodies["Recent Turns"] = recent_body
        rendered = _render_sections(fixed_bodies)
        if len(rendered) > budget:
            raise AssertionError("bounded context renderer exceeded its budget")
        return rendered


def _format_turn(role: str, content: str, timestamp: Any) -> str:
    indented = _redact(content.strip()).replace("\n", "\n  ")
    finite_timestamp = _finite_timestamp(timestamp)
    timestamp_label = (
        f"{finite_timestamp:.6f}" if finite_timestamp is not None else "unknown"
    )
    return f"- {role.upper()} @{timestamp_label}:\n  {indented}"


def _exact_cwd_instruction(exact_cwd: str) -> str:
    serialized_cwd = json.dumps(
        exact_cwd,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "- [exact cwd] Every command and file operation MUST pass "
        f"cwd={serialized_cwd}; sidebar project grouping is not cwd."
    )


def _finite_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _compact(value: str) -> str:
    return " ".join(value.split())


def _memory_references(value: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    uri_spans: list[tuple[int, int]] = []
    for match in _MEMORY_URI_RE.finditer(value):
        uri_spans.append(match.span())
        matches.append((match.start(), match.group(0).rstrip(".,;:!?)")))

    for pattern, group in (
        (_MEMPALACE_DRAWER_RE, 0),
        (_MEMPALACE_CONTEXT_RE, "reference"),
        (_GBRAIN_EXPLICIT_WIKI_RE, "reference"),
        (_GBRAIN_CONTEXT_RE, "reference"),
    ):
        for match in pattern.finditer(value):
            if any(start <= match.start() < end for start, end in uri_spans):
                continue
            reference = match.group(group).rstrip(".,;:!?)")
            matches.append((match.start(), reference))
    for match in _GBRAIN_WIKI_RE.finditer(value):
        reference = match.group("reference").rstrip(".,;:!?)")
        namespace = reference.partition("/")[0].casefold()
        if namespace in _GBRAIN_WIKI_NAMESPACES:
            matches.append((match.start(), reference))

    ordered: list[str] = []
    seen: set[str] = set()
    for _, reference in sorted(matches, key=lambda item: item[0]):
        if reference not in seen:
            ordered.append(reference)
            seen.add(reference)
    return ordered


def _redact(value: str) -> str:
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", value)
    redacted = _OPENAI_KEY_RE.sub("[REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED]", redacted)
    return _redact_assignments(redacted)


def redact_sensitive_text(value: str) -> str:
    """Redact supported credential forms from arbitrary bridge text."""
    return _redact(value)


def _redact_assignments(value: str) -> str:
    output: list[str] = []
    cursor = 0
    while match := _ASSIGNMENT_START_RE.search(value, cursor):
        output.append(value[cursor : match.end()])
        output.append("[REDACTED]")
        value_start = match.end()
        value_end = _assignment_value_end(
            value,
            value_start,
            key_indent=_assignment_key_indent(value, match.start()),
        )
        cursor = max(value_end, value_start)
    output.append(value[cursor:])
    return "".join(output)


def _assignment_value_end(value: str, start: int, *, key_indent: int) -> int:
    if start >= len(value):
        return start

    if value[start] in "\r\n":
        _, body_start = _line_end_and_next(value, start)
        return _indented_block_end(value, body_start, key_indent)

    line_end, next_line_start = _line_end_and_next(value, start)
    header = value[start:line_end]
    if _YAML_BLOCK_HEADER_RE.fullmatch(header):
        return _indented_block_end(value, next_line_start, key_indent)

    here_string = _POWERSHELL_HERE_STRING_HEADER_RE.fullmatch(header)
    if here_string is not None:
        return _terminated_line_block_end(
            value,
            next_line_start,
            terminator=f"{here_string.group('quote')}@",
            strip_tabs=False,
        )

    for delimiter in ('"""', "'''", r"\"\"\"", r"\'\'\'"):
        if value.startswith(delimiter, start):
            return _quoted_value_end(value, start, delimiter)

    for delimiter in ('"', "'", r"\"", r"\'"):
        if value.startswith(delimiter, start):
            return _quoted_value_end(value, start, delimiter)

    if value[start] in "{[":
        return _structured_value_end(value, start)

    heredoc = _HEREDOC_HEADER_RE.search(header)
    if heredoc is not None:
        return _terminated_line_block_end(
            value,
            next_line_start,
            terminator=heredoc.group("delimiter"),
            strip_tabs=bool(heredoc.group("strip_tabs")),
        )

    if _has_odd_trailing_backslashes(header):
        return _continued_value_end(value, start)

    index = start
    while index < line_end and value[index] not in ",;}]":
        index += 1
    return index


def _assignment_key_indent(value: str, assignment_start: int) -> int:
    line_start = (
        max(
            value.rfind("\n", 0, assignment_start),
            value.rfind("\r", 0, assignment_start),
        )
        + 1
    )
    prefix = value[line_start:assignment_start]
    if not prefix.strip(" \t"):
        return _indent_width(prefix)
    if _YAML_SEQUENCE_ITEM_PREFIX_RE.fullmatch(prefix):
        return len(prefix.expandtabs(8))
    return 0


def _line_end_and_next(value: str, start: int) -> tuple[int, int]:
    line_feed = value.find("\n", start)
    carriage_return = value.find("\r", start)
    candidates = [
        position for position in (line_feed, carriage_return) if position >= 0
    ]
    if not candidates:
        return len(value), len(value)
    end = min(candidates)
    next_start = end + 1
    if value[end] == "\r" and next_start < len(value) and value[next_start] == "\n":
        next_start += 1
    return end, next_start


def _indent_width(value: str) -> int:
    indentation = value[: len(value) - len(value.lstrip(" \t"))]
    return len(indentation.expandtabs(8))


def _indented_block_end(value: str, start: int, key_indent: int) -> int:
    line_start = start
    while line_start < len(value):
        line_end, next_line_start = _line_end_and_next(value, line_start)
        line = value[line_start:line_end]
        if not line.strip(" \t"):
            line_start = next_line_start
            continue
        indent = _indent_width(line)
        if indent > key_indent:
            line_start = next_line_start
            continue
        peer_content = line.lstrip(" \t")
        if _is_peer_boundary(peer_content):
            return _line_break_start_before(value, line_start)
        return len(value)
    return len(value)


def _line_break_start_before(value: str, line_start: int) -> int:
    if line_start >= 2 and value[line_start - 2 : line_start] == "\r\n":
        return line_start - 2
    if line_start >= 1 and value[line_start - 1] in "\r\n":
        return line_start - 1
    return line_start


def _is_peer_boundary(content: str) -> bool:
    return bool(
        _PEER_KEY_RE.match(content)
        or content.startswith("#")
        or _YAML_DOCUMENT_BOUNDARY_RE.fullmatch(content)
    )


def _terminated_line_block_end(
    value: str,
    start: int,
    *,
    terminator: str,
    strip_tabs: bool,
) -> int:
    line_start = start
    while line_start < len(value):
        line_end, next_line_start = _line_end_and_next(value, line_start)
        line = value[line_start:line_end]
        candidate = line.lstrip("\t") if strip_tabs else line
        if candidate == terminator:
            return line_end
        line_start = next_line_start
    return len(value)


def _continued_value_end(value: str, start: int) -> int:
    line_start = start
    while line_start < len(value):
        line_end, next_line_start = _line_end_and_next(value, line_start)
        line = value[line_start:line_end]
        if not _has_odd_trailing_backslashes(line):
            return line_end
        if next_line_start >= len(value):
            return len(value)
        line_start = next_line_start
    return len(value)


def _has_odd_trailing_backslashes(value: str) -> bool:
    stripped = value.rstrip(" \t")
    trailing_count = len(stripped) - len(stripped.rstrip("\\"))
    return trailing_count % 2 == 1


def _quoted_value_end(value: str, start: int, delimiter: str) -> int:
    index = start + len(delimiter)
    while index < len(value):
        delimiter_is_unescaped = (
            not delimiter.startswith("\\") or value[index - 1] != "\\"
        )
        if delimiter_is_unescaped and value.startswith(delimiter, index):
            return index + len(delimiter)
        if value[index] == "\\" and not delimiter.startswith("\\"):
            index += 2
        else:
            index += 1
    return len(value)


def _structured_value_end(value: str, start: int) -> int:
    closing_for = {"{": "}", "[": "]"}
    stack = [closing_for[value[start]]]
    index = start + 1
    quoted_delimiters = ('"""', "'''", r"\"\"\"", r"\'\'\'", '"', "'", r"\"", r"\'")
    while index < len(value):
        delimiter = next(
            (
                candidate
                for candidate in quoted_delimiters
                if value.startswith(candidate, index)
            ),
            None,
        )
        if delimiter is not None:
            index = _quoted_value_end(value, index, delimiter)
            continue
        character = value[index]
        if character in closing_for:
            stack.append(closing_for[character])
        elif character in "}]":
            if character != stack[-1]:
                return len(value)
            stack.pop()
            if not stack:
                return index + 1
        elif character == "\\":
            index += 1
        index += 1
    return len(value)


def _render_sections(bodies: Mapping[str, str]) -> str:
    return (
        "\n\n".join(
            f"## {section}\n{bodies.get(section, '')}" for section in _SECTION_ORDER
        )
        + "\n"
    )


def _rendered_section_body(payload: str, section: str) -> str | None:
    heading = f"## {section}\n"
    starts: list[int] = []
    if payload.startswith(heading):
        starts.append(len(heading))
    separator = f"\n\n{heading}"
    search_from = 0
    while (position := payload.find(separator, search_from)) >= 0:
        starts.append(position + len(separator))
        search_from = position + len(separator)
    if len(starts) != 1:
        return None
    start = starts[0]
    end = payload.find("\n\n## ", start)
    return payload[start:] if end < 0 else payload[start:end]


def _group_section_items(
    items: Sequence[_SectionItem],
) -> dict[str, list[_SectionItem]]:
    grouped: dict[str, list[_SectionItem]] = {}
    for item in items:
        grouped.setdefault(item.section, []).append(item)
    for values in grouped.values():
        values.sort(key=lambda item: item.order)
    return grouped


def _select_recent(items: Sequence[_RecentItem], budget: int) -> list[_RecentItem]:
    if budget <= 0:
        return []
    selected_reversed: list[_RecentItem] = []
    used = 0
    for item in reversed(items):
        separator = 1 if selected_reversed else 0
        cost = len(item.text) + separator
        if used + cost <= budget:
            selected_reversed.append(item)
            used += cost
            continue
        if item.tool_noise:
            continue
        marker = "\n  [turn truncated]"
        remaining = budget - used - separator
        keep = remaining - len(marker)
        if keep > 0:
            selected_reversed.append(_RecentItem(item.text[:keep] + marker))
        elif remaining > 0:
            selected_reversed.append(_RecentItem(item.text[:remaining]))
        break
    return list(reversed(selected_reversed))


def _select_other(items: Sequence[_SectionItem], budget: int) -> list[_SectionItem]:
    if budget <= 0:
        return []
    selected: list[_SectionItem] = []
    section_counts: dict[str, int] = {}
    used = 0
    for item in sorted(
        items, key=lambda value: (-value.priority, -value.order, value.text)
    ):
        separator = 1 if section_counts.get(item.section, 0) else 0
        cost = len(item.text) + separator
        if used + cost > budget:
            continue
        selected.append(item)
        section_counts[item.section] = section_counts.get(item.section, 0) + 1
        used += cost
    return selected


def _selected_other_bodies(items: Sequence[_SectionItem]) -> dict[str, str]:
    return {
        section: "\n".join(item.text for item in values)
        for section, values in _group_section_items(items).items()
    }


def _stable_pack_id(request: ContextPackRequest) -> str:
    return _stable_pack_id_with_safety(request, request.stale, request.diverged)


def _stable_pack_id_with_safety(
    request: ContextPackRequest, stale: bool, diverged: bool
) -> str:
    digest = hashlib.sha256()
    for value in (
        "context-pack-v2",
        request.bridge_id,
        request.source_session_id,
        request.target_provider.value,
        request.source_cursor,
        request.source_hash,
        str(request.budget_chars),
        f"stale={int(stale)}",
        f"diverged={int(diverged)}",
        request.exact_cwd or "",
        *request.worktree_warnings,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"pack:{digest.hexdigest()}"


def _context_pack_from_row(row: Mapping[str, Any]) -> ContextPack:
    return ContextPack(
        id=row["id"],
        bridge_id=row["bridge_id"],
        source_session_id=row["source_session_id"],
        target_session_id=row["target_session_id"],
        source_cursor=row["source_cursor"],
        source_hash=row["source_hash"],
        budget_chars=int(row["budget_chars"]),
        payload=row["payload"],
        created_at=float(row["created_at"]),
        immutable_at=(
            float(row["immutable_at"]) if row["immutable_at"] is not None else None
        ),
    )


__all__ = ["ContextPackBuilder", "ContextPackRequest"]
