---
title: "HERMES-AUTH-001: DGX SSH authentication recovery"
status: REVIEWED_PENDING_MERGE
date: 2026-08-14
type: reliability
ticket: HERMES-AUTH-001
target_repo: hermes-agent
---

# HERMES-AUTH-001: DGX SSH authentication recovery

## Gate

This repo-local ticket centralizes the safe connection path from Windows/Codex
to the authenticated DGX Spark host. GitHub issues are disabled for this
repository. The mechanism must never store passwords, MFA codes, private keys,
tokens, or host-key bypasses in the repository.

Current gate: `IMPLEMENTED_PENDING_REVIEW`.

Required sequence: independent review of the ticket and wrapper, implement the
consensus correction set, re-review, then local hermetic tests, CI, and only
separately authorized deployment or user-environment installation.

## Observed failure and verified recovery

The Windows OpenSSH route returned `Permission denied (publickey,password)`.
A bounded WSL route subsequently succeeded:

```text
SSH_OK
55-0940189-03
cwliao
```

The requested `/home/cwlia/.ssh/id_ed25519` path was absent in that WSL
instance; SSH succeeded through another available identity/agent. The fix must
therefore try the existing agent/default identity before reporting that
reauthentication is required. A single missing preferred key is not proof that
authentication is unavailable.

## Objective

Make future DGX operations reuse the authenticated WSL SSH agent and a short
lived SSH control connection automatically. If the connection genuinely needs
human authentication, emit one machine-detectable `REAUTH_REQUIRED` result and
provide an explicit interactive bootstrap path. Do not silently fall back to a
new headless agent or bypass host-key verification.

## Proposed mechanism

- `scripts/dgx_ssh.sh` is the canonical WSL wrapper for `probe`, `exec`,
  `auth`, and `bootstrap`.
- `scripts/dgx_ssh.ps1` is the Windows entrypoint. It tries the canonical WSL
  wrapper first, then uses native Windows OpenSSH only for an authentication
  failure; it keeps the same strict host-key and bounded-time policy.
- `probe` and `exec` use `BatchMode=yes`, `StrictHostKeyChecking=yes`, bounded
  connect/keepalive timeouts, and `ControlMaster=auto` with a ten-minute
  `ControlPersist` socket under the WSL runtime directory.
- A readable configured identity is added as a candidate, but the wrapper does
  not force it with `IdentitiesOnly=yes`; an already-loaded SSH agent can still
  satisfy authentication when that file is absent or different.
- `bootstrap` first probes and reuses any working identity. Only when that
  fails may the user interactively generate/load a key and authorize its public
  half on DGX. The wrapper never receives or persists a password/MFA value.
- Exit code `75` means `REAUTH_REQUIRED`; host-key errors remain fail-closed and
  are not converted into authentication success.

## Implementation evidence

- Added `scripts/dgx_ssh.sh`, `scripts/dgx_ssh.ps1`, and
  `docs/dgx-ssh-recovery.md`.
- The WSL wrapper syntax check passed with `bash -n`.
- PowerShell parser validation passed.
- Hermetic policy tests passed: `2 passed`.
- Live read-only wrapper evidence passed through both entrypoints:
  `probe -> SSH_OK`, host `55-0940189-03`, user `cwliao`; PowerShell
  `exec hostname` returned the same host.
- The Windows entrypoint now has a native OpenSSH fallback for WSL
  authentication failures; its fallback policy is covered by the static
  contract test. A WSL remote-command failure is not misclassified as an auth
  failure and is returned directly.
- No bootstrap, key generation, `ssh-copy-id`, remote file write, service
  restart, or agent launch was performed. The currently working identity/agent
  was reused.
- The PowerShell-to-WSL path conversion bug found by the live smoke test was
  corrected before recording the passing evidence.
- Self-review correction: host-key and general transport failures now remain
  fail-closed instead of being mislabeled as `REAUTH_REQUIRED`; the native
  Windows fallback is entered only for the wrapper's explicit authentication
  failure status, and `bootstrap` does not proceed after a non-auth failure.
- Consensus correction set implemented in commit `a1e6a8a62`: PowerShell native
  probe now returns structured output/status data so internal fallback does not
  pollute command output; WSL path resolution failure falls back to native
  `probe`/`exec`/`auth`; native Windows honors `HERMES_DGX_IDENTITY`; bootstrap
  key-generation failure is reported as `BOOTSTRAP_KEYGEN_FAILED` with status
  70; and shell tests exercise permission-denied, host-key, success, and
  remote-argument forwarding behavior.
