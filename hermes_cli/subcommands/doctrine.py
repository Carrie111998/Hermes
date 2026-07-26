"""CLI for inspecting and versioning routing doctrine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hermes_cli.routing import bootstrap, drift, schema
from hermes_cli.sqlite_util import retrying_write_txn


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _load_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid doctrine plan at {source}: {exc}") from exc
    return bootstrap.validate_payload(payload, require_created_by=False)


def _rows_for_version(conn, version: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, version, lane, rung, complexity, primary_provider,
               primary_model, fallback_chain_json, forbid_paths_json,
               notes, priority, created_ts
          FROM routing_doctrine
         WHERE version = ?
         ORDER BY priority DESC, id ASC
        """,
        (int(version),),
    ).fetchall()
    return [
        {
            **dict(row),
            "fallback_chain": json.loads(row["fallback_chain_json"]),
            "forbid_paths": json.loads(row["forbid_paths_json"]),
        }
        for row in rows
    ]


def _active_version(conn) -> int:
    row = conn.execute(
        "SELECT active_version FROM routing_doctrine_meta WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("routing doctrine has no active version")
    return int(row["active_version"])


def _cmd_dump(args: argparse.Namespace) -> int:
    bootstrap.bootstrap_if_needed()
    conn = schema.connect()
    try:
        version = int(args.version) if args.version else _active_version(conn)
        rows = _rows_for_version(conn, version)
    finally:
        conn.close()
    if not rows:
        print(f"No routing doctrine rules for version {version}.")
        return 1
    print(json.dumps({"version": version, "rules": rows}, indent=2))
    return 0


def _rule_identity(rule: dict[str, Any]) -> tuple:
    return (
        rule["lane"],
        rule["rung"],
        rule["complexity"],
        rule["primary_provider"],
        rule["primary_model"],
    )


def _plan_diff(payload: dict[str, Any]) -> dict[str, int]:
    bootstrap.bootstrap_if_needed()
    conn = schema.connect()
    try:
        active = _rows_for_version(conn, _active_version(conn))
    finally:
        conn.close()
    current = {_rule_identity(rule): rule for rule in active}
    planned = {_rule_identity(rule): rule for rule in payload["rules"]}
    added = set(planned) - set(current)
    removed = set(current) - set(planned)
    changed = {
        key
        for key in set(current) & set(planned)
        if (
            current[key]["priority"] != planned[key]["priority"]
            or current[key]["notes"] != planned[key]["notes"]
            or current[key]["fallback_chain"] != planned[key]["fallback_chain"]
            or current[key]["forbid_paths"] != planned[key]["forbid_paths"]
        )
    }
    return {
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
    }


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        payload = _load_plan(args.file)
    except ValueError as exc:
        print(f"Invalid doctrine plan: {exc}")
        return 1
    diff = _plan_diff(payload)
    print(
        f"Doctrine plan: {diff['added']} added, "
        f"{diff['removed']} removed, {diff['changed']} changed."
    )
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    try:
        payload = _load_plan(args.file)
    except ValueError as exc:
        print(f"Invalid doctrine plan: {exc}")
        return 1
    diff = _plan_diff(payload)
    bootstrap.bootstrap_if_needed()
    conn = schema.connect()
    try:
        next_version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM routing_doctrine"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    if not args.confirm:
        print(
            f"Dry run: would create inactive doctrine version {next_version} "
            f"({diff['added']} added, {diff['removed']} removed, "
            f"{diff['changed']} changed). Re-run with --confirm."
        )
        return 0

    now = bootstrap.utc_now()
    conn = schema.connect()
    try:
        with retrying_write_txn(conn):
            for rule in payload["rules"]:
                bootstrap._insert_rule(
                    conn,
                    version=next_version,
                    rule=rule,
                    created_ts=now,
                )
    finally:
        conn.close()
    print(f"Created inactive doctrine version {next_version}.")
    return 0


def _version_exists(conn, version: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM routing_doctrine WHERE version = ? LIMIT 1",
            (int(version),),
        ).fetchone()
        is not None
    )


