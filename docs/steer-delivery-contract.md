# Steer Delivery Contract

> **Status:** authoritative contract for every /steer surface in the Hermes
> ecosystem.
> **Audience:** plugin authors who implement a `POST /steer` endpoint
> (dashboard/desktop backend `plugin_api.py`), and anyone debugging steer
> delivery to a kanban worker.
> **Source of truth:** this document. Core guarantees delivery; a steer
> *surface* (plugin) implements the endpoint policy below.
> **Last updated:** 2026-08-23

## Purpose

A **steer** is a short operator instruction aimed at a live or queued kanban
task: "stop", "use the other branch", "answer in Spanish". Steers are delivered
through the **task-comment bridge** — the same durable `task_comments` channel
that powers the kanban comment thread — never by mutating worker state
directly. The worker's run loop drains new comments as out-of-band input
between tool calls (`agent.steer()`), so an operator can talk to a task without
the block → comment → unblock dance or a restart.

This contract makes the steer-delivery semantics **common across every session
and project**, not a plugin-local policy. Any surface that exposes a steer
endpoint (dashboard plugin, desktop plugin, TUI, script) must implement the
exact status→response behavior in §2. The core owns the durability and the
delivery mechanics (§3); the surface owns only the endpoint policy — and that
policy is fixed here.

## 1. The status → response table

Given a `POST /steer` request carrying `{task_id, steer_text}` against a task in
the given status:

| Task status | Behavior | HTTP | Response body |
|---|---|---|---|
| `running` | **Live steer** — comment written to the task-comment bridge; lands on the worker's next tool drain (≥6s poll) | 200 | `{"ok": true, "comment_id": N, "task_id": …, "delivery": "comment-bridge (>=6s poll; lands on next tool drain)", "resolved_board": …}` |
| `todo` / `ready` / `blocked` / `scheduled` / `triage` / `review` | **Deferred — saved for the next run.** Comment written anyway and returned 200 with `deferred: true`; it rides the next run's context via the comment thread (`build_worker_context`) | 200 | `{"ok": true, "comment_id": N, "task_id": …, "deferred": true, "delivery": "saved for next run — lands in the next run's context via the comment thread", "resolved_board": …}` |
| `done` / `archived` | **Rejected** — no next run exists, so the steer would never be consumed; it is not saved (a steer is never silently lost, nor parked on a task that can never read it) | 409 | `{"detail": "task is '<status>' (done/archived) — no next run; steer not saved"}` |
| task not found | Rejected | 404 | `{"detail": "task <id> not found"}` |
| empty `steer_text` | Rejected (nothing to deliver) | 400 | `{"detail": "steer_text is required"}` |

Notes that bind every implementation:

- **A steer is a durable comment first.** Every 200 response has already
  committed the comment to `task_comments`. There is no "accepted but lost"
  state: if the request returns 200, the steer is durably stored.
