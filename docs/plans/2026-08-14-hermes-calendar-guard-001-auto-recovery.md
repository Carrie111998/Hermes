---
title: "HERMES-CALENDAR-GUARD-001: release-aware calendar guard recovery"
status: IMPLEMENTED_PENDING_MERGE
date: 2026-08-14
type: reliability
ticket: HERMES-CALENDAR-GUARD-001
target_repo: hermes-agent
---

# HERMES-CALENDAR-GUARD-001: release-aware calendar guard recovery

## Gate

This ticket addresses the hourly `Hermes calendar safety guard` alert and
defines a safe automatic recovery path. It is plan-only until implementation,
independent review, merge, and DGX deployment are separately authorized.
No DGX files, service units, cron jobs, or running processes are changed by
this ticket.

## Observed symptom

Job `85dcd4b817d6` runs hourly and repeatedly reports:

```
Gateway is running stale code: boot af99f0f1ad, disk 1c14d2b9df.
manual gateway restart required
```

The DGX gateway is not dead. At the live check it was the user-level
`hermes-gateway.service`, `ActiveState=active`, `SubState=running`,
`MainPID=2299452`, `NRestarts=0`, using release
`63bcd7acbbb93d2c797090800ac1e4677b590449`.

The alert is a false positive caused by an identity contract mismatch:

- the boot fingerprint still contains the old checkout identity
  `af99f0f1ad...`;
- the active release contains `.hermes-release-sha` with `63bcd7ac...`, but
  not the marker name `RELEASE_COMMIT` expected by the guard;
- the guard therefore falls back to the untouched live checkout and reports
  its `1c14d2b9df...` revision instead of the active release identity.

The same inspection also found transient Telegram bootstrap/connect timeouts
and two supervised stop/start events. Those are separate network/runtime
signals and must not be treated as proof of code skew.

## Approved implementation boundary

1. Move the guard's decision logic into the repository. Add a small,
   testable release-identity/guard module under `hermes_cli/` plus a tracked
   `scripts/hermes_calendar_guard.sh` wrapper. The DGX
   `~/.hermes/scripts/hermes_calendar_guard.sh` becomes a release-installed
   wrapper, not an independently edited source file. The module owns marker
   parsing, service inspection, incident classification, state transitions,
   and redacted output; the wrapper only selects `HERMES_HOME` and invokes the
   release module. Tests exercise the module with fake marker files and fake
   `systemctl` output, while one deployment test verifies the wrapper points at
   the matching release. This closes the current gap where the live guard is
   outside the repo and untested by CI.
2. Add a release identity reader as new code. For the exact user unit
   `hermes-gateway.service`, query `systemctl --user show` for
   `ActiveState`, `SubState`, `MainPID`, and `WorkingDirectory`; reject missing
   units, non-numeric PIDs, unexpected multi-line values, unavailable user
   scope/DBus, and paths outside the configured `HERMES_HOME/releases/` or
   legacy checkout boundary. The reader must recognize every marker currently
   present in production releases: canonical `.hermes-release-sha`, legacy
   `RELEASE_COMMIT`, and legacy `RELEASE_SHA`. If multiple recognized markers
   exist with the same value, prefer the canonical name; if values conflict,
   return `BLOCKED`. An unrecognized marker filename is also `BLOCKED` with an
   actionable diagnostic, never a silent fallback to checkout Git identity.
   Use checkout Git identity only for a verified legacy non-release
   installation. Never use the live checkout HEAD as the identity of an active
   release snapshot.
3. Replace, rather than duplicate, the current non-atomic unread
   `gateway_boot_fingerprint` writer. Keep the same path but write a versioned
   JSON record atomically through a same-directory temporary file and rename;
   include boot identity, release path, PID, and timestamp. The reader accepts
   the legacy one-line format for one migration window and rewrites it at the
   next boot. `gateway.code_skew`'s in-memory `_boot_fingerprint`, its
   `detect_code_skew()` path, the on-disk record, and the calendar guard all
   call the same runtime-identity helper; `/model` skew refusal therefore
   cannot disagree with the calendar guard. No second boot-state file is
   introduced. Development checkouts retain the existing Git fingerprint
   behavior, and a focused test covers both the `/model` consumer and the
   calendar guard against the same release fixture.
