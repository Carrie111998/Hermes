#!/usr/bin/env python3
"""P6 cutover validation V1 — target accuracy on REAL data, read-only.

Proves what the shadow soak could not: that on a genuine live process +
transcript census, the pure planner classifies trees correctly and, when the
trigger is (synthetically) armed, selects the tree we would expect and
rejects everything else for the right reason.

SAFETY — this harness cannot act:
  * it constructs NO executor and calls NO terminate function;
  * the only live reads are one ``live_snapshot()`` and transcript stat()s;
  * the trigger evidence is SYNTHETIC and fed only to the PURE planner
    (build_plan / assess_tree) — it manufactures no host pressure and emits
    nothing to the event bus.

It forces mode=shadow regardless of config, so even the plan it builds is a
shadow projection. Output is a full per-tree assessment plus the projected
target and the rejection histogram, for human review across a few captures.

Usage:  python scripts/p6_v1_target_dryrun.py [--min-idle-minutes N]
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from claude_fleet_control import planner  # noqa: E402
from claude_fleet_control.controller import (  # noqa: E402
    Controller,
    default_config_path,
    live_snapshot,
    load_policy,
)
from claude_fleet_control.models import (  # noqa: E402
    MODE_SHADOW,
    PressureEvidence,
)


def _fmt_rss(n: int) -> str:
    return f"{n / (1024 ** 2):.0f} MiB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-idle-minutes", type=float, default=None,
                    help="override the idle threshold for this dry-run only")
    args = ap.parse_args(argv)

    policy, notes = load_policy(default_config_path())
    if policy is None:
        print("V1: config error:", "; ".join(notes))
        return 2
    # Force shadow; V1 never enforces. Optionally relax idle so a normally
    # active box still surfaces candidate trees to inspect.
    policy = dataclasses.replace(policy, mode=MODE_SHADOW)
    if args.min_idle_minutes is not None:
        policy = dataclasses.replace(policy, idle_min_minutes=args.min_idle_minutes)

    controller = Controller(config_path=default_config_path())
    now = time.time()
    snap = live_snapshot()
    assessments, root_count = controller._assess_all(snap, policy, now)

    print("=" * 72)
    print("P6 V1 TARGET DRY-RUN (read-only; no executor, no emit, no pressure)")
    print("=" * 72)
    print(f"processes: {len(snap.records)}  snapshot_complete: {snap.complete}")
    print(f"CLI roots (fleet census): {root_count}  "
          f"(trigger floor is >{policy.fleet_min_roots})")
    print(f"idle threshold: {policy.idle_min_minutes} min   "
          f"budgets: <= {policy.max_tree_processes} proc / "
          f"{_fmt_rss(policy.max_tree_rss_bytes)}")
    print("-" * 72)

    if not assessments:
        print("no Claude CLI roots on this box — nothing to assess.")
        return 0

    for a in sorted(assessments, key=lambda a: a.root.create_time):
        state = "ELIGIBLE" if a.eligible else ("protected" if a.protected else "not-idle")
        idle = f"{a.idle_minutes:.0f}m" if a.idle_minutes is not None else "n/a"
        print(f"  root pid {a.root.pid:<7} {a.root.identity:<18} "
              f"members={len(a.members):<3} rss={_fmt_rss(a.total_rss):<9} "
              f"idle={idle:<6} transcript={a.transcript.resolution:<10} {state}")
        if a.reasons:
            print(f"        reasons: {', '.join(a.reasons)}")

    # Synthetic trigger: force the fleet count above the floor and hand the
    # planner a valid D7 spawn_latency verdict. Seed each eligible tree at
    # strike 1 so build_plan advances to the second strike and actually
    # selects — showing the real selection/ordering on real trees.
    synthetic_pressure = PressureEvidence(
        True, "ok", event_id="V1-SYNTHETIC", event_timestamp="synthetic",
        age_seconds=0.0, sustained_ms=9999.0,
    )
    prior = {
        a.strike_key: {"recorded_at": now - 300.0, "count": 1.0}
        for a in assessments if a.eligible and a.strike_key
    }
    forced_count = max(root_count, policy.fleet_min_roots + 1)
    plan = planner.build_plan(
        assessments=assessments,
        fleet_root_count=forced_count,
        pressure=synthetic_pressure,
        prior_strikes=prior,
        last_enforce_intent_at=None,
        policy=policy,
        now=now,
        run_id="v1-dryrun",
        extra_reasons=("v1_synthetic_trigger",),
    )

    print("-" * 72)
    print(f"SYNTHETIC-TRIGGER PROJECTION (fleet forced to {forced_count}, "
          f"D7 forced valid, eligible trees pre-seeded to strike 1)")
    print(f"  decision: {plan.decision}")
    if plan.selected:
        s = plan.selected
        print(f"  WOULD SELECT: root pid {s.root_pid} ({s.root_identity})")
        print(f"    members={s.member_count} rss={_fmt_rss(s.total_rss)} "
              f"idle={s.idle_minutes:.0f}m strikes={s.strikes} action={s.action}")
    else:
        print("  WOULD SELECT: nothing (no eligible second-strike tree)")
    print("  rejection histogram:")
    for code, count in plan.rejections:
        print(f"    {code}: {count}")
    print("-" * 72)
    print("REVIEW: confirm the selected tree is genuinely idle and disposable, "
          "and that actor/gateway/Docker/WSL/cross-user/active trees appear as "
          "protected above. No process was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
