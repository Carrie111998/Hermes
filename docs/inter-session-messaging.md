# Inter-session messaging

Hermes can expose a profile-scoped `inter_session` map so named sessions inside one runtime can send each other durable internal messages.

## Activation

Add a top-level config block:

```yaml
inter_session:
  enabled: true
  agent_id: my-agent
  sessions:
    management:
      label: "Management"
      role: management
      source:
        platform: whatsapp
        chat_type: group
        chat_id: "...@g.us"
        user_id: "system:inter-session"
        user_name: "Hermes"
      pa_job_type: management
      can_send_to: [ops]
      external_output: normal
    ops:
      label: "Ops"
      role: ops
      source:
        platform: whatsapp
        chat_type: group
        chat_id: "...@g.us"
      pa_job_type: ops
      can_send_to: [management]
      external_output: never
```

When enabled, configured sessions receive a compact `Current Hermes session` + peer map in prompt context every turn. The `send_session_message(to, body)` tool is exposed only when `inter_session.enabled` has configured sessions.

## Delivery lifecycle

1. Source session calls `send_session_message(to, body)`.
2. Hermes resolves the current configured session from the active gateway context.
3. Hermes enforces `can_send_to` and writes `state.db.session_mailbox` with `status='pending'`.
4. The gateway mailbox watcher claims pending rows and dispatches a synthetic `MessageEvent(internal=True)` into the target configured `SessionSource`.
5. The target session processes the message as a normal turn; the rendered input remains in its transcript.
6. If the target returns an external response and `external_output` is `normal`, the gateway sends it through the target platform adapter. If the adapter send fails or the adapter is not active, the row is marked `failed`. If `external_output` is `never`, no external send is attempted.

The watcher never calls `switch_session()` and does not rebind transcripts. If the target session is active, the row stays pending for a later watcher tick.

## Diagnostics

SQLite checks:

```sql
SELECT id, agent_id, from_session_name, to_session_name, status, attempts,
       created_at, claimed_at, delivered_at, failed_at, last_error
FROM session_mailbox
ORDER BY created_at DESC
LIMIT 20;

SELECT * FROM session_mailbox WHERE status IN ('pending','failed') ORDER BY created_at;
```

Runtime checks:

- confirm `inter_session.enabled: true` and `sessions` names match what the prompt shows.
- confirm the target session is not currently active if rows remain `pending`.
- inspect `last_error` for failed rows.
- confirm the target transcript contains the rendered `[Message from Hermes session: ...]` user input.

## Rollback

Set `inter_session.enabled: false` and restart the gateway. This hides the tool and stops the watcher from delivering new rows. Existing `session_mailbox` rows remain durable for later inspection or manual retry. The schema is additive; rollback does not require dropping the table.

## Out of scope for v1

- broadcast delivery.
- raw `session_id` addressing.
- transcript switching/rebinding.
- cross-runtime messaging.
