# Hermes Direct Operations release gate — 2026-07-30

## Result

**SOURCE GATE: PASS. HELD INSTALL: PASS. TRUSTED READ PROOF: PASS.
ACTIVATION: PROHIBITED.**

This is the minimal global repair requested after the July 30 incidents. It
preserves Hermes' native agency and existing specialist skills while enforcing
only the failure boundaries needed to prevent critical mistakes:

- load the smallest directly governing skill set;
- classify investigation/recommendation turns as read-only;
- require explicit implementation authority before source or installed-skill
  mutation;
- isolate implementation in a clean default-branch worktree;
- keep ordinary business operations and native tool use available;
- preserve one exact confirmation for genuinely protected effects;
- emit real progress at about 90 seconds;
- close every run as done, blocked, failed, or cancelled;
- distinguish final generation from final delivery;
- require source readback for claimed effects;
- make an owner stop durable; and
- permanently remove hygiene dependency edges that can start the gateway.

This does not add a database capability plane, generic operation gateway, new
UI, or persistent service. Route and schedule incidents remain regression
proofs, not the center of the architecture. Business intent, target resolution,
and protected-effect policy remain in the canonical Maione operator and the
small set of relevant specialist skills.

## Immutable source identity

- upstream repository: `NousResearch/hermes-agent`
- upstream default branch commit:
  `dbe14424ed192b83993e5655629b0dd5714f3355`
- integrated source commit:
  `a9fa619d1a808d339eff5bc702ae99d291bf7fd2`
- integrated source tree:
  `250d887c91fb82a4da476e2ebe3e485d0080cf45`
- direct parent:
  `9c31627c5c9b29bda1bd8b69d4f66bce23f6882c`
- clean integration worktree:
  `/home/ed/.hermes/worktrees/direct-ops-live-integration-20260730`
- branch:
  `codex/direct-ops-live-integration-20260730`

The integrated source contains both the preserved live Telegram behavior and
the current upstream default branch. The upstream commit is an ancestor of the
integrated commit. The merge that joined the safety branch and live behavior
resolved exactly three expected conflicts: model-status recognition, Telegram
command/authorization ordering, and Telegram regression fixtures. A later
merge of current upstream was conflict-free and changed only frontend/TUI
sources on the upstream side. Subsequent commits added the pinned trusted-read
MCP classification, allowed only the pure deferred-tool catalog bridges during
investigation, and added executor-level proof that deferred calls are checked
against the exact underlying tool effect before dispatch.

Before integration, the live repository's two dirty Telegram files and commit
were preserved in Git refs, an immutable stash object, a verified Git bundle,
exact file copies, and a binary patch under:

`/home/ed/.hermes/quarantine/direct-ops-integration-backup-20260731`

No service, runtime, provider, or business-data mutation occurred during source
integration or testing.

## Behavioral release proof

Every runner invocation below used retries disabled. Test homes were isolated
where supported.

### Frozen full inventory

At safety commit `dc2372f238a2629d974987e09e09ed80fd0570d3`, the repository's
entire discovered Python test inventory ran once:

```text
2,481 files
23,189 tests passed
22 tests failed in 13 files
runner wall: 2,877.8 seconds
workers: 8
retries: 0
stderr: empty
```

This run intentionally froze every residual failure instead of retrying or
hiding it. Its transcript is:

`C:\Users\Ed\AppData\Local\Temp\hermes-direct-ops-full-isolated-dc2372.out.log`

SHA-256:
`19D64B42E2969362B5D22E7392C8E50E2D9A5074357CBD408D81C1A6E19BF919`

The 22 failures covered concurrency/timing, Windows line endings, stale test
expectations, platform-specific host artifacts, and noninteractive subprocess
behavior. Each failure was then reproduced in a smaller deterministic lane and
either corrected or proved platform-specific.

### Final changed-area and protected-confirmation gate

At immutable final commit `d4a9df32a4c475d4f5a116d7d2941a7cd90fb152`:

```text
37 files
647 tests passed
0 failed
runner wall: 62.4 seconds
workers: 8
retries: 0
stderr: empty
```

