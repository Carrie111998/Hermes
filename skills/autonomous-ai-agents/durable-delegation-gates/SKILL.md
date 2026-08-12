---
name: durable-delegation-gates
description: "Use when orchestrating parallel delegation with verification gates, checkpoints, recovery, metrics. Structured manager for long-horizon agentic workflows (builds on persistence + long-horizon)."
version: 1.0.0
author: Team Stockfish Harvesters (Hermes)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, gates, parallel, orchestration, checkpoints, recovery, metrics, verification, long-horizon, persistence]
    category: autonomous-ai-agents
    related_skills: [persistence-evolution-framework, long-horizon-agentic-workflows, agentic-test-campaigns, hermes-agent, lofoten-stockfish-harvest]
---

# Durable Delegation Gates

Structured parallel delegation manager with **verification gates**, state **checkpoints**, **recovery** protocols, and **metrics** collection. Fills critical gaps in Hermes long-horizon orchestration: delegation is powerful but currently fire-and-forget (limited shared durable state, weak recovery, no built-in parallel gates or drift detection).

Inspired by Lofoten maelstrom navigation: treat parallel subagents as vessels in swirling currents — use gates (like harbor locks), checkpoints (safe harbors), and metrics (tide logs) to prevent loss in eddies (failures, token exhaustion, concurrent conflicts).

## Overview

Hermes `delegate_task` enables parallel sub-agents, but gaps include:
- No native verification gate before parent admits child results to durable state.
- Weak cross-delegate shared state (rely on manual memory/files).
- Limited recovery from partial failure, token limits, network drops, drift.
- No first-class metrics for efficiency (startup vs mature), recoveries, gate pass rates.
- Risk of invalid state from concurrent writes or cache invalidation.

This skill provides:
- **Gate primitives**: Pre/post delegation JSON verdict schema (verdict: done|continue|blocked, evidence: hash+artifacts, retained_state_edits, next_action).
- **Checkpoint manager**: Structured CHECKPOINT.md + wave state with Kt contracts (ι, ot, ct, vt, Xt), hash chains.
- **Recovery engine**: Resume from last good checkpoint + partial results; handle drift, partials, re-delegate only deltas.
- **Parallel orchestration**: Bounded fan-out, role contracts (Manager/Planner/Engineer/Reviewer/Breaker), conflict detection.
- **Metrics & traces**: JSONL per delegation + aggregate (tokens, time, gate_pass, recovery_count, efficiency_delta).
- **Hardenings**: Oppositional probes built-in (invalid state injection, concurrent write tests, token limit simulation).

Builds directly on `persistence-evolution-framework`, `long-horizon-agentic-workflows`, `agentic-test-campaigns`. Pairs perfectly with `lofoten-stockfish-harvest` for gated parallel research.

## When to Use

- Complex multi-step or parallel missions (research campaigns, code + test + doc, multi-agent team workflows) > single delegation horizon.
- When results must be **gated** before durable writes (memory, skills, reports, CHECKPOINT updates).
- Long-horizon that must survive interrupts, token pressure, profile restarts, network variability.
- When you need **observable metrics** and **recoverable state** for evolution or reporting.
- "Run parallel sub-tasks for X with full gates and recovery."

**Don't use for:**
- Trivial single subtask (plain delegate_task suffices).
- Purely interactive one-shot (no need for durable gates).
- When no shared state or verification is required.

## Architecture & Primitives

**Roles (via delegation + main session):**
- **Manager** (grok/high): Owns top Kt contract, decides waves, admits only on gate "done".
- **Planner**: Decompose ot into parallel sub-missions; emit task specs with contracts.
- **Engineer/Worker**: Execute (may be leaf or use tools); produce artifacts + self-hash.
- **Reviewer**: Independent (separate delegate or sub-skill); strict JSON only verdict + evidence. Never trusts Engineer output alone.
- **Breaker** (oppositional): Attempts to break (inject bad state, concurrent, etc.); logs scars for hardening.