4. Make the guard compare the active release identity with the boot record.
   A live-checkout mismatch that is not the running release is informational,
   not stale-code evidence. A missing, conflicting, or unverifiable identity
   is `BLOCKED` with the exact evidence needed for operator action.
5. Implement recovery as a supervisor-owned user-systemd path, not as a
   child restart command. The guard writes an atomic, incident-keyed recovery
   request and exits; a separately installed user `Type=oneshot` recovery
   service, triggered by a user path/timer, performs the restart outside the
   gateway cgroup. It must not inherit `_HERMES_GATEWAY=1`; the unit has an
   explicit minimal environment and no gateway process ancestry. The service
  follows the POSIX `flock` pattern from `cron/jobs.py::_jobs_lock()` (without
  importing the cron module into the supervisor), the rolling bounded-window semantics from
   `gateway/restart_loop_guard.py`, and the `system=` user/system scope
   convention in `hermes_cli/gateway.py`. It enforces an absolute timeout and
   verifies the user-level unit, old/new MainPID, active release path, marker,
   and post-restart boot record before reporting `RECOVERED`.
6. Add explicit persistent guard state, with a schema version and atomic
   writes, containing the incident key, attempts, cooldown, last outcome, and
   notification timestamp. The incident key includes the boot identity,
   active release identity, and reason. `OK` and successful `RECOVERED` are
   quiet on later hourly runs; failed or ambiguous recovery emits one bounded
   `BLOCKED` notification per incident/cooldown window. Telegram/network
   failures remain a separate reason and never create a code-skew recovery
   request.
7. Add focused tests for marker precedence/conflict, systemctl output parsing,
   path/PID validation, legacy fingerprint migration, atomic replacement,
   false-positive prevention, `_HERMES_GATEWAY` isolation, supervisor request
   creation, lock/cooldown/restart-loop behavior, post-restart verification,
   state recovery, and notification deduplication.
8. Bind marker emission to a tracked release-snapshot writer. Before
   implementation, locate the actual builder used to create
   `HERMES_HOME/releases/<name>`; if it is absent from the repo, add the
   smallest tracked builder or explicitly move the existing deploy step into
   the repo. It must stamp exactly canonical `.hermes-release-sha` from the
   merged source SHA and reject a release artifact without it. `scripts/release.py`
   must either perform this stamping when it is the canonical builder or be
   explicitly documented as the GitHub-release publisher that is not the
   snapshot writer. No future out-of-band marker filename is accepted.

## Explicit non-goals

- Do not make the calendar guard restart the gateway inline from inside the
  gateway process.
- Do not restart the gateway for Telegram DNS/connectivity timeouts alone.
- Do not treat `systemctl` system scope as equivalent to `systemctl --user`.
- Do not overwrite the live checkout or copy the active release into it.
- Do not change DGX service configuration, enable timers, modify cron job
  `85dcd4b817d6`, merge, deploy, or claim end-to-end Telegram delivery in the
  implementation phase without separate authorization.
- Do not combine this ticket with AUTH-001 or the plan-only AUTH-002 gate.

## Acceptance criteria

1. Against an active release containing `.hermes-release-sha`, the guard is
   silent when boot and active release identities match, even when the live
   checkout has a different HEAD.
2. Legacy `RELEASE_COMMIT` releases remain supported; missing or conflicting
   markers fail closed with an actionable diagnostic.
3. Legacy `RELEASE_SHA` releases are recognized during migration, while any
   conflicting or unknown marker state is `BLOCKED`; newly built releases
   contain only canonical `.hermes-release-sha`.