Transcript:

`C:\Users\Ed\AppData\Local\Temp\hermes-direct-ops-changed-d4a9df.out.log`

SHA-256:
`45C0E000E9B95EFA2D07ED210BA1CC09D7A7D1B3121CAE525D892F904ECD56EF`

This gate includes:

- Ed's exact quote-analysis investigation regression;
- minimal skill selection and oversized-skill rejection;
- investigation-versus-implementation enforcement;
- dirty checkout isolation and clean-worktree proof;
- native business-operation and native external-tool preservation;
- untrusted MCP effect rejection outside operation turns;
- pre-write repository re-probe and post-write verification;
- freeze after partial or unknown source effects;
- real 90-second progress delivery;
- done/blocked/failed/cancelled terminal receipts;
- final generated versus delivered/unknown/failed states;
- durable owner holds and no implicit gateway start;
- hygiene migration, compensation, and safe restart failure;
- exact requested/prior/unknown Telegram setting receipts; and
- the existing one-shot destructive confirmation primitive.

The three protected-confirmation files contributed 14 passing tests inside
this gate:

- `tests/gateway/test_destructive_slash_confirm.py`
- `tests/hermes_cli/test_destructive_slash_confirm_gate.py`
- `tests/tools/test_slash_confirm.py`

### Frozen-failure closure

The files with stale deterministic assertions were rerun serially after their
corrections:

```text
6 files
300 tests passed
0 failed
workers: 1
retries: 0
```

The remaining timing/platform group was rerun at the final source commit:

```text
6 files
113 tests passed
0 failed
runner wall: 59.7 seconds
workers: 1
retries: 0
```

Those exact files were:

- `tests/agent/test_compression_concurrent_fork.py`
- `tests/gateway/test_compression_failure_session_sync.py`
- `tests/tools/test_termux_api_detection.py`
- `tests/tools/test_tts_command_providers.py`
- `tests/tools/test_transcription_tools.py`
- `tests/tui_gateway/test_slash_worker_mcp_discovery.py`

Transcript SHA-256:
`42F970732FC8486A6BDB59F9DBCC464A1DF33856DFD01A0D8946F2175CFEC256`

The Windows-native line-ending and Hindsight pair was rerun with Python
3.11.15:

```text
2 files
54 tests passed
0 failed
2 skipped
runner wall: 46.5 seconds
workers: 1
retries: 0
```

Transcript:

`C:\Users\Ed\AppData\Local\Temp\hermes-direct-ops-windows-eol-hindsight-93db.log`

SHA-256:
`D8B190FFA3274C9CEDBCE4AADAC35264837369C5064B5086BB43FABE98FAF530`

Both test-file blobs are byte-identical between the Windows test commit and
the final integrated commit:

- `test_update_eol_churn.py`:
  `01bc648ee9567740187cdd91f8df486d44a611f6`
- `test_hindsight_provider.py`:
  `1554c21594b9bf4f73cfb24e99f53a87d21b86e1`

Together these gates account for every failure in the frozen full inventory.

### Trusted deferred-read closure

At current installed source commit
`a9fa619d1a808d339eff5bc702ae99d291bf7fd2`, the exact request-phase,
deferred-tool, and MCP effect boundary reran:

```text
4 files
180 tests passed
0 failed
runner wall: 14.27 seconds
retries: 0
```

Transcript:

`C:\Users\Ed\AppData\Local\Temp\hermes-direct-ops-focused-a9fa.log`

SHA-256:
`53CF8A254ECC301A4734752A90CE9533BB6B57B0C6CBD430C3B14E130F8F7676`

This includes both sequential and concurrent executor proofs:

- a deferred `tool_call` targeting a registered `READ_ONLY` tool is unwrapped
  to the exact underlying name and arguments, then reaches its handler;
- the same path targeting an `UNKNOWN` tool is blocked before its handler;
- `tool_search` and `tool_describe` may inspect only the session-scoped catalog
  and cannot dispatch a target themselves; and
- the trusted MCP source hash is re-read immediately before each dispatch.

The immediately preceding production-identical head also ran the expanded
changed-area gate:

