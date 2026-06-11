# Agent Runtime Contract

Status: runtime specification  
Date: 2026-06-10

## Goal

Define what must happen between the web UI, Hermes gateway, sandbox, agent
runtime, tools, and long-running media jobs.

This contract separates "chat is alive" from "media job is running". A media job
may outlive a websocket reconnect, browser refresh, or worker restart.

## Runtime Shape

```text
Web UI
  -> Edge/Gateway
  -> session.create / session.resume
  -> prompt.submit
  -> Agent runtime
  -> skill router
  -> tool call
  -> media job event log
  -> worker/provider
  -> asset registration
  -> event fanout back to UI
```

## Session Lifecycle

Required methods:

- `session.create`: create a new conversation and run context.
- `session.resume`: restore messages, active jobs, selected assets, and task files.
- `prompt.submit`: submit user text and typed attachments.
- `slash.exec`: optional command path for explicit actions.

Session state must include:

- user/workspace/project ids
- model selection
- active skill profile
- active sandbox id, if attached
- active task files root
- active media jobs
- selected assets

## Event Stream

The UI should not poll the transcript only. It needs gateway events.

Required events:

| Event | Purpose |
|---|---|
| `message.start` | Assistant message began. |
| `message.delta` | Streaming text. |
| `message.complete` | Assistant turn complete. |
| `thinking.delta` | Optional reasoning/status text. |
| `status.update` | High-level phase change. |
| `tool.start` | Tool call began. |
| `tool.progress` | Tool status/progress. |
| `tool.complete` | Tool call finished. |
| `tool.error` | Tool failed with typed error. |
| `media_job.created` | Durable media job created. |
| `media_job.updated` | Job state changed. |
| `asset.ready` | Output asset registered and previewable. |
| `approval.requested` | User decision required. |
| `approval.resolved` | User approved/edited/rejected. |

## Sandbox Lifecycle

The sandbox is a task computer, not an implementation detail.

Required operations:

- `sandbox.create`
- `sandbox.attach`
- `sandbox.sleep`
- `sandbox.wake`
- `sandbox.recycle`
- `sandbox.restore_artifacts`

The sandbox must not hold static provider keys. It receives a short-lived,
restricted token such as `HF_JWT_TOKEN`, then TokenRouter handles credential
exchange and provider policy.

## Task Files

Each session may produce files that are not yet product assets:

- uploaded originals
- prompt JSON
- storyboard JSON/images
- generated scripts
- logs
- thumbnails
- intermediate frames
- final media

Task files become asset library entries only through explicit registration or
promotion.

## Human Approval Gateway

Approval is required for actions that spend money, expose private media, touch
logged-in accounts, run local commands, or publish externally.

Decision types:

- `approve`
- `edit`
- `reject`
- `respond`

The agent must pause and resume from durable state. A page refresh must not lose
the approval request.

## Error Contract

Errors must be typed:

- `missing_credential`
- `unsupported_model_capability`
- `invalid_asset_ref`
- `provider_rejected_input`
- `quota_exceeded`
- `job_timeout`
- `asset_upload_failed`
- `sandbox_unavailable`
- `approval_required`

Do not convert these into vague apologies. UI and agent both need the typed
error so they can show recovery actions.

## Acceptance

- Refreshing the browser during a media job does not lose the job.
- A resumed session shows active media jobs and their current states.
- A failed provider request has a visible provider error and retry path.
- The agent cannot claim completion without an event, artifact, or ledger record.

