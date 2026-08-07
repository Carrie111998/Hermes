"""DevFlow Delegation Plane (DDP) — Stage 1 control plane.

Canonical delegation surface through which ANY Hermes component delegates
improvement/bug-fix work to DevFlow: contract validation, target allowlist,
flood control, durable ledger, lifecycle telemetry, and a durable mailbox
queue. Stage 1 performs NO build/PR/merge/deploy; adapters default to
dry-run classification.

Spec: docs/superpowers/specs/2026-08-06-devflow-delegation-plane-design.md
"""

__version__ = "1.0.0"