def _cmd_activate(args: argparse.Namespace) -> int:
    bootstrap.bootstrap_if_needed()
    target = int(args.version)
    conn = schema.connect()
    try:
        current = _active_version(conn)
        exists = _version_exists(conn, target)
    finally:
        conn.close()
    if not exists:
        print(f"Doctrine version does not exist: {target}")
        return 1
    if target == current:
        print(f"Doctrine version {target} is already active.")
        return 0
    if not args.confirm:
        print(
            f"Dry run: would activate doctrine version {target} and "
            f"deactivate version {current}. Re-run with --confirm."
        )
        return 0
    now = bootstrap.utc_now()
    conn = schema.connect()
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                UPDATE routing_doctrine_meta
                   SET active_version = ?,
                       previous_version = ?,
                       last_activated_ts = ?,
                       last_activated_by = 'cli'
                 WHERE singleton = 1
                """,
                (target, current, now),
            )
            conn.execute(
                """
                INSERT INTO routing_doctrine_activations (
                    activated_version, deactivated_version, activated_ts,
                    activated_by, activation_type, notes
                ) VALUES (?, ?, ?, 'cli', 'activate', ?)
                """,
                (target, current, now, f"activated version {target}"),
            )
    finally:
        conn.close()
    print(f"Activated doctrine version {target}; previous version {current}.")
    return 0


def _cmd_deactivate(args: argparse.Namespace) -> int:
    bootstrap.bootstrap_if_needed()
    conn = schema.connect()
    try:
        row = conn.execute(
            """
            SELECT active_version, previous_version
              FROM routing_doctrine_meta
             WHERE singleton = 1
            """
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["previous_version"] is None:
        print("No previous doctrine version is available.")
        return 1
    current = int(row["active_version"])
    previous = int(row["previous_version"])
    if not args.confirm:
        print(
            f"Dry run: would deactivate version {current} and restore "
            f"version {previous}. Re-run with --confirm."
        )
        return 0
    now = bootstrap.utc_now()
    conn = schema.connect()
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                UPDATE routing_doctrine_meta
                   SET active_version = ?,
                       previous_version = NULL,
                       last_activated_ts = ?,
                       last_activated_by = 'cli'
                 WHERE singleton = 1
                """,
                (previous, now),
            )
            conn.execute(
                """
                INSERT INTO routing_doctrine_activations (
                    activated_version, deactivated_version, activated_ts,
                    activated_by, activation_type, notes
                ) VALUES (?, ?, ?, 'cli', 'deactivate', ?)
                """,
                (
                    previous,
                    current,
                    now,
                    f"deactivated version {current}",
                ),
            )
    finally:
        conn.close()
    print(f"Restored doctrine version {previous}; deactivated {current}.")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    bootstrap.bootstrap_if_needed()
    conn = schema.connect()
    try:
        rows = conn.execute(
            """
            SELECT id, activated_version, deactivated_version, activated_ts,
                   activated_by, activation_type, notes
              FROM routing_doctrine_activations
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(args.limit),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No doctrine activation history.")
        return 0
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_decisions(args: argparse.Namespace) -> int:
    bootstrap.bootstrap_if_needed()
    clauses = []
    values: list[Any] = []
    for column, value in (
        ("task_id", args.task),
        ("session_id", args.session),
        ("profile", args.profile),
        ("route", args.route),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(str(value))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(int(args.limit))
    conn = schema.connect()
    try:
        rows = conn.execute(
            f"""
            SELECT *
              FROM routing_decisions
              {where}
             ORDER BY id DESC
             LIMIT ?
            """,
            tuple(values),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No routing decisions.")
        return 0
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    return 0


def _print_ranked(label: str, rows: list[tuple]) -> None:
    print(f"{label}:")
    if not rows:
        print("  (none)")
        return
    for row in rows:
        if len(row) == 2:
            print(f"  {row[0]}: {row[1]}")
        else:
            source, chosen, count = row
            print(
                f"  {source[0]}/{source[1]} -> "
                f"{chosen[0]}/{chosen[1]}: {count}"
            )


def _cmd_drift(args: argparse.Namespace) -> int:
    if args.refresh_all and args.explain:
        print("--refresh-all and --explain cannot be combined.")
        return 1
    if args.explain:
        try:
            bucket = drift._hour_bucket(args.explain)
        except ValueError as exc:
            print(f"Invalid bucket timestamp: {exc}")
            return 1
        start, end = drift._bucket_bounds(bucket)
        conn = schema.connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                  FROM routing_decisions
                 WHERE chosen_at >= ? AND chosen_at < ?
                 ORDER BY id ASC
                 LIMIT 100
                """,
                (start, end),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            print(f"No routing decisions for bucket {bucket}.")
            return 0
        for row in rows:
            print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
        return 0

    if args.refresh_all:
        bucket_count = drift.source_bucket_count()
        if not args.confirm:
            print(
                f"Dry run: would rebuild {bucket_count} buckets. "
                "Re-run with --confirm."
            )
            return 0
        rebuilt = drift.refresh_all_buckets()
        print(f"{rebuilt} buckets rebuilt.")
        return 0

    result = drift.compute_drift_window(
        hours=int(args.hours),
        lane=args.lane,
        profile=args.profile,
    )
    print(f"Doctrine drift (last {result['window_hours']}h)")
    print(f"Total decisions: {result['total_decisions']}")
    for name in ("followed", "overridden", "bypassed", "no_rule"):
        print(
            f"{name}: {result[f'{name}_count']} "
            f"({result[f'{name}_pct']:.1f}%)"
        )
    if int(result["forced_legacy_count"]) > 0:
        print(
            f"forced_legacy: {result['forced_legacy_count']} "
            f"({result['forced_legacy_pct']:.1f}%)"
        )
    if int(result["all_failed_count"]) > 0:
        print(
            f"all_failed: {result['all_failed_count']} "
            f"({result['all_failed_pct']:.1f}%)"
        )
    _print_ranked("Top override lanes", result["top_override_lanes"])
    _print_ranked("Top override callers", result["top_override_profiles"])
    _print_ranked("Top overridden model pairs", result["top_overridden_pairs"])
    if result["top_cascade_failure_classes"]:
        _print_ranked(
            "Top cascade failure classes",
            result["top_cascade_failure_classes"],
        )
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctrine",
        help="Inspect and version Atlas routing doctrine",
    )
    children = parser.add_subparsers(dest="doctrine_command", required=True)

    dump = children.add_parser("dump", help="Print doctrine rules")
    dump.add_argument("--version", type=_positive_int)
    dump.set_defaults(func=_cmd_dump)

    plan = children.add_parser("plan", help="Validate and diff a doctrine file")
    plan.add_argument("file")
    plan.set_defaults(func=_cmd_plan)

    apply = children.add_parser(
        "apply",
        help="Create an inactive doctrine version",
    )
    apply.add_argument("file")
    apply.add_argument("--confirm", action="store_true")
    apply.set_defaults(func=_cmd_apply)

    activate = children.add_parser(
        "activate",
        help="Activate an existing doctrine version",
    )
    activate.add_argument("version", type=_positive_int)
    activate.add_argument("--confirm", action="store_true")
    activate.set_defaults(func=_cmd_activate)

    deactivate = children.add_parser(
        "deactivate",
        help="Restore the previous doctrine version",
    )
    deactivate.add_argument("--confirm", action="store_true")
    deactivate.set_defaults(func=_cmd_deactivate)

    history = children.add_parser(
        "history",
        help="List doctrine activation events",
    )
    history.add_argument("--limit", type=_positive_int, default=50)
    history.set_defaults(func=_cmd_history)

    decisions = children.add_parser(
        "decisions",
        help="List audited routing decisions",
    )
    decisions.add_argument("--limit", type=_positive_int, default=50)
    decisions.add_argument("--task")
    decisions.add_argument("--session")
    decisions.add_argument("--profile")
    decisions.add_argument("--route")
    decisions.set_defaults(func=_cmd_decisions)

    drift_parser = children.add_parser(
        "drift",
        help="Summarize and reconcile doctrine routing drift",
    )
    drift_parser.add_argument("--hours", type=_positive_int, default=24)
    drift_parser.add_argument("--lane")
    drift_parser.add_argument("--profile")
    drift_parser.add_argument("--refresh-all", action="store_true")
    drift_parser.add_argument("--confirm", action="store_true")
    drift_parser.add_argument("--explain")
    drift_parser.set_defaults(func=_cmd_drift)

    # CS-13 end-to-end doctrine round-trip smoke.
    from hermes_cli.smoke.roundtrip import register_cli as _register_smoke_cli

    _register_smoke_cli(children)

    # CS-19 read-only Monday cutover rehearsal. It is registered here so the
    # established doctrine integration remains the sole main-parser seam.
    from hermes_cli.subcommands.cutover import register_cli as _register_cutover_cli

    _register_cutover_cli(subparsers)


__all__ = ["register_cli"]
