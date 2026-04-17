# Hermes Communication Layer — Event Bus Architecture

**Date:** 2026-04-15
**Status:** Approved
**Author:** Diego + Claude Opus 4.6

## Problem Statement

Since migrating the JobFlow architecture from OpenClaw to Hermes, all proactive notifications have gone silent. Jaum (the main WhatsApp-facing agent) only responds to messages — no preemptive alerts, status updates, or digest delivery. Five compounding failures caused this:

1. **`jaum-daytime-relay` delivery broken** — `deliver=origin` doesn't resolve for cron jobs (no origin session)
2. **Telegram not enabled** — `TELEGRAM_ENABLED` flag was missing from `.env`; gateway never initialized the adapter
3. **Notifier digests go nowhere** — `jobflow-notifier` writes to Jaum's mailbox inbox, but nothing relays to the user
4. **OpenClaw legacy duplicates failing** — 7+ cron jobs running in parallel with Hermes, 3 with consecutive WhatsApp gateway errors
5. **No event-driven delivery** — Everything is poll-based or cron-based with no real-time push

## Architectural Decision

**Approach B: Event Bus with Notification Subscribers** — a SQLite-backed event bus where all agent activities emit typed events, consumed by pluggable subscribers for notification delivery, memory integration, and audit logging.

**Why not Approach A (Notification Router):** Too lightweight — fixes the immediate problem but doesn't scale. Would need re-engineering in 3 months for real-time alerts and per-agent bots.

**Why not Approach C (Full Event-Driven Rewrite):** Overkill. Replaces a working filesystem mailbox with untested infrastructure. The problem is delivery, not inter-agent messaging.

### Design Principles

- Filesystem mailbox remains source of truth for inter-agent coordination
- Event bus handles notification/observability as a separate concern
- No external dependencies — SQLite, same as the rest of Hermes
- Telegram is primary channel (24/7, forum topics, detailed)
- WhatsApp is escalation-only channel (action-required, system health, high-value signals)
- Memory integration via targeted real-time writes for high-signal events
- Existing learning pipelines (learning-loop, evey-memory-consolidate, evey-learner) are unchanged

---

## 1. Event Bus Core

### Event Schema

```python
{
  "event_id": "uuid4",
  "event_type": "job_scored",        # Typed enum
  "source": "matcher",               # Agent profile that emitted the event
  "timestamp": "ISO8601",            # UTC
  "priority": "high",                # critical | high | normal | low
  "payload": { ... },                # Event-type-specific data
  "correlation_id": "uuid4",         # Links related events across the pipeline
  "job_id": "external_key",          # Optional, for job pipeline events
  "tags": ["jobflow", "vip"]         # Optional, for filtering/routing
}
```

### Event Type Catalog

| Event Type | Source | Default Priority | Description |
|---|---|---|---|
| `cron_started` | any | low | Cron job began execution |
| `cron_completed` | any | normal | Cron job finished successfully |
| `cron_failed` | any | high | Cron job errored |
| `cron_failed_consecutive` | system | critical | 3+ consecutive failures on same job |
| `job_discovered` | scout | normal | New job found on a board |
| `job_scored` | matcher | normal | Job scored (includes score + recommendation) |
| `job_high_score` | matcher | high | Score >= 8.75, auto-routed to tailor. WhatsApp escalation threshold is >= 9.0 (see Section 2.2) |
| `job_vip_discovered` | sentinel | high | VIP job from LinkedIn saved |
| `tailor_completed` | tailor | normal | Resume/cover letter generated |
| `application_ready` | applier | high | Dry run complete, awaiting approval |
| `application_submitted` | applier | high | Application submitted successfully |
| `application_failed` | applier | critical | Submission failed |
| `application_blocked` | applier | critical | Blocked question needs human answer |
| `stage_transition` | tracker | normal | Job moved pipeline stages |
| `interview_signal` | tracker | critical | Interview request or scheduling |
| `offer_signal` | tracker | critical | Offer received |
| `followup_due` | tracker | high | 14-day no-response, follow-up suggested |
| `digest_generated` | notifier | low | Digest ready for delivery |
| `gateway_health` | system | high | Gateway up/down state change |
| `agent_error` | any | high | Unhandled agent error |
| `memory_consolidated` | system | low | Learning loop or consolidation ran |
| `skill_evolved` | system | low | Skill evolution completed |
| `mailbox_message` | any | low | Inter-agent mailbox message (mirror) |

