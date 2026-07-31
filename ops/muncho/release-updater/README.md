# Pinned release updater foundation

This directory contains the isolated, unprivileged release-builder boundary
and a deliberately inert bootstrap for a dedicated unit-input rotation
stager.  Nothing here enables, starts, or schedules a production release
update.

The rotation-stager bootstrap is narrower than the dormant updater.  A
root-only create-only author publishes one exact Git tree, one externally
verified complete Linux/x86_64 wheelhouse, and digest-pinned ``uv`` and system
Python inputs to the builder.  The output starts empty and builder-owned.  A
separate installer may place the sysusers/tmpfiles contracts, builder unit,
and fixed builder/promoter/stager wrappers, but it neither reloads systemd nor
enables or starts any unit.  Promotion publishes an unreachable root-owned
release; the stager wrapper verifies that whole release before using its
pinned interpreter.  Neither authoring, installation, building, nor promotion
changes a release symlink, gateway, application data, or credentials.

Foundation code is revision-qualified.  Each exact source revision is
published create-only below
`/usr/lib/muncho-release-updater-releases/<40-hex-revision>/`; the shared v2
wrapper and v2 builder template select only that exact directory through
protocol enums.  A newer foundation therefore never overwrites or silently
changes the bytes that built an older job.  The installer remains inert: a
separate, explicit `systemctl daemon-reload` is required before a newly
created template can be used, and installation still never enables or starts
it.  The original fixed-v1 foundation remains immutable audit evidence and is
not used by the v2 rotation-stager promotion path.

Wheel resolution is intentionally not part of the privileged author.  It
accepts only an already-verified, self-hashed manifest declaring a complete
transitive closure for CPython 3.11 on Linux x86_64 and revalidates the exact
closed directory inventory, sizes, and SHA-256 digests without network access.

The Stage C transaction is deliberately fail-closed and is **not yet an
operational updater**:

- the required
  `scripts/canary/production_release_update_entrypoint.py` does not exist, so a
  real source candidate cannot pass the builder contract;
- the production host-action backend exposes only read-only validation and
  observation; every host mutation phase returns
  `production_release_host_action_primitive_unavailable`;
- no production updater runner, recovery-gate unit, boot scanner, or updater
  activation installer is shipped.  The inert rotation-stager foundation
  above is not an updater activation path.

One fixed-root recovery coordinator now exists as a dormant library boundary.
It has no arguments and no activation surface.  While holding the global
authority lock, it can only normalize an already-existing active marker, open
the exact already-existing journal named by that marker, ask the runtime to
recover and live-revalidate a terminal state, and retire that exact marker.
An absent marker is idle and creates nothing.  Any journal, runtime, or
revalidation failure before retirement leaves the marker in place for a later
retry.  Exact marker retirement is crash-convergent after unlink and never
deletes the immutable transaction journal.  No caller can use this boundary to
create a transaction or begin fresh execution.

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
fresh-execution runtime caller or updater activation path is shipped yet.
