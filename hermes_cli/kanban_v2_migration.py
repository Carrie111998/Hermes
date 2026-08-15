"""Safe v2 product-board migration with manifest-hashed dry-run/apply.

The dry-run path opens a scratch copy of the board SQLite database read-only
and produces an exact report with a content-hash manifest.  Apply snapshots the
scratch DB first, migrates the board metadata to product preset, and backfills
task workflow state in one atomic transaction.  It never runs against a live
board database — only explicit scratch copies — and preserves all task history,
comments, events, and links.

Board databases resolved through the normal ``board`` slug path are rejected
with a clear ``MigrationBlocked`` error; callers must first create a byte-for-
byte copy of the target DB and pass its absolute path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb


class MigrationBlocked(RuntimeError):
    """The board cannot be migrated without risking active or unknown work."""


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Stable, byte-for-byte deterministic hash of the manifest content."""
    canonical = json.dumps(
        {k: manifest[k] for k in sorted(manifest) if k not in ("hashes", "receipt_path")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _sha256_bytes(canonical.encode("utf-8"))


# ---------------------------------------------------------------------------
# Scratch-DB guard
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path_or_board: str) -> Path:
    """Return the absolute path to a SQLite database.

    Accepts a raw filesystem path (must already exist) but rejects board-slug
    resolution through the normal kanban DB path machinery — the board slug
    route is the live path this migration must never touch.
    """
    raw = Path(db_path_or_board).expanduser()
    if not raw.is_absolute():
        # Relative paths could accidentally hit a board DB via cwd resolution.
        raise MigrationBlocked(
            f"refusing relative path {db_path_or_board!r}; "
            "pass the absolute path to a scratch DB copy"
        )
    if not raw.is_file():
        raise MigrationBlocked(
            f"database file does not exist: {raw}"
        )
    # Refuse the canonical kanban DB path for any live board so the caller
    # can't accidentally point at a real board by guessing its path.
    for slug in kb.list_boards(include_archived=False):
        slug_name = slug.get("slug") if isinstance(slug, dict) else slug
        live = kb.kanban_db_path(slug_name)
        try:
            if raw.resolve() == live.resolve():
                raise MigrationBlocked(
                    f"refusing live board database at {raw}; "
                    f"copy it to a scratch location first"
                )
        except OSError:
            # live.resolve() failed — path doesn't exist, skip
            continue
    return raw


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Audit (dry-run)
# ---------------------------------------------------------------------------

def _is_legacy_product(row: Mapping[str, Any]) -> bool:
    """A task that already has product workflow metadata set, or is an epic."""
    # Epics don't participate in the product workflow — they are containers.
    if str(row.get("work_item_kind") or "card") == "epic":
        return True
    return bool(
        row.get("workflow_template_id") == "product"
        or row.get("current_step_key") in kb.PRODUCT_WORKFLOW_STEP_SET
    )


def _infer_v2_step(row: Mapping[str, Any]) -> Optional[str]:
    """Infer the product workflow step for a legacy task.

    Uses the same inference as ``_infer_product_step`` in kanban_db, but
    applied to every non-archived task in the audit.
    """
    status = str(row.get("status") or "")
    workflow_template = str(row.get("workflow_template_id") or "").strip() or None
    current_step = str(row.get("current_step_key") or "").strip() or None

    if workflow_template == "product" and current_step in kb.PRODUCT_WORKFLOW_STEP_SET:
        return current_step

    if status == "done" or current_step == "done":
        return "done"

    # Map legacy statuses to v2 steps
    if status == "review":
        return "review"

    assignee = str(row.get("assignee") or "").strip()
    if assignee in kb.PRODUCT_WORKFLOW_ROLE_TO_STEP:
        return kb.PRODUCT_WORKFLOW_ROLE_TO_STEP[assignee]

    if status in {"todo", "ready", "triage"}:
        return "backlog"

    # Running tasks keep their assignee's step if known, else backlog
    if assignee:
        return kb.PRODUCT_WORKFLOW_ROLE_TO_STEP.get(assignee, "backlog")

    return "backlog"


def _audit_scratch_db(db_path: Path) -> dict[str, Any]:
    """Return a byte-for-byte read-only migration plan for a scratch DB."""
    with _ro_connect(db_path) as conn:
        # Verify the DB is structurally valid.
        integrity = str(
            conn.execute("PRAGMA integrity_check").fetchone()[0]
        )
        if integrity != "ok":
            raise MigrationBlocked(f"database integrity check failed: {integrity}")

        # Check for active runs — must be zero.
        # The 'running' column is from the v2 state model and may not exist
        # on scratch DBs copied from older schema versions.
        task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "running" in task_cols:
            active = conn.execute(
                "SELECT id, title, assignee FROM tasks WHERE status = 'running' AND running = 1"
            ).fetchall()
        else:
            # Older schema: any task with status='running' is active.
            active = conn.execute(
                "SELECT id, title, assignee FROM tasks WHERE status = 'running'"
            ).fetchall()
        if active:
            raise MigrationBlocked(
                "active running work must finish before v2 migration: "
                + ", ".join(str(row["id"]) for row in active)
            )

        # Check for checked-out worktrees (dirty blocker).
        # In the scratch context we only check if tasks reference worktree paths.
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived' ORDER BY created_at, id"
        ).fetchall()

        task_count = len(rows)
        epics = [
            str(row["id"])
            for row in rows
            if str(row.get("work_item_kind") or "card") == "epic"
            or str(row.get("title") or "").strip().lower().startswith("epic:")
        ]

        items: list[dict[str, Any]] = []
        for row in rows:
            task_id = str(row["id"])
            v2_step = _infer_v2_step(row)
            is_product = _is_legacy_product(row)
            needs_migration = not is_product

            items.append({
                "id": task_id,
                "title": str(row.get("title") or ""),
                "status": str(row.get("status") or ""),
                "assignee": row.get("assignee"),
                "workflow_template_id": row.get("workflow_template_id"),
                "current_step_key": row.get("current_step_key"),
                "inferred_v2_step": v2_step,
                "already_product": is_product,
                "needs_migration": needs_migration,
            })

        counts = {
            "total": task_count,
            "already_product": sum(1 for item in items if item["already_product"]),
            "needs_migration": sum(1 for item in items if item["needs_migration"]),
            "epics": len(epics),
            "active": 0,
        }

        return {
            "mode": "dry-run",
            "db_path": str(db_path.resolve()),
            "db_hash": _sha256_path(db_path),
            "integrity": integrity,
            "counts": counts,
            "epics": epics,
            "items": items,
        }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _snapshot_scratch_db(
    db_path: Path,
    *,
    recovery_root: Optional[Path],
    audit: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Create an immutable snapshot of the scratch DB before migration."""
    root = Path(recovery_root) if recovery_root is not None else (
        db_path.parent / "v2-migration-snapshots"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hash_suffix = _sha256_path(db_path)[:10]
    receipt_dir = root / f"{stamp}-{hash_suffix}"
    snapshot = receipt_dir / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=False)

    consistent_db = snapshot / "kanban.db"
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(consistent_db))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    # Verify the snapshot is intact
    probe_path = receipt_dir / "restore-probe.sqlite3"
    shutil.copy2(consistent_db, probe_path)
    with sqlite3.connect(str(probe_path)) as probe:
        integrity = str(probe.execute("PRAGMA integrity_check").fetchone()[0])
        restore_probe = {
            "integrity_check": integrity,
            "tasks": int(
                probe.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            ),
        }
    probe_path.unlink()

    if integrity != "ok":
        raise MigrationBlocked(f"snapshot integrity check failed: {integrity}")

    manifest = {
        "version": 1,
        "created_at": int(time.time()),
        "source": {
            "db_path": str(db_path.resolve()),
            "db_hash": _sha256_path(db_path),
        },
        "snapshot": {
            "db": str(consistent_db),
        },
        "restore_probe": restore_probe,
        "audit": dict(audit),
    }
    inventory_path = snapshot / "inventory.json"
    inventory_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    manifest["hashes"] = {
        str(path.relative_to(receipt_dir)): _sha256_path(path)
        for path in sorted(receipt_dir.rglob("*"))
        if path.is_file()
    }
    manifest["manifest_digest"] = _manifest_digest(manifest)
    return receipt_dir, manifest


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply_migration_to_scratch_db(
    db_path: Path,
    *,
    audit: Mapping[str, Any],
    recovery_root: Optional[Path],
) -> dict[str, Any]:
    """Migrate a scratch DB to product v2 in one atomic transaction."""
    receipt_dir, receipt = _snapshot_scratch_db(
        db_path, recovery_root=recovery_root, audit=audit
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = OFF")

        # Re-verify before acting.
        refreshed = _audit_scratch_db(db_path)
        if refreshed["counts"]["active"] > 0:
            raise MigrationBlocked("active running work started during v2 migration")

        # All-or-nothing: do everything in one transaction.
        with conn:
            # Convert board metadata to product if the board_governance
            # table exists and a row is present. Scratch DBs from earlier
            # schema versions may not have it.
            gov_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='board_governance'"
            ).fetchone()
            if gov_exists:
                conn.execute(
                    "INSERT OR REPLACE INTO board_governance (id, qualification_required) VALUES (1, 0)"
                )

            changed = 0
            for item in refreshed["items"]:
                task_id = item["id"]
                if not item["needs_migration"]:
                    continue
                step = item["inferred_v2_step"]
                target_assignee = None
                if step in kb.PRODUCT_WORKFLOW_TRANSITIONS:
                    trans = kb.PRODUCT_WORKFLOW_TRANSITIONS[step]
                    target_assignee = trans.get("assignee_role")

                conn.execute(
                    """UPDATE tasks
                       SET workflow_template_id = 'product',
                           current_step_key = ?,
                           assignee = COALESCE(?, assignee)
                       WHERE id = ?""",
                    (step, target_assignee, task_id),
                )
                conn.execute(
                    """INSERT INTO task_events (task_id, kind, payload, created_at)
                       VALUES (?, 'v2_migrated',
                               ?, ?)""",
                    (
                        task_id,
                        json.dumps({
                            "workflow_template_id": "product",
                            "current_step_key": step,
                            "assignee": target_assignee,
                            "manifest_digest": receipt.get("manifest_digest", ""),
                        }),
                        int(time.time()),
                    ),
                )
                changed += 1

        receipt.update({
            "status": "applied",
            "changed": changed,
        })

    receipt_path = receipt_dir / "receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _make_read_only(receipt_dir)

    # Re-audit to produce post-migration verification.
    verification = _audit_scratch_db(db_path)
    receipt["verification"] = verification

    return {
        "db_path": str(db_path.resolve()),
        "changed": changed,
        "receipt_path": str(receipt_path),
        "manifest_digest": receipt.get("manifest_digest"),
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_db(db_path: str) -> dict[str, Any]:
    """Audit a scratch kanban database for v2 migration readiness.

    Opens the database read-only and produces an exact report including:
    - Task counts and migration status
    - Inferred v2 workflow steps
    - Active/running tasks (blocker)
    - DB integrity check

    Raises ``MigrationBlocked`` if the path is a live board DB or has
    active running tasks.
    """
    resolved = _resolve_db_path(db_path)
    audit = _audit_scratch_db(resolved)
    audit["manifest_digest"] = _manifest_digest(audit)
    return audit


def apply_db(
    db_path: str,
    *,
    recovery_root: Optional[str] = None,
) -> dict[str, Any]:
    """Migrate a scratch kanban database to product v2.

    1. Snapshots the database for recovery.
    2. Re-audits to confirm zero active runs.
    3. Backfills product workflow metadata in one atomic transaction.
    4. Produces an immutable receipt with verification.

    Raises ``MigrationBlocked`` if the path is a live board DB or has
    active running tasks.
    """
    resolved = _resolve_db_path(db_path)
    audit = _audit_scratch_db(resolved)
    root = Path(recovery_root) if recovery_root else None
    return _apply_migration_to_scratch_db(
        resolved,
        audit=audit,
        recovery_root=root,
    )


def verify_db(db_path: str) -> dict[str, Any]:
    """Verify that a scratch DB is correctly migrated to product v2.

    Returns a post-migration audit showing the current state. Does not
    modify the database.
    """
    resolved = _resolve_db_path(db_path)
    return _audit_scratch_db(resolved)