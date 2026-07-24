# Fleet Parent Desktop Routing — RED/GREEN Evidence

Date: 2026-07-24
Worktree: `C:\Users\HieuKa\AppData\Local\hermes\worktrees\fleet-parent-routing-20260724`
Base: `a61183b56fdb45b9d2a0f2f6b8482e665ccf702f`
Governance: `C:\Users\HieuKa\Desktop\SakaanFleetGovernance\GOVERNANCE.md`
Governance bytes: `73111`
Governance SHA-256: `629F23FF6A629F602A3FFC035EB8D33C7364A797E73D5B684214C7758E8EB3F5`

## Restored baseline

- Nine historical fleet commits were cherry-picked in the planned order with
  `git cherry-pick -x`; authorship and source hashes are present in each commit.
- `bash scripts/run_tests.sh tests/hermes_cli/fleet -q`
  - GREEN: 10 files, 121 tests passed.
- `bash scripts/run_tests.sh tests/hermes_cli/test_web_server_profile_unification.py -q`
  - GREEN: 1 file, 34 tests passed.
  - Windows note: the clean-env runner needed `USERPROFILE`, `HOMEDRIVE`, and
    `HOMEPATH` passed through temporarily so `Path.home()` could resolve. The
    temporary harness edit was removed immediately after the run.
- `npm test -- --run src/app/settings/fleet-router-settings.test.tsx src/app/settings/settings-provider-navigation.test.tsx src/app/settings/providers-settings.test.tsx src/hermes-profile-scope.test.ts`
  - GREEN: 4 files, 15 tests passed.

## Phase 1 — purpose-aware policy/config

### RED: policy contracts

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_policy.py -q`

Expected RED:

- Collection failed because `MeasurementKind` did not exist in
  `hermes_cli.fleet.types`.
- This is the intended missing contract for capacity comparability and the
  purpose-aware admission tests; it is not a syntax or fixture accident.

### RED: capacity contracts

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_capacity.py -q`

Expected RED:

- Collection failed because `MeasurementKind` did not exist.
- The failing import is the intended missing measured/estimated/unknown
  provenance contract.

### RED: config and lane capabilities

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_config.py -q`

Expected RED:

- 13 tests passed and 4 failed.
- Missing behavior was isolated to `parent_desktop_enabled`, deprecated
  stale-capacity flag handling, and explicit worker/parent lane capabilities.

### GREEN: policy/config/capacity

Commands:

- `bash scripts/run_tests.sh tests/hermes_cli/fleet/test_policy.py -q`
  - GREEN: 33 tests passed.
- `bash scripts/run_tests.sh tests/hermes_cli/fleet/test_capacity.py -q`
  - GREEN: 19 tests passed.
- `bash scripts/run_tests.sh tests/hermes_cli/fleet/test_config.py -q`
  - GREEN: 17 tests passed.
- `bash scripts/run_tests.sh tests/hermes_cli/fleet -q`
  - First compatibility run: 105 passed, 25 failed because restored fixtures
    did not provide the new explicit billing evidence.
  - Second compatibility run: 128 passed, 2 failed because two restored tests
    still expected stale usage to invalidate a lane.
  - Final GREEN: 10 files, 130 tests passed.

The compatibility fixes added explicit observed proof to fixtures instead of
defaulting or synthesizing it in production. The live doctor now reports
overage as unknown unless a separate billing-status source proves it off.