- Post-correction local evidence: `tests/scripts/test_dgx_ssh_wrapper.py`
  passed `6 passed`; PowerShell parser validation and WSL `bash -n` passed.
- The behavior suite now runs a Windows PowerShell mock harness that exercises
  WSL authentication failure -> native fallback, WSL path-resolution failure
  -> native fallback, single-output preservation, and configured identity
  forwarding. Shell behavior tests fail when neither `bash` nor `wsl.exe` is
  available instead of silently skipping.
- Final local evidence: `tests/scripts/test_dgx_ssh_wrapper.py` passed
  `8 passed`, including spaced-identity auth argument forwarding and remote
  exit-75 sentinel stripping; PowerShell parser, WSL `bash -n`, and
  `git diff --check` passed.
- Follow-up path coverage found and fixed the same output/status aggregation
  defect in native PowerShell `auth`; the mock harness now executes `probe`,
  `exec`, and `auth` fallback paths plus a real shell bootstrap keygen failure,
  with `7 passed` recorded locally.
- Follow-up collision fix: the WSL-to-PowerShell `exec` bridge marks a remote
  command exit status `75` before returning it, so PowerShell does not mistake
  a real remote `75` for `REAUTH_REQUIRED` and execute a non-idempotent command
  twice. Native interactive auth now uses a waited, inherited-console process
  so password/MFA prompts are not captured into a buffered output array.
- Collision regression evidence: the hermetic suite now has `8 passed`,
  including a remote-command exit-75/no-native-fallback assertion.
