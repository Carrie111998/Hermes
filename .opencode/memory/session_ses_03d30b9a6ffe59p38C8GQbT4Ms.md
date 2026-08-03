<!-- stm:v1 -->
## Session Memory

### User Instructions
- …

### Long Horizon Context
- …

### Decisions
- …

### Conclusions
- …

### Active References
- …

<existing_memory>
## Session Memory

### User Instructions
- None captured yet.

### Long Horizon Context
- None captured yet.

### Decisions
- None captured yet.

### Conclusions
- None captured yet.

### Active References
- None captured yet.

</existing_memory>

<conversation_update>
USER:
Investigate the Hermes agent cron scheduler mechanisms in /home/capeta/Code/hermes-agent/. Answer these questions with file:line citations:

1. How does the cron scheduler read jobs?
   - Look at cron/scheduler.py and cron/jobs.py
   - Does the scheduler poll jobs.json every tick, or load once into memory?
   - If loaded into memory, is there any mechanism to reload/re-read without restart?

2. Check for any "reload" or "refresh" methods in the scheduler.

3. How does the CLI `hermes cron` command interact with the scheduler? Does it tell the scheduler to reload after add/edit/remove?

4. How does the cronjob tool (tools/cronjob_tool.py or similar) interact with the scheduler?

5. Is the cron scheduler a separate process, a thread, or part of the gateway process?

For each finding, provide the exact file:line and relevant code snippets.

---

ASSISTANT:
I'll investigate the cron scheduler mechanisms in the Hermes agent codebase. Let me start by locating the relevant files.

---

ASSISTANT:
The scheduler.py is large (194K). Let me examine the structure of both key files and the tool file.

---

ASSISTANT:
Both paths resolve to the same directory (symlink). Let me read the core scheduler `tick()` function and the job-loading mechanisms.

---

ASSISTANT:
The `tick()` function calls `get_due_jobs()`. Let me find that in jobs.py and examine how jobs are loaded/read.

---

ASSISTANT:
Now let me read `load_jobs()` and `get_due_jobs()` in jobs.py:

---

ASSISTANT:
This confirms the built-in scheduler re-reads jobs.json on every tick. Now let me examine the scheduler provider abstraction and search for reload/refresh mechanisms.

---

ASSISTANT:
Now let me find where `_notify_provider_jobs_changed()` is called, the CLI cron command, and the cronjob tool.

---

ASSISTANT:
Now let me examine the CLI cron command and the gateway ticker.

---

ASSISTANT:
Let me see where the gateway actually starts the cron ticker thread in `start_gateway`:

---

ASSISTANT:
Now let me check whether the CLI cron command notifies the scheduler after mutations, and look at the cronjob tool:

---

ASSISTANT:
Let me examine the `cronjob` dispatch function in the tool and the CLI's mutation handlers:

---

ASSISTANT:
Now let me read the `cronjob()` dispatch function in the tool to see which mutations notify the provider:

---

ASSISTANT:
Let me check the REST API surfaces and the `/cron` slash command handler:

---

ASSISTANT:
Let me verify the remaining pieces — `list_jobs`, the REST mutations, and the web dashboard cron surface:

---

ASSISTANT:
Let me check the CLI's `cron_tick` verb and the jobs store definition at the top of jobs.py:

---

ASSISTANT:
Let me verify the gateway's `/cron` slash command dispatch and the `_execute_job_now` helper used by the `run` action:

---

ASSISTANT:
Let me confirm the REST mutation handlers and dashboard cron API both notify the provider:

---

ASSISTANT:
I now have a complete picture of the cron scheduler architecture. Here are my findings:

---

## 1. How the scheduler reads jobs — it POLLS `jobs.json` every tick (no in-memory cache)

The built-in scheduler has **no in-memory job list**. Every 60-second tick re-reads the entire `jobs.json` file from disk. The call chain:

**`InProcessCronScheduler.start()`** (the ticker loop) → **`cron_tick()`** → **`tick()`** → **`get_due_jobs()`** → **`load_jobs()`** → file I/O

- `cron/scheduler_provider.py:225-261` — the loop: `while not stop_event.is_set(): ... cron_tick(...); ...; stop_event.wait(interval)` (60s default)
- `cron/scheduler_provider.py:231` — calls `cron_tick(verbose=False, ...)`
- `cron/scheduler.py:4079` — `due_jobs = get_due_jobs()` inside `tick()`
- `cron/jobs.py:1946-1947` — `get_due_jobs()` acquires `_jobs_lock()` and calls `_get_due_jobs_locked()`
- `cron/jobs.py:1953` — `raw_jobs = load_jobs()` — the crucial line: it re-loads from disk on **every** scan
- `cron/jobs.py:970-1019` — `load_jobs()` opens `jobs.json` and `json.load()`s it on every call (line 983-984); no caching layer anywhere

This is confirmed explicitly in docstrings:
- `cron/scheduler_provider.py:82` — `on_jobs_changed()` docstring: *"Built-in: no-op (it re-reads jobs.json on every tick)"*
- `cron/scheduler.py:4021` — *"No-op for the built-in (it re-reads jobs.json each tick)"*
- `cron/jobs.py:3167` (scheduler.py) — "value is intentionally re-read from storage every tick"

`list_jobs()` also reads fresh every call — `cron/jobs.py:1445`: `jobs = [_normalize_job_record(j) for j in load_jobs()]`.

