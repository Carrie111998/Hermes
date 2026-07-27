# Success-stage successors

The gateway worker-bridge watcher can create one bounded next-stage task after
an explicitly opted-in parent reaches `succeeded` or `accepted`. Successors are
created only after parent success, so the primary dispatcher cannot start a
dependent stage early.

## Explicit opt-in

The parent task must author the complete next-stage policy in metadata:

```json
{
  "on_success": {
    "enabled": true,
    "next": {
      "worker": "codex",
      "objective_template": "Implement plan {parent_task_id}.\n\n{summary}",
      "metadata": {},
      "workspace": {"repository": "C:/repo"},
      "priority": 50
    }
  }
}
```

`enabled: true`, a non-empty `worker`, and a non-empty
`objective_template` are mandatory. The only template fields interpreted are
`{parent_task_id}` and `{summary}`; summary text is capped at 4,000
characters. A plain successful task never chains.

`next.workspace` is optional and defaults to a copy of the parent workspace.
`next.metadata` is optional and may carry its own `on_success` or
`on_findings` policy for a deliberately authored later stage. Parent metadata
is not inherited, so a parent `on_success` cannot accidentally repeat.

## Successor contract

Creation uses only the public `WorkerBridge.create_task` API. Exactly one child
is created with:

- `parent_task_id` set to the successful parent task id;
- `metadata.source_stage` set to the parent task id;
- incremented `metadata.successor_chain_depth`;
- `metadata.continuation_kind: stage` unless the authored next-stage metadata
  identifies a more specific kind such as `review`;
- idempotency key `stage-successor:<parent task id>`.

Existing children are detected through `source_stage`, and the deterministic
idempotency key also protects the bridge API boundary against concurrent or
replayed creation.

## Configuration

```yaml
worker_bridge:
  stage_successors:
    enabled: true
    max_chain: 4
```

Defaults are shown above. `max_chain` is clamped to a non-negative integer and
uses the same `successor_chain_depth` field as failure successors and
review-findings continuations.

## Safety and compatibility

This policy only creates bridge tasks. It does not commit, push, deploy, bypass
approval/input gates, or dispatch work itself. Existing auto-dispatch and
failure-successor behavior is unchanged.

Existing tasks without `metadata.on_success.enabled: true` are unaffected.
Historical successful tasks that already carry that explicit opt-in are
eligible on the first watcher pass; idempotency limits that replay to one
child. Disable `worker_bridge.stage_successors` before restart if those tasks
must be triaged first.

A gateway restart is required to load the code wiring. No database migration
is required.

## Tests

Run:

```powershell
C:\Python314\python.exe -m pytest tests/gateway/test_stage_successors.py tests/gateway/test_success_pipeline_continuation_e2e.py -q
```

## Rollback

Set `worker_bridge.stage_successors.enabled: false` to stop new stage
successors on the next watcher cycle. Existing successors remain ordinary
worker-bridge tasks and require no database surgery.
