# CODEX REPORT

Ngày thực hiện: 2026-08-29

## Phase 0 — Resolved Architecture Owners

| Owner | Resolved module:symbol | Evidence |
| --- | --- | --- |
| `HERMES_VERSION` | `hermes_cli.__version__` | `hermes_cli/__init__.py:17`, imported as `_HERMES_VERSION` in `run_agent.py:312`, ACP alias in `acp_adapter/server.py:226` |
| `MASTER_TASK_OWNER` | `hermes_cli.kanban_db.Task` + kanban lifecycle writers | `hermes_cli/kanban_db.py:1052`, `3158`, `5352`, `6246`, `6490`, `6652`, `9164` |
| `TASK_STATE_OWNER` | `hermes_cli.kanban_db` `tasks.status` transitions | `hermes_cli/kanban_db.py:3493-3527`, `5445-5457`, `6241-6251`, `6603-6612`, `6737-6745` |
| `TASK_EVENT_OWNER` | `hermes_cli.kanban_db._append_event()` over `task_events` | `hermes_cli/kanban_db.py:1442-1449`, `4300-4320` |
| `KANBAN_TASK_EVENT_SUPPORT` | Native lifecycle rows in `task_events` | `create_task(... "created")` at `3538-3557`; `complete_task(... "completed")` at `5547-5551`; `block_task(... "blocked")` hook at `6464`; `request_review(... "review_requested")` at `6638-6648`; `request_changes(... "changes_requested")` at `6756-6767`; timeout at `8512-8535`; breaker final `gave_up` at `9282-9284` |
| `TERMINAL_NOTIFICATION_OWNER` | `tools.terminal_tool` → `tools.process_registry` → `gateway.run._run_process_watcher()` | `tools/terminal_tool.py:3512-3533`, `tools/process_registry.py:1632-1654`, `gateway/run.py:26411-26529` |
| `TELEGRAM_HOME_ROUTE` | `gateway.config.HomeChannel` + `GatewayConfig.get_home_channel()` + `gateway.delivery._deliver_to_platform()` | `gateway/config.py:474-486`, `gateway/config.py:1099-1104`, `gateway/delivery.py:546-553` |
| `TELEGRAM_SEND_OWNER` | Gateway adapter send path + standalone sender | `plugins/platforms/telegram/adapter.py:6215-6252`, `6307-6378`; standalone `tools/send_message_tool.py:1346-1524` |
| `EXISTING_DEDUP_OWNER` | `kanban_notify_subs.last_event_id` + atomic claim/rewind; approval `request_id`; subprocess queue first-move guard | `hermes_cli/kanban_db.py:11443-11448`, `11766-11814`, `11834-11860`; `tools/approval.py:2784-2791`, `2829-2868`, `2893-2905`; `tools/process_registry.py:1632-1654` |
| `CONSENT_REQUEST_OWNER` | Dangerous-command approval broker in `tools.approval` with gateway callback bridge in `gateway.run` | `tools/approval.py:2784-2905`, `4446-4545`, `5094-5109`, `5662-5675`, `5772-5780`; `gateway/run.py:6113-6216`; Telegram remote allow/deny already exists via `plugins/platforms/telegram/adapter.py:6307-6378` |

### Phase 0 conclusion

- Native kanban lifecycle is authoritative for master-task state and terminality.
- Remote/canonical consent already exists in Hermes today, but V3 explicitly demotes Telegram to notification-only for master-task consent.
- Raw subprocess completion is independent telemetry, not master-task authority.

## Phase 1 — Existing Telegram Task Notification Paths