**Gate Contract (always JSON, machine readable):**
```json
{
  "verdict": "done|continue|blocked",
  "source": "independent_reviewer|self|verifier_script",
  "evidence": "sha256:abc123... + concrete files + key facts",
  "retained_state_edits": ["memory:key:...", "file:CHECKPOINT.md:delta", ...],
  "next_action": "null or specific follow-up",
  "metrics": {"tokens": 1234, "time_s": 45, "artifacts": 3},
  "timestamp": "...",
  "wave": 2
}
```

**State Model:**
- Top: CHECKPOINT.md (Kt + progress + gate history + last hashes)
- Per-delegate: subdir or tagged memory + artifact bundle with sha
- Global: mission-trace.jsonl , delegation-metrics.jsonl
- Locks: .lock files or atomic write patterns for concurrent safety.

**Recovery Flow:**
1. Load last CHECKPOINT + session_search for context.
2. Validate hashes vs live artifacts.
3. Re-delegate only incomplete/failed (delta plan).
4. Re-gate merged results.
5. On drift: Breaker mode or manual pivot in ct.

## Core Workflow (Gate Every Durable Change)

1. **Init / Resume**
   - Load or create CHECKPOINT.md with ι (immutable), current ot, ct (e.g. ["gated-only", "no concurrent writes without lock", "token<30k per delegate"]), vt (gate JSON + hash match + tests), Xt.
   - Reset or load metrics ledger.
   - todo init phases.

2. **Plan Parallel**
   - Delegate Planner (or self) to emit list of sub-tasks with per-task contracts.
   - Assign bounded concurrency (default 3, config or ct).
   - Use `delegate_task` with background=true for long ones; capture handles.

3. **Execute with Gates**
   - For each: spawn Engineer with full context + sub-contract.
   - On return: immediately run gate (Reviewer delegate or local verifier).
   - Gate **before** any write: memory add, file update, CHECKPOINT append, publish.
   - Record: child output hash, gate JSON, time/tokens.

4. **Checkpoint & Metrics**
   - Atomic append to CHECKPOINT (preserve history, add retained).
   - Append JSONL: wave, delegate_id, role, verdict, evidence_sha, tokens, recovery, pivot_reason.
   - Update efficiency: compare to prior wave.

5. **Recovery / Error Paths**
   - Network fail / timeout: resume from checkpoint, re-spawn only affected.
   - Token limit: split task, re-plan with smaller ot.
   - Invalid state / concurrent conflict: Breaker detects (hash mismatch), block + log + re-init from last good.
   - Gate "blocked": retain evidence, escalate to Manager for Xt update or abort wave.

6. **Close Wave**
   - All gates "done" → Manager admits, updates ct/ot for next, mirrors to vault.
   - Produce summary report + full trace.

## Usage Recipes

**Basic gated parallel (in session):**
```bash
# Init
mkdir -p .hermes/plans/2026-08-12-delegation-test/{artifacts,logs}
# Write CHECKPOINT.md with Kt

# Delegate parallel (example 2 workers)
delegate_task(goal="Research topic A with citations", context="full CHECKPOINT + contract", background=true)
delegate_task(goal="Research topic B with citations", context=..., background=true)

# On results: gate explicitly (or via skill)
# Reviewer delegate or python verifier
# Then memory + checkpoint only on pass
```

**Full campaign with this skill + agentic-test-campaigns:**
Use contract from agentic-test-campaigns, load this + persistence + lofoten-... 

**Cron autonomous wave:**
hermes cron create "0 * * * *" --name durable-delegation-wave --skills "durable-delegation-gates,persistence-evolution-framework,agentic-test-campaigns" --prompt "Load CHECKPOINT.md. Plan and execute next parallel gated wave. Gate everything. Append metrics. Resume on any prior partials."

**Metrics example (JSONL line):**
{"wave":1,"delegate":"eng-001","role":"Engineer","verdict":"done","evidence_sha":"def456","tokens":8920,"active_s":67,"recovery":0,"gate_source":"reviewer-delegate","artifacts":["report-A.md"],"efficiency_vs_prior":0.78}