- GitHub CI evidence for code SHA `bcca3df06` is green: run
  `31784150099` ([CI run](https://github.com/cwliao/hermes-agent/actions/runs/31784150099));
  all required checks passed, 4,941 Linux tests passed, and the Windows-only
  PowerShell harness was explicitly skipped on the Linux runner. The local
  Windows run executed all 8 wrapper tests successfully.
- GitHub CI evidence for final implementation SHA `f016b925f` is green: run
  `31785366128` ([CI run](https://github.com/cwliao/hermes-agent/actions/runs/31785366128));
  all required checks and all 8 Python test slices passed.
- The broader `tests/scripts` collection remains environment-blocked outside
  this change: the native interpreter lacks `httpx`, and the project venv's
  pytest temp root has an existing Windows ACL denial. These are recorded as
  environment evidence, not treated as AUTH-001 test failures.

## Scope boundaries

In scope: local wrapper scripts, deterministic auth-status classification,
bounded SSH reuse, unit/shell-contract tests, and operator documentation.

Out of scope: editing the live DGX checkout, changing `sshd_config`, disabling
host-key checks, storing credentials, automating MFA, modifying Telegram, or
creating a new Claude/AGY headless session.

## Acceptance criteria

1. A successful existing WSL agent/default-identity probe returns `SSH_OK` and
   does not emit a reauth warning.
2. A failed non-interactive probe returns `REAUTH_REQUIRED` with exit code 75;
   it never prompts for a password and never runs `sshpass`, `expect`, or an
   equivalent credential bypass.
3. Repeated `exec` calls reuse the control connection when available and are
   bounded by connect/keepalive timeouts.
4. Host-key verification is strict; unknown or changed host keys cannot be
   auto-accepted by the wrapper.
5. `bootstrap` is the only interactive path and keeps all secrets outside the
   repository; a successful existing identity prevents key generation.
6. Hermetic tests cover command policy, exit/status vocabulary, argument
   forwarding, and no-secret/no-headless invariants. No test makes a network
   call.
7. Separate evidence names local tests, GitHub CI, DGX connection, and any
   downstream agent-review result.

## Review record

- Initial local design review: pending implementation review.
- DGX auth preflight: `PASS` via WSL on 2026-08-14; the preferred explicit key
  path was absent but another available SSH identity/agent succeeded.
- Real-session routing evidence: no uniquely addressable Hermes Claude/AGY
  session was available on DGX, WSL, or native Windows. The DGX Claude remote
  processes were either session servers without a Hermes cwd or a client in
  `/home/cwliao/dgx-workspace`; they were not reused for this ticket.
- DGX AGY packet-only review: `REVISE` from AGY 1.1.13. Packet SHA256:
  `3b4e3f8bef76fc89555132dc76e10825d048b2d317475dc504c1f95df139eb26`.
  Findings: PowerShell native-probe output/status separation; WSL path
  resolution must fall back to native Windows; PowerShell must honor
  `HERMES_DGX_IDENTITY`; static tests need behavioral coverage.
- DGX Claude packet-only review: `REVISE` using the existing authenticated
  Claude CLI in no-session-persistence print mode after real Hermes sessions
  were unavailable. Findings: PowerShell native-probe output/status handling;
  behavioral rather than static tests; distinguish bootstrap keygen/tool
  failures from `REAUTH_REQUIRED`; attach CI evidence after the correction.
- Reconciliation: Claude and AGY agree on the two medium-risk corrections
  (PowerShell fallback output/status handling and behavioral tests). The
  complete correction set also includes WSL resolution fallback,
  `HERMES_DGX_IDENTITY` parity, and distinct bootstrap failure status. Target
  and WSL distro de-duplication is a non-blocking maintainability follow-up.
- Consensus: `REVISE`; AUTH-001 is not ready for merge or deployment until the
  correction set is implemented, independently re-reviewed, and CI is rerun.
- Initial correction implementation: commit `a1e6a8a62` pushed to
  `ticket/hermes-auth-001`; subsequent follow-up commits are recorded below.
- First correction re-review packet (`4e79dc46563518bc144bf84a1f94ed34532288bfffc419dd3c80840dc987dee4`):
  AGY returned `PASS`; Claude returned `REVISE` because PowerShell `exec`/`auth`
  and shell bootstrap failure paths still lacked behavioral execution.
- Follow-up correction commits `83ffc667c` and `bcca3df06` added structured
  native-auth status handling, behavioral `exec`/`auth`/bootstrap coverage, and
  the explicit non-Windows skip policy; CI evidence above is attached before
  the final re-review.
- Final review correction set: native interactive auth now quotes
  `Start-Process -ArgumentList` values so configured identity paths containing
  spaces survive parsing; the internal remote-exit sentinel is stripped before
  caller-visible output; and CI now includes a `windows-latest` wrapper-test
  job so PowerShell behavior is continuously exercised rather than only
  locally reported.
- Final review cycle found and addressed a remote-exit-75 collision between
  wrapper `REAUTH_REQUIRED` and ordinary remote command statuses; final
  independent re-review is required against the successor commit.
- Final AGY/Claude review of packet
  `9a26739739333c0a0991faea1e577a3df92508ed3b05f4f73c5779c06b3a9c8e` found
  `REVISE` from Claude for the remote-exit-75 collision, native auth argument
  quoting, sentinel leakage, and missing Windows CI coverage. Those findings
  are implemented in the successor commit and require one more independent
  review cycle.

- Final correction commit `e9d5f9ab1` adds `BASH_ENV` shell-function shims to
  the Git Bash fallback test harness. This prevents a Windows runner without
  Ubuntu WSL from resolving the real OpenSSH client, while retaining the
  existing fake-SSH behavior. Local hermetic evidence is `8 passed`; the
  GitHub Windows job in run `31789781906` recorded `8 passed in 9.84s` with no
  skipped tests or network connection attempt. The full CI run is green:
  [31789781906](https://github.com/cwliao/hermes-agent/actions/runs/31789781906).
- The final review packet was built from branch SHA `e9d5f9ab1` and matched on
  DGX by SHA256
  `36e8716c8a7096d74affcd0dae967a38a1508af6b83d3120d2facd37c8cd727a`.
  AGY and Claude independently returned `VERDICT: PASS` for that exact packet.
  Claude's host/user disclosure note was reconciled against live GitHub
  metadata: the repository is public. It is accepted as a non-blocking,
  explicitly acknowledged operational follow-up for a future target-config
  hardening ticket; it is not a credential or authentication bypass.
- The CI classifier was independently exercised with only
  `scripts/dgx_ssh.sh` and `scripts/dgx_ssh.ps1`; it returned `python=true`,
  so the `windows-wrapper-tests` gate is not silently skipped for wrapper-only
  changes. Merge and deployment remain separate authorization gates.
- Consensus: `PASS`; AUTH-001 is ready for a separately authorized merge, but
  this task did not merge, deploy, or modify the live DGX checkout.
