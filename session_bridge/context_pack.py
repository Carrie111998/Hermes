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
from .store import SessionBridgeStore


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
    r"(?:mempalace|gbrain)://[^\s<>{}\[\]]+|https?://[^\s<>{}\[\]]*(?:mempalace|gbrain)[^\s<>{}\[\]]*",
    re.IGNORECASE,
)
_MEMPALACE_DRAWER_RE = re.compile(r"\bdrawer_[A-Za-z0-9][A-Za-z0-9_.-]*")
_GBRAIN_CONTEXT_RE = re.compile(
    r"\bgbrain(?:\s+(?:page|wiki))?\s*(?::|=|at)?\s+"
    r"(?P<reference>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)",
    re.IGNORECASE,
)
_GBRAIN_WIKI_RE = re.compile(
    r"\[\[(?P<reference>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)\]\]"
)

_BEARER_RE = re.compile(
    r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/-]{8,}"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_GENERIC_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:password|token)\s*=\s*)"
    r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s;,]+)'
)


class ContextPackBuilder:
    """Build and persist a deterministic, bounded source snapshot handoff."""

    def __init__(self, db: SessionDB, store: SessionBridgeStore) -> None:
        self.db = db
        self.store = store

    def build(self, request: ContextPackRequest) -> ContextPack:
        self._validate_request(request)
        session = self.db.get_session(request.source_session_id)
        if session is None:
            raise KeyError(request.source_session_id)

        expected_pack_id = _stable_pack_id(request)
        existing = self._get_exact_pack(request)
        if existing is not None:
            self._validate_persisted_identity(
                existing,
                request=request,
                expected_pack_id=expected_pack_id,
                expected_target_session_id=self._find_target_session(request),
            )
            return _context_pack_from_row(existing)

        messages = self.db.get_messages(request.source_session_id)
        external = self.store.get_external_session(request.source_session_id)
        target_session_id = self._find_target_session(request)
        target_external = (
            self.store.get_external_session(target_session_id)
            if target_session_id is not None
            else None
        )
        snapshot_timestamp, warnings = self._snapshot_timestamp(
            request.source_session_id,
            session,
            messages,
        )
        if request.stale:
            warnings.append(
                "- [stale source] The source refresh did not reach a confirmed current snapshot."
            )
        if request.diverged:
            warnings.append(
                "- [diverged] Both linked descendants advanced; this pack does not merge them."
            )
        if external is not None and (
            (
                external["last_native_cursor"] is not None
                and external["last_native_cursor"] != request.source_cursor
            )
            or (
                external["last_native_hash"] is not None
                and external["last_native_hash"] != request.source_hash
            )
        ):
            warnings.append(
                "- [snapshot identity mismatch] The requested cursor/hash differs from the latest indexed identity."
            )

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

    def _get_exact_pack(self, request: ContextPackRequest) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
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
        return dict(row) if row is not None else None

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
            raise ValueError("context pack target-provider/snapshot identity mismatch")
        if row["target_session_id"] != expected_target_session_id:
            raise ValueError("context pack target identity mismatch")

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
        source_session_id: str,
        session: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[float, list[str]]:
        candidates = [float(session["started_at"])]
        ended_at = session.get("ended_at")
        if ended_at is not None:
            ended_timestamp = float(ended_at)
            if math.isfinite(ended_timestamp):
                candidates.append(ended_timestamp)
        candidates.extend(
            timestamp
            for row in messages
            if math.isfinite(timestamp := float(row["timestamp"]))
        )

        warnings: list[str] = []
        try:
            activity = self.store.get_state(
                f"session-bridge:external-activity:{source_session_id}"
            )
        except (TypeError, ValueError):
            activity = None
            warnings.append(
                "- [invalid activity watermark] The persisted source activity state is malformed and was ignored."
            )
        if activity is not None:
            last_active = activity.get("last_active")
            if (
                not isinstance(last_active, (int, float))
                or isinstance(last_active, bool)
                or not math.isfinite(float(last_active))
            ):
                warnings.append(
                    "- [invalid activity watermark] The persisted source activity timestamp is not finite numeric data and was ignored."
                )
            else:
                candidates.append(float(last_active))
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
                if completed.returncode != 0:
                    warnings.append(
                        "- [repository unavailable] The recorded cwd is not an accessible git repository."
                    )
                else:
                    status_lines = completed.stdout.splitlines()
                    if status_lines:
                        values.extend(f"- Git status: {line}" for line in status_lines)
                    else:
                        values.append("- Git status: clean")

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
            _compact(str(row["content"]))
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
                for raw_line in content.splitlines():
                    line = _compact(raw_line)
                    if not line:
                        continue
                    if _DECISION_RE.search(line) or _CONSTRAINT_RE.search(line):
                        decision_lines[line] = (message_index, line)
                    if _OPEN_WORK_RE.search(line) or (
                        row.get("role") == "user" and line.endswith("?")
                    ):
                        open_lines[line] = (message_index, line)
                searchable = content
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
    return f"- {role.upper()} @{float(timestamp):.6f}:\n  {indented}"


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
        (_GBRAIN_CONTEXT_RE, "reference"),
        (_GBRAIN_WIKI_RE, "reference"),
    ):
        for match in pattern.finditer(value):
            if any(start <= match.start() < end for start, end in uri_spans):
                continue
            reference = match.group(group).rstrip(".,;:!?)")
            matches.append((match.start(), reference))
    return [reference for _, reference in sorted(matches, key=lambda item: item[0])]


def _redact(value: str) -> str:
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", value)
    redacted = _OPENAI_KEY_RE.sub("[REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED]", redacted)
    return _GENERIC_ASSIGNMENT_RE.sub(r"\1[REDACTED]", redacted)


def _render_sections(bodies: Mapping[str, str]) -> str:
    return (
        "\n\n".join(
            f"## {section}\n{bodies.get(section, '')}" for section in _SECTION_ORDER
        )
        + "\n"
    )


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
            selected_reversed.append(_RecentItem(item.text[:keep].rstrip() + marker))
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
    digest = hashlib.sha256()
    for value in (
        "context-pack-v1",
        request.bridge_id,
        request.source_session_id,
        request.target_provider.value,
        request.source_cursor,
        request.source_hash,
        str(request.budget_chars),
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
