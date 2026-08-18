---
name: kanban-worker
description: Complete a Kanban worker run with metadata-only handoff and validated token usage.
---

# Kanban worker handoff

Use the existing Kanban completion path (`hermes kanban complete` or the
`kanban_complete` tool). Keep the human-readable summary useful but concise;
machine-readable facts belong in completion metadata.

## Token metadata contract

When the worker actually receives token usage from its provider, it may attach
this complete object under `token_usage`:

```json
{
  "schema": "hermes.worker.v1",
  "provider": "grok",
  "model": "grok-build-0.1",
  "input_tokens": 0,
  "output_tokens": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "total_tokens": 0,
  "estimated_cost_usd": null,
  "source": "worker_reported"
}
```

The required fields are `schema`, `provider`, `model`, `input_tokens`,
`output_tokens`, `total_tokens`, and `source`. Cache fields and cost are
optional. Token values must be non-negative integers; cost is finite,
non-negative USD. If any field is unavailable or fails local validation, omit
the whole `token_usage` object. Never invent, estimate, or partially emit
usage numbers.

Do not put credentials, prompt text, message bodies, raw provider responses,
or absolute sensitive paths in summaries or metadata. The summary timer
validates this contract again and ignores invalid records as a complete unit.