### Storage

- **SQLite database:** `~/.hermes/events/event_bus.db`
- **Table:** `events` with columns matching schema + `status` (pending/delivered/expired) + `created_at`
- **Index:** `(event_type, status, timestamp)` for subscriber queries
- **Retention:** 30 days, nightly cleanup
- **WAL mode** for concurrent reads (subscribers) and writes (producers)

### API

```python
class EventBus:
    def emit(event_type, source, payload, priority="normal",
             correlation_id=None, job_id=None, tags=None) -> str  # returns event_id

    def subscribe(subscriber_id, event_types=None,
                  min_priority=None) -> list[Event]  # events since subscriber's cursor

    def ack(subscriber_id, event_ids: list)  # advance subscriber cursor

    def query(event_type=None, source=None, since=None,
              correlation_id=None) -> list[Event]  # ad-hoc queries
```

Each subscriber tracks its own read cursor (`subscriber_cursors` table), enabling independent fan-out consumption.

---

## 2. Notification Subscribers

Six subscribers consume events from the bus:

### 2.1 TelegramNotifier

Routes events to Telegram forum topics based on event type and priority.

**Topic routing:**

| Topic | Events |
|---|---|
| Alerts & Actions | `application_blocked`, `application_failed`, `interview_signal`, `offer_signal`, `cron_failed_consecutive`, `gateway_health` |
| Scout / Discoveries | `job_discovered`, `job_vip_discovered` |
| Matcher / Scores | `job_scored`, `job_high_score` |
| Tailor & Applier | `tailor_completed`, `application_ready`, `application_submitted` |
| Tracker / Pipeline | `stage_transition`, `followup_due` |
| Digests & Summaries | `digest_generated` |
| System Health | `cron_started`, `cron_completed`, `cron_failed`, `agent_error`, `memory_consolidated`, `skill_evolved` |
| Agent Comms | `mailbox_message` |

**Cross-posting:** Events with `high`/`critical` priority that are action-required post to both their natural topic AND Alerts & Actions.

**Formatting:** Header line `[PRIORITY] EVENT_TYPE from SOURCE @ TIME`, human-readable payload summary. `low` events batched for up to 5 minutes.

**Delivery:** 24/7, no quiet hours. Uses existing Telegram adapter `send()` targeting `telegram:GROUP_CHAT_ID:TOPIC_THREAD_ID`.

### 2.2 WhatsAppEscalator

Sends digested, actionable messages to WhatsApp for events that demand attention.

**Escalation tiers:**

| Tier | Event Types | Quiet Hours Behavior |
|---|---|---|
| Immediate | `interview_signal`, `offer_signal` | Breaks through |
| Urgent | `application_blocked`, `application_failed`, `cron_failed_consecutive`, `gateway_health` (down) | Queued |
| Important | `job_high_score` (>= 9.0), `application_ready`, `followup_due` | Queued |
| Digest | DigestComposer morning output | Sent at 7:01am |

**Throttle:** 15-minute window. Multiple events combined into bullet-pointed message. Max ~3-8 WhatsApp messages/day expected.

**Format:** Plain text, no markdown. Actionable messages include clear call-to-action. Ends with "Details in Telegram."

**Quiet hours:** 11pm-7am ET. Queued events flush as "Overnight Summary" at 7:01am. Breakthrough events never trigger a queue flush of non-breakthrough events.

### 2.3 DigestComposer

Produces 3x/day structured digests.

**Schedule:** 8:00am (morning), 1:00pm (midday), 6:00pm (evening). Runs as a timer-based subscriber within the gateway process (not a cron job). The timer fires at these fixed times and queries the event bus for events since the last digest.

**Replaces:** `jaum-daytime-relay` (deleted) and delivery portion of `jobflow-notifier` (simplified).

**Digest structure:**
- Pipeline snapshot (counts by stage)
- Events since last digest (per-agent summaries)
- Action items (approvals pending, follow-ups due)
- System health (cron job status, gateway status)

**Delivery:** Posts to Digests & Summaries Telegram topic. Morning digest also delivered via WhatsApp (condensed version).

