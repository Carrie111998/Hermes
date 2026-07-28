# AUTO_REDISPATCH_LOCATION_AUDIT

**Scope:** Read-only forensic location audit of worker-task automatic failure-handling / retry / re-dispatch.  
**Date:** 2026-07-23  
**Profile under study:** `<PROFILE>` (live gateway: `pythonw -m hermes_cli.main --profile <PROFILE> gateway run`)  
**Store:** `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\workers\bridge.db`  
**Constraint honored:** no code, config, service, or database modifications.

---

## Executive summary

The intended pipeline was **never** “on failure, silently insert a successor task row.” It was a multi-layer system:

1. **In-task verification repair** (same `task_id`, budget-limited) inside the worker-bridge orchestrator.
2. **Gateway push daemon** (`gateway/worker_bridge_watchers.py`) that alerted the orchestrator agent on terminal status and also auto-started *already-created* pending tasks.
3. **`worker-alert-gate` plugin** that forced the *active* agent to triage failures before new dispatches; re-dispatch of a *new* repair task was an **agent action**, not a core automatic create.

What remains live today:

| Layer | Status |
|-------|--------|
| Orchestrator `verification.auto_repair` (same-task follow-up) | **Present and active** in plugin package |
| Thin `created` → start dispatcher | **Present and active** in running gateway |
| Gateway terminal-alert + rich auto-dispatch + ultra mixin | **Source deleted; not importable; not wired** |
| `worker-alert-gate` triage / ack / dispatch-block | **Present** under <PROFILE> plugins |
| Automatic *create* of successor task on failure | **Does not exist as code** (by design; agent must create) |

**Conclusion for `task-0b6b1e99a188`:** it exhausted same-task auto-repair, terminal-failed on independent verification (`cmd.exe` not found), and no successor was auto-created because **no component creates successors**. The push-wake path that would have prompted the agent immediately is **dead**. The alert gate eventually surfaced the failure when the agent was already running; an operator/agent then ACKed and later created `task-0041c22521d2` manually.

---

## 1. Exact current source path(s)

### 1.1 Present and loadable (active behavior)

| Path | Role |
|------|------|
| `C:\Users\<USER>\AppData\Local\hermes\hermes-agent\gateway\worker_task_dispatcher.py` | Thin gateway mixin: polls and starts tasks in `created` via `dispatch_pending`. Wired into `GatewayRunner` (import ~1929; MRO ~3064; watcher spawn ~8122–8127). mtime **2026-07-22 15:57**. |
| `C:\Users\<USER>\AppData\Local\hermes\hermes-agent\gateway\run.py` | Only worker-bridge background loop remaining: `_worker_task_dispatcher_watcher`. **No** references to `worker_bridge_watchers` / `WorkerBridgeWatch` / `worker_bridge_ultra`. |
| `C:\Users\<USER>\AppData\Local\hermes\plugins\worker-bridge\hermes_worker_bridge\dispatch.py` | `dispatch_pending()` + detached runner spawn. Used by the thin dispatcher. mtime **2026-07-22 15:56**. |
| `C:\Users\<USER>\AppData\Local\hermes\plugins\worker-bridge\hermes_worker_bridge\orchestrator.py` | **Same-task** verification auto-repair (`_auto_repair_budget`, `verification.auto_repair` event, `continue_task`). Config: `worker_bridge.verification_auto_repair` (<PROFILE> config = `1`). |
| `C:\Users\<USER>\AppData\Local\hermes\plugins\worker-bridge\hermes_worker_bridge\workflows.py` | Task-type defaults for `metadata.auto_repair`. |
| `C:\Users\<USER>\AppData\Local\hermes\plugins\worker-bridge\hermes_worker_bridge\cli.py` / `runner.py` | Pass `verification_auto_repair` into bridge construction. |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\plugins\worker-alert-gate\` | `alert_core.py`, `plugin.py`, tests. Syncs `bridge.db` → `workers/alert_queue.json`; injects pending alerts on `pre_llm_call`; **blocks** new dispatch until failure ACKs; does **not** enqueue successors. Explicitly documents that an earlier **auto-re-dispatch cron** was rejected for lacking judgment. |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\scripts\worker_task_alerts.py` | Cron/daemon human-facing Discord text alerts (stdout). Independent cursor: `cron/worker_task_alerts_state.json` (**stale** at `last_event_id=26501`). |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\skills\devops\worker-alert-system\SKILL.md` | Operator doc for the **intended** full pipeline; still points at `gateway/worker_bridge_watchers.py` as the push daemon. |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\docs\worker-task-alerts.md` | Design for cron-side `worker_task_alerts.py` (notify, not auto-create). |
| `C:\Users\<USER>\AppData\Local\hermes\hermes-agent\tests\gateway\test_worker_task_dispatcher.py` | Tests for thin dispatcher only. |