4. The guard logic and wrapper are sourced from the repository and covered by
   CI tests; DGX scripts are installed from the matching release snapshot.
5. The in-memory `gateway.code_skew` consumer and the calendar guard agree on
   the same release identity and boot record for release and checkout fixtures.
6. A real code-skew incident can be recovered only through the supervisor-
   owned, locked, bounded path, and the result proves the new MainPID and
   matching release fingerprint before reporting `RECOVERED`.
7. Repeated hourly runs do not produce repeated stale-code alerts after a
   successful recovery. Failed or exhausted recovery produces one bounded
   `BLOCKED` report with evidence and a retry/cooldown state.
8. Telegram/network failures remain separately classified and do not trigger
   code-skew recovery.
9. Focused tests, `py_compile`, `git diff --check`, and an independent
   authenticated DGX/WSL/Windows review sequence are recorded separately from
   merge, deployment, service health, and Telegram delivery evidence.

## Review record

- Ticket opened as a repo-local plan on 2026-08-14.
- Live diagnosis evidence: user-level gateway active/running; MainPID
  `2299452`; active release SHA `63bcd7ac...`; guard boot fingerprint
  `af99f0f1ad...`; checkout fallback `1c14d2b9df...`; active release marker
  name mismatch confirmed.
- DGX Spark Claude recursive review: `REVISE`. Required correction set:
  - put the guard comparison/marker logic in an in-repo testable module, or
    explicitly define the untracked DGX wrapper/library boundary and how AC6
    tests it;
  - state that the release identity reader and atomic boot-fingerprint writer
    are new code, specify `systemctl --user show` failure handling, and
    replace rather than duplicate the current non-atomic unread fingerprint;
  - explicitly decouple any recovery helper from the gateway cron subprocess
    tree and inherited `_HERMES_GATEWAY=1` (for example via a supervisor-owned
    oneshot path or a documented `setsid`/double-fork boundary);
  - name and reuse `cron/jobs.py::_jobs_lock()`,
    `gateway/restart_loop_guard.py`, and `hermes_cli/gateway.py`'s `system=`
    scope convention;
  - define the new persistent alert-deduplication state and its tests.
- WSL Claude: authenticated, but the bounded recursive review timed out after
  210s without a verdict. WSL AGY: bounded review timed out after 210s without
  a verdict. Windows AGY: binary present but unauthenticated. Windows Claude:
  authenticated via `claude.cmd`, but the bounded review timed out without a
  verdict. These are availability results, not PASS results.
- Correction pass: the plan was revised to cover all five DGX correction
  items, plus the live `RELEASE_SHA` marker and canonical marker-writer
  boundary found on the second DGX pass.
- Independent second-pass consensus requirement: one authenticated Claude and
  one authenticated AGY, independently reviewing the same correction set.
  Consensus was reached with DGX Spark Claude `PASS` and WSL AGY `PASS`, both
  with no findings or correction set. Native Windows Claude also returned
  `PASS` as an unnecessary supplemental check; it was not required for the
  gate. WSL Claude timed out and Windows AGY was unauthenticated; neither is
  counted.
- Cross-review gate: `PASS` for the corrected plan, with the required
  Claude+AGY consensus reached. The ticket remains plan-only; implementation,
  merge, deployment, and cron reconfiguration remain pending separate
  authorization.
- Implementation pass started after the plan consensus. Added the repo-owned
  identity/guard/recovery modules, canonical snapshot marker writer, guard
  wrapper, user-systemd recovery templates/installer, and focused tests.
- Local evidence before implementation review: calendar guard/identity tests
  `12 passed`; legacy code-skew tests excluding the unrelated model-switch
  import path `9 passed, 2 deselected`; targeted `py_compile` passed and
  `git diff --check` passed. The full model-switch pair remains unavailable in
  the default Python because `httpx` is not installed; the managed `.venv`
  test run is blocked by the existing Windows pytest temp-root ACL.