| Path | Trigger | `MASTER_TASK_AWARE` | `SUBPROCESS_ONLY` | Telegram route | Dedup | Current authority | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Kanban notify subscriptions | `gateway.kanban_watchers._kanban_notifier_watcher()` tails `task_events` | `true` | `false` | `adapter.send(chat_id, text, metadata)` | `kanban_notify_subs.last_event_id` via `claim_unseen_events_for_sub()` | Native kanban lifecycle | `KEEP` as canonical source; Telegram formatting/decision now converged through router |
| Terminal `notify_on_complete` | background process exit watcher | `false` | `true` | `_enqueue_process_completion_notification()` / adapter send path | `process_registry` first-move guard + gateway in-memory completion handling | subprocess exit | `DEMOTE` to `SUBPROCESS_TELEMETRY_ONLY` |
| Gateway approval / inline Telegram buttons | dangerous command / execute_code / MCP elicitation approval | `false` before patch; `true` after metadata enrichment | `false` | `send_exec_approval()` or text fallback | approval `request_id` / queue | approval broker | `REPAIR` for master-task scope: Telegram now receives passive consent notification only |

## Phase 2 / 3 / 4 / 5 / 6 / 7 / 9 — Implemented Canonical Design

### Single authority

- `MASTER_TASK_NOTIFICATION_AUTHORITY_COUNT=1`
- Notification decision owner: `gateway/master_task_notifications.py:346` `CanonicalNotificationRouter.decide`
- Durable lifecycle owner feeding the router: `hermes_cli/kanban_db.py:4300-4320` + native writers (`5352`, `6246`, `6490`, `6652`, `9164`)

### Duplicate path count

- `DUPLICATE_MASTER_NOTIFICATION_PATH_COUNT=0`
- `gateway/run.py:116` marks terminal `notify_on_complete` as `_TERMINAL_NOTIFY_ON_COMPLETE_SUBPROCESS_TELEMETRY_ONLY = True`
- `gateway/run.py:26507-26510` documents that subprocess completion must never become master-task completion authority

### Telegram consent

- `TELEGRAM_CONSENT_AUTHORITY=false`
- `tools/approval.py:242-275` enriches gateway approval payloads with authoritative master-task metadata from `HERMES_KANBAN_TASK`
- `gateway/run.py:6145-6172` builds a passive master-task consent event and sends it with `adapter.send(...)`
- For Telegram master-task consent, the branch returns before `send_exec_approval()`, so Allow/Deny stays in canonical UI only

### New module

- `gateway/master_task_notifications.py:25-63` defines `MasterTaskEvent`, `Decision`, `DeliveryResult`
- `gateway/master_task_notifications.py:124-343` maps kanban lifecycle events and consent payloads into canonical semantics
- `gateway/master_task_notifications.py:346-489` implements router decision, in-process dedup, and Vietnamese operator messages

### Integration points

- `gateway/kanban_watchers.py:263`, `713-719`, `812` routes Telegram kanban events through the canonical router and remembers successful deliveries
- `tools/approval.py:3862`, `5094`, `5662`, `5772` stamps all gateway approval payload variants with master-task metadata when running inside a kanban worker

### Semantic mapping

| Native source | Canonical event |
| --- | --- |
| `completed` | `TASK_COMPLETED` |
| `blocked` | `TASK_BLOCKED` |
| `review_requested` | `TASK_WAITING_OPERATOR` |
| `changes_requested` | `TASK_BLOCKED` |
| `block_loop_detected` | `TASK_BLOCKED` |
| `gave_up` with `trigger_outcome=timed_out` | `TASK_TIMED_OUT` |
| `gave_up` otherwise | `TASK_FAILED` |
| raw `timed_out` / `crashed` | claimed for dedup + wake, but `authoritative=false` and not notified as master terminal state |

## Phase 8 — Test Results

### Required witness table

| Witness | Result | Evidence |
| --- | --- | --- |
| SUCCESS | `PASS` | `tests/test_master_task_notifications.py:73`; exactly 1 Telegram delivery, title primary, summary present, duplicate route skipped |
| CHILD PROCESS | `PASS` | `tests/test_master_task_notifications.py:109`; raw `timed_out` delivered `0`, final `completed` delivered `1` |
| CONSENT | `PASS` | `tests/test_master_task_notifications.py:147`; `request_id` dedup kept consent delivery at `1`, completed event resumed same `task_id` |
| BLOCKED / FAILURE | `PASS` | `tests/test_master_task_notifications.py:205`; blocked mapped once, raw `crashed` skipped, final `gave_up` mapped once as failure |
| DEDUP / reconnect | `PASS` | `tests/test_master_task_notifications.py:255`; second `claim_unseen_events_for_sub()` returned `[]`, delivery count stayed `1` |

