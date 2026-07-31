# Feishu conversation identity

Feishu exposes a top-level message root (`om_*`) and, after a thread exists,
a native delivery thread (`omt_*`). Hermes must not use one identifier for
both responsibilities.

- `SessionSource.conversation_id` is the stable model-session lane. A
  top-level message uses its own message ID; replies use the thread root ID.
- `SessionSource.thread_id` is the native platform delivery target only.
- Session keys, participant-sharing decisions, restart recovery, and resume
  scope checks use `conversation_id`, falling back to `thread_id` for adapters
  that expose only one identity.
- Outbound metadata and profile route matching continue to use `thread_id`.
- `sessions.conversation_id` persists the logical lane independently from the
  existing `sessions.thread_id` delivery field. Compression children inherit
  both.

All gateway key construction goes through the same config-aware helper so the
adapter guard, Feishu batching, SessionStore, and GatewayRunner agree on the
profile namespace before a turn enters the runner.
