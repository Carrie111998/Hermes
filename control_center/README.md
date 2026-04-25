# Hermes Control Center — Phase D of ADR-0020

A lightweight FastAPI + HTMX dashboard at `http://localhost:9120` that gives Diego a single pane of glass for the new event types we added in Phases B + C iter2.

## Why a separate port from `:9119`

`:9119` already runs the React/Vite-based **Hermes admin dashboard** (`hermes web`) — managing the agent platform itself (sessions, config, plugins). Its plugin system requires UMD bundles and a Vite build pipeline, which would take much longer than the actual Phase D work. Building a sibling at `:9120` with HTML+HTMX (no JS toolchain) ships faster and stays cleanly decoupled.

| Port | Service | Tech | Purpose |
|---|---|---|---|
| `:9119` | Hermes admin dashboard | React + Vite | Manage Hermes platform itself (sessions, config, plugins) |
| `:9120` | Hermes Control Center | FastAPI + HTMX | Daily operator pane for HITL approvals, Critic proposals, health overlay, activity feed |

## Layout

Single page, 4 panels:

```
┌────────────────────────────────────────────────────────────────────┐
│  Header: Hermes Control Center · Phase D · :9120 · refresh time    │
│  Quick links: :9119 admin, Langfuse, DevFlow MC, Telegram          │
├────────────────────────────────────────────────────────────────────┤
│  Platform Health  (60 probes summary, components needing attention) │
├────────────────────────────────┬───────────────────────────────────┤
│  Pending HITL Approvals        │  Critic Proposals                 │
│  (paused LangGraph runs)       │  (drift-cluster proposals)        │
│  approve · reject · reason     │  applied · skip · snooze          │
├────────────────────────────────┴───────────────────────────────────┤
│  Recent Activity (last 40 events from event_bus.db)                 │
│  jobflow + apply + critic + agent_error + ...                       │
└────────────────────────────────────────────────────────────────────┘
```

All panels auto-refresh every 30s via HTMX `hx-trigger="every 30s"`. No manual refresh needed.

## Data sources (all read-only except resolution log)

| panel | source |
|---|---|
| Health | `C:\Users\diego\architecture-map\status.json` (laptop-monitor.ps1, 61 probes) |
| Approvals | `~/.hermes/graphs/approval-log.jsonl` cross-referenced with `~/.hermes/control_center/state.db` (resolutions) |
| Proposals | `~/.hermes/profiles/critic/workspace/whatsapp_queue.jsonl` + `mailbox/main/inbox/*_CRITIC_PROPOSAL_*.json` |
| Activity | `~/.hermes/events/event_bus.db` (last 40 events of selected types) |

A small SQLite at `~/.hermes/control_center/state.db` tracks resolution decisions (approved/rejected/applied/skipped/snoozed) so resolved items disappear from the queue across restarts.

## Approve/reject mechanics

When you click **approve** or **reject** in the Approvals panel, the API endpoint:

1. Calls `graphs.resume_full(thread_id, payload)` — the same `Command(resume=...)` path that `bin/jobflow_approve.py` uses.
2. The graph wakes from its checkpoint, the `approval_hitl_node` interrupt returns the payload, and the rest of the pipeline runs (apply → tracker_update → END).
3. Records the decision in `state.db` so the row disappears from the queue.

For Critic proposals, **applied/skip/snooze** just record state — actually applying a propose-only proposal is iter3 work (the auto-apply executors + LLM-driven replay).

## Layout files

```
agent-src/control_center/
├── __init__.py
├── app.py                   FastAPI routes + route handlers
├── storage.py               Read-only data accessors + resolution-state SQLite
├── templates/
│   ├── index.html           Shell with HTMX hx-get triggers
│   ├── _health_panel.html
│   ├── _approvals_panel.html
│   ├── _proposals_panel.html
│   └── _activity_panel.html
└── static/                  (reserved; CDN-only assets in v1)
```

CLI runner: `bin/control_center_run.py`.

## Running

```bash
# Manual (foreground)
python ~/.hermes/bin/control_center_run.py

# Manual with hot-reload (dev)
python ~/.hermes/bin/control_center_run.py --reload

# Custom port
python ~/.hermes/bin/control_center_run.py --port 9121
```

In production: **`Hermes-ControlCenter` Windows Task Scheduler entry**, trigger `AtLogOn`, restart-on-failure (5x with 1-min interval), no execution-time limit.

## Health probe

`laptop-monitor.ps1` adds `Hermes Control Center :9120` (tier=important) which curls `/api/health` and matches `"ok":true`. Total probes now 61.

## iter2 (shipped 2026-04-24)

- **WebSocket live event stream** at `/ws`. The page receives `{kind:event, panel, summary, ...}` frames and triggers HTMX `refresh-now` events on just the affected panel. Heartbeats every 30s. Auto-reconnect with exponential backoff. The 30s polling stays as a belt-and-suspenders fallback. (`agent-src/control_center/ws.py`)
- **Activity filter** — `event_type` dropdown (any/job_scored/critic_proposal/etc.), `source` text contains, `since` shorthand (5m/30m/1h/6h/24h). Submits via HTMX on change/keyup. Storage layer accepts `event_type`, `source_substring`, `since_minutes` filters with parameterized SQL.
- **Critic proposal apply** — clicking *applied* on a safe-kind proposal runs the real executor:
  * `skill.ranking` -> bumps counters in `skills/<skill>/metadata.json`
  * `matcher.threshold_adjust` -> writes the env var to `~/.hermes/.env` (replaces or appends)
  * Other kinds (prompt_edit, dimension_weight, structural, reasoning_effort, cron.cadence) -> records *intent only* with a placeholder reversal note. Diego still sees the proposal disappear from the queue, but the real change is manual. Each apply (executed or intent-only) writes a JSON reversal to `profiles/critic/workspace/reversals/` and an entry to `changelog.jsonl` tagged `applied_via: control_center`.
