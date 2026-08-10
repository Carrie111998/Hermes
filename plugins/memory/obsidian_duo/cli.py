"""Plugin-native diagnostics for Memory Duo."""

from __future__ import annotations

import json
from pathlib import Path


COMMANDS = ("status", "doctor", "rebuild-index", "reconcile", "pending", "conflicts", "stats")


def _paths():
    from hermes_constants import get_hermes_home
    from .config import ObsidianDuoConfig

    home = Path(get_hermes_home())
    config = ObsidianDuoConfig.load(home)
    return home, config


def run_diagnostics() -> dict:
    try:
        home, config = _paths()
    except Exception as exc:
        return {"config": "unavailable", "message": type(exc).__name__}
    vault = Path(config.vault_path)
    db_path = home / "obsidian_duo" / "memory.db"
    try:
        from .vault import ObsidianVault
        checked_vault = ObsidianVault(vault, config.managed_folder)
        managed_root_valid = checked_vault.managed_root.is_dir() or not checked_vault.managed_root.exists()
    except Exception:
        managed_root_valid = False
    result = {
        "vault_reachable": vault.is_dir(),
        "managed_folder": (vault / config.managed_folder).is_dir(),
        "managed_root_valid": managed_root_valid,
        "database": db_path.exists(),
    }
    if db_path.exists():
        import sqlite3
        try:
            with sqlite3.connect(db_path) as conn:
                result["sqlite_integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                result["incomplete_journal"] = conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE state != 'committed'"
                ).fetchone()[0]
        except sqlite3.Error:
            result["sqlite_integrity"] = False
    return result


def _open_broker():
    from .broker import EmbeddedMemoryBroker
    from .policy import MemoryPolicy
    from .retrieval import MemoryRetriever
    from .store import SqliteMemoryStore
    from .vault import ObsidianVault

    home, config = _paths()
    store = SqliteMemoryStore(home / "obsidian_duo" / "memory.db")
    vault = ObsidianVault(Path(config.vault_path), config.managed_folder)
    broker = EmbeddedMemoryBroker(
        config=config, store=store, vault=vault, policy=MemoryPolicy(),
        retriever=MemoryRetriever(store),
    )
    broker.start()
    return broker


def obsidian_duo_command(args) -> None:
    command = getattr(args, "obsidian_duo_command", "status")
    if command == "doctor":
        result = run_diagnostics()
    elif command in {"status", "rebuild-index", "reconcile", "pending", "conflicts", "stats"}:
        broker = _open_broker()
        try:
            if command == "status":
                result = {"status": broker.status().__dict__}
            elif command == "rebuild-index":
                result = {"rebuild": broker.vault.rebuild_from_vault(
                    broker.store, full=bool(getattr(args, "full", False))
                ).__dict__}
            elif command == "reconcile":
                recovery = broker.recover()
                result = {
                    "reconciled": broker.process_manual_changes(),
                    "recovered": recovery.recovered,
                    "malformed": recovery.malformed,
                }
            elif command == "pending":
                rows = broker.store.connection().execute(
                    "SELECT candidate_id, payload, status, created_at FROM candidates ORDER BY created_at"
                ).fetchall()
                result = {"pending": [dict(row) for row in rows]}
            elif command == "conflicts":
                rows = broker.store.connection().execute(
                    "SELECT memory_id, conflicting_memory_id, reason, status FROM conflicts ORDER BY conflict_id"
                ).fetchall()
                result = {"conflicts": [dict(row) for row in rows]}
            else:
                rows = broker.store.connection().execute(
                    "SELECT name, value FROM metrics ORDER BY name"
                ).fetchall()
                result = {"stats": {row["name"]: row["value"] for row in rows}}
        finally:
            broker.shutdown(1.0)
    else:
        result = {"command": command, **run_diagnostics()}
    print(json.dumps(result, sort_keys=True, default=str))


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="obsidian_duo_command")
    rebuild = subs.add_parser("rebuild-index", help="Rebuild the managed-memory index")
    rebuild.add_argument("--full", action="store_true")
    for command in ("status", "doctor", "reconcile", "pending", "conflicts", "stats"):
        subs.add_parser(command)
    subparser.set_defaults(handler_fn=obsidian_duo_command)
