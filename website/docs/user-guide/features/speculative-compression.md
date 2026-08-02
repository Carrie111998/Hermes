---
title: Speculative Compression
description: Opt-in tool-wait context preparation for the built-in compressor.
---

# Speculative Compression

Speculative compression is an **opt-in, disabled-by-default** latency optimization for the built-in context compressor. While an external tool runs, a detached worker may prepare a summary of an immutable copy of the conversation prefix; the foreground path installs the candidate only under the normal per-session compression lock after re-validating fingerprint, boundary, and compressor identity. If no candidate is usable, the existing synchronous pre-API compaction remains the safety fallback.

It never runs on its own: scheduling happens only during tool execution, at or above `compression.speculative.start_ratio` of the effective input budget.

:::caution Operational impact
Compression is one of the few operations that rewrites already-sent history and invalidates the provider prompt-cache prefix. Speculative compression performs the **same** kind of rewrite, at the same episodic boundaries — but it must never do so more often, with a stale transcript, or under a different model than the live compressor would. The invariants in [Operational invariants](#operational-invariants) are load-bearing; review changes against them.
:::

## Where the code lives

| Piece | Location |
|---|---|
| Worker module (settings, snapshot, candidate, manager, executor) | `agent/speculative_compression.py` |
| Foreground install path | `agent/conversation_compression.py` (`compress_context` + `speculative_candidate` param) |
| Preflight + post-tool admission | `agent/turn_context.py` (`_try_install_speculative_candidate`) and `agent/conversation_loop.py` |
| Agent wiring / invalidation | `agent/agent_init.py`, `run_agent.py` |
| Tests | `tests/agent/test_speculative_compression*.py` (5 files) |

## Configuration

```yaml
compression:
  speculative:
    enabled: false        # opt-in; the runtime stays off until set true
    start_ratio: 0.70     # prepare after this effective input pressure
    hard_ratio: 0.85      # wait/fall back synchronously at this pressure
    max_age_seconds: 180  # discard candidates older than this (0 = immediate expiry)
    hard_wait_seconds: 2  # bounded foreground wait for a ready candidate
    during_tool_wait: true

```

Ratios apply to the **effective input budget** (`context_length − max_tokens` output reservation) of the **currently active model**. `compression.threshold_tokens` (absolute cap), when set below the ratio watermarks, caps the speculative triggers too. On small context windows where the minimum-window floor collapses the watermarks, speculation disables itself.

Rollback: set `enabled: false` and restart the CLI/gateway. There is no runtime toggle.

## How to tell it is running

- **Classic CLI status bar**: `spec:on` (armed) → `spec:✓` (a candidate was committed this session). Only visible when enabled.
- **TUI**: `session_info.speculative_compression` is `"enabled" | "installed" | null` (typed on `SessionInfo`; the Pi status rule does not render it yet).
- **Info logs**: `hermes logs | grep speculative` — every state transition is logged with a `disposition` (INFO level):

| Disposition | Meaning |
|---|---|
| `started` | Worker job launched for a tool-wait snapshot |
| `coalesced` | A newer snapshot arrived while the job ran; it will rerun if different |
| `rerun` | Coalesced snapshot differs → fresh job |
| `ready` | Candidate published to the manager |
| `installed` | Candidate committed under the compression lock (success) |
| `rejected` / `deferred_lock` | Install refused (stale/expired/mismatch) or deferred by lock contention |
| `stale` | Candidate expired or no longer matches the transcript |
| `restored` | A deferred candidate was requeued for a later attempt |
| `blocked_cooldown` / `blocked_ineffective` | Scheduling skipped (summary LLM cooldown / anti-thrash) |
| `cancelled` | Session invalidation |

## Troubleshooting runbook

**Feature enabled but `spec:on` never becomes `spec:✓`**
The session never reached `start_ratio` (check `context_percent` in the status bar), or scheduling is blocked (`blocked_*` dispositions in logs), or candidates are constantly `stale`. A `stale` loop with a healthy transcript usually means the boundary message changed every turn (rare); the synchronous path still compresses normally.

**Aux summary calls during a 429 cooldown**
This was a shipped bug and is fixed: scheduling is gated on the live compressor's cooldown and anti-thrash state. If you see it again, the gate regressed — check `schedule_tool_wait_candidate` in `agent/speculative_compression.py` and its tests.

**Turn hangs / agent frozen**
The manager is process-global and daemon-threaded. A verified deadlock in callback registration was fixed in `3b1255cbe` (callbacks register outside the manager lock). If a hang reappears, capture `py-spy dump` or a stack dump and look for `_finish_job` / `maybe_start` lock waits. Restarting the process is the immediate remedy; the daemon threads die with the process.

**Candidate installed under the wrong model**
Candidates carry a compressor-settings fingerprint (model, context length, thresholds). A model switch invalidates them; install is rejected. If you see an install after a model switch, the fingerprint comparison regressed.

**Memory growth in long-lived gateway processes**
Completed manager entries are pruned on every access. If `hermes-memory/memory-distillation-log.md` audits show growth, check `_prune_completed_entries` coverage.

**Model fallback churn while the feature is on**
Each fallback changes the compressor fingerprint, so candidates die at install — that is correct, not a bug. The feature simply degrades to the synchronous path during fallback storms.

## Operational invariants

Review any change against these (each maps to a reviewed finding):

1. **No transcript corruption**: the preserved tail is spliced verbatim; the summary never merges into a tail message (`_TAIL_MARKER` check in `build_candidate`).
2. **No double-compression**: single-flight per session; install consumes the candidate; deferred installs requeue (`restore_candidate`) instead of re-running the summary LLM.
3. **No compression while blocked**: scheduling and hard-pressure forcing respect the summary-LLM cooldown and the anti-thrash "ineffective" breaker (ready candidates may still install — their LLM call already happened).
4. **No stale installs**: fingerprint (covered prefix + boundary) and compressor identity are re-validated under the session compression lock before commit.
5. **No unbounded state**: completed/failed manager entries are pruned on every access point.
6. **No deadlocks**: `add_done_callback` is never registered while holding the manager lock.
7. **No behavior change when disabled**: with `enabled: false` the feature must be a no-op on every code path (status `None`, no fragments, no worker threads spawned on behalf of the agent — the process-global manager is only touched when an agent has the feature enabled).

## Verification

```bash
scripts/run_tests.sh tests/agent/test_speculative_compression.py \
  tests/agent/test_speculative_compression_commit.py \
  tests/agent/test_speculative_compression_config.py \
  tests/agent/test_speculative_compression_manager.py \
  tests/agent/test_speculative_compression_tool_overlap.py -q
```

Regression-sensitive neighbors (run when touching `compress_context`, preflight, or the post-tool gate):

```bash
scripts/run_tests.sh tests/agent/test_turn_context.py \
  tests/agent/test_preflight_compression_gate.py \
  tests/agent/test_compression_rotation_state.py \
  tests/agent/test_compression_anti_thrash_persistence.py \
  tests/agent/test_compression_anti_thrash_recovery.py \
  tests/agent/test_compression_max_attempts_config.py \
  tests/run_agent/test_run_agent.py -q
```

History: the feature shipped in `60fb7531d` and was hardened by two independent blind reviews (10 + 7 findings, including one reproduced global deadlock) in `3b1255cbe`. Future changes to this feature should be reviewed against the findings listed in that commit message.