```text
39 files
710 tests passed
0 failed
runner wall: 102.4 seconds
workers: 8
retries: 0
stderr: empty
```

Transcript:

`C:\Users\Ed\AppData\Local\Temp\hermes-direct-ops-changed-9c3162.out.log`

SHA-256:
`D38095E608CF38C66756A94598ECCD005325F2992936BFEF39A9A2BD89BF0134`

The only later source change was a test-harness refactor that removed a new
advisory type diagnostic; it did not change production code. The exact-head
180-test gate above reran after that refactor.

## Independent security review

The final independent review found and blocked one deterministic false-success
bug before cutover: Telegram participation persistence passed an unsupported
serializer argument, swallowed the resulting exception, and still announced
success. The corrected final commit:

- uses the canonical fail-closed config writer;
- reads the raw source back after the write;
- reports success only for the exact requested state;
- reports not changed only for the exact prior state;
- reports unknown for unreadable or third state;
- restores only in-memory state for non-success;
- never blindly rewrites disk; and
- has real success/reload, pre-write failure, and third-state/no-rollback
  regressions.

The first independent review's immutable-source verdict was **GO for held
cutover only** at
`d4a9df32a4c475d4f5a116d7d2941a7cd90fb152`. It found no remaining
source/security blocker and explicitly prohibited start, restart, unmask, or
hold release before the external activation proof.

A second independent review inspected the trusted-read registration and the
deferred-tool repair. Its final verdict was **GO**: only the two exact Terrain
read tools are `READ_ONLY`; sibling quote mutations and generated MCP
resource/prompt utilities remain `UNKNOWN`; catalog inspection cannot execute
a target; and both executor paths reclassify the exact underlying tool before
dispatch.

## Static, dependency, type, and build truth

At current installed source:

- repository-wide `ruff check .`: PASS;
- Windows-footgun scan: PASS, 896 files;
- affected Python bytecode compilation: PASS;
- `uv lock --check`: PASS, 250 packages;
- environment dependency check: PASS, no broken requirements;
- `git diff --check`: PASS; and
- worktree clean: PASS.

The repository pins `ty 0.0.21` as advisory and its CI runs type reporting with
`--exit-zero`. A repository-wide invocation produced the existing advisory
backlog and reported that not every project file could be analyzed; no false
full-repository type-pass is claimed. The exact three changed files completed
with 31 pre-existing advisory diagnostics and zero diagnostics on newly added
lines. The repository's own
`setup.py` explicitly rejects wheel/sdist packaging because Hermes runs from an
editable source checkout. Therefore the production build proof for this
installation is editable-source identity, import-path readback, dependency
integrity, upstream CI, and the held live source hash—not a fabricated wheel.

At the upstream base commit, the required aggregate check, all eight Python
test slices, JavaScript/TypeScript checks, OSV review status, timing report, and
both amd64 and arm64 container builds passed. The integration branch must be
published and its own CI read before activation.

## Owner-stop and held-install proof

The existing live state before cutover was read-only verified as:

- `hermes-gateway.service`: inactive/dead, `NRestarts=0`,
  `RefuseManualStart=yes`;
- `hermes-gateway-hygiene.service`: inactive/dead,
  `RefuseManualStart=yes`, no `Wants=`, `BindsTo=`, or `Upholds=` pull edge;
- `hermes-gateway-hygiene.timer`: inactive/dead,
  `RefuseManualStart=yes`, no next run; and
- the hygiene service's remaining `After=hermes-gateway.service` is ordering
  only and cannot start the gateway.

Installed pre-migration hashes:

- hygiene unit:
  `2b2c3100df1ab72fd52c67c49acb671e5cd7e728516abffe4a090e6ef9c13bb0`
- hygiene watchdog:
  `ae234383fd2e1652cbc7f2af07dd2ec4149aa294c77b26364e80cd2f29482367`

The live virtual environment already identifies
`file:///home/ed/.hermes/hermes-agent` as its editable source. The safe cutover
is therefore a protected Git fast-forward plus source/import readback while all
three runtime holds remain active. It must not invoke an installer or start
path.