**Data source:** Queries event bus for events since last digest timestamp.

### 2.4 MemoryWriter

Writes high-signal events to the appropriate memory layer per CLAUDE.md routing rules.

| Event | Target | Content |
|---|---|---|
| `job_high_score` | GBrain `put_page` + `add_timeline_entry` | Company page, "High-score job discovered" |
| `application_submitted` | GBrain `add_timeline_entry` | Application record on company page |
| `application_failed` | GBrain `add_timeline_entry` + Agent MEMORY.md | Failure record + investigation note |
| `interview_signal` | GBrain + MemPalace | Timeline entry + verbatim evidence drawer |
| `offer_signal` | GBrain + MemPalace | Timeline entry + verbatim evidence drawer |
| `stage_transition` | GBrain `add_timeline_entry` | Pipeline progression record |
| `cron_failed_consecutive` | Agent MEMORY.md | Operational failure note |
| `gateway_health` (down) | Agent MEMORY.md | Platform outage record |
| `followup_due` | MemPalace `add_drawer` | Followup timing evidence |

**Rate limits:** Max 10 GBrain writes/hour, 5 MemPalace writes/hour. Overflow queued, never dropped.

**Deduplication:** Check `correlation_id` before writing GBrain timeline entries.

**NOT written:** `cron_started`, `cron_completed`, `job_discovered`, `job_scored` (below 8.75), `mailbox_message`, `digest_generated`.

### 2.5 AuditLogger

Records every event to append-only JSONL at `~/.hermes/events/audit.jsonl`. One line per event. Rotated weekly, retained 90 days.

### 2.6 TelegramMirror

Watches `~/.hermes/mailbox/*/inbox/` for new JSON files (poll every 60s). Emits `mailbox_message` events for substantive inter-agent messages. Tracks seen files via `.watermark` file.

**Mirrored message types:** `SCOUT_DISCOVERY`, `SCORE_REQUEST`, `SCORE_RESULT`, `SCORE_BATCH_SUMMARY`, `TAILOR_REQUEST`, `TAILOR_COMPLETE`, `TAILOR_REVISION`, `SUBMIT_REQUEST`, `DRY_RUN_COMPLETE`, `SUBMIT_CONFIRM`, `BLOCKED_QUESTION`, `PIPELINE_UPDATE`, `STATUS_REQUEST`, `STATUS_RESPONSE`, `FOLLOWUP_ALERT`, `NOTIFICATION`, `HIGH_SCORE_ALERT`, `VIP_DISCOVERY`, `VIP_PROMOTE`, `KB_QUERY`, `KB_RESPONSE`, `ERROR`.

**Filtered out (not mirrored):** Inbox sweeper operations, processed-folder moves, empty inbox scans, any file not matching the `{timestamp}_{TYPE}_{from}.json` naming convention.

---

## 3. Event Producers

### 3.1 CronEventEmitter

Wraps the cron execution pipeline with pre/post hooks:

```
Before job → emit cron_started
Job completes → emit cron_completed + parse output for domain events
Job errors → emit cron_failed (+ cron_failed_consecutive if 3+)
```

**Output parsing:** Inspects agent output for domain signals (job discoveries, scores, submissions, stage transitions) and emits corresponding typed events. Agents already produce typed mailbox messages — the parser reads from the same structured data.

### 3.2 GatewayHealthMonitor

Runs every 60 seconds within the gateway process:
- Pings WhatsApp bridge health endpoint
- Checks Telegram bot connectivity (cached 5 min)
- Emits `gateway_health` only on state change (up→down or down→up)

### 3.3 MailboxWatcher

Polls `~/.hermes/mailbox/*/inbox/` every 60 seconds. Emits `mailbox_message` events for substantive inter-agent messages. Tracks watermark to avoid re-processing.

### 3.4 Delivery Target Migration

All cron jobs change `deliver` field to `local`. Cron jobs focus on doing work. The event bus handles notification delivery. This removes the single point of failure that caused the silence.

---

## 4. Telegram Group Setup

### One-Time Setup Script

`hermes-telegram-setup.py` — run after creating the group and adding `@j4um_bot` as admin.

