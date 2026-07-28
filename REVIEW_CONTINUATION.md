# Review-findings continuation

The gateway worker-bridge watcher can create one bounded fix task after a
successful review or audit reports an actionable finding. This complements
failure successors: the review itself succeeded, but its result still requires
work.

## Explicit opt-in

Dispatchers opt a review task in through task metadata:

```json
{
  "continuation_kind": "review",
  "on_findings": {
    "enabled": true,
    "fix_objective_template": "Fix the verified review findings:\n{findings}"
  }
}
```

`on_findings.enabled: true` and a non-empty `fix_objective_template` are
mandatory. Review-like prose alone never enables automatic fixes. The template
may use `{findings}` and `{review_task_id}`; when `{findings}` is omitted, the
gateway appends the findings.

The task must also identify itself as a review through
`metadata.continuation_kind: review`, a review/audit-bearing `metadata.role` or
`metadata.type`, or an explicit `Finding:` / `Findings:` heading or label in
the objective or result summary.

## Finding selection and exclusions

The policy reads declared Markdown result artifacts (up to 1 MB each) and the
task result summary. A finding is actionable when its block contains an
explicit `classification: real_now` / `real_later`, or an explicit severity at
or above the configured threshold. Accepted severity forms include
`Severity: high`, `[high]`, and `high severity`.

The gateway records a log no-op and creates nothing when findings are only
theoretical or below threshold. It also excludes failed tasks, ambiguous tasks
without opt-in, unreadable/non-Markdown artifacts, reviews at the chain cap,
and any task marked `continuation_kind: fix`.

## Successor contract

Creation uses only the public `WorkerBridge.create_task` API. Exactly one child
is created with:

- `parent_task_id` set to the review task id;
- `metadata.continuation_kind: fix`;
- `metadata.source_review` set to the review task id;
- incremented `continuation_chain_depth` and `successor_chain_depth`;
- idempotency key `review-continuation:<review id>:<SHA-256 finding hash>`.

The copied `on_findings` signal is removed from the fix metadata. Existing fix
children are detected by `source_review`, so watcher replay cannot create a
second child even after that child reaches a terminal state.

## Configuration

```yaml
worker_bridge:
  review_continuation:
    enabled: true
    max_chain: 2
    require_severity: high
```

Defaults are shown above. `require_severity` accepts `low`, `medium`, `high`,
or `critical`. Explicit `real_now` / `real_later` classification remains
actionable independently of severity.

## Tests

`tests/gateway/test_review_continuation.py` covers high-severity continuation,
theoretical/low no-op, missing opt-in, replay idempotency, the chain cap,
fix-task exclusion, Markdown artifact extraction, configuration disablement,
and defaults.

Run:

```powershell
C:\Python314\python.exe -m pytest tests/gateway/test_review_continuation.py -q
```

## Rollback

Set `worker_bridge.review_continuation.enabled: false` for immediate runtime
rollback. Code rollback consists of removing the watcher call and
`gateway/review_continuation.py`; existing child tasks remain ordinary
worker-bridge tasks and are not deleted or altered.
