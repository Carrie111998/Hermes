# Hermes graphs on LangGraph — Phases B + C of ADR-0020

## Status (as of 2026-04-24)

- **Phase B (JobFlow)**: all 4 migration stages scaffolded and validated.
  Calibration window started.
- **Phase C (Critic)**: graph subgraph shipped. Daily run at 07:30 ET consumes
  Hermes-Matcher-Diff outputs + the Langfuse evaluation dataset, runs LLM-driven
  drift detection + proposal generation, classifies vs `allowed_knobs.json`,
  emits propose-only items to `mailbox/main/inbox` + `whatsapp_queue.jsonl`.

- Graph module: `agent-src/graphs/` (`jobflow.py` + `_profile.py` + `_prompts.py`)
- CLI runner: `~/.hermes/bin/jobflow_run.py`
- Backend: LangGraph 1.1.9 + LangChain 1.2 + OpenAI (gpt-4o-mini default)
- Observability: OTel spans → Langfuse. LLM call auto-promoted to GENERATION
  observation (prompt + structured completion visible in UI).

Validated on real LinkedIn JDs from the `hermes-jobs-v1` dataset:

| Job | Score | Decision | Notable call-out |
|---|---|---|---|
| JPMorganChase Exec Dir Agentic AI (Wilmington, DE) | 5.75 | REVIEW | Correctly applied non-remote penalty |
| GitLab VP Enterprise Transformation (Remote US) | 5.75 | REVIEW | Correctly flagged `industry_fit=2` (SaaS, not finance) |

Both runs latency ~3–5s end-to-end; full trace trees in Langfuse
(`http://localhost:3050/project/hermes-meta/traces`).

---

## Architecture (current)

```
          START
            │
            ▼
   ┌─────────────────┐
   │  load_profile   │   reads cv-handler/workspace/kb/master-resume.md
   │  (deterministic)│
   └────────┬────────┘
            │ state.profile_summary
            ▼
   ┌─────────────────┐
   │  match_score    │   ChatOpenAI(gpt-4o-mini, temperature=0.1)
   │  (LLM call)     │   .with_structured_output(MatcherScore)
   └────────┬────────┘
            │ state.{score, recommendation, breakdown, penalties, strengths, gaps, rationale}
            ▼
   ┌─────────────────┐
   │ route_decision  │   score >= 8.75 -> tailor
   │ (deterministic) │   score >= 5.0  -> review
   └────────┬────────┘   else          -> archive
            │
            ▼
           END
```

The `MatcherScore` Pydantic schema mirrors the existing Matcher agent's
SCORE_RESULT mailbox contract exactly, so the graph is a drop-in replacement
when we cut over.

---

## Migration path (shadow → replace)

### Stage 0 — Shadow mode (current; iterate for 1 week)

- Graph invoked manually via `jobflow_run.py`. Existing mailbox-based
  Matcher agent (cron `0 */2 * * *`) keeps running unchanged.
- Every graph run lands in Langfuse. Diego annotates `expected_output` on
  the `hermes-jobs-v1` dataset items over time.
- Critic (Phase C) replays the dataset through both paths, diffs scores,
  flags calibration gaps.

**Exit criterion:** graph scores agree with mailbox-Matcher scores within
±1.0 on 8 of 10 dataset items AND Diego approves recommendations at ≥80%.

### Stage 1 — Shadow-via-cron (1-2 weeks)

- New cron job `jobflow-matcher-shadow` runs every 2h (same cadence as
  `jobflow-matcher`), pulls the same Matcher mailbox inputs, invokes the
  graph on each, writes SCORE_RESULT to a PARALLEL mailbox
  `mailbox/matcher-shadow/outbox/`.
- `mailbox_translator.py` gains a `shadow_mode=true` flag on emitted
  JOB_SCORED events so downstream can filter.
- Critic runs a daily diff job: for every job scored by both, log delta.
  Critic proposes recalibrations of the system prompt weights.

**Exit criterion:** shadow outputs stable for 7 days; zero production
breakage from graph stream.

### Stage 2 — Single-source-of-truth cutover (1 day)

