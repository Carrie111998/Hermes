"""``hermes sessions import`` — import external coding-agent sessions.

Currently supports Claude Code session transcripts (``~/.claude/projects/
*.jsonl``) and writes them as native Hermes sessions via the SessionDB write
API, so they become searchable with ``session_search`` and show up in
``hermes sessions list`` with ``source=claude_code``.

Import is idempotent: sessions already in the store are skipped unless the
source JSONL has grown (Claude Code still running / session re-opened), in
which case the transcript is atomically replaced via
:meth:`SessionDB.replace_messages` — no duplicates either way.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_state import SessionDB

# Safety caps (mirror SessionDB._IMPORT_* limits).
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_MESSAGES_PER_SESSION = 10_000


class ImportReport:
    """Aggregated outcome of one import run (one host)."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.imported: int = 0
        self.updated: int = 0
        self.skipped: int = 0
        self.failed: int = 0
        self.errors: List[str] = []

    def summary(self) -> str:
        return (
            f"{self.host}: {self.imported} imported, {self.updated} updated, "
            f"{self.skipped} skipped, {self.failed} failed"
        )


def parse_claude_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], Optional[str], bool, Optional[str]]:
    """Parse one Claude Code session JSONL into Hermes-shaped messages.

    Returns ``(messages, title, ended, started_ts)``. Records without a
    user/assistant message (``system``, ``mode``, ``last-prompt``,
    ``fork-context-ref``, …) are skipped; a torn tail line (Claude still
    appending) is skipped rather than aborting the file.
    """
    messages: List[Dict[str, Any]] = []
    title: Optional[str] = None
    ended = False
    started_ts: Optional[str] = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail write — Claude still appending

            ltype = rec.get("type")
            ts = rec.get("timestamp")

            if ltype == "summary":
                ended = True
                if not title and rec.get("summary"):
                    title = str(rec["summary"])[:120]
                continue
            if ltype == "ai-title":
                if not title and rec.get("aiTitle"):
                    title = str(rec["aiTitle"])[:120]
                continue
            if ltype not in ("user", "assistant"):
                continue

            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                if not content.strip():
                    continue
                if started_ts is None and ts:
                    started_ts = ts
                messages.append({
                    "role": "user" if ltype == "user" else "assistant",
                    "content": content,
                    "timestamp": ts,
                })
                continue

            if not isinstance(content, list):
                continue

            if started_ts is None and ts:
                started_ts = ts

            if ltype == "user":
                # blocks: tool_result (→ role=tool), text (→ role=user)
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_result":
                        result = _block_text(block.get("content"))
                        if not result:
                            continue
                        messages.append({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": block.get("tool_use_id"),
                            "timestamp": ts,
                        })
                    elif btype == "text":
                        text = block.get("text") or ""
                        if text.strip():
                            messages.append({
                                "role": "user",
                                "content": text,
                                "timestamp": ts,
                            })
            else:  # assistant
                tool_calls = []
                text_parts = []
                reasoning: Optional[str] = None
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text") or "")
                    elif btype == "thinking":
                        thinking = block.get("thinking")
                        if thinking:
                            reasoning = reasoning or thinking
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "tool_calls": tool_calls,
                        "reasoning": reasoning,
                        "timestamp": ts,
                    })
                elif text_parts and "".join(text_parts).strip():
                    messages.append({
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "reasoning": reasoning,
                        "timestamp": ts,
                    })

    return messages, title, ended, started_ts


