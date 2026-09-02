---
title: "Updater worker-generation boundary"
description: "How Linux user-systemd services cross an in-place Hermes update safely"
---

# Updater worker-generation boundary

Hermes source installs currently update a checkout in place. A long-lived
Python process retains imported callables and module objects even after the
files beneath it change. A stale-import incident demonstrated the generic
failure mode: a new caller began passing a keyword accepted by the updated
worker implementation, while an old worker process still held the previous
callable contract. Updating files without proving process replacement can
therefore create a mixed generation that neither revision supports.

## Linux user-systemd design

`hermes_cli/update_systemd_boundary.py` supplements the cross-platform update
inventory. It is activated only when the existing platform probe says user
systemd services are relevant. Its command runner, identity collector, clock,
and canary verifier are injectable; isolated tests do not contact the real
user manager.

Before checkout or merge changes source, the updater:

1. Runs `systemctl --user list-unit-files` and `list-units` without shell
   patterns, filters names in Python, then requests exact `show` records.
2. Captures enabled/running `hermes-gateway*.service`,
   `hermes-dashboard.service`, and `hermes-webui.service` units, including
   enabled state, `ActiveState`, `SubState`, `MainPID`, monotonic process start
   time, `ExecStart`, and unit fragment path.
3. Discovers installed/enabled Buzz health/e2e timers and the concierge e2e
   service as optional lifecycle hooks. Site-specific units are never assumed
   to exist.

Once source and dependencies form the installed generation, the boundary:

1. Stops discovered monitors, then stops every captured worker.
2. Treats `failed` plus `MainPID=0` as quiesced after the intentional stop, but
   runs `reset-failed` before restart. A live PID is never treated as quiesced.
3. Starts dashboard/WebUI and the default (or first discovered) gateway
   canary, and proves active state, a different PID, and a monotonic start time
   newer than the generation boundary.
4. Starts and proves the remaining gateways, re-inventories exact membership,
   and names every missing or extra enabled unit. Existing gateway code stamps
   provide the later checkout SHA/version proof in the generic fleet check;
   injected runtime identity fields are compared when a deployment supplies
   them.
5. Leaves monitors paused while the updater's existing authoritative code-SHA
   fleet matrix and plan-vs-execution reconciliation run. Only after both pass,
   re-enables/starts discovered timers and explicitly starts the discovered
   concierge e2e service once.

Probe, stop, canary, restart, identity, or reconciliation failures are fatal.
Monitors remain paused, and recovery guidance names affected units. The older
generic update restart path skips units already proven by this boundary, while
retaining its cross-platform and non-systemd coverage.

## Recovery and rollback

If the transition fails, do not re-arm health/e2e timers until the fleet is
consistent. For every named unit:

```bash
systemctl --user reset-failed <unit>
systemctl --user restart <unit>
systemctl --user show <unit> -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestampMonotonic
```

Check gateway runtime/code stamps with the normal Hermes status and update
receipt diagnostics. If the new generation itself is faulty, restore the
pre-update snapshot or reset the checkout to the recorded pre-update commit,
refresh dependencies, and repeat the same controlled fleet transition. Never
restart only a subset onto a checkout shared by the whole fleet.

## Recommended deployment evolution

The in-place checkout remains an avoidable source/worker race. Production
installations should move toward immutable, versioned release directories:

```text
releases/<commit>/source + venv
current -> releases/<commit>
```

Build and validate the new directory before activation, atomically repoint the
`current` symlink (or update unit `ExecStart` paths), then restart workers
through the same canary and exact-reconciliation boundary. Keep the previous
release until proof completes so rollback is an atomic repoint plus controlled
restart, rather than another mutation of files beneath running interpreters.