## Integration Points

- Load with `persistence-evolution-framework` for state primitives.
- Use `long-horizon-agentic-workflows` role patterns + Kt.
- Pair with `lofoten-stockfish-harvest` for gated parallel catch/weave.
- `agentic-test-campaigns` for designing the test campaign around it (see below).
- `todo` for phase tracking inside Manager.
- `session_search` + memory for bootstrap on resume.
- `computer_use` or terminal for verifying live artifacts in gates (e.g. run-tests, sha).

## Common Pitfalls

1. **Admitting before gate** — Child result written to state without reviewer JSON + hash match. Fix: enforce "gate before write" rule in every prompt + script; Manager only acts on full gate record.
2. **Shared state without locks** — Concurrent delegates overwrite. Fix: per-wave subdirs, atomic writes (tmp+rename), hash-on-read for conflict detect, .lock files.
3. **No delta recovery** — Restart re-does all work. Fix: always store per-subtask status + artifact shas in CHECKPOINT; re-plan only !done.
4. **Metrics only at end** — Lose per-delegate data on crash. Fix: append JSONL immediately on gate return; fsync or use durable log.
5. **Over-parallel without bounds** — Token/cost explosion or rate limits. Fix: ct caps + delegation.max_concurrent_children awareness + queue in Planner.
6. **Reviewer not independent** — Same context/model as Engineer → collusion. Fix: separate delegate call with minimal context + strict "independent_reviewer" source + different model if possible.
7. **Ignoring cache/prompt invalidation** — State changes mid-delegation. Fix: pass immutable snapshot hashes; use checkpoints; warn on any mid-run edit.
8. **Forgetting oppositional** — Gates only tested happy path. Fix: always run Breaker phase in campaign (inject bad JSON, simulate fail, concurrent write test).

## Verification Checklist

- [ ] CHECKPOINT.md exists with valid Kt (all 5 fields), current wave, gate log, artifact hashes.
- [ ] Every delegation result has associated gate JSON on disk before any durable side-effect.
- [ ] Metrics JSONL has entries for all delegates in wave; recovery counts accurate.
- [ ] Resume test: interrupt (kill, token sim), reload CHECKPOINT, only incomplete re-run, gates re-pass.
- [ ] Concurrent safety: 3+ parallel delegates writing same scope → no corruption (hashes match, locks respected).
- [ ] Oppositional: at least 2 break attempts (invalid verdict JSON, cache drift, network) logged with scars + recovery success.
- [ ] Efficiency delta recorded (mature wave tokens/time < startup by >15%).
- [ ] All artifacts have sha256; gate evidence references them.
- [ ] Skill frontmatter valid; description trigger-focused <57 chars visible.
- [ ] End-to-end in test campaign: plan → parallel delegates → gates → checkpoint → metrics → recovery → mirror.

## Oppositional Assessment & Hardening (Built-in)

This skill **requires** running breaker tests (see agentic-test-campaigns design):
- Inject malformed gate JSON → must block + log + not admit.
- Force concurrent writes to CHECKPOINT/memory → detect via hash, serialize or abort wave.
- Simulate token limit mid-delegate → split + resume from partial artifact.
- Network drop on delegate return → treat as continue, re-fetch or use cached partial + re-gate.
- Cache invalid (edit source mid) → re-verify on gate, record delta.
- Drift (CHECKPOINT hash != live) → full state sync + Breaker review.

Results of hardening go into scars/ + updated ct.

## One-Shot + Full Campaign

See `agentic-test-campaigns` references for contract examples. Use this skill as the "orchestrator layer" for any long-horizon.

**Minimal gated delegation test:**
- Create CHECKPOINT with 2 sub tasks.
- Delegate both (background).
- Collect, run 2 reviewer gates (JSON only).
- Only on both "done": update CHECKPOINT + metrics + vault mirror.
- Kill session mid, restart, verify resume only re-did incomplete.

This skill makes delegation **durable, gated, measurable, and recoverable** — turning maelstrom chaos into navigated fleet operations.