- Delete `jobflow-matcher` cron entry.
- Rename `jobflow-matcher-shadow` -> `jobflow-matcher`.
- Graph path becomes the sole Matcher. Mailbox-based Matcher profile
  retires; SOUL.md preserved for archaeology but no cron fires it.

**Rollback plan:** `hermes cron create` re-creates the original
`jobflow-matcher` entry from git. Single-command undo.

### Stage 3 — Extend graph to Tailor / Applier / Tracker (iterative)

Planned edges (post-Matcher cutover):

```
load_profile -> match_score -> route_decision ┬─> tailor_node   (if PROCEED)
                                              ├─> review_queue  (if REVIEW)
                                              └─> archive_queue (if ARCHIVE)
tailor_node -> dry_run_node -> approval_hitl -> apply_node -> tracker_update
```

Each addition is a separate Stage 0/1/2 migration. Human-in-the-loop
(HITL) at `approval_hitl` uses LangGraph's interrupt mechanism to pause
the graph pending WhatsApp approval (new event type).

**Sentinel VIP-lane** bypasses route_decision: Sentinel-sourced jobs skip
straight to `tailor_node` (as today in the mailbox path).

---

## Stage-3 iter2 — what landed (2026-04-24)

**Real LangGraph HITL.** `approval_hitl_node` now calls `langgraph.types.interrupt(...)`
when `HERMES_JOBFLOW_AUTO_APPROVE` is unset. `invoke_full()` returns with
`__interrupt__` in state; `bin/jobflow_run.py` surfaces the resume command:

```bash
python ~/.hermes/bin/jobflow_approve.py --thread-id <tid> --approved [--reason ...]
python ~/.hermes/bin/jobflow_approve.py --thread-id <tid> --rejected --reason '...'
python ~/.hermes/bin/jobflow_approve.py --list   # show checkpointed thread_ids
```

State is restored from the `SqliteSaver` at `~/.hermes/graphs/checkpoints.db`
keyed by thread_id; `resume_full(thread_id, payload)` feeds `Command(resume=payload)`
back into the interrupted node and the graph runs to completion.

**Real event-bus emission from graph nodes.** Three new `EventType` entries:

| event_type | priority | Telegram topic | WhatsApp tier |
|---|---|---|---|
| `approval_request` | HIGH | `tailor_applier` | URGENT |
| `apply_packet` | NORMAL | `tailor_applier` | IMPORTANT |
| `critic_proposal` | NORMAL | `system` | IMPORTANT |

Plus `stage_transition` (already defined) is now emitted from `tracker_update_node`.

**Apply packet to mailbox.** When approved, `apply_node` writes a complete
`APPLY_PACKET` to `mailbox/main/inbox/` with the cover paragraph, resume hints,
primary angle, and ATS apply URL. The graph still NEVER auto-submits — Diego
clicks the final button. A future operator-driven script can consume
`APPLY_PACKET` events to drive browser-harness submission.

**Tracker → DevFlow Postgres.** `tracker_update_node` emits a
`STAGE_TRANSITION` event; the existing `hermes_to_devflow.py` bridge picks
it up on its next 15-min cron and projects to `workflow_runs` for the
Mission Control dashboard.

### What's intentionally NOT in iter2

- **No browser-harness automation of the final submit click.** Still
  deliberately manual. iter3 may add an operator script that consumes
  `APPLY_PACKET` events and pre-navigates the apply URL.
- **No CV-Handler RPC.** `_profile.py` still reads the master resume statically.

---

## Running the graph

### Stage-1 (Matcher-only, default)

```bash
# On a dataset item from Langfuse
python ~/.hermes/bin/jobflow_run.py --langfuse-item linkedin-4388593427 --pretty

# On a Sentinel-enriched LinkedIn job (JSON in vip-jobs/)
python ~/.hermes/bin/jobflow_run.py --vip-id 4388593427

# On a Scout-discovered job
python ~/.hermes/bin/jobflow_run.py --scout-id 001a7379-afb8-4f4e-aaa3-e387b953ffc6

# Arbitrary Scout-shaped JSON file
python ~/.hermes/bin/jobflow_run.py --file /path/to/job.json
```

