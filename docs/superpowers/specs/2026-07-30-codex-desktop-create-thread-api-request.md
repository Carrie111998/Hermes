# Codex Desktop Project-Aware Idempotent Task Creation API Request

**Date:** 2026-07-30
**Owner:** Codex Desktop task-creation boundary
**Consumer:** Hermes Session Bridge sidebar broker
**Status:** Upstream API request; not implemented by the installed Desktop build

## Summary

Codex Desktop should extend its native `create_thread` operation so one call can
atomically:

1. create a task in an existing saved project;
2. start its app-server thread with an exact ordered set of runtime workspace
   roots;
3. persist the Desktop project association; and
4. return proof of the resulting placement.

The operation must be idempotent. A retry after an ambiguous transport result
must resolve to the original task rather than creating a duplicate.

This is a Desktop API change. It does not add Desktop-private project IDs to the
generic Rust app-server `ThreadStartParams`, `ThreadResumeParams`, or
`ThreadForkParams` contracts.

## Problem

The current Desktop tool accepts a prompt and a project target, but does not
accept:

- runtime workspace roots;
- a caller-owned idempotency key; or
- a placement-proof response containing project, cwd, and roots.

The bridge can therefore create a task in the `Session Inbox` project, but it
cannot atomically attach the exact Claude source directory or prove the final
runtime placement.

The attempted two-stage workaround is not acceptable. A live canary created one
project-bound Desktop task and then resumed the same app-server thread with
`runtimeWorkspaceRoots`. The response retained the task ID and cwd but returned
no roots, and a fresh `thread/read` exposed no root field. The bridge correctly
failed closed rather than enabling an unverifiable placement path. See
`docs/superpowers/canaries/2026-07-30-project-aware-resume-canary.md`.

## Requested request contract

Extend the Desktop-owned `create_thread` operation with two optional fields:

```json
{
  "prompt": "<registration prompt>",
  "target": {
    "type": "project",
    "projectId": "local-session-inbox",
    "environment": {
      "type": "local"
    }
  },
  "runtimeWorkspaceRoots": [
    "C:\\Users\\diego\\.hermes",
    "<exact Claude source cwd>"
  ],
  "idempotencyKey": "sidebar:<signed bridge identity>"
}
```

For Session Bridge delivery:

- `target.type` must be `project`;
- `target.environment.type` must be `local`;
- `target.projectId` is the exact saved `Session Inbox` project ID returned by
  Desktop project discovery;
- the first runtime root is the saved project's canonical primary folder;
- the second runtime root is the exact canonical Claude source cwd;
- `idempotencyKey` is derived from the signed bridge identity and is stable
  across retries of the same logical delivery.

The existing request remains backward compatible when the new fields are
omitted.

## Required success response

A ready result must return the exact created or replay-resolved task identity
and placement proof:

```json
{
  "threadId": "<exact task ID>",
  "hostId": "local",
  "projectId": "local-session-inbox",
  "cwd": "C:\\Users\\diego\\.hermes",
  "runtimeWorkspaceRoots": [
    "C:\\Users\\diego\\.hermes",
    "<exact Claude source cwd>"
  ],
  "idempotencyStatus": "created"
}
```

On a successful replay, the same response is returned with:

```json
{
  "idempotencyStatus": "replayed"
}
```

`threadId` must always be a ready, durable task ID. This contract must not
return only a `clientThreadId` for local project creation.

## Ownership and atomicity

Desktop owns the operation because it is the only layer that owns both:

- saved-project identity and project-to-thread assignment; and
- orchestration of the underlying app-server thread creation.

Desktop must perform the following logical transaction:

1. Validate the request before creating a thread:
   - the project exists;
   - the project is local;
   - the requested environment is supported;
   - the project has a canonical primary folder;
   - every runtime root is absolute and valid for the target host;
   - the first root equals the canonical project folder;
   - roots contain no duplicates after canonicalization;
   - the idempotency key is non-empty and within the documented size limit.
2. Reserve the idempotency key with a canonical request fingerprint.
3. Start one app-server thread using the project primary folder as `cwd` and
   the ordered runtime roots as runtime configuration.
4. Persist the Desktop project assignment for that exact thread ID.
5. Read back or otherwise prove the effective cwd and ordered roots.
6. Mark the idempotency record complete.
7. Return the placement-proof response.

Desktop must not report success until project assignment and placement proof
are durable. Intermediate state must not become a second user-visible task on
retry.

The durable idempotency record must survive Desktop restart and process crash.
An in-memory cache is insufficient.

## Idempotency semantics

The idempotency identity is scoped to the authenticated Desktop user and local
host.

### Same key and same arguments

A repeated request with the same key and the same canonical request fingerprint
must return the original task:

- same `threadId`;
- same `hostId`;
- same `projectId`;
- same `cwd`;
- same ordered runtime roots;
- `idempotencyStatus: "replayed"`.

It must not call app-server thread creation again.

### Same key and different arguments

A repeated key with any different semantic argument must fail deterministically
with an idempotency conflict. Differences include:

- prompt digest;
- project ID;
- environment;
- ordered canonical runtime roots;
- model or reasoning override, when supplied.

The response must identify the conflict without exposing the original prompt or
other sensitive payloads. It must not create or mutate a task.

### Ambiguous app-server result

If app-server may have created the thread but Desktop loses the immediate
response, Desktop must reconcile the reserved idempotency record against the
authoritative thread catalog before retrying creation.

