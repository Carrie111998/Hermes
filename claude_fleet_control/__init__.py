"""P6 Claude fleet controller — bounded, fail-closed, shadow-first.

Successor lane to the commit-gated session reaper (``Hermes-Claude-Session-Reaper``
-> ``cull-claude-sessions.py``), built for the failure mode that reaper was
proven blind to five times on 2026-08-26: process-churn storms where commit
charge and physical RAM both read healthy while CreateProcess starves (loops
claim ``host-spawn-churn-20260826``). Detection is D7's ``spawn_latency`` axis
in events.producers.resource_monitor; this package is the ACTION half, and it
is deliberately separate — D7 emits ``resource_pressure`` and stops.

Layering contract (tests pin it):
  * ``models``   — frozen, JSON-serializable records only.
  * ``planner``  — pure decisions. No psutil, no EventBus, no filesystem, no
                   wall-clock, no printing, no termination.
  * ``controller`` — live adapters and one-pass orchestration.
  * ``executor`` — the injected, revalidating, irreversible boundary. Never
                   constructed in shadow mode.

Enforcement is double-gated: config ``mode: enforce`` with a matching
``approved_enforce_digest`` AND an explicit ``--allow-enforce`` invocation
flag. The tracked config ships ``shadow`` and the scheduled runner omits the
flag, so this change cannot terminate anything.
"""

from claude_fleet_control import controller, executor, models, planner

__all__ = ["models", "planner", "controller", "executor"]