**Actions:**
1. Create 8 forum topics via `createForumTopic` API
2. Write topic registry to `~/.hermes/telegram/topics.json`
3. Write `TELEGRAM_HOME_CHANNEL` to `.env`
4. Send test message to each topic
5. Pin welcome message in General topic

### Topic Registry

```json
{
  "group_chat_id": "-100XXXXXXXXXX",
  "topics": {
    "alerts":        {"thread_id": N, "name": "Alerts & Actions"},
    "scout":         {"thread_id": N, "name": "Scout / Discoveries"},
    "matcher":       {"thread_id": N, "name": "Matcher / Scores"},
    "tailor_applier": {"thread_id": N, "name": "Tailor & Applier"},
    "tracker":       {"thread_id": N, "name": "Tracker / Pipeline"},
    "digests":       {"thread_id": N, "name": "Digests & Summaries"},
    "system":        {"thread_id": N, "name": "System Health"},
    "agent_comms":   {"thread_id": N, "name": "Agent Comms"}
  },
  "created_at": "ISO8601"
}
```

### Per-Topic Verbosity Control

`~/.hermes/telegram/verbosity.json`:

```json
{
  "scout":         {"mode": "all"},
  "matcher":       {"mode": "all"},
  "tailor_applier": {"mode": "all"},
  "tracker":       {"mode": "all"},
  "alerts":        {"mode": "all"},
  "digests":       {"mode": "all"},
  "system":        {"mode": "digest_only"},
  "agent_comms":   {"mode": "significant_only"}
}
```

Modes: `all`, `digest_only` (batch every 30 min), `significant_only` (high/critical only), `off`.

Hot-reloadable — subscriber reads config on each delivery cycle.

### Future: Per-Agent Bots

When ready, add `bot_token` field per topic in `topics.json`. TelegramNotifier posts using the agent's own bot identity. No other changes needed.

---

## 5. Quiet Hours

**Config:** `~/.hermes/notifications/quiet_hours.json`

```json
{
  "enabled": true,
  "start": "23:00",
  "end": "07:00",
  "timezone": "America/New_York",
  "breakthrough_events": ["interview_signal", "offer_signal"],
  "queue_file": "~/.hermes/notifications/quiet_queue.json"
}
```

**Behavior:**
- WhatsApp: queued during quiet hours, flushed at 7:01am as "Overnight Summary"
- Telegram: unaffected, posts 24/7
- Breakthrough events: sent immediately regardless of quiet hours
- Breakthrough does NOT trigger flush of queued non-breakthrough events

---

## 6. Jaum's Revised Role

**Before:** Single point of failure for all notifications (daytime-relay cron → Jaum agent → WhatsApp).

**After:**
- **Reactive:** Responds to user WhatsApp messages (status, approvals, commands) — unchanged
- **Decoupled from notifications:** WhatsAppEscalator delivers through the gateway adapter directly, not through Jaum's agent loop. Notifications flow regardless of Jaum's session state.

---

## 7. Legacy Cleanup

### OpenClaw Cron Jobs — All Disabled

| Job | Reason |
|---|---|
| `scout-morning/midday/evening` | Duplicated by `jobflow-scout` |
| `daily-digest` | Duplicated by `jobflow-notifier` + DigestComposer |
| `followup-check` | Duplicated by `jobflow-tracker-followup` |
| `weekly-report` | Duplicated by `jobflow-tracker-weekly` |
| `matcher-score` | Duplicated by `jobflow-matcher` |
| `tailor-approved-sweep` | Duplicated by `jobflow-tailor` |
| `stale-jobs-archiver` | Ported to new Hermes `jobflow-archiver` |

### Hermes Cron Adjustments

| Job | Change |
|---|---|
| `jaum-daytime-relay` | **Deleted** — replaced by DigestComposer |
| `jobflow-notifier` | **Simplified** — generates digest data only, no delivery |
| `jobflow-archiver` | **New** — Sunday 2am, archives stale jobs >30 days |
| All other jobs | `deliver` field → `local`, events via CronEventEmitter wrapper |

### Final Inventory: 17 Hermes Jobs, 0 OpenClaw Jobs

---

## 8. File Structure

### New Configuration Files

