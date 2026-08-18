---
name: kanban-worker
description: Complete a Kanban worker run with metadata-only handoff and validated token usage.
---

# Kanban worker handoff

Use the existing Kanban completion path (`hermes kanban complete` or the
`kanban_complete` tool). Keep the human-readable summary useful but concise;
machine-readable facts belong in completion metadata.

## Four-lane worker contract

For a lane-bound swarm, the task card names the expected lane and preflight
skill (empty only for the native_hermes lane). A worker may complete only after its preflight succeeds and must use
this metadata shape:

```json
{
  "role": "worker",
  "root_id": "<swarm root id>",
  "lane_id": "native_hermes|claude|grok|agy",
  "preflight_skill_id": "<named skill>",
  "outcome": "completed",
  "verified_clean": true
}
```

The verifier must use `role="verifier"`, the matching `root_id`,
`gate="pass"`, and both `expected_lane_count` and `verified_lane_count` equal
to four. The synthesizer must use `role="synthesizer"`, the matching root,
`outcome="completed"`, and `result_present=true`.

The human-visible joke or other deliverable belongs in the existing task
`result` field. Do not copy it into metadata, audit events, dashboard
correlation fields, or Telegram delivery metadata. The swarm CLI syntax is
`PROFILE:TITLE[:SKILL,SKILL]`; the third segment is skill names only, so put
bounded instructions in the task title/body.

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
