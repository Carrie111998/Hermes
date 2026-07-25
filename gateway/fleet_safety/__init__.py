"""Fleet-safety guards for the Hermes gateway.

This package is fleet-safety infrastructure (Hermes/CEO domain). It exists to
stop a single agent session from silently draining a provider's entire wallet
the way the 2026-07-25 incident did: a "resume the previous task" turn looped
unattended for ~13 hours, made ~1,800 model calls each re-sending a ~160k-token
context at reasoning effort=max with 3 retries per failure, and burned
SuperGrok's whole weekly API limit in under a day — while the local usage
tracker still reported the provider at ~10% used.

Design constraints (see AGENTS.md):

- **Paper-safe.** Nothing here touches billing, credentials, or exchange/order
  state. The only side effects are (1) interrupting a runaway *agent loop* and
  releasing its turn lease, and (2) capping which provider new *routing*
  requests may target. It never spends, refunds, or moves money.
- **Deterministic core.** Every decision function takes ``now`` (epoch seconds)
  and its inputs explicitly — no ``time.time()`` inside the engine — so the
  unit tests are reproducible under the repo's hermetic ``PYTHONHASHSEED=0``
  invariant.
- **Effects are injected.** The detector/enforcer never import gateway
  internals; the live wiring in :mod:`gateway.fleet_safety.integration` adapts
  the real registries to the small callable seams the engine expects. This
  keeps the correctness-critical logic testable in isolation and the core
  blast radius small (builder != inspector).
"""

from gateway.fleet_safety.deadloop_guard import (
    GuardThresholds,
    RunawayGuard,
    SessionObservation,
    Trip,
    TripReason,
)
from gateway.fleet_safety.wallet_cap import (
    RoutingRequest,
    WalletAction,
    WalletCap,
    WalletCapConfig,
    WalletDecision,
)
from gateway.fleet_safety.usage_verify import VerifiedUsage, verify_usage, verified_usage_for
from gateway.fleet_safety.selector import (
    LaneConfig,
    SelectedLane,
    get_effort_ladder,
    rank_fallback_chain,
    resolve_effort_from_map,
    select_best_lane,
)
from gateway.fleet_safety.report import format_kill_report
from gateway.fleet_safety.enforcer import (
    EnforcementResult,
    GuardEnforcer,
    KillActions,
)

__all__ = [
    "GuardThresholds",
    "RunawayGuard",
    "SessionObservation",
    "Trip",
    "TripReason",
    "RoutingRequest",
    "WalletAction",
    "WalletCap",
    "WalletCapConfig",
    "WalletDecision",
    "VerifiedUsage",
    "verify_usage",
    "verified_usage_for",
    "LaneConfig",
    "SelectedLane",
    "get_effort_ladder",
    "rank_fallback_chain",
    "resolve_effort_from_map",
    "select_best_lane",
    "format_kill_report",
    "EnforcementResult",
    "GuardEnforcer",
    "KillActions",
]
