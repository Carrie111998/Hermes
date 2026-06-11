# Execution Plane Design

Status: infrastructure design  
Scope: sandbox, browser, workspace volume, workflows, events, GPU/media workers.

## Objective

The execution plane turns a chat request into controlled work: tool calls, browser use, file operations, Atlas jobs, and long-running workflows. It must be recoverable and observable.

## Execution Lifecycle

```text
session.created
  -> prompt.accepted
  -> router.thinking
  -> missing_fields.requested OR workflow.selected
  -> tool.started
  -> job.created
  -> job.running
  -> asset.created
  -> qa.completed
  -> response.completed
```

Recommended user-visible states:

| State | Meaning |
|---|---|
| `idle` | no active run |
| `thinking` | agent/router is deciding |
| `clarifying` | waiting for required user input |
| `preparing_assets` | uploads, references, or roles are being resolved |
| `creating` | provider/media work has been submitted |
| `rendering` | video/image worker is running |
| `reviewing` | QA, preview, or safety check is running |
| `complete` | final artifact or answer is available |
| `blocked` | waiting on approval, quota, provider, or missing capability |
| `failed` | visible structured error |

## Sandbox Runtime

Recommended base from the architecture selection: Kata Containers for production cloud isolation, with E2B-style task-computer semantics as a product reference.

Sandbox responsibilities:

- run terminal/file tools in an isolated environment.
- mount project/session workspace.
- access only allowed network targets.
- receive only scoped Hermes tokens.
- emit stdout/stderr/tool events.
- publish produced files as assets through the control plane.

Sandbox non-responsibilities:

- no provider credentials.
- no direct object storage credentials except scoped upload/download tokens.
- no direct access to protected skill references.
- no cross-project browser context or workspace reuse.

## Workspace Volume

Recommended base: JuiceFS CSI or equivalent POSIX-on-object-storage design.

Mount layout:

```text
/workspace/input/        uploaded media and resolved references
/workspace/output/       generated files pending publish
/workspace/tmp/          session temp
/workspace/cache/        bounded tool cache
/workspace/public/       files explicitly published to asset library
```

Rules:

- session output is private until published.
- publish creates an asset row and object manifest.
- protected skill `references/` are not mounted into user sandbox.
- path traversal and symlink escapes are denied by mount policy and tests.

## Browser Context Service

Browser contexts are not generic files. They are credential-bearing state.

Required operations:

```text
browser_context.create(project_id, domains?, ttl?)
browser_context.attach(session_id, context_id)
browser_context.snapshot(context_id)
browser_context.revoke(context_id)
browser_context.delete(context_id)
```

Policy:

- context reuse requires explicit project ownership.
- authenticated context usage requires approval for high-risk domains/actions.
- screenshot, click, and keyboard events are logged as tool events.
- browser downloads enter the asset pipeline, not arbitrary sandbox files.

## Durable Orchestration

Recommended base: Temporal.

Temporal owns:

- session run workflow.
- media job group workflow.
- approval wait and timeout.
- retry and compensation.
- provider webhook reconciliation.
- failed worker recovery.

Do not use Temporal for every token delta. Token and UI deltas belong in the event bus.

Workflow shape:

```text
RunWorkflow
  - route request
  - resolve assets
  - maybe ask missing field
  - execute skill stage
  - submit media jobs
  - wait for job results or provider webhook
  - publish assets
  - run QA
  - finalize answer
```

## Realtime Event Bus

Recommended base: NATS JetStream.

Event naming:

```text
session.{session_id}.message.delta
session.{session_id}.message.complete
session.{session_id}.tool.started
session.{session_id}.tool.progress
session.{session_id}.tool.completed
session.{session_id}.job.created
session.{session_id}.job.progress
session.{session_id}.asset.created
session.{session_id}.approval.requested
session.{session_id}.error
```

Event contract:

- every event has `event_id`, `session_id`, `run_id`, `created_at`.
- every tool event has `tool_call_id`.
- every media event has `job_id` when applicable.
- UI reconnect uses cursor replay.
- event stream is not a substitute for durable DB projections.

## GPU / Media Workers

Recommended base: Kueue + NVIDIA GPU Operator for self-hosted GPU fabric; Atlas provider routes for external managed models.

Worker responsibilities:

- validate job envelope.
- call TokenRouter for provider access.
- submit Atlas image/video job.
- poll or receive webhook.
- download or register output asset.
- emit progress events.
- update job projection.

P0 worker types:

```text
image_generate_worker
image_edit_worker
video_generate_worker
asset_download_worker
qa_worker
```

## Failure Policy

| Failure | Required behavior |
|---|---|
| sandbox start timeout | structured `sandbox_unavailable` error and retry policy |
| browser context denied | visible approval or policy error |
| provider queue timeout | job stays recoverable; UI shows blocked/running, not frozen |
| event bus outage | durable workflow continues; UI reconnects from DB/event cursor |
| asset publish failure | final answer cannot claim artifact completion |
| worker crash | Temporal retries activity or marks recoverable failure |

## Validation Checks

- Kill a worker during a video job; workflow resumes or marks recoverable failure.
- Refresh browser during generation; UI replays events and job state.
- Attempt to mount Tenant A workspace from Tenant B session; deny.
- Submit tool call without scoped token; deny before provider call.
- Produce a final response without asset row; test must fail.