After source installation, the supported hygiene migration must:

1. re-read all three holds and installed hashes;
2. preserve exact originals under the migration quarantine;
3. remove any gateway pull dependency;
4. add owner-hold checks before evaluation and delayed restart;
5. reload only the user systemd manager;
6. verify exact file hashes and loaded unit properties;
7. run a second time with no changed paths; and
8. prove all units remain held and inactive with no next timer run.

### Held cutover receipt

The held cutover completed without starting, restarting, unmasking, or
releasing any unit:

- live editable checkout:
  `/home/ed/.hermes/hermes-agent`
- installed runtime source commit:
  `a9fa619d1a808d339eff5bc702ae99d291bf7fd2`
- installed runtime tree:
  `250d887c91fb82a4da476e2ebe3e485d0080cf45`
- worktree readback: clean
- preserved live-dirty stash:
  `8c5a02acc7849dcdf0e748862c4133589374982d`
- stash parent:
  `25973ee9c45b2b0405f66696aa3995130cbde46d`
- stash contents: exactly
  `plugins/platforms/telegram/adapter.py` and
  `tests/gateway/test_telegram_group_gating.py`
- stashed file hashes match their pre-cutover readback:
  `f3f7ef4e44adfb645da8e0eef9078d38dcd9c61c4d2472e1a0246585b7580c74`
  and
  `61b881d27a40e2fabcae972d4214225ea2c20597dad2dc2e4228e2949cf87dc5`

A durable owner hold was written and read back before hygiene migration:

- path: `/home/ed/.hermes/.gateway-owner-hold.json`
- SHA-256:
  `e17f0fd7f597d76adc7f8a0128dcc8a385b00e23781ec8460ba7e07520baee9e`
- state: `held`
- target PID: none
- reason: activation pending coordinated read-only verification

Hygiene migration receipt:

- unit before/after SHA-256:
  `2b2c3100df1ab72fd52c67c49acb671e5cd7e728516abffe4a090e6ef9c13bb0`
  (already contained no pull dependency, so unchanged);
- watchdog before SHA-256:
  `ae234383fd2e1652cbc7f2af07dd2ec4149aa294c77b26364e80cd2f29482367`;
- watchdog after SHA-256:
  `0befac78c0a26778fecdde2605e525017fd9484af9f56cbc1ef4dd8e42b65e29`;
- exact backup:
  `/home/ed/.hermes/quarantine/gateway-hygiene-pre-direct-ops/original-gateway-hygiene-watchdog.py`;
- backup SHA-256:
  `ae234383fd2e1652cbc7f2af07dd2ec4149aa294c77b26364e80cd2f29482367`;
- watchdog Python compilation: PASS;
- direct watchdog status under hold:
  `owner hold active; no restart evaluated`; and
- second migration: no changed paths and no new backup paths.

Editable import readback resolved every inspected module to the live checkout:

- `agent.request_phase`:
  `f2e0a5a26f7ccf1ffa3f98bcee5118f98e978624268e5bf8953a6b0ac0281bac`;
- `gateway.delivery_ledger`:
  `a5e62028803533ca2bbb18e4509a62ee86f0448be5db4ebb93ae7c16a798cf2f`;
- `hermes_cli.gateway_hygiene`:
  `c7cc5ac6999407f5e9433fabe4aef05fe4054dd5bb8de88c9356d68b76ebe77b`;
  and
- `plugins.platforms.telegram.adapter`:
  `f3c508dde846a948d849e4b48935eed79e558158615186adf06298b088a8f154`.

Final held-state readback:

- gateway: inactive/dead, `NRestarts=0`, `RefuseManualStart=yes`;
- hygiene service: inactive/dead, `RefuseManualStart=yes`, no pull dependency;
- hygiene timer: inactive/dead, `RefuseManualStart=yes`, no next run; and
- no `hermes_cli.main gateway run` process exists.

### Pinned trusted-read configuration

The live MCP configuration was updated atomically while all holds remained
active:

- config SHA-256:
  `427f8e55b3ca020e0265a2064a9c476ae937f0185f4dbde5c5cc7632bec1cbad`;