- **`deferred: true` is a delivery promise, not a refusal.** The steer was
  accepted and will reach the worker on its next run, as part of the run's
  context (§3.3). UIs should render this distinctly (e.g. a "saved for next
  run" toast) instead of showing the steer as delivered live.
- **Only the terminal lanes 409.** `done`/`archived` are the sole statuses
  with no future run. Everything else in `VALID_STATUSES` has either a live
  run now (`running`) or a run ahead of it, so a steer is queued rather than
  rejected. 409 must never be used for "not running right now" — that is
  exactly the deferred case.

## 2. Surface requirements (the contract every endpoint implements)

A conforming `POST /steer` surface MUST:

1. Accept `{task_id, steer_text}` (and optionally a `board` query param that
   resolves through the core board-resolution helper when provided).
2. Strip `steer_text`; reject empty text with 400.
3. Look up the task; reject unknown ids with 404.
4. Map the task status through the §1 table exactly:
   - `running` → live comment + 200 (no `deferred` field, or `deferred: false`)
   - non-terminal, non-running → comment + 200 with `deferred: true`
   - `done`/`archived` → **no comment written**, 409
5. Write the comment through the core `add_comment(conn, task_id, author,
   body=text)` helper — the durable comment is the delivery vehicle; never
   invent a parallel mechanism.
6. Never mutate worker state, never call `agent.steer()` from the endpoint,
   never fabricate a delivery the bridge didn't make.

The surface does NOT need to implement polling, watermarking, or context
injection — those are core (§3) and shared by every worker. If a surface cannot
reach the core kanban DB (e.g. `hermes_cli.kanban_db` unavailable), it returns
500 rather than a fake success.

## 3. Core mechanics (guaranteed by core, not by the surface)

### 3.1 Durable comment channel

`task_comments` is a core SQLite table; `add_comment` commits the steer row
transactionally. This is what makes "accepted = durable" true across restarts
and across surfaces. Any steer that returns 200 exists in this table.

### 3.2 Live delivery: the ≥6s poll bridge

The worker's run loop calls `tools.kanban_tools.inject_new_comments_from_env`
between tool calls. It is **self-gating and rate-limited**:

- no-op unless the process is a kanban worker (`HERMES_KANBAN_TASK` set) and
  the agent exposes `steer`;
- rate-limited to `_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0` — a live steer
  lands within roughly one poll interval (a few seconds), never synchronously
  and never inside the in-flight tool call;
- watermarked per task id so already-seen comments are never re-injected;
- never raises into the agent loop (best-effort).

Injected comments are folded into the agent's next tool result as an
OUT-OF-BAND steer via `agent.steer()` (non-interrupting by design — it does
not abort the current tool call).

### 3.3 Deferred delivery: comment-thread context pickup

A deferred steer is consumed by the task's **next run**, not by any live
loop. The dispatcher's `build_worker_context` includes the task's comment
thread (most recent 30 comments shown; older collapsed) in every spawned
worker's context. The deferred comment therefore appears in the next run's
context as part of the thread — the worker reads it as history and acts on it.

### 3.4 The run-start watermark baseline (no swallowed steers)

The live injector seeds its per-task watermark on first poll. Historically it
seeded to the *current max comment id*, which meant a comment landing between
spawn/context-build and the first poll was neither in the worker's context
(built earlier) nor injected (seeded past) — a silent swallow.

The dispatcher now pins an internal **run-start baseline** env var
(`HERMES_KANBAN_COMMENT_BASELINE`, set to the task's max comment id at spawn —
an internal dispatcher→worker process bridge, not a user-facing config knob)
and the injector seeds to that baseline and injects comments past it. The
spawn→first-poll swallow window is closed: every comment written after the run
started is delivered. One precision note: a comment landing between the
baseline pin (spawn) and the worker's first context build is delivered TWICE —
once in-thread (it is already in `build_worker_context`'s comment thread) and
once as a live injection. That is a duplicate, never a loss: the guarantee is
"never swallowed," not "exactly once." This is core behavior; surfaces must not
re-implement it.

## 4. Why the 200-with-deferred shape (design notes)

- **Accept-and-queue beats reject-and-retry.** An operator steering a queued
  task wants "I'll do that when it runs", not an error they must notice and
  re-send later. The comment is durable either way, so queueing loses nothing.
- **Terminal 409 is honesty.** Saving a steer onto a `done`/`archived` task
  would be a silent fake delivery — the comment would sit forever unread. The
  409 says so plainly.
- **One policy, all surfaces.** The status→response mapping is fixed here so a
  steer behaves identically whether it arrives from a dashboard plugin, a
  desktop plugin, or a script. A surface's only job is to translate this
  contract to its own wire format.

## Attribution

- **Design raised by Silksteele** on 2026-08-19 (steer-delivery semantics for
  kanban tasks), **resolved 2026-08-23** with the full deferred-steer design
  (steer-delivery race card t_234c49d1).
- **Reference implementation:** crew-mandala plugin commit `a3393aa`
  (`fix(plugin): /steer queues non-running steers (saved-for-next-run UX)`),
  which ported the §1 table to `dashboard/plugin_api.py`. This document is the
  canonical restatement of that behavior for every steer surface.
- Core durability + comment-thread pickup pre-existed the port; the run-start
  watermark baseline (card t_95ab583a) closed the remaining spawn→first-poll
  swallow window in core.

## References

- `tools/kanban_tools.py::inject_new_comments_from_env` — live poll bridge
  (`_COMMENT_POLL_MIN_INTERVAL_SECONDS`, watermark, baseline seed)
- `hermes_cli/kanban_db.py::_default_spawn` / `_max_comment_id_for_task` —
  dispatcher pins `HERMES_KANBAN_COMMENT_BASELINE`
- `hermes_cli/kanban_db.py::build_worker_context` — comment-thread context
  pickup (most recent 30 comments)
- `hermes_cli/kanban_db.py::add_comment` / `list_comments_after` — durable
  comment channel and incremental read
- `run_agent.py::steer` — non-interrupting steer injection into the next tool
  result
- Reference surface: crew-mandala `dashboard/plugin_api.py::steer` (a3393aa)