The caller may safely replay the same Desktop request. Desktop either:

- returns the single reconciled task and completes the record; or
- returns a typed `creation_ambiguous` error while retaining the reservation
  for later reconciliation.

It must never replacement-create merely because the first response was
ambiguous.

### In-progress replay

If the same request arrives while its first execution is still in progress,
Desktop should wait for the bounded original result or return a typed
`idempotency_in_progress` response. The caller can retry the same key.

## Required errors

Errors should be typed and stable enough for broker recovery:

| Code | Meaning | Task created |
|---|---|---|
| `project_not_found` | Saved project ID is unknown | No |
| `project_host_mismatch` | Project is not on the requested host | No |
| `invalid_runtime_workspace_roots` | Roots are invalid, duplicated, or do not start with the project folder | No |
| `idempotency_conflict` | Same key was used with different arguments | No new task |
| `idempotency_in_progress` | Matching creation has not settled | No new task |
| `creation_ambiguous` | Desktop cannot yet prove whether the reserved creation completed | Never replacement-create |
| `placement_proof_failed` | Thread exists but effective project/cwd/roots could not be proven | No success response |

Errors after a task identity is known should include that exact `threadId` in a
structured recovery field while withholding a success result. This lets the
bridge retain the identity and prevents duplicate creation.

## Placement proof

The response fields are evidence, not request echo:

- `hostId` comes from the actual execution host;
- `projectId` comes from the persisted Desktop thread assignment;
- `cwd` comes from the effective app-server thread;
- `runtimeWorkspaceRoots` comes from the effective runtime configuration after
  canonicalization.

Desktop must preserve root order. The bridge depends on:

1. Session Inbox root;
2. exact Claude source cwd.

If the underlying app-server cannot expose effective runtime roots, Desktop
must add an internal proof mechanism before offering this API. Echoing the
request without verifying the applied configuration is not sufficient.

## Recents and discoverability

A successful response means the task is discoverable through Desktop's normal
thread inventory:

- assigned to the saved `Session Inbox` project;
- present in project Recents;
- present in global Recents;
- readable through the returned exact `threadId`.

The product should document its normal indexing bound. Session Bridge requires
visibility within five minutes and expects the ordinary path to complete within
seconds.

## Security and privacy

- `idempotencyKey` is opaque and must not be rendered in the conversation.
- Logs may record a hash of the key, never its raw value.
- Prompt content and source cwd remain sensitive telemetry.
- Project and path validation occurs before app-server dispatch.
- Runtime roots grant no permission beyond the selected permission profile;
  they only define the session's runtime workspace boundary.

## Upstream acceptance tests

### Contract tests

1. Existing calls without new fields retain current behavior.
2. A valid project-aware request returns exact project/cwd/root proof.
3. Root canonicalization is host-native and order-preserving.
4. Invalid or duplicate roots fail before thread creation.
5. A non-project target rejects `runtimeWorkspaceRoots` until explicitly
   supported by a separate contract.

### Idempotency tests

1. Same key and same arguments return the same task ID.
2. Same key and same arguments invoke app-server creation exactly once.
3. Same key and different arguments return `idempotency_conflict`.
4. The idempotency record survives Desktop restart.
5. Concurrent matching calls converge on one task.
6. App-server response loss followed by replay resolves to one task.
7. Unresolved ambiguity never triggers replacement creation.

### Atomicity and failure-injection tests

1. Invalid project creates no task.
2. App-server failure before identity creates no project assignment.
3. Failure after thread creation retains the exact identity for reconciliation.
4. Project-assignment persistence failure does not return success.
5. Runtime-root proof failure does not return success.
6. Crash at every transaction boundary converges to either:
   - no task and a retryable reservation; or
   - one fully placed task and a completed idempotency record.

### Product integration tests

1. The returned task appears in the target project and global Recents within the
   documented indexing bound.
2. A five-session burst creates exactly five tasks with five distinct keys.
3. Replaying that burst creates zero additional tasks.
4. Every task has the exact Session Inbox project, inbox cwd, and ordered
   inbox/source roots.
5. A Desktop restart between reservation and response still converges without
   duplication.

## Session Bridge adoption gate

Hermes must not enable continuous delivery against this contract until a
Desktop build exposes the new request and response fields and passes one live
canary.

The adoption canary must prove:

- exactly one Desktop call with one stable idempotency key;
- exact returned task ID;
- exact local host and Session Inbox project;
- exact `.hermes` cwd;
- exact ordered inbox/source runtime roots;
- one signed marker;
- readable continuation brief and last-five-message preview;
- project and global Recents visibility within five minutes;
- same-key replay returns the same task;
- no fork, replacement create, direct Codex state mutation, or ordinary user
  task interruption.

Until then, continuous sidebar delivery remains disabled and pending/retry work
must fail closed rather than creating unverifiable tasks.

## Current implementation evidence

The contract was assessed against:

- Codex Desktop build `26.721.11231.0`;
- Codex CLI/app-server `0.145.0`;
- the exposed Desktop `create_thread` tool schema;
- the public app-server `thread/start` and `thread/resume` documentation; and
- the live same-ID resume canary.

The installed Desktop build is a signed packaged application, not a writable
source checkout. Its exposed task-creation schema has no
`runtimeWorkspaceRoots` or `idempotencyKey`, and its ready response does not
promise project/cwd/root proof. The local Hermes repository therefore cannot
implement this Desktop-owned change without an upstream Desktop build.