### Additional compatibility checks

- `tests/gateway/test_kanban_notifier.py::test_active_named_profile_subscription_is_delivered` — `PASS`
- `tests/gateway/test_kanban_notifier.py::test_notifier_delivers_block_loop_detected_triage_ping` — `PASS`
- `tests/gateway/test_kanban_notifier.py::test_review_requested_wakes_the_origin_session` — `PASS`
- `tests/gateway/test_kanban_changes_requested_notifier.py::test_changes_requested_notify_wake_is_actionable_and_exactly_routed` — `PASS`
- `tests/gateway/test_kanban_changes_requested_notifier.py::test_changes_requested_reason_is_redacted_path_safe_and_truncated` — `PASS`

### Exact pytest commands and output

Command 1:

```bash
python -m pytest tests/test_master_task_notifications.py -q --basetemp .pytest_tmp
```

Output 1:

```text
......                                                                   [100%]
6 passed in 2.14s
```

Command 2:

```bash
python -m pytest tests/test_master_task_notifications.py tests/gateway/test_kanban_notifier.py::test_active_named_profile_subscription_is_delivered tests/gateway/test_kanban_notifier.py::test_notifier_delivers_block_loop_detected_triage_ping tests/gateway/test_kanban_notifier.py::test_review_requested_wakes_the_origin_session tests/gateway/test_kanban_changes_requested_notifier.py::test_changes_requested_notify_wake_is_actionable_and_exactly_routed tests/gateway/test_kanban_changes_requested_notifier.py::test_changes_requested_reason_is_redacted_path_safe_and_truncated -q --basetemp 'C:\Users\Vu Tuan\AppData\Local\Temp\codex-pytest-base'
```

Output 2:

```text
...........                                                              [100%]
11 passed in 3.18s
```

## `git diff --stat`

```text
 gateway/kanban_watchers.py                         | 30 ++++++++++++
 gateway/run.py                                     | 40 ++++++++++++++++
 .../test_kanban_changes_requested_notifier.py      |  9 ++--
 tests/gateway/test_kanban_notifier.py              |  7 +--
 tests/hermes_cli/test_kanban_notify.py             |  2 +-
 tools/approval.py                                  | 55 ++++++++++++++++++----
 6 files changed, 127 insertions(+), 16 deletions(-)
```

Note:

- `git diff --stat` above is real machine output from the current worktree state.
- New files (`gateway/master_task_notifications.py`, `tests/test_master_task_notifications.py`, `CODEX_REPORT.md`) are still untracked in this snapshot because `git add -N ...` failed with `.../.git/worktrees/v3/index.lock` already present.
- Per task safety boundary, em không đụng `index.lock` ngoài worktree để ép lại git metadata.

## Limitations / Deferred Acceptance

- No live Telegram send was executed. Transport acceptance on a real gateway remains `DEFERRED`.
- No gateway/service restart was performed, per task safety boundary.
- Consent notification is intentionally passive on Telegram for master tasks; actual resolution still requires Hermes UI.
- Attempted cleanup of `.pytest_tmp` was blocked by tool policy despite targeting only task-generated workspace content; artifact cleanup remains `DEFERRED`.
- Attempted `git add -N gateway/master_task_notifications.py tests/test_master_task_notifications.py CODEX_REPORT.md` failed because `C:/Users/Vu Tuan/AppData/Local/hermes/hermes-agent/.git/worktrees/v3/index.lock` already existed; git-index enrichment for untracked-file diff stats remains `DEFERRED`.