### Stage-3 (full graph: Matcher + Tailor + HITL + Apply + Tracker)

```bash
# Headless test (auto-approve, PROCEED_THRESHOLD override for calibration)
HERMES_JOBFLOW_PROCEED_THRESHOLD=7.5 \
  python ~/.hermes/bin/jobflow_run.py \
    --file ~/.hermes/profiles/matcher-shadow/workspace/smoke-test-job.json \
    --full --auto-approve --thread-id stage3-e2e-1

# Real run (approval gated, Tailor->approval_hitl awaits Diego's reply)
python ~/.hermes/bin/jobflow_run.py --vip-id 4388593427 --full
```

All invocations write structured JSON state to stdout and emit spans to
Langfuse. `--pretty` makes stdout human-readable for eyeball review.

### Stage-1 -> Stage-1.5 shadow cron (Windows Task Scheduler)

Both tasks are registered under `Hermes-Matcher-*`:

```powershell
# Every 2h: run shadow Matcher on last 3h of Scout-discovered jobs
Get-ScheduledTask -TaskName 'Hermes-Matcher-Shadow'
# Daily 07:00: compute prod-vs-shadow diff report
Get-ScheduledTask -TaskName 'Hermes-Matcher-Diff'
```

Underlying scripts: `bin/matcher_shadow_run.py` + `bin/matcher_diff.py`.
Reports land at `~/.hermes/profiles/matcher-shadow/workspace/diff-reports/<date>.md`.

### Stage-2 cutover / rollback

```bash
# Dry-run (default): shows what would happen
python ~/.hermes/bin/phase_b_cutover.py
python ~/.hermes/bin/phase_b_rollback.py

# Execute
python ~/.hermes/bin/phase_b_cutover.py --confirm     # graph becomes sole Matcher
python ~/.hermes/bin/phase_b_rollback.py --confirm    # restore mailbox Matcher cron
```

Cutover journal at `~/.hermes/infra/phase-b/cutover-journal.jsonl`.
Original cron definition preserved at `jobflow-matcher.backup.json`.

### Audit logs (Stage-3)

| path | content |
|---|---|
| `~/.hermes/graphs/approval-log.jsonl` | every approval request + resolution |
| `~/.hermes/graphs/apply-log.jsonl` | every dry-run receipt (what would be submitted) |
| `~/.hermes/graphs/tracker-log.jsonl` | every pipeline stage transition |
| `~/.hermes/graphs/checkpoints.db` | SqliteSaver state for resumable runs |

---

## Phase C — Critic LangGraph

Lives at `graphs/critic.py` + `graphs/_critic_prompts.py`. CLI: `bin/critic_run.py`.

### Pipeline

```
START
  ↓
load_calibration       reads diff-reports/*.json + Langfuse hermes-jobs-v1
  ↓
detect_drift           LLM: identifies systematic patterns (DriftClusterList)
  ↓
generate_proposals     LLM: ProposalList tied to clusters + allowed_knobs context
  ↓
classify_proposals     deterministic: auto_apply vs propose_only per allowed_knobs.json
  ↓
auto_apply             v1: only narrow knob-mapped + risk!=high; rest -> propose_only
  ↓
emit_proposals         writes CRITIC_PROPOSAL to mailbox/main/inbox + whatsapp_queue.jsonl
  ↓
finalize               appends to changelog.jsonl + writes retro markdown
  ↓
END
```

### Relationship to existing `critic_retro.py`

They are **complementary**, not duplicative:

| feature | `profiles/critic/workspace/critic_retro.py` | `graphs/critic.py` |
|---|---|---|
| Cadence | Weekly Sunday 20:00 ET | Daily 07:30 ET |
| Data source | `audit.jsonl` + cron metadata + agent MEMORY.md | diff-reports + Langfuse dataset |
| Reasoning | Hardcoded Reflexion rules | LLM-driven drift detection |
| Scope | Failure clusters + dormancies (system health) | Matcher scoring drift (calibration) |
| Output | `retros/<date>_weekly.md` | `retros/<date>_critic-graph_<id>.md` + mailbox proposals |

