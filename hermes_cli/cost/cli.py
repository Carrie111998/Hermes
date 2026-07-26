"""CLI reporting for the CS-02 spend ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from hermes_cli.cost import caps, config, ledger


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _cmd_today(_args: argparse.Namespace) -> int:
    from hermes_cli.cost.kill_switch import list_killed_tasks

    killed = list_killed_tasks(
        since_ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    )
    if killed:
        print(f"Per-task killed today: {len(killed)}")
        for row in killed:
            print(
                f"  {row['task_id']} {row.get('lane') or '-'} "
                f"reason={row['reason']} at {row['killed_ts']}"
            )
    print("Hermes cost today (UTC; thresholds are advisory)")
    for lane, cap in config.LANE_DAILY_CAPS_AUD.items():
        print(f"{lane}: AUD {caps.daily_spend_aud(lane):.2f} / {cap:.2f}")
    print(
        "escalation: "
        f"AUD {caps.escalation_spend_today_aud():.2f} / "
        f"{config.ESCALATION_DAILY_CAP_AUD:.2f}"
    )
    print(
        f"global: AUD {caps.daily_spend_aud():.2f} / "
        f"{config.GLOBAL_DAILY_CAP_AUD:.2f}"
    )
    try:
        from hermes_cli.routing.drift import compute_drift_window

        drift = compute_drift_window(hours=24)
    except Exception:
        drift = {"total_decisions": 0}
    if int(drift["total_decisions"]) == 0:
        print("Doctrine drift 24h: no decisions")
    else:
        print(
            "Doctrine drift 24h: "
            f"followed {drift['followed_pct']:.1f}% / "
            f"overridden {drift['overridden_pct']:.1f}% / "
            f"bypassed {drift['bypassed_pct']:.1f}%"
        )
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    print(f"Task: {args.task_id}")
    print(
        f"Spend: AUD {caps.task_spend_aud(args.task_id):.2f} / "
        f"{config.PER_TASK_CAP_AUD:.2f}"
    )
    print(f"Calls: {ledger.task_call_count(args.task_id)}")
    return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    rows = ledger.last_entries(args.n)
    if not rows:
        print("No cost ledger rows.")
        return 0
    print(
        f"{'ID':>6}  {'Timestamp':20}  {'Lane':16}  {'Vendor':10}  "
        f"{'AUD':>10}  Model"
    )
    for row in rows:
        print(
            f"{row.id:>6}  {row.ts:20}  {row.lane:16}  {row.vendor:10}  "
            f"{row.aud_amount:>10.4f}  {row.model_slug}"
        )
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "cost",
        help="Inspect the synchronous model-call spend ledger.",
    )
    cost_subparsers = parser.add_subparsers(dest="cost_command", required=True)

    today = cost_subparsers.add_parser(
        "today", help="Show today's UTC spend against advisory thresholds."
    )
    today.set_defaults(func=_cmd_today)

    task = cost_subparsers.add_parser(
        "task", help="Show spend and call count for one task."
    )
    task.add_argument("task_id")
    task.set_defaults(func=_cmd_task)

    tail = cost_subparsers.add_parser(
        "tail", help="Show the newest cost ledger rows."
    )
    tail.add_argument("--n", type=_positive_int, default=20)
    tail.set_defaults(func=_cmd_tail)


__all__ = ["register_cli"]