- preserved prior config:
  `/home/ed/.hermes/quarantine/direct-ops-trusted-read-config/config.yaml.before-trusted-read-20260731`;
- prior config SHA-256:
  `07dbd10870832572925bd37b72758c2e458d80806de252fb272b3dc26aee68c9`;
- pinned local source:
  `/home/ed/.hermes/scripts/terrain_quote_mcp.py`;
- pinned source SHA-256:
  `2e8c8827663500129da2ed21c695380589dc3122b28156729c985db8e2638b74`;
- launch command:
  `/home/ed/.hermes/hermes-agent/venv/bin/python`;
- sole launch argument:
  `/home/ed/.hermes/scripts/terrain_quote_mcp.py`; and
- exact trusted tools:
  `find_clients` and `list_client_properties`.

The executable and source must remain absolute, local, identity-stable, under
16 MiB, and hash-identical immediately before dispatch. Any URL, extra argument,
tool-name ambiguity, source drift, or sibling MCP operation fails closed to
`UNKNOWN`. This source hash is owner attestation and change detection, not a
claim that imported dependencies or backend credentials are structurally
read-only.

### Held natural-language read proof

Session `20260731_024805_e36fa0` received ordinary owner wording to return one
current client record and its property list, read-only, with source readback
and one terminal result.

- started: `2026-07-31T02:48:06.574-04:00`;
- ended: `2026-07-31T02:48:38.471-04:00`;
- elapsed: `31.897 seconds`;
- terminal result: `done`;
- final output: generated with source readback;
- final delivery: CLI generated only, not represented as Telegram-delivered;
- effective business tools:
  `mcp__terrain_quote__find_clients` and
  `mcp__terrain_quote__list_client_properties`;
- terminal fallback: none;
- arbitrary code execution: none;
- write/mutation tool: none; and
- business-data mutation: none.

The session ended durably with `ended_at` and `end_reason=agent_close`. Its
redacted session record shows only `skill_view`, `tool_describe`, and
scope-checked deferred `tool_call` bridges, which resolved to the two trusted
reads above.

Activation remains a separate coordinated release decision. No business canary
may be manufactured. The only valid later canary is a genuine low-risk
owner-requested operation with its normal specialist skill and source readback.
The required preactivation existing-skill lookup is now complete. Activation
still requires explicit coordination because the gateway owner hold remains in
force and Telegram delivery has intentionally not been exercised.

## Residual boundary

Hermes captures repository state at turn intake, re-probes immediately before
each native mutating tool, verifies typed file effects afterward, advances
expected state only after verified effects, and freezes later mutation after a
partial or unknown effect.

One cross-process race remains: another program can edit a repository after
Hermes' final pre-write probe because ordinary Git/filesystem writers do not
share a mandatory global lock. Hermes detects drift before its next mutation,
but eliminating the last window requires OS/container isolation or universal
cooperative locking. This release does not represent that residual race as
solved.

Two cleanup items were discovered and deliberately deferred rather than
expanding this release:

- `terrain-operations` renders 41,214 characters, above the 32,000-character
  per-result skill budget. The guard rejected it without loading partial
  instructions, and the trusted lookup still completed. Its root should be
  reduced to a compact router with bounded supporting references.
- Runtime logs warn that linked SQLite `3.50.4` lacks later WAL-reset
  corruption fixes. Upgrade to the supported fixed SQLite/Python build in a
  separate dependency-maintenance change with session-store regressions.

## Release state vocabulary

- **source-ready**: reviewed code and local evidence pass;
- **installed-and-held**: live editable source and hygiene files match the
  reviewed release while the Telegram gateway remains inactive;
- **trusted-read-verified**: an ordinary owner lookup completed through the
  pinned read path with source readback while all runtime holds stayed active;
- **activated**: gateway explicitly started after separate coordination; and
- **business-verified**: a genuine later owner request completed with source
  readback.

The first three states are claimed. Hermes is installed, protected, and
read-verified, but not activated, Telegram-delivery-verified, or
business-mutation-verified. Health checks or source installation alone must
never be described as live business proof.
