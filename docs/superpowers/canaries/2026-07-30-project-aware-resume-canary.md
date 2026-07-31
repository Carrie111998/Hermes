# Project-Aware Same-ID Resume Canary

**Date:** 2026-07-30 America/New_York / 2026-07-31 UTC
**Outcome:** Failed closed at runtime-root verification
**Continuous sidebar delivery:** Disabled throughout

## Purpose

Test the approved two-stage placement hypothesis:

1. create one Codex task in the saved local `Session Inbox` project;
2. bind the exact returned task ID;
3. resume that same ID through Codex app-server with the inbox and Claude
   source directories as ordered runtime workspace roots; and
4. prove that project placement, cwd, roots, readable preview, and identity all
   survive.

No production source session was used for the canary.

## Disposable source

- Source session: `claude:5ac88d9a-cab9-499f-b830-8356b39362d6`
- Source cwd:
  `C:\Users\diego\Developer\session-bridge-canaries\project-aware-20260730`
- Initial source timestamp: `2026-07-31T00:12:27.849Z`
- Final source message timestamp: `2026-07-31T00:23:53.015Z`
- Source message count at capture: 12
- Preview source cursor offset: 62314
- Preview digest:
  `d4fe5c22bd909ab2c07f40d9594f52ad32fe16549b7455d04067d0c3a81efbc6`

The Claude account was at its weekly usage limit, so the five chronological
user/assistant exchanges consist of five unique canary prompts and Claude's
deterministic account-limit responses. This still gives an unambiguous
last-five ordering check without using or modifying a production transcript.

## Queue and Desktop creation

- Sidebar job queued: `2026-07-31T00:24:17.705108Z`
- Session Inbox project ID:
  `local-e59c279a6cdda9313cf111e46a80b027`
- Session Inbox cwd: `C:\Users\diego\.hermes`
- Desktop create target: saved project, local environment
- Exact returned task ID:
  `019fb591-09fc-78e3-80e0-6a621116f456`
- Desktop task creation timestamp: `2026-07-31T00:26:49Z`
- Exact ID was durably bound before any app-server mutation.

Desktop proof before resume:

- exact task ID matched;
- host was `local`;
- project ID matched the Session Inbox;
- cwd was `C:\Users\diego\.hermes`;
- the registration turn completed and became idle;
- the registration prompt contained exactly one signed bridge marker;
- the continuation brief and exact last-five preview were present.

The task appeared in the global thread inventory with the Session Inbox project
ID and `.hermes` cwd approximately 12 seconds after creation, within the
five-minute bound.

## Same-ID app-server resume

The one-shot Codex 0.145.0 app-server client initialized with
`experimentalApi=true` and dispatched exactly one mutation:

```json
{
  "threadId": "019fb591-09fc-78e3-80e0-6a621116f456",
  "cwd": "C:\\Users\\diego\\.hermes",
  "runtimeWorkspaceRoots": [
    "C:\\Users\\diego\\.hermes",
    "C:\\Users\\diego\\Developer\\session-bridge-canaries\\project-aware-20260730"
  ]
}
```

The definite `thread/resume` response proved:

```json
{
  "thread_id": "019fb591-09fc-78e3-80e0-6a621116f456",
  "cwd": "C:\\Users\\diego\\.hermes",
  "runtime_workspace_roots": null,
  "status": {"type": "idle"},
  "turn_count": 1
}
```

A new process then performed one read-only `thread/read` for the exact same ID.
Its thread object exposed `cwd` but no runtime-root or workspace-root field.
The returned thread keys were:

```text
agentNickname, agentRole, canAcceptDirectInput, cliVersion, createdAt, cwd,
ephemeral, extra, forkedFromId, gitInfo, historyMode, id, modelProvider, name,
parentThreadId, path, preview, recencyAt, sessionId, source, status,
threadSource, turns, updatedAt
```

Desktop inventory after resume still proved:

- task ID unchanged;
- project ID still matched the Session Inbox;
- cwd still matched `C:\Users\diego\.hermes`;
- task remained idle.

## Acceptance matrix

| Check | Result |
|---|---|
| Exactly one Desktop create | Pass |
| Exact returned ID bound before mutation | Pass |
| Session Inbox project before resume | Pass |
| `.hermes` cwd before resume | Pass |
| Same task ID after resume | Pass |
| Session Inbox project after resume | Pass |
| `.hermes` cwd after resume | Pass |
| Exact ordered runtime roots proven | **Fail** |
| Exactly one signed marker | Pass |
| Continuation brief and last-five preview | Pass |
| Global/project inventory within five minutes | Pass |
| Ordinary working task not used as broker target | Pass |
| No fork or replacement create | Pass |

## Settlement

Because ordered runtime-root persistence could not be proven, the exact bound
job was settled as `placement_mismatch` at
`2026-07-31T00:28:39.643468Z`. The authenticated disposable task was archived
by its exact ID. No replacement task was created.

The approved architecture gate therefore failed. Tasks 3 and later must not be
implemented against this assumption, and continuous sidebar delivery must
remain disabled until the creation substrate is redesigned and approved.