**Consequence:** any mutation (via CLI, model tool, REST, or even a hand-edit of `jobs.json`) is picked up by the next tick **without restart**. There is no cache to invalidate.

---

## 2. Reload/refresh methods — the provider-notification hook (`on_jobs_changed`), not a cache reload

There is **no "reload" method** on the built-in scheduler because it re-reads every tick. What exists is a provider abstraction for external schedulers (e.g. Chronos):

- `cron/scheduler_provider.py:78-83` — **`CronScheduler.on_jobs_changed()`**: called after every successful store mutation (create/update/remove/pause/resume) so an external provider can re-provision/cancel. Built-in: no-op (re-reads each tick).
- `cron/scheduler_provider.py:115-119` — **`CronScheduler.reconcile()`**: external providers converge their registry toward `jobs.json` (arm missing one-shots, cancel orphans). Built-in: no-op.
- `cron/scheduler_provider.py:91-113` — **`CronScheduler.fire_due()`**: inbound fire webhook path — claims the job via store CAS and runs it via `run_one_job`. Built-in never calls this.
- `cron/scheduler.py:4015-4030` — **`_notify_provider_jobs_changed()`**: the shared dispatch helper — `resolve_cron_scheduler().on_jobs_changed()` — best-effort, never raises.

The ABC contract note at `cron/scheduler_provider.py:74-76` explains these are non-abstract, default-safe hooks so the built-in inherits working behavior without overriding.

---

## 3. CLI `hermes cron` — routes ALL mutations through the cronjob tool; the notify happens inside the tool

The CLI (`hermes_cli/cron.py`) does **not** talk to the scheduler directly and does **not** issue a reload. It funnels every mutation through `_cron_api()`, which is a thin wrapper over the exact same `cronjob` function the model tool uses:

- `hermes_cli/cron.py:45-48` — `_cron_api()`: `from tools.cronjob_tools import cronjob as cronjob_tool; return json.loads(cronjob_tool(**kwargs))`
- `hermes_cli/cron.py:341-353` — `cron_create()` → `_cron_api(action="create", ...)`
- `hermes_cli/cron.py:404-416` — `cron_edit()` → `_cron_api(action="update", ...)`
- `hermes_cli/cron.py:438-439` — `_job_action()` (pause/resume/run/remove) → `_cron_api(action=action, job_id=job_id)`

Because it delegates to the tool, the provider-notify runs inside the tool (see Q4). The CLI additionally:
- Has a `hermes cron tick` verb that runs one synchronous tick: `hermes_cli/cron.py:193-196` — `cron_tick()` → `from cron.scheduler import tick; tick(verbose=True)`
- Warns when no gateway is running (`hermes_cli/cron.py:66-96`), noting there is no standalone daemon.
- The interactive `/cron` slash command (`hermes_cli/cli_commands_mixin.py:1229-1236`) also wraps `cronjob_tool` the same way.
- The `/cron` slash command is marked `cli_only=True` in `hermes_cli/commands.py:199-201`.

---

## 4. The `cronjob` tool — calls `_notify_provider_jobs_changed_safe()` after every mutating action

`tools/cronjob_tools.py::cronjob()` (line 659) is the single action-oriented entry point. After each successful store mutation it calls the notify wrapper:

- `tools/cronjob_tools.py:38-45` — `_notify_provider_jobs_changed_safe()` → `from cron.scheduler import _notify_provider_jobs_changed; _notify_provider_jobs_changed()` (best-effort)
- `create`: line 754 — after `create_job(...)` (lines 736-753)
- `remove`: line 814 — after `remove_job(job_id)`
- `pause`: line 830 — after `pause_job(...)`
- `resume`: line 835 — after `resume_job(...)`
- `run` (only when claim won): line 850 — after `_execute_job_now(job)`
- `update`: line 966 — after `update_job(job_id, updates)`

The `run` action executes the job **immediately**, bypassing the ticker: `tools/cronjob_tools.py:604-656` — `_execute_job_now()` uses `claim_job_for_fire` (at-most-once CAS) then delegates to `run_one_job` (the shared execute→save→deliver→mark body).

**REST surface mirrors the tool:** `gateway/platforms/api_server.py:1129-1136` defines `_notify_cron_provider_jobs_changed()` (same pattern), called after REST cron mutations at lines 5263 (create), 5320, 5340, 5360, 5380 (update/remove/pause/resume).

**One gap worth noting:** the dashboard web-server cron endpoints (`hermes_cli/web_server.py:12207-12232` — `_call_cron_for_profile`) call `cron.jobs` functions directly and do **not** call the notify. Harmless for the built-in (re-reads each tick) but an external provider would only learn of dashboard-made changes on its next `reconcile()`.

---

## 5. Process model — a daemon THREAD inside the gateway process (not a separate process)

The scheduler is **not** a separate process. It runs as a background daemon thread inside whatever long-lived process hosts it:

- `gateway/run.py:24246-24297` — in `start_gateway()`:
  - line 24251-24253: `cron_provider = resolve_cron_scheduler()`, creates `cron_stop = threading.Event()`
  - line 24290-24297: `cron_thread = threading.Thread(target=cron_provider.start, args=(cron_stop,), kwargs=cron_start_kwargs, daemon=True, name="cron-scheduler")` then `cron_thread.start()`
- `cron/scheduler_provider.py:162-261` — `InProcessCronScheduler.start()` **blocks** in the 60s loop `while not stop_event.is_set(): ... stop_event.wait(interval)` (this runs inside the daemon thread).