Both write to the same `workspace/changelog.jsonl` and respect the same
`allowed_knobs.json`. Eventually we'll merge them into a single Critic that
multiplexes by trigger; for now keep them separate.

## Phase C iter2 — what landed (2026-04-24)

**`reflexion_replay_node` between classify and auto_apply.** For every
`matcher.threshold_adjust` proposal, deterministically replay against the
paired-jobs set: parse the new threshold, re-decide each job, count
recommendation flips, compute new agreement vs production. Detect
intra-batch contradictions (one proposal lowers the threshold, another
raises it) and flag both with `status="contradiction_detected"` so Diego
sees the conflict in the retro before approving anything. Non-threshold
proposals get `status="kind_not_replayable"` (LLM-driven re-scoring is iter3).

**Real `skill.success_ranking` executor.** `_execute_skill_ranking()`
parses JSON or text-form `specific_change`, opens
`~/.hermes/skills/<skill>/metadata.json`, bumps success/fail counters,
recomputes confidence, writes a structured JSON reversal at
`workspace/reversals/<ts>_<proposal_id>.json` with the prior values.
Verified end-to-end: bump `agent-config` success +2 → confidence 1.0;
revert -2 → counters 0/0.

**`CRITIC_PROPOSAL` event-type emission.** `emit_proposals_node` now also
emits to the event bus (alongside the mailbox + WhatsApp queue writes).
TOPIC_ROUTING routes to `system` topic; whatsapp_escalator queues at
IMPORTANT tier. Replay evidence travels in the event payload so the
notifier can show flip counts + contradiction flags inline.

### Phase C iter5 — contradiction resolver landed (2026-04-25)

The graph gained a `resolve_contradictions_node` between `reflexion_replay`
and `auto_apply`. When replay flags ≥2 proposals as `contradiction_detected`
(typical: one threshold proposal lowers the env var, another raises it), the
resolver:

1. Bundles the contradicting proposals + cluster context into a `CRITIC_RESOLVER_*`
   prompt
2. Calls `codex_structured_invoke(ProposalList, ...)` once
3. Replaces the contradicting proposals with the LLM's 0-2 unified replacements
4. Tags each replacement with `_resolved_from: [<original_pids>]` for audit

The resolver's system prompt prefers: switch to `matcher.prompt_edit` (rubric
refinement, non-directional) → switch to `matcher.dimension_weight` (re-weight
the drifting dimension) → return EMPTY (no proposal beats a bad proposal).

**Validated** on a synthetic threshold-up + threshold-down pair: resolver
correctly produced ONE `matcher.prompt_edit` proposal that refines the
skills_overlap rubric instead of moving the threshold either way.

### Phase C iter5 — what's still NOT in scope

- **Real `agent.reasoning_effort` and `cron.cadence_within_50pct` executors.**
  Still propose-only with placeholder reversals; iter6 implements per-knob
  apply paths (config.yaml mutation, jobs.json patching).
- **No replay re-run on resolver output.** Single-pass design — if the
  resolver's output itself contradicts, Diego sees it in the retro and
  decides. Adding a LangGraph cycle for multi-pass replay is iter6.

---

## OAuth + gpt-5.5 model contract (2026-04-24)

**Diego mandate:** every LLM call across the Hermes platform goes through
the `openai-codex` OAuth path with model `gpt-5.5`. No `OPENAI_API_KEY`
side paths, no OpenRouter, no Anthropic primary paths.

### Where this applies

| layer | model source | LLM client |
|---|---|---|
| Hermes profile agents (main, scout, matcher, tailor, applier, tracker, sentinel, notifier, cv-handler, devflow) | `model: gpt-5.5` in each `profiles/*/config.yaml` + root `~/.hermes/config.yaml` | Hermes runtime (`openai-codex` provider) via `~/.hermes/auth.json` |
| Auxiliary tools (vision, web_extract, compression, etc.) | `_CODEX_AUX_MODEL = "gpt-5.5"` in `agent/auxiliary_client.py` | `CodexAuxiliaryClient` wrapping `OpenAI()` against `chatgpt.com/backend-api/codex` |
| LangGraph (this dir: `jobflow.py`, `critic.py`) | `DEFAULT_MODEL = "gpt-5.5"` (env: `HERMES_JOBFLOW_MODEL` / `HERMES_CRITIC_MODEL`) | `obs/oauth_llm.py::codex_structured_invoke()` direct Responses-API call |