- Implementation correction pass: added legacy one-line boot-record coverage,
  explicit unresolved-fingerprint no-write coverage, an explicit `None` guard
  before boot-record serialization, and warning visibility for boot-record
  write failures. Final focused evidence: calendar guard/identity tests
  `12 passed`; legacy code-skew tests excluding the unrelated model-switch
  import path `11 passed, 2 deselected`; targeted `py_compile` passed and
  `git diff --check` passed.
- WSL AGY implementation review initially returned `REVISE`; its valid
  coverage request was implemented. Its repeated `None.startswith()` finding
  was disproved by the existing early return and the passing no-write test.
  Final WSL AGY review: `PASS`, `FINDINGS: NONE`, `CORRECTION_SET: NONE`.
- Authenticated DGX Spark Claude was attempted three times with bounded
  read-only packets (full packet, 50-KB packet, and low-effort 180-second
  timeout); no verdict was produced. Authenticated WSL Claude fallback also
  timed out after 180 seconds without a verdict. These are availability
  results, not PASS results. Required independent Claude+AGY consensus was
  therefore not established; implementation review gate is `BLOCKED`.
- Merge, deployment, cron/unit installation, and DGX runtime verification were
  not performed and remain separately unauthorized.
- 2026-08-15 recheck: authenticated WSL Claude was retried with the same
  candidate implementation in a 36-KB packet, Sonnet, low effort, no tools,
  and a 150-second bound; it timed out without a verdict. The required
  Claude+AGY consensus remains unavailable, so the gate stays `BLOCKED`.
- 2026-08-15 requested-host recheck: authenticated DGX Spark Claude received a
  36-KB read-only packet and returned `Execution error` after the 180-second
  bounded attempt without a verdict. Authenticated Windows Claude received
  the same packet and returned `API Error: Unable to connect to API
  (ConnectionRefused)`. Neither result is a review verdict or PASS; AGY's
  existing final `PASS` remains one-sided evidence only.
- 2026-08-15 retry: DGX Spark Claude was retried with a reduced 34.5-KB
  read-only packet, Sonnet, low effort, and a 180-second bound; it again
  returned `Execution error` without a verdict. The review gate remains
  `BLOCKED`.
- Post-correction review round: authenticated DGX Claude returned a grounded
  `REVISE` finding for the exhausted-incident hourly re-alert loop. The
  correction pass added `recovery_exhausted` dedupe state, serialized
  `check_once()` request/state writes under the recovery lock, normalized
  missing/pruned release directories to `GatewayIdentityError`, narrowed the
  unknown-marker heuristic, documented the intentional fail-closed lock
  semantics, and added regression tests. Local evidence after this pass:
  calendar guard `15 passed`; code-skew `11 passed, 2 deselected`;
  `py_compile` and `git diff --check` passed.
- Post-correction re-review could not complete: DGX Claude with tools timed
  out/returned `Execution error` in two bounded attempts; packet-only DGX
  Claude returned `Execution error` in a bounded attempt; Windows Claude
  timed out on the minimal PING; WSL AGY returned
  `Eligibility check failed ... EOF`. The prior AGY `PASS` predates this
  correction set and is not reused. Current implementation gate remains
  `BLOCKED`; no merge, deployment, cron/unit installation, or DGX runtime
  change was performed.
- 2026-08-15 AGY retry: authenticated WSL AGY was invoked through the
  canonical absolute binary with `--mode plan --sandbox`, explicit Hermes
  workspace, a 52-KB packet, and a 180-second bound. Post-correction result:
  `PASS`, `FINDINGS: NONE`, `CORRECTION_SET: NONE`. Claude post-correction
  verdict is still unavailable, so independent consensus is not established
  and merge/deploy remain blocked.