### 1.2 Intended gateway alert / auto-dispatch / ultra — source **missing**, bytecode **orphaned**

| Path | Evidence |
|------|----------|
| `C:\Users\<USER>\AppData\Local\hermes\hermes-agent\gateway\worker_bridge_watchers.py` | **MISSING.** `import gateway.worker_bridge_watchers` → `ModuleNotFoundError`. |
| `C:\Users\<USER>\AppData\Local\hermes\hermes-agent\gateway\worker_bridge_ultra.py` | **MISSING.** Same import failure. |
| `...\gateway\__pycache__\worker_bridge_watchers.cpython-311.pyc` | Present (68 582 B, mtime **2026-07-14 17:03**). Filename still points at deleted `.py`. |
| `...\gateway\__pycache__\worker_bridge_watchers.cpython-314.pyc` | Present (68 621 B, mtime **2026-07-13 17:41**). |
| `...\gateway\__pycache__\worker_bridge_ultra.cpython-311.pyc` | Present (48 121 B, mtime **2026-07-14 17:03**). |
| `...\gateway\__pycache__\worker_bridge_ultra.cpython-314.pyc` | Present (48 438 B, mtime **2026-07-13 20:00**). |

Bytecode-recovered API surface of `worker_bridge_watchers` (still the best in-place reconstruction of the deleted source):

- Mixin: `GatewayWorkerBridgeWatchersMixin`
- Methods: `_worker_bridge_notifier_watcher`, `_worker_bridge_tick`, `_worker_bridge_auto_dispatch`, `_worker_bridge_idle_nudge`, `_resolve_worker_alert_target`, …
- Helpers: `collect_new_transitions`, `select_dispatchable_tasks`, `claim_task_for_dispatch`, `format_alert_text`, cursor `gateway_alerts_cursor.json`
- Config keys: `worker_bridge.gateway_alerts` (+ `auto_dispatch`, env `HERMES_WORKER_BRIDGE_ALERTS` / `HERMES_WORKER_BRIDGE_AUTODISPATCH`)
- Depends on deleted `gateway.worker_bridge_ultra` (`GatewayWorkerBridgeUltraMixin`, `resolve_ultra_settings`)

**Alert text contract (from pyc strings):** injected synthetic turn tells the agent a task hit a terminal state; pending-work section tells it to triage failures then `hermes worker tasks start <task_id>`. It does **not** call `create_task` itself.

### 1.3 Supporting docs / reviews (not runtime)

| Path |
|------|
| `C:\Users\<USER>\AppData\Local\hermes\CODE_REVIEW_worker_bridge_watchers.md` |
| `C:\Users\<USER>\AppData\Local\hermes\worker_bridge_watchers_code_review.md` |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\evals\watchers_review.md` |
| `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\evals\bridge-issues-handoff.md` |

### 1.4 Git evidence (active tree)

- Commit `6cd47d16e` (**2026-07-23 07:05 -0500**): `feat(gateway): worker task auto-dispatch + cron pre-flight resilience (post-rebuild protection)` added **only** `gateway/worker_task_dispatcher.py` (+ wiring/tests). It is a **partial post-rebuild substitute**, not a restore of the full watchers/ultra modules.
- `git log -- gateway/worker_bridge_watchers.py` yields no retained history of that path in the current repo object set beyond the thin-dispatcher era (watchers never reappeared as tracked source after rebuild).

### 1.5 Runtime config still enabling the *old* path

`C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\config.yaml` still has (duplicated key block):

```yaml
worker_bridge:
  verification_auto_repair: 1
  gateway_alerts:
    enabled: true
    interval_seconds: 15
    statuses: [succeeded, failed, timed_out]
    platform: discord
    chat_id: '1516173910670839953'
