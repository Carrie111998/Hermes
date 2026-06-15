# Christopher / TGG inter-session messaging ops

TGG is the first consumer of Hermes generic `inter_session`.

## Active v1 sessions

- `management` → `120363426509183563@g.us` (`TGG Christopher Mgmt Live Test 20260528`), `tgg_management`, external output normal. **TEST target for v1; flip to prod `120363407903158826@g.us` (`Christopher x TGG Management`) after testing.**
- `amk_ops` → `120363421424519051@g.us`, `tgg_ops_ingest`, external output never.
- `pg_ops` → `120363423568509280@g.us`, `tgg_ops_ingest`, external output never.
- `hg_ops` → `120363422582425366@g.us`, `tgg_ops_ingest`, external output never.
- `sk_ops` → `120363403845802098@g.us`, `tgg_ops_ingest`, external output never.

`christopher-pcl` and broadcast are intentionally out of v1.

## Expected flows

- ops → management: ops records facts, then `send_session_message(to="management", body="...")`. Management authors any WhatsApp-visible wording.
- management → specific ops: management calls `send_session_message(to="amk_ops"|"pg_ops"|"hg_ops"|"sk_ops", body="...")`. The ops session processes silently and can reply back to `management`.

`group_sessions_per_user: false` is set in the TGG profile so each configured WhatsApp group is one Hermes session instead of one session per sender.

## Diagnostics

On the Christopher host/profile:

```bash
sqlite3 ~/.hermes-christopher-tgg/state.db \
  "SELECT id, from_session_name, to_session_name, status, attempts, last_error FROM session_mailbox ORDER BY created_at DESC LIMIT 20;"
```

Rows stuck `pending` usually mean the target session is active or the watcher is not running. Rows `failed` carry `last_error`.

## Rollback

Set `inter_session.enabled: false` in `/home/pclaw/.hermes-christopher-tgg/config.yaml` and restart `christopher-tgg-hermes.service`. This disables the tool and watcher without deleting queued rows.