```
~/.hermes/
├── events/
│   ├── event_bus.db          # SQLite event store (30-day retention)
│   ├── audit.jsonl           # Append-only audit trail
│   └── audit/                # Rotated weekly archives (90-day retention)
├── notifications/
│   ├── quiet_hours.json      # Quiet hours config
│   └── quiet_queue.json      # WhatsApp messages queued during quiet hours
├── telegram/
│   ├── topics.json           # Topic registry (created by setup script)
│   └── verbosity.json        # Per-topic verbosity control
```

### New Source Files

```
hermes/events/
├── __init__.py
├── bus.py                    # EventBus (emit, subscribe, ack, query)
├── schema.py                 # Event dataclass, EventType enum, Priority enum
├── producers/
│   ├── __init__.py
│   ├── cron_emitter.py       # CronEventEmitter
│   ├── mailbox_watcher.py    # MailboxWatcher
│   └── health_monitor.py     # GatewayHealthMonitor
└── subscribers/
    ├── __init__.py
    ├── base.py               # BaseSubscriber abstract class
    ├── telegram_notifier.py
    ├── whatsapp_escalator.py
    ├── digest_composer.py
    ├── memory_writer.py
    ├── audit_logger.py
    └── telegram_mirror.py

scripts/
└── hermes-telegram-setup.py  # One-time Telegram group setup
```

### Runtime Model

All components run within the existing gateway process. No new daemons, ports, or containers.

- **Gateway startup:** EventBus init, subscriber registration
- **Subscriber loops:** 5-second poll for real-time subscribers, 60-second for batching
- **Cron execution:** CronEventEmitter hooks into existing pre/post pipeline
- **Gateway shutdown:** Subscribers flush pending batches

---

## Research References

- [Four Design Patterns for Event-Driven Multi-Agent Systems](https://www.confluent.io/blog/event-driven-multi-agent-systems/) — Confluent
- [AI Agent Orchestration Patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns) — Microsoft Azure
- [The Agentic Infrastructure Overhaul: 3 Non-Negotiable Pillars for 2026](https://www.cio.com/article/4112116/the-agentic-infrastructure-overhaul-3-non-negotiable-pillars-for-2026.html) — CIO
- [Agent Observability for Multi-Agent Systems](https://www.novatechflow.com/2026/03/agent-observability-for-multi-agent.html) — NovatechFlow
- [Event-Driven Architecture for AI Agent Systems](https://zylos.ai/research/2026-03-02-event-driven-architecture-ai-agent-systems) — Zylos Research
- [Multi-Agent Observability Reference Architecture](https://microsoft.github.io/multi-agent-reference-architecture/docs/observability/Observability.html) — Microsoft
- [Building Multi-Agent AI Systems in 2026: A2A, Observability, and Verifiable Execution](https://dev.to/chunxiaoxx/building-multi-agent-ai-systems-in-2026-a2a-observability-and-verifiable-execution-10gn) — DEV Community

---

## 2026-04-16 Post-Silence-Fix Addendum

After initial rollout on 2026-04-15, six compounding silences prevented all user-facing notifications.  Diagnosis and fix plan in `docs/superpowers/plans/2026-04-16-hermes-comms-layer-fixes.md`.  Key architectural updates:

- **Canonical paths (Option A):** All notification/event state lives at the single root resolved by `events.paths.*` (wrapping `hermes_constants.get_default_hermes_root()`).  Profile-scoped directories hold only per-agent state (memory, sessions, workspace, config.yaml).

- **MailboxTranslator subscriber (Option B):** Structured mailbox messages are the source of truth for domain events.  A new subscriber reads `mailbox_message` events and emits typed domain events.  The regex output parser in `CronEventEmitter` is retired.

- **Persistent subscriber state:** `DigestComposer._last_digest_at`, `TelegramNotifier._batch_buffer`, gateway loop `last_digest_hour`, and WhatsApp `last_flush_date` all persist via `events/state.py` atomic JSON helpers.

- **Periodic WAL checkpoint:** Every 60s, for external observability.

- **Telegram fallback transport:** Under NordVPN / restricted networks, set `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1`.  Sticky-IP logic now resets after 5 consecutive failures.

- **CLI diagnostic:** `python -m hermes_cli.events_doctor` validates path canonicality, bus schema, subscriber cursors, recent event flow, and optional live Telegram connectivity.