```

Config is **orphaned**: nothing in the running gateway process loads `gateway_alerts` anymore.

Cursor left behind by the old daemon:

`C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\workers\gateway_alerts_cursor.json`

```json
{
  "last_event_id": 64706,
  "updated_at": 1784217431.4180303,
  "last_nudge": 1783912788.063282,
  "last_auto_dispatch": 1784217431.4175076
}
```

Live `events` table max `event_id` ≈ **198966**. Cursor is frozen far behind; only meaningful if the old watcher is restored (must re-baseline or it would either skip or flood depending on restore strategy).

---

## 2. Exact Windows.old path(s)

Root present: `C:\Windows.old\Users\<USER>\AppData\Local\hermes\`

| Finding | Path / result |
|---------|----------------|
| Full `hermes-agent\gateway\` tree | **Absent** (`Test-Path ...\Windows.old\...\hermes-agent\gateway` → **False**). Windows.old hermes-agent is a partial tree (has `hermes_cli`, `agent`, `apps`, …) without gateway sources. |
| `worker_bridge_watchers.py` / `worker_bridge_ultra.py` | **Not found** under Windows.old hermes (recursive search). |
| Worker-bridge plugin packages (orchestrator-era copies in worktrees) | `C:\Windows.old\Users\<USER>\AppData\Local\hermes\.claude\worktrees\confident-hawking-11b1d5\plugins\worker-bridge\` |
| | `C:\Windows.old\Users\<USER>\AppData\Local\hermes\.claude\worktrees\dazzling-vaughan-ca6c0d\plugins\worker-bridge\` |
| | `C:\Windows.old\Users\<USER>\AppData\Local\hermes\.claude\worktrees\pensive-shannon-96f4bf\plugins\worker-bridge\` |
| | `C:\Windows.old\Users\<USER>\AppData\Local\hermes\plugins\worker-bridge\` |
| <PROFILE> worker-alert-gate in a worktree | Under `...\dazzling-vaughan-ca6c0d\profiles\<PROFILE>\plugins\worker-alert*` (path truncated by explorer; package family present). |
| Active install archive of stray bridge | `C:\Users\<USER>\AppData\Local\hermes\_archive\stray-hermes_worker_bridge-2026-07-13\` (partial; not the gateway watchers). |

**Windows.old does not hold a recoverable copy of `worker_bridge_watchers.py` / `worker_bridge_ultra.py`.** Best archaeological copies of the deleted gateway modules on this machine are the **current** `__pycache__\*.pyc` files listed in §1.2.

---

## 3. Does the currently running gateway have the full auto-failure → re-dispatch behavior?

### Process evidence (read-only)

- Live: `pythonw.exe -m hermes_cli.main --profile <PROFILE> gateway run` (PID observed during audit).
- Also: detached runners `hermes_worker_bridge.runner` for in-flight tasks (e.g. this audit task).

### Log evidence (`profiles\<PROFILE>\logs\gateway.log`)

| Pattern | Last seen |
|---------|-----------|
| `worker-bridge alerts: watching ...` / `injecting N transition(s)` | **2026-07-15** (and earlier on 2026-07-13/14). **No lines after rebuild.** |
| `worker task auto-dispatched N task(s): ...` (thin dispatcher) | Continuously **2026-07-22 → 2026-07-23**, including `task-0b6b1e99a188` at `2026-07-23 11:06:07`. |

### Import / wiring evidence

- `gateway.worker_bridge_watchers` / `gateway.worker_bridge_ultra`: **not importable** (no `.py`; orphaned pyc not on import path without source).
- `GatewayRunner` MRO: `GatewayWorkerTaskDispatcherMixin` only for worker-bridge (plus kanban/authz/slash).
- Therefore the running process **cannot** execute the old notifier/auto-dispatch/ultra loops even if config enables them.

### Behavior matrix (running gateway)

| Capability | Live? |
|------------|-------|
| Auto-start tasks already in `created` | **Yes** (`worker_task_dispatcher` + `dispatch_pending`) |
| Alert orchestrator on `failed`/`succeeded`/`timed_out` via synthetic Discord turn | **No** (watchers source gone) |
| Auto-dispatch `queued`/`paused` with claim guards, capacity, idle nudge | **No** (was in watchers; thin path only handles `created`) |
| Same-task verification auto-repair | **Yes** (orchestrator in runner process, not gateway) |
| Auto-create successor / repair **new** `task_id` on failure | **No** (never core; agent + alert gate only) |
| Force triage of failures when agent already in a turn | **Yes** (`worker-alert-gate` plugin) |

**Verdict:** The currently running gateway has **partial** auto-dispatch (`created` only) and **does not** have the intended failure-alert / rich re-dispatch / ultra-on-success gateway behavior.

---

## 4. Why `task-0b6b1e99a188` did not automatically create a successor

### 4.1 What the task actually did (bridge.db, RO)

| Field | Value |
|-------|--------|
| `task_id` | `task-0b6b1e99a188` |
| worker | `codex` |
| status | `failed` |
| created | 2026-07-23T16:06:07Z |
| final fail | 2026-07-23T16:11:59Z |
| error | `independent verification failed` |
| `metadata.auto_repair` | `1` |
| `runtime.auto_repair_attempts` | `1` (budget exhausted) |
| `follow_up_turns` | `1` |
| `spec.parent_task_id` | `null` |
| Artifacts | `...\workers\artifacts\task-0b6b1e99a188\` |

Key events:

1. `task.auto_dispatched` — thin gateway dispatcher started it (also logged).
2. First verify `ok=false` → `verification.auto_repair` attempt **1/1** → follow-up turn with message that `cmd.exe` is not recognized.
3. Second verify still `ok=false` → terminal `failed`. **No further auto_repair** (budget 1).
4. **No** `task.created` child with `parent_task_id=task-0b6b1e99a188`.

### 4.2 Why no automatic successor row

1. **By design, no core path creates a new task on failure.**  
   - Orchestrator only continues the **same** task under repair budget.  
   - Watchers (when alive) inject instructions; agent must `create` + `start`.  
   - `worker-alert-gate` explicitly **replaced** “auto-re-dispatch cron” so judgment (ack + optional agent re-create) is required.  
   - Thin `dispatch_pending` only starts existing `created` rows.

2. **Same-task repair already ran and still failed.**  
   Root cause was environment (`'cmd.exe' is not recognized` in the bridge verifier), not a missing re-queue of the same job. Budget was 1; second failure was terminal for that task.

3. **Gateway push wake was dead**, so the agent was **not** immediately woken by `worker-bridge alerts: injecting...` when the task failed (~16:12Z). Last such log line was **2026-07-15**.

4. **Alert gate only acts when the agent is already turning.**  
   Queue record shows:
   - `first_seen` ≈ 2026-07-23T17:28:56Z (**~77 minutes** after failure)
   - `acked_at` ≈ 17:29:34Z, action `fixed`, note that re-dispatch needs a verifier command that does not depend on `cmd.exe`
   - Audit: `logs/worker-alert-gate.jsonl` same ACK

5. **Successor that did appear was manual / agent-driven, not automatic:**
   - `task-0041c22521d2` created **2026-07-23T17:31:21Z** (after ACK), same objective family, `parent_task_id` still `null`, status later **succeeded**.
   - This is re-dispatch by the orchestrator after triage, not system auto-enqueue.

6. **Stale cursors** confirm long-term drift of the push layers:
   - Gateway alerts cursor stuck at event **64706**
   - Cron worker_task_alerts cursor stuck at **26501**
   - Alert-gate cursor advanced (saw event **195603** for this failure) only when a turn ran

---

## 5. Smallest recovery action

### Immediate (this objective / fleet hygiene) — no code restore required

1. **Treat `task-0b6b1e99a188` as terminal + triaged.** Already ACKed `fixed`. Do not expect the system to spawn another child of that id.
2. **Prefer the already-created successor:** `task-0041c22521d2` (**succeeded**) for the same ULTRA anchor-gate objective. Collect/integrate if not already done.
3. **If another repair pass is needed:** create a **new** task with a verification command that does not rely on bare `cmd.exe` on PATH (e.g. absolute `C:\Windows\System32\cmd.exe` or invoke `C:\Python314\python.exe -m pytest` directly), then let the live thin auto-dispatcher start it from `created`.

### Structural (restore intended auto-alert + rich auto-dispatch) — smallest code recovery

Ordered by leverage:

1. **Recover source** of `gateway/worker_bridge_watchers.py` and `gateway/worker_bridge_ultra.py` from the orphaned cpython-311 pyc (decompile) or any external backup not on this disk; Windows.old does **not** contain them.
2. **Re-wire** `GatewayRunner` to inherit `GatewayWorkerBridgeWatchersMixin` (and ultra as designed) and re-spawn `_worker_bridge_notifier_watcher` at startup — mirror the old skill docs / pre-rebuild layout.
3. **Reconcile** with the post-rebuild thin `worker_task_dispatcher` (avoid double-dispatch of `created` tasks: either fold thin logic back into watchers or gate one of them).
4. **Re-baseline** `workers/gateway_alerts_cursor.json` (delete or set to current max event id) so restore does not skip ~130k events silently or replay a flood without intent.
5. **Restart** <PROFILE> gateway so imports load the restored modules.
6. Confirm log line: `worker-bridge alerts: watching <bridge.db> every 15s (statuses=...)`.

Optional parallel: advance/reset `cron/worker_task_alerts_state.json` if Discord cron text alerts are still desired.

**Do not** expect restoring only `worker_task_dispatcher` to recreate successors — it never did.

---

## Evidence appendix (quick index)

| Item | Location |
|------|----------|
| Live bridge DB | `C:\Users\<USER>\AppData\Local\hermes\profiles\<PROFILE>\workers\bridge.db` |
| Failed task artifacts | `...\workers\artifacts\task-0b6b1e99a188\` |
| Alert queue (acked fixed) | `...\workers\alert_queue.json` entry `task-0b6b1e99a188` |
| Alert audit | `...\logs\worker-alert-gate.jsonl` |
| Gateway log | `...\logs\gateway.log` |
| Frozen gateway alert cursor | `...\workers\gateway_alerts_cursor.json` |
| Orphaned watchers/ultra pyc | `hermes-agent\gateway\__pycache__\worker_bridge_{watchers,ultra}.cpython-3{11,14}.pyc` |
| Thin dispatcher commit | `6cd47d16e` “post-rebuild protection” |
| Design skill (intended architecture) | `profiles\<PROFILE>\skills\devops\worker-alert-system\SKILL.md` |

---

## Answers to the five required items (compact)

1. **Current source paths if present:** live logic in `gateway/worker_task_dispatcher.py`, `plugins/worker-bridge/.../orchestrator.py` (+ `dispatch.py`), `profiles/<PROFILE>/plugins/worker-alert-gate/`. Full alert/re-dispatch daemon sources **missing**; only pyc leftovers under `gateway/__pycache__/`.
2. **Windows.old paths if present:** worker-bridge plugin trees under `C:\Windows.old\Users\<USER>\AppData\Local\hermes\` (plugins + `.claude` worktrees). **No** `worker_bridge_watchers.py` / `worker_bridge_ultra.py`; **no** `hermes-agent\gateway` tree.
3. **Running gateway has full behavior?** **No.** Only thin `created` auto-start. Alert injection / rich auto-dispatch / ultra mixin not loaded.
4. **Why no auto successor for `task-0b6b1e99a188`?** Repair budget spent; verification still failed; **no code creates successors**; push watcher dead; gate only forced triage ~77m later; agent later created `task-0041c22521d2` by hand.
5. **Smallest recovery:** for the objective — use/finish `task-0041c22521d2` or create a new task with a PATH-safe verify command; for the platform — restore watchers+ultra from pyc/backup, rewire `run.py`, re-baseline cursor, restart gateway.
