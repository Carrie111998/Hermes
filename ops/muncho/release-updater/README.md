# Pinned release updater foundation

This directory currently contains only the isolated, unprivileged release
builder boundary.  Nothing here installs, enables, starts, or schedules a
production release update.

The Stage C transaction is deliberately fail-closed and is **not yet an
operational updater**:

- the required
  `scripts/canary/production_release_update_entrypoint.py` does not exist, so a
  real source candidate cannot pass the builder contract;
- the production host-action backend exposes only read-only validation and
  observation; every host mutation phase returns
  `production_release_host_action_primitive_unavailable`;
- no production runner, recovery-gate unit, boot scanner, or installer is
  shipped.

Do not install or activate these assets as a release updater until the complete
host mutation set, fixed production entrypoint, recovery gate, and disposable
Linux power-loss/restart E2E suite land together.  The missing entrypoint is an
intentional deployment interlock, not a packaging omission.

The dormant runtime currently uses transaction intent v4, authority-record v2,
and event v2 (`muncho-production-release-update-intent.v4`,
`muncho-production-release-update-authority-record.v2`, and
`muncho-production-release-update-event.v2`).  An activation installer must
prove that no legacy v3/v1/v1 authority or journal evidence, or any earlier
format, exists on the host, or perform an explicitly reviewed migration before
enabling any updater or recovery caller.

Release unit-input rotation evidence now uses the
`release-unit-input-authority-rotations-v5` audit namespace.  The historical
v4 namespace is immutable legacy evidence: the v5 implementation never scans,
migrates, rewrites, or deletes it.  An activation installer must therefore
handle any v4 evidence explicitly rather than assuming the current code has
adopted it.

The eventual activation caller has a load-bearing publication order.  It must
first finish publishing a clean, exact release-journal authority record and
only then create the global active-transaction marker.  Recovery opens only an
existing journal and refuses a journal whose authority publication is pending;
publishing the active marker first could otherwise leave a transaction that
cannot be recovered safely.

Unit-input finalization is split into durable preauthorization, activation, and
terminal abort boundaries.  Fresh approvals are checked before
preauthorization; finalization consumes the exact persisted preauthorization
without consulting wall-clock freshness again.  An append-only
activation-begin marker is published immediately before the first live write,
and permanently forbids abort even if rollback restores the predecessor.  No
runtime caller or updater activation path is shipped yet.
