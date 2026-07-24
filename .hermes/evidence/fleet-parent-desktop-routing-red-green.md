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

## Phase 2 — atomic parent pins and turn leases

### RED: state reader

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_state.py -q`

Expected RED:

- 7 tests passed and 1 failed because `FleetStore.read_parent_pin` did not
  exist.

### RED: service preview

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_service.py -q`

Expected RED:

- 8 tests passed and 1 failed because `FleetService.preview_parent` did not
  exist.

### RED: parent admission contract

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_parent_admission.py -q`

Expected RED:

- Collection failed because the immutable `ParentPin` and
  `ParentLeaseHandle` contracts did not exist.
- The new file covers atomic admission, idempotence, 32-way concurrency,
  lineage persistence, lease generation safety, fail-closed unavailable pins,
  secret-free audit, and transaction rollback.

### RED: purpose-separated inspection

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_parent_admission.py -q`

Expected RED after the state path was GREEN:

- 8 tests passed and 1 failed because inspection had no `purposes` payload and
  therefore could not distinguish worker eligibility from parent eligibility.

### GREEN: parent pin state, service, and inspection

Commands:

- `bash scripts/run_tests.sh tests/hermes_cli/fleet/test_parent_admission.py tests/hermes_cli/fleet/test_cli.py -q`
  - GREEN: 2 files, 17 tests passed.
- `bash scripts/run_tests.sh tests/hermes_cli/fleet -q`
  - GREEN: 11 files, 141 tests passed.

The parent admission transaction now persists one immutable lineage pin,
advances a purpose-specific rotation cursor once, and writes a secret-free
audit event atomically. Parent turn leases are owner/generation safe, expired
leases release reserved capacity without deleting the pin, and an unavailable
pinned lane fails closed without evaluating an alternate route.

The Windows parallel runner emitted a post-result `UnicodeEncodeError` while
printing its checkmark through cp1252. The process exit code was zero and the
authoritative test summaries above reported zero failed tests; production and
test files were not changed to mask the runner-only display issue.

## Phase 3 — gateway admission and exact native route

### RED: Fleet Auto admission before deferred construction

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 1 test failed because `tui_gateway.server` had no
  `_admit_fleet_parent_session` gateway boundary.
- The focused test requires admission to happen before the deferred build,
  and requires the immediate response plus live session overrides to report
  the selected exact Grok route.

### RED: explicit manual-model precedence

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 1 test passed and 1 failed because the immediate server response did not
  expose `model_source=manual` or the corresponding `MANUAL_OVERRIDE` reason.
- The admission spy was not invoked, proving the pre-existing bypass portion
  while isolating the missing truthful server metadata.

### RED: exact native runtime without fallback

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 2 tests passed and 1 failed because `_make_agent` did not accept an
  `exact_route` contract.
- The focused test arms a valid xAI OAuth runtime, makes the ordinary fallback
  resolver fatal if invoked, and requires `fallback_model=None` plus priority
  service disabled at `AIAgent` construction.

### RED: safe durable route metadata

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 3 tests passed and 1 failed because first-activity session persistence did
  not include the fleet lineage root, lane, adapter kind, route identity, and
  model-source fields.
- The test also guards that qualification evidence hashes are not copied into
  ordinary session metadata.

### RED: post-build session-info authority

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 4 tests passed and 1 failed because `_session_info` dropped the committed
  model source, fleet lane, route identity, and safe display label after agent
  construction.

### RED: resume resolves the authoritative lineage pin

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 5 tests passed and 1 failed because the gateway fleet bridge had no
  `restore_parent_route` path from safe SessionDB metadata to the
  authoritative fleet SQLite pin.

### RED: active-turn occupancy guard

Command:

`bash scripts/run_tests.sh tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 6 tests passed and 1 failed because the gateway fleet bridge had no
  `acquire_parent_turn_guard`.
- The focused contract requires release of the exact owner/generation lease
  and a future-admission cooldown after a provider failure.

### Owner correction RED: Claude is a native Anthropic parent

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_policy.py tests/hermes_cli/fleet/test_live.py tests/tui_gateway/test_fleet_parent_route.py -q`

Expected RED:

- 49 tests passed and 9 failed.
- Failures proved the restored Claude lane was still external-only, the live
  doctor had no explicit Claude Code OAuth evidence seam, and the exact gateway
  runtime admitted only Codex and Grok.

### Owner correction GREEN: exact Claude Code OAuth route

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet/test_policy.py tests/hermes_cli/fleet/test_live.py tests/tui_gateway/test_fleet_parent_route.py -q`

Result:

- 58 tests passed and 0 failed.
- Claude now qualifies only from an attributable live Claude Code OAuth record
  and constructs the native Anthropic Messages route directly.
- `ANTHROPIC_API_KEY`, generic runtime fallback, custom endpoints, fast mode,
  service priority, and fallback models cannot satisfy the exact route.

### Gateway and Fleet phase verification

Command:

`bash scripts/run_tests.sh tests/hermes_cli/fleet tests/tui_gateway/test_fleet_parent_route.py -q`

Result:

- 158 tests passed and 0 failed across 12 files.

The canonical wrapper cannot collect `tests/test_tui_gateway_server.py` on
native Windows because its `env -i` process omits `USERPROFILE` and
`LOCALAPPDATA`; `pathlib.Path.home()` fails before test collection. A fallback
run used the same per-file runner with a newly created isolated test home,
credential variables removed, and deterministic test environment values.
It reported 430 passed after one retry. The first attempt had one unrelated
metadata-mirror event-order mismatch; the builder diff does not modify that
test's compute-host completion function, so no unrelated change was made.

### Antigravity persistent-parent live gate

Read-only discovery proved installed `agy` version `1.1.6` exposes
`--conversation`, `--continue`, and `--remote-control`, and `agy models`
includes exact `gemini-3.1-pro-high`.

An isolated empty-directory first-turn canary was terminated after the CLI
outlived its own 60-second print timeout. Its bounded log reported that the
current Antigravity CLI is not logged in, so strict two-turn continuity and
served-model evidence cannot be established on this machine. External-parent
activation must remain fail-closed; catalog visibility and a dormant
contract-tested driver may not be represented as live qualification.
