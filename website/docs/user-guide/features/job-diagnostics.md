---
sidebar_position: 7
title: Long-job diagnostics
description: Inspect timing, blockers, heartbeats, and safe resume checkpoints.
---

# Long-job diagnostics

Hermes records local, profile-scoped diagnostics for each agent session and
task lane. The record answers four operator questions without calling a model:

1. What is running, blocked, idle, stale, or dead?
2. Where did the elapsed time go?
3. Why is this job slow?
4. Which recorded phase can be resumed safely, and what can run in parallel?

The operator commands are read-only. They do not launch a command, retry a
phase, stop a process, change a session, or edit the diagnostics record.

## CLI quick reference

```bash
# Compact dashboard: active, blocked, longest-running, idle, providers,
# worktrees, and the next operator decisions.
hermes jobs
hermes jobs status

# Inspect one job and every lane.
hermes jobs show 'session:019f-example'

# Explain the dominant timing buckets, retries, blockers, and output age.
hermes jobs why-slow 'session:019f-example'
hermes jobs why-slow 'session:019f-example' \
  --lane 'task:telegram-123'

# Recommend compatible pending lanes. This never launches them.
hermes jobs parallel
hermes jobs parallel 'session:019f-example'

# Validate the exact last checkpoint and print the recorded command.
# This never executes the command.
hermes jobs resume-plan 'session:019f-example'
hermes jobs resume-plan 'session:019f-example' \
  --lane 'task:telegram-123'

# Machine-readable state and resume decisions.
hermes jobs status --json
hermes jobs show 'session:019f-example' --json
hermes jobs resume-plan 'session:019f-example' --json
```

`resume-plan` exits with status 2 when repository identity, evidence hashes,
process ownership, or command metadata makes the resume unsafe.

## Telegram and other messaging platforms

The same reports are available through the gateway slash command. A Telegram
turn is tracked automatically under its Hermes session and task identity.

```text
/jobs
/jobs status
/jobs show session:019f-example
/jobs why-slow session:019f-example
/jobs why-slow session:019f-example task:telegram-123
/jobs parallel
/jobs parallel session:019f-example
/jobs resume-plan session:019f-example
/jobs resume-plan session:019f-example task:telegram-123
```

These commands are safe to run while another turn is active. They inspect the
local diagnostics snapshot and do not interrupt or queue against that turn.
Normal gateway command-access rules still apply; if Telegram uses an explicit
`user_allowed_commands` list, add `jobs` for non-admin users who should see
the reports.

Slack keeps its existing native slash commands within the platform's fixed
limit, so use the catch-all form there: `/hermes jobs`,
`/hermes jobs why-slow <job-id>`, and the other subcommands shown above.

## Timing model

Each job reports total elapsed time plus these structured buckets:

| Bucket | Meaning |
|---|---|
| `model_wait` | Provider/API response time, including retry delay inside the call |
| `tool_execution` | Shell and non-test tool execution |
| `test` | Recognized test commands such as `scripts/run_tests.sh`, `pytest`, and `npm test` |
| `review` | Review/lint/type-check commands and explicit review phases |
| `blocked_idle` | Time explicitly spent waiting, blocked, or idle |
| `evidence_generation` | Hashing, rendering, report-building, and explicit evidence phases |
| `compression` | Context-compression work |

Concurrent spans are unioned for job-wall accounting. If two ten-second lanes
overlap for five seconds, the dashboard reports 15 seconds of job-wall work,
not 20. The saved five lane-seconds appear as `parallel_overlap`.
Buckets are semantic rather than a forced partition: for example, a review
phase can contain model wait and tool time, so bucket percentages may overlap.
`busy_wall` is the cross-category deduplicated value.

## Lane visibility and liveness

Every lane includes:

- persisted and effective status;
- current step and last meaningful output;
- elapsed time and output age;
- retry count;
- blocker and next expected action;
- PID/create-time identity plus Hermes session/task identity;
- provider/model;
- exact worktree, branch, HEAD, and dirty-tree fingerprint;
- phase commands and evidence paths.

The effective status distinguishes:

- `working` — process and heartbeat are current;
- `waiting` — an external response or operator action is expected;
- `blocked` — a classified blocker prevents progress;
- `idle` — the process is alive but meaningful output is overdue;
- `stale` — the persisted heartbeat is too old to trust;
- `dead` — the recorded PID exited or was reused;
- `pending`, `completed`, and `failed`.

The blocker vocabulary is deliberately closed:

```text
code_failure
test_failure
missing_authorization
operator_presence_requirement
external_process_conflict
wrong_worktree_or_branch
hash_mismatch
remote_or_provider_failure
stale_session
infrastructure_issue
```

## Heartbeat configuration

The gateway keeps its existing long-running notification interval and adds
state-aware rendering and duplicate suppression:

```yaml
agent:
  gateway_notify_interval: 180  # seconds; 0 disables messaging heartbeats

job_diagnostics:
  enabled: true
  idle_after_seconds: 300
  stale_after_seconds: 900
  meaningful_output_warning_seconds: 600
  heartbeat_repeat_seconds: 540
```

A state transition (`working` to `waiting`, `blocked`, `idle`, or `dead`)
emits on the next interval. Unchanged text is suppressed until
`heartbeat_repeat_seconds`, and platforms that support message edits continue
to update one heartbeat message in place.

## Safe checkpoints and resume

Hermes session resume and job-phase resume solve different problems:

- `/resume` restores a conversation.
- `hermes jobs resume-plan` validates a long-job phase checkpoint.

A phase checkpoint records completed phase IDs, command, worktree, branch,
HEAD, dirty-tree fingerprint, process identity, and evidence paths with SHA-256
hashes. Before a job runner invokes an incomplete phase, it verifies all of
them. A mismatch fails closed before the phase callback runs. A completed
phase is skipped, so restarting the runner cannot duplicate it.

The operator command intentionally stops at a plan. It prints the recorded
command and validation verdict but never launches it. Code that owns a durable
long job uses the phase runner:

```python
from hermes_cli.job_diagnostics import JobRun, JobStateStore, TimingCategory

job = JobRun.start(
    JobStateStore(),
    job_id="release-check",
    lane_id="tests",
    title="Release checks",
    worktree="/workspace/hermes-agent",
).define_phases([
    {
        "phase_id": "focused-tests",
        "category": TimingCategory.TEST,
        "command": "scripts/run_tests.sh tests/affected/ -q",
    },
])

job.run_phase(
    "focused-tests",
    run_focused_tests,
    evidence_paths=["artifacts/focused-tests.txt"],
)
```

If `focused-tests` is already complete, `run_focused_tests` is not called.
If the repository or evidence changed, `RepositoryDriftError` is raised before
it is called.

## Parallel recommendations

`hermes jobs parallel` is conservative. It considers a pending lane safe only
when:

- all recorded dependencies are complete;
- its resume checkpoint validates;
- no owner process is still running;
- it shares no exclusive resource with an active/recommended lane; and
- writable lanes use different worktrees (same-worktree overlap is allowed
  only when both lanes are explicitly read-only).

The report is advice only. It never claims a lane or starts a process.

## Storage and privacy

State lives under:

```text
$HERMES_HOME/jobs/diagnostics/
```

Records are written atomically with profile isolation and restrictive file
permissions. Hermes stores activity summaries, not prompts or raw model/tool
output. Persisted command text goes through mandatory secret redaction.
Diagnostics never query a git remote. Malformed or missing files are reported
as warnings and do not prevent healthy jobs from appearing.
