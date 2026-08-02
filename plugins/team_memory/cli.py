"""Operator CLI for reviewed team memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hermes_cli.config import cfg_get, load_config_readonly

from . import storage


def _config() -> dict:
    try:
        value = load_config_readonly()
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _configured_workspace() -> str:
    cfg = _config()
    return str(
        cfg_get(
            cfg,
            "team_memory",
            "workspace_id",
            default=cfg_get(cfg, "team_memory", "workspace", default=""),
        )
        or ""
    ).strip()


def _workspace(args: argparse.Namespace, *, required: bool = True) -> str:
    value = str(getattr(args, "workspace", "") or _configured_workspace()).strip()
    if required and not value:
        raise ValueError(
            "workspace is required; pass --workspace or configure "
            "team_memory.workspace_id"
        )
    return value


def _db(args: argparse.Namespace) -> Path | None:
    value = str(getattr(args, "db", "") or "").strip()
    return Path(value).expanduser() if value else None


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="team_memory_action")

    init = subs.add_parser("init", help="Create or migrate the scoped SQLite store")
    init.add_argument("--workspace", required=True)
    init.add_argument("--db", default="", help="Optional explicit database path")

    status = subs.add_parser("status", help="Show scope, paths, and row counts")
    status.add_argument("--workspace", default="")
    status.add_argument("--db", default="")

    search = subs.add_parser("search", help="Search reviewed shared memory")
    search.add_argument("query")
    search.add_argument("--workspace", default="")
    search.add_argument("--category", default="all")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--db", default="")

    listing = subs.add_parser("list", aliases=["ls"], help="List stored memory entries")
    listing.add_argument("--workspace", default="")
    listing.add_argument("--category", default="")
    listing.add_argument("--project-id", default="")
    listing.add_argument(
        "--include-expired",
        action="store_true",
        help="Include expired entries for operator audit",
    )
    listing.add_argument("--db", default="")

    add = subs.add_parser("add", help="Add one manually reviewed memory entry")
    add.add_argument("--workspace", default="")
    add.add_argument("--project-id", default="")
    add.add_argument("--category", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--author", required=True)
    add.add_argument("--tags", default="")
    add.add_argument("--memory-key", default="")
    add.add_argument("--source-type", default="manual")
    add.add_argument("--source-ref", default="")
    add.add_argument("--review-status", choices=["approved", "draft", "archived"], default="approved")
    add.add_argument(
        "--valid-until",
        default="",
        help="Optional ISO-8601 expiry (for example 2026-12-31T23:59:59Z)",
    )
    add.add_argument("--replace", action="store_true")
    add.add_argument("--db", default="")

    delete = subs.add_parser("delete", help="Delete one entry from the current workspace")
    delete.add_argument("memory_id", type=int)
    delete.add_argument("--workspace", default="")
    delete.add_argument("--db", default="")
    delete.add_argument("--yes", action="store_true", help="Confirm deletion")

    migrate = subs.add_parser("migrate", help="Repair schema and rebuild FTS5")
    migrate.add_argument("--workspace", required=True)
    migrate.add_argument("--db", default="")

    metrics = subs.add_parser("metrics", help="Show recorded search metrics")
    metrics.add_argument("--workspace", default="")
    metrics.add_argument("--agent-variant", default="")
    metrics.add_argument("--metrics-db", default="")

    uninstall = subs.add_parser("uninstall", help="Remove only Stage 1 memory files")
    uninstall.add_argument("--db", default="")
    uninstall.add_argument("--metrics-db", default="")
    uninstall.add_argument("--yes", action="store_true", help="Confirm irreversible removal")

    subparser.set_defaults(func=team_memory_command)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def team_memory_command(args: argparse.Namespace) -> int:
    action = getattr(args, "team_memory_action", None)
    try:
        if action in {"init", "migrate"}:
            path = storage.init_database(_db(args), workspace_id=_workspace(args))
            print(f"initialized: {path}")
            return 0
        if action == "status":
            cfg = _config()
            path = _db(args) or storage.get_db_path(cfg)
            metrics_path = (
                storage.get_metrics_path(cfg)
                if not _db(args)
                else storage.get_metrics_path(cfg, db_path=path)
            )
            workspace = _workspace(args, required=False)
            result = {
                "enabled": cfg_get(cfg, "team_memory", "enabled", default=cfg_get(cfg, "features", "team_memory", default=False)),
                "workspace_id": workspace or None,
                "database_path": str(path),
                "metrics_path": str(metrics_path),
                "database_exists": path.exists(),
            }
            if path.exists() and workspace:
                result["memory_count"] = len(storage.list_all_memories(workspace_id=workspace, db_path=path))
            _print_json(result)
            return 0
        if action == "search":
            category = None if args.category in {"", "all"} else args.category
            rows = storage.search_memory(
                args.query,
                category=category,
                limit=args.limit,
                workspace_id=_workspace(args),
                db_path=_db(args),
            )
            _print_json(rows)
            return 0
        if action in {"list", "ls"}:
            rows = storage.list_all_memories(
                category=args.category or None,
                project_id=args.project_id or None,
                workspace_id=_workspace(args),
                include_expired=args.include_expired,
                db_path=_db(args),
            )
            _print_json(rows)
            return 0
        if action == "add":
            memory_id = storage.add_memory(
                args.category,
                args.title,
                args.content,
                args.author,
                args.tags,
                workspace_id=_workspace(args),
                project_id=args.project_id,
                source_type=args.source_type,
                source_ref=args.source_ref,
                review_status=args.review_status,
                valid_until=args.valid_until or None,
                memory_key=args.memory_key or None,
                replace=args.replace,
                db_path=_db(args),
            )
            print(f"memory_id: {memory_id}")
            return 0
        if action == "delete":
            if not args.yes:
                print("refusing to delete without --yes")
                return 2
            deleted = storage.delete_memory(
                args.memory_id,
                workspace_id=_workspace(args),
                db_path=_db(args),
            )
            print("deleted" if deleted else "not found")
            return 0 if deleted else 1
        if action == "metrics":
            rows = storage.get_query_metrics(
                args.agent_variant or None,
                workspace_id=_workspace(args),
                metrics_path=(Path(args.metrics_db).expanduser() if args.metrics_db else None),
            )
            _print_json(rows)
            return 0
        if action == "uninstall":
            if not args.yes:
                print("refusing to remove database files without --yes")
                return 2
            removed = storage.uninstall_database(
                _db(args),
                metrics_path=(Path(args.metrics_db).expanduser() if args.metrics_db else None),
            )
            print("removed" if removed else "nothing to remove")
            return 0
        print("usage: hermes team-memory {init,status,search,list,add,delete,migrate,metrics,uninstall}")
        return 2
    except Exception as exc:
        print(f"team-memory: {type(exc).__name__}: {exc}")
        return 1
