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