### Why a custom helper for LangGraph (not langchain's `with_structured_output`)

The Codex endpoint at `chatgpt.com/backend-api/codex` exposes the **Responses
API** in a non-standard shape that langchain doesn't generate:
- Top-level `instructions` (string, required) — langchain sends `messages` instead
- `input` is `[{role, content}]` (no system role inside it)
- `temperature`, `max_output_tokens` rejected (400 if present)
- `store: False` required for chatgpt.com privacy

`obs/oauth_llm.py::codex_structured_invoke(schema, instructions, user, ...)`
constructs this shape directly via the `openai` SDK (`client.responses.stream(...)`),
collects streamed text deltas, and parses to a Pydantic schema. Mirrors
`agent/auxiliary_client.py::CodexAuxiliaryClient` body, trimmed for
structured-JSON-only callers.

### OAuth token resolution

`obs/oauth_llm.py::_get_oauth_token()` walks the same priority chain as
`agent/auxiliary_client.py::_read_codex_access_token()`:

1. `~/.codex/auth.json` (Codex CLI shared file — freshest after `hermes auth add`)
2. `~/.hermes/auth.json::credential_pool.openai-codex[*]` active entry
3. `resolve_codex_runtime_credentials()` (top-level providers + auto-refresh)
4. `_read_codex_tokens()` raw read (last resort, may be stale)

Tokens are JWT-validated — expired tokens are rejected at every step.
Cached for 600s in-process; auto-refreshed beyond that.

### Rotation runbook (when refresh chain breaks)

```bash
# 1. Fresh OAuth login (browser device-code flow)
hermes auth add openai-codex --type oauth

# 2. Sync fresh tokens from ~/.codex/auth.json into ~/.hermes/auth.json
#    (without this, the gateway still uses stale tokens even though my
#     graphs work, because `hermes auth add` doesn't write top-level providers)
python ~/.hermes/bin/sync_codex_oauth_to_hermes.py

# 3. Restart the gateway so subscribers re-resolve creds
taskkill //F //PID <gateway_pid>
PYTHONIOENCODING=utf-8 nohup hermes gateway run > ~/.hermes/gateway.log 2>&1 &

# 4. Smoke test
python ~/.hermes/bin/jobflow_run.py --langfuse-item linkedin-4392439748
```

### Running

```bash
# Manual
python ~/.hermes/bin/critic_run.py --window 7

# Specific dataset
python ~/.hermes/bin/critic_run.py --window 14 --dataset hermes-jobs-v1
```

### Schedule

| task | cadence | command |
|---|---|---|
| `Hermes-Critic-Daily` | daily 07:30 ET | `python bin/critic_run.py --window 7` |
| `Hermes-Critic-Weekly` (legacy) | Sunday 20:00 ET | `python profiles/critic/workspace/critic_retro.py` |

### Audit + outputs

| path | content |
|---|---|
| `~/.hermes/profiles/critic/workspace/changelog.jsonl` | one entry per Critic run |
| `~/.hermes/profiles/critic/workspace/whatsapp_queue.jsonl` | propose-only items awaiting Diego review |
| `~/.hermes/profiles/critic/workspace/retros/*.md` | per-run human-readable retro |
| `~/.hermes/profiles/critic/workspace/reversals/*.txt` | reversal scripts (placeholders in v1) |
| `~/.hermes/mailbox/main/inbox/*_CRITIC_PROPOSAL_critic.json` | proposals routed to Jaum |

## Changing the LLM backend

Set `HERMES_JOBFLOW_MODEL` in `~/.hermes/.env`. Default is `gpt-4o-mini`.
For higher quality (and cost), try `gpt-4o`. Anthropic support via
`langchain-anthropic` is a one-file swap in `jobflow.py` once we set
`ANTHROPIC_API_KEY`.