- **Opt-in auth via `HERMES_CC_TOKEN` env var** (`agent-src/control_center/app.py::TokenAuthMiddleware`). When unset (default): no auth. When set: every request except `/api/health` and `/static/*` requires the token via `X-Hermes-Token` header, `hermes_cc_token` cookie, or `?token=...` query param. The query param on first navigation auto-sets the cookie for the session. WebSocket bypasses middleware (Starlette processes the WS upgrade pre-middleware) — re-add a WS-side check if you ever expose to LAN.

## iter3 (shipped 2026-04-24)

- **`bin/apply_packet_operator.py`** — closes the JobFlow loop. Reads `APPLY_PACKET` events from the bus, opens browser-harness on Diego's dedicated Chrome at the apply URL, captures a screenshot, sends a Telegram message with screenshot+caption to the **JobFlow Decisions** topic so Diego knows it's ready to submit.
  * **Manual-trigger CLI** (not a cron daemon) — running every 5 min would yank the browser away mid-submission. Diego runs it when ready.
  * Cursor at `~/.hermes/control_center/apply_packet_cursor.json` tracks processed `event_id`s; only navigates the OLDEST unprocessed packet (`--all` for batch with confirm prompts).
  * On harness failure, packet stays pending — retry-able after `refresh_harness.ps1`.
  * Modes: `--list` / `--packet-id <id>` / `--all` / `--dry-run`.

- **Control Center "Open next APPLY_PACKET" button** at the top of the page. POSTs to `/api/apply_packet/next`, which subprocesses the operator and surfaces the JSON result (success/error) inline. `htmx-indicator` spinner while the 25-30s navigation runs.

- **`bin/critic_revert.py`** — undo iter2's apply executor.
  * `--list` / `--proposal-id <pid>` / `--reversal-file <path>` / `--dry-run`
  * Per-kind reverters mirror the apply executor: `matcher.threshold_adjust` restores the env var (or deletes the line if prior was unset); `skill.success_ranking` restores the prior counters; intent-only kinds are no-op-on-disk but logged.
  * Successful revert renames the reversal JSON to `*.reverted.json` so we don't double-revert. Appends an `{"action": "reverted", ...}` entry to `changelog.jsonl` for audit chain completeness.
  * **E2E validated**: synthetic `matcher.threshold_adjust` apply → `.env` updated → revert → `.env` restored to absent → reversal archived → changelog has both entries.

## iter4 (shipped 2026-04-24)

- **LLM-driven Reflexion replay for `matcher.prompt_edit` proposals** (`graphs/critic.py::_llm_prompt_edit_replay`). When `HERMES_CRITIC_LLM_REPLAY=1` is set, the Critic graph re-invokes the matcher with the proposed prompt addition appended to MATCHER_SYSTEM_PROMPT, against each evidence_job in the cluster (capped at 6 jobs to bound LLM cost). Compares new scores to the shadow baseline + production score; emits one of:
  * `would_close_drift` — new scores moved toward production by ≥40% on ≥2/3 of evidence jobs
  * `made_drift_worse` — moved AWAY from production
  * `no_effect` — within 0.2 of baseline
  * `mixed_effect` — direction inconsistent
  * `evidence_not_resolvable` — couldn't fetch any job_data
  * `replay_error` — LLM call failed
  * Default OFF to keep daily Critic runs cheap (~25s); ~5s per evidence-job extra when on.

- **`graphs/_job_data.py`** — fetches Scout-shape job dicts by id across (1) `mailbox/matcher-shadow/outbox`, (2) `mailbox/matcher/outbox`, (3) `mailbox/tracker/processed/SCOUT_DISCOVERY`, (4) Langfuse hermes-jobs-v1 dataset. Cached per-process (lru_cache size 64).

- **Browser-harness auto-refresh on operator failure** (`bin/apply_packet_operator.py`). When the first `browser-harness -c "..."` call fails (daemon down, stale CDP_WS, etc.), the operator automatically runs `refresh_harness.ps1` (which re-launches dedicated Chrome at CDP :9222 + writes fresh BU_CDP_WS to .env + restarts the daemon) and retries the navigation once. Default ON; opt out with `--no-auto-refresh-harness`. Refresh budget: 180s.

- **Bulk proposal actions** (`POST /api/proposals/bulk-skip-low-risk` + `POST /api/proposals/bulk-snooze-all`). Two buttons at the top of the Proposals panel:
  * `⊘ skip all low-risk` — marks every low-risk pending proposal as skipped (with `hx-confirm`). Useful when Diego wants to clear noise.
  * `⏱ snooze all` — snoozes every pending proposal, regardless of risk. For "I'm busy, hide them all and I'll review later".

## Future iterations (parked)

- **Telegram-bot deep links** — click an event in the activity feed and jump to the matching Telegram thread. Requires storing `tg_message_id` on event payloads at emission time.
- **Drag-to-dismiss** on health components that are intentionally down
- **WS-side auth** — currently `/ws` bypasses the token middleware (Starlette quirk). Re-add a token-check inside `ws_endpoint()` before exposing externally.
- **`agent.reasoning_effort` and `cron.cadence` apply executors** — currently intent-only since no proposals of those kinds have flowed yet.
- **Self-loop on contradictions** — when reflexion flags `contradiction_detected`, re-invoke `generate_proposals` with replay feedback to converge before emitting (Phase C iter5).