def _block_text(content: Any) -> str:
    """Flatten a Claude block content (str, list of parts, or None) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "image":
                    continue
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def _message_count(db: SessionDB, session_id: str) -> int:
    try:
        msgs = db.get_messages(session_id)
        return len(msgs) if msgs else 0
    except Exception:  # noqa: BLE001 — count is a hint, not a gate
        return 0


def _ts_to_float(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def import_claude_sessions(
    projects_dir: str,
    host: str,
    db: Optional[SessionDB] = None,
    dry_run: bool = False,
    include_subagents: bool = False,
) -> ImportReport:
    """Import every Claude Code session under ``projects_dir``.

    Session ids are deterministic (``claude_<host>_<session-uuid>``) so
    re-runs dedupe. When a stored session's message count differs from the
    source file (the JSONL grew), the transcript is atomically replaced via
    ``replace_messages``.
    """
    report = ImportReport(host)
    root = Path(projects_dir).expanduser()
    if not root.is_dir():
        report.failed += 1
        report.errors.append(f"{root} is not a directory")
        return report

    files = sorted(
        p for p in root.rglob("*.jsonl")
        if not p.name.startswith(".")
        and (include_subagents or "subagents" not in p.parts)
    )
    if not files:
        report.skipped += 1
        return report

    own_db = False
    if db is None:
        db = SessionDB()
        own_db = True
    assert db is not None

    try:
        for fpath in files:
            size = fpath.stat().st_size
            if size == 0:
                report.skipped += 1
                continue
            if size > MAX_FILE_BYTES:
                print(f"  SKIP {fpath.name}: {size / 1e6:.1f}MB > {MAX_FILE_BYTES // 1048576}MB cap")
                report.skipped += 1
                continue

            session_id = f"claude_{host}_{fpath.stem}"

            try:
                messages, title, ended, _ = parse_claude_jsonl(fpath)
            except Exception as exc:  # noqa: BLE001 — keep importing the rest
                print(f"  FAIL {fpath.name}: {exc}")
                report.failed += 1
                report.errors.append(f"{fpath.name}: {exc}")
                continue

            if not messages:
                report.skipped += 1
                continue
            if len(messages) > MAX_MESSAGES_PER_SESSION:
                messages = messages[:MAX_MESSAGES_PER_SESSION]

            existing = db.get_session(session_id)
            existing_count = _message_count(db, session_id) if existing else 0

            if existing and existing_count == len(messages):
                report.skipped += 1
                continue

            if dry_run:
                action = "UPDATE" if existing else "IMPORT"
                print(f"  [{action}] {session_id}  {len(messages)} msgs")
                report.imported += 1
                continue

            if existing:
                # JSONL grew while Claude was still running: atomic replace.
                db.replace_messages(session_id, messages)
                report.updated += 1
                continue

            db.create_session(
                session_id,
                source="claude_code",
                model="claude-code",
                cwd=str(fpath.parent),
                profile_name=host,
            )
            for m in messages:
                db.append_message(
                    session_id,
                    role=m["role"],
                    content=m.get("content"),
                    tool_calls=m.get("tool_calls"),
                    tool_call_id=m.get("tool_call_id"),
                    reasoning=m.get("reasoning"),
                    timestamp=_ts_to_float(m.get("timestamp")) or None,
                )
            if title:
                try:
                    db.set_session_title(session_id, title)
                except ValueError:
                    pass
            if ended:
                db.end_session(session_id, "ended")
            report.imported += 1
    finally:
        if own_db:
            db.close()

    return report


def build_import_session_parser(subparsers) -> None:
    """Attach the ``sessions import`` subcommand parser."""
    p = subparsers.add_parser(
        "import",
        help="Import sessions from another agent (Claude Code)",
        description=(
            "Import external coding-agent session transcripts as native "
            "Hermes sessions. Currently supports Claude Code "
            "(~/.claude/projects/*.jsonl) with source='claude_code'."
        ),
    )
    p.add_argument(
        "format",
        choices=["claude-code", "claude_code"],
        help="Which agent's session format to import",
    )
    p.add_argument(
        "projects_dir",
        help="Path to the agent's projects directory "
        "(e.g. ~/.claude/projects)",
    )
    p.add_argument(
        "--host",
        default="remote",
        help="Host label baked into session ids (default: remote)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report only — write nothing",
    )
    p.add_argument(
        "--db",
        default=None,
        help="state.db path (default: Hermes default)",
    )
    p.add_argument(
        "--include-subagents",
        action="store_true",
        help="Also import subagents/*.jsonl child sessions (default: skip)",
    )


def cmd_sessions_import(args) -> int:
    """Handler for ``hermes sessions import`` (injected from main())."""
    if args.format not in ("claude-code", "claude_code"):
        print(f"Error: unsupported format '{args.format}'")
        return 2

    db: Optional[SessionDB] = None
    if args.db:
        db = SessionDB(db_path=Path(args.db))

    started = time.time()
    try:
        report = import_claude_sessions(
            projects_dir=args.projects_dir,
            host=args.host,
            db=db,
            dry_run=args.dry_run,
            include_subagents=args.include_subagents,
        )
    finally:
        if db is not None:
            db.close()

    print(f"\n=== {report.summary()} ({time.time() - started:.1f}s) ===")
    for err in report.errors:
        print(f"  error: {err}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    # Standalone entry point for manual runs outside the hermes CLI.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("projects_dir", help="Path to ~/.claude/projects")
    ap.add_argument("--host", default="remote", help="Host label for session ids")
    ap.add_argument("--db", default=None, help="state.db path (default: Hermes default)")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report only")
    ap.add_argument("--include-subagents", action="store_true",
                    help="Also import subagents/*.jsonl child sessions")
    args = ap.parse_args()
    sys.exit(cmd_sessions_import(args))