- 2026-08-15 final consensus retry: authenticated DGX Spark Claude was invoked
  with model `haiku`, a 52-KB packet containing the same post-correction
  implementation, packet-only read-only scope, and a 180-second bound. It
  returned `PASS`, `FINDINGS: NONE`, `CORRECTION_SET: NONE`. Together with
  the post-correction authenticated AGY `PASS` on the same candidate packet,
  the independent Claude+AGY correction-set consensus is now `PASS`.
- Review gate is complete. Merge, CI/PR, deployment, service health, and
  Telegram delivery remain separate gates and are not implied by this review.
- 2026-08-15 complete-packet model re-review: the earlier Haiku `PASS` used a
  packet that contained literal truncation markers and is not valid evidence
  for the full implementation. Authenticated DGX Spark Claude Opus and
  Sonnet both reviewed the complete post-correction packet and returned
  `REVISE`. Findings included recovery classification, long lock ownership,
  attempt accounting, systemd timeout/environment semantics, and exhausted
  BLOCKED delivery. The prior PASS record is superseded; merge and deployment
  are blocked.
- Correction pass after complete-packet review: recovery requests are now
  created only for proven `SKEW`; service-down and unverifiable states are
  deduplicated `BLOCKED` diagnostics; attempts are claimed before restart
  outside the lock; exhausted requests are removed and surfaced once by the
  hourly path; recovery verification tolerates legacy missing release paths;
  systemd uses `TimeoutStartSec=300` and `UnsetEnvironment`; installer and
  snapshot/marker validation are fail-closed; focused tests now pass `29` with
  `2` unrelated model-switch tests deselected, plus `py_compile` and
  `git diff --check`. Independent re-review is still required.
- Second correction pass after the complete-packet re-review: the installed
  wrapper is now rendered with the matching release path; BLOCKED/SKEW
  notifications and pending requests are deduplicated at hourly cadence;
  recovery attempts have a rolling window, stale RUNNING-claim recovery,
  one absolute deadline, and reset-on-success accounting; legacy boot records
  are migration-only; and marker-name validation is case-insensitive with
  documented file suffix exclusions. Focused evidence after this pass is
  `33 passed, 2 deselected` for the exact calendar/code-skew selection, plus
  `py_compile` and `git diff --check`. Final independent re-review is pending.
- Final independent review consensus after the second correction pass:
  authenticated DGX Spark Claude Sonnet 5 reviewed the complete two-part
  packet and returned `PASS` with only non-blocking P2/P3 observations;
  authenticated WSL AGY (Gemini 3.7 Flash) reviewed the same packet and
  returned `PASS`, `FINDINGS: NONE`, `CORRECTION_SET: NONE`. The packet had no
  intentional truncation. The accepted review gate is `PASS`; merge, CI,
  deployment, service health, and Telegram delivery remain separate gates.
- Final local evidence after the accepted correction set: `34 passed, 2
  deselected` for `tests/hermes_cli/test_calendar_guard.py` and
  `tests/test_code_skew.py -k 'not ModelSwitchSkewGuard'`; targeted
  `py_compile` passed; `git diff --check` passed.
- Deployment correction: the first merged snapshot exposed two installer
  integration defects before guard rollout was complete. Global placeholder
  replacement made the rendered wrapper fall back to the live checkout, and
  `Path.resolve()` collapsed the Hermes venv entry point to its bare base
  interpreter. The wrapper now checks only rendered-path existence, and the
  installer preserves the venv path with `Path.absolute()`; an installer
  regression assertion was added.
- Correction-set local evidence: calendar guard tests `23 passed`;
  `py_compile` and `git diff --check` passed. Authenticated DGX Spark Claude
  Sonnet 5 reviewed the complete correction packet and returned `PASS` with
  only non-blocking P2/P3 observations. Authenticated WSL AGY reviewed the
  identical packet with Gemini 3.7 Flash Low and returned `PASS`,
  `FINDINGS: NONE`, `CORRECTION_SET: NONE`.
