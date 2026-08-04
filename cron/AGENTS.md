# Cron Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns scheduled-job
storage and execution.

`cron/jobs.py` owns the store and `cron/scheduler.py` owns the tick loop.
Preserve these invariants:

- a three-minute hard interrupt bounds agent sessions;
- catch-up is half the period, clamped to 120 seconds through two hours;
- missed one-shot jobs get the existing 120-second grace window;
- the tick lock prevents duplicate schedulers;
- cron skips memory providers by default;
- delivery remains in a framed cron session rather than mutating another
  conversation's role history.

`workdir` is the explicit opt-in to repository context and its policy files.
Jobs without it remain detached from a repository. Script-only jobs use the
existing `no_agent` path rather than constructing a fake agent turn.

User behavior and supported schedule formats are documented in
[`website/docs/user-guide/features/cron.md`](../website/docs/user-guide/features/cron.md);
implementation detail belongs in
[`website/docs/developer-guide/cron-internals.md`](../website/docs/developer-guide/cron-internals.md).
