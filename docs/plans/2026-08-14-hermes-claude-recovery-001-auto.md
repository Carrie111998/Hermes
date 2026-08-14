---
title: "HERMES-CLAUDE-RECOVERY-001: safe Claude Remote Control recovery"
status: MERGED_DEPLOYED
date: 2026-08-14
type: reliability
ticket: HERMES-CLAUDE-RECOVERY-001
target_repo: hermes-agent
---

# HERMES-CLAUDE-RECOVERY-001: safe Claude Remote Control recovery

## Gate

This ticket adds an opt-in, host-local CLI recovery path for an already
configured Claude Remote Control session. It does not create a Claude session,
perform OAuth, read credentials, inject into a TTY, or restart/kill processes.
Review, CI, merge, and deployment remain separate gates.

## Problem

The existing visible Windows/WSL Claude Remote Control launcher can be absent
or delayed even when Claude CLI authentication is still valid. Hermes needs a
repeatable status and repair action that can be scheduled on the host owning
the Windows Scheduled Task, without confusing a KLIB-scoped session with a
Hermes-scoped session.

## Approved implementation boundary

- Add `hermes claude-recovery status` and `hermes claude-recovery repair`.
- Run only on the native Windows host that owns Task Scheduler, with
  `powershell.exe` and `wsl.exe` available. Invoking Windows Task Scheduler
  from inside WSL is not claimed as supported by this ticket.
- Require explicit `external_cli.remote_control_recovery.enabled: true` plus
  a configured Scheduled Task name and Remote Control name. Empty defaults are
  intentional: the known `Codex-Claude-WSL-RemoteControl` task is KLIB-scoped
  and must not be reused for Hermes.
- Inspect task state, a count-only Remote Control process probe, and Claude auth
  status. Never print command output, response bodies, session URLs, or tokens.
- Repair may call only `Start-ScheduledTask` for the configured task, and only
  when auth is valid, the task exists, and there is no existing session. It
  never starts a new Claude binary directly and never kills or restarts a task.
- A running task with no session is reported for operator review rather than
  being started again. Multiple matching sessions fail closed.

## Out of scope

- Creating or modifying Windows Scheduled Tasks;
- OAuth/MFA, token refresh, credential copying, or authentication automation;
- selecting or repurposing the existing KLIB task;
- DGX monitoring, SSH repair, agentmemory writes, Telegram delivery, or LLM
  watchdogs;
- automatic installation, merge, deployment, or timer enablement.

## Acceptance criteria

1. The CLI fails closed when disabled, unconfigured, off-host, unauthenticated,
   task-missing, task-running-without-session, or ambiguous.
2. Repair is idempotent and can only trigger the configured existing task.
3. Tests cover command construction, configuration validation, status
   classification, duplicate-session refusal, auth failure, and redaction.
4. Documentation states that a successful trigger is not proof of a ready
   Claude session; readiness must be observed separately.
5. Local tests and CI are recorded separately from external Claude/AGY review,
   DGX runtime evidence, merge, and deployment.

## Review record

- Ticket opened: 2026-08-14.
- Implementation completed locally on the ticket branch.
- Focused tests: `15 passed` in
  `tests/hermes_cli/test_claude_recovery.py` using the managed `.venv`.
- Existing parser/batch regression tests: `30 passed` in
  `tests/hermes_cli/test_subparser_routing_fallback.py` and
  `tests/hermes_cli/test_subcommands_batch.py`; combined local test evidence
  is `45 passed`.
- Static checks: Ruff passed, `py_compile` passed, and `git diff --check`
  passed (with only pre-existing LF/CRLF warnings).
- CLI smoke: isolated `HERMES_HOME` returned redacted JSON status
  `DISABLED` for both `claude-recovery --json` and
  `claude-recovery status --json`; no task, process, auth, or credential action
  was performed.
- Local recursive review round one found that `Running`, `Queued`, and
  `Disabled` task states needed separate fail-closed handling; round two passed
  after restricting repair to `Ready` and adding regression coverage.
- Cross-review round three found that YAML type confusion could make the
  string `"false"` truthy and that non-finite timeout values were not rejected;
  both were corrected and covered by tests. Local review now passes.
- Local security cross-review: `PASS` after the above correction set.
- Local operations cross-review: `PASS` for host/task ownership, idempotency,
  and the no-mutation boundary; live readiness remains unverified because no
  Hermes-scoped task is configured or enabled.
- Review correction: polling now returns a definitive terminal failure observed
  after the repair trigger instead of replacing it with generic
  `REPAIR_TRIGGERED`; transient missing/running/queued states remain bounded by
  the absolute deadline. Regression coverage was added.
- Independent review round four used the same bounded read-only behavior packet
  after the correction: Claude `PASS` and AGY `PASS`. Claude additionally
  verified the committed source and regression test behavior. Consensus reached
  on the correction set; no KLIB session was repurposed and no permission bypass
  was used.
- GitHub CI rerun `31768361031` passed all required checks, including 8/8
  Python test slices, lints, security scans, e2e, and common-ancestor checks.
- PR #12 merged into `main` at
  `7018f93aaee7aa0319ee342ea860ad90da206c9b`.
- DGX deployment used isolated release
  `/home/cwliao/.hermes/releases/v2026.8.14-hermes-claude-recovery-7018f93aaee`
  with the merged SHA and archive checksum markers. Remote `compileall`
  passed before activation.
- The service now uses drop-in
  `25-hermes-claude-recovery-7018f93aa.conf`; verified MainPID `1969858`,
  active/running state, `ExecMainStatus=0`, `NRestarts=0`, matching cwd and
  `PYTHONPATH`, and no Traceback/ERROR for the new PID. Rollback is preserved
  by the preceding drop-in and release snapshot.
- Ticket status is `MERGED_DEPLOYED`; no Hermes-scoped scheduled task was
  enabled and no Telegram end-to-end readiness claim is made.
- Existing KLIB-scoped Claude recovery evidence is not reused as Hermes
  readiness evidence.
