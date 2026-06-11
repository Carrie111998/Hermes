# Sandbox Lifecycle

Status: partial — a multi-backend execution environment abstraction (local,
Docker, Modal, Daytona, Singularity, SSH) with file sync and session init
exists; the product-level sandbox lifecycle verbs (`sandbox.create/attach/
sleep/wake/recycle/restore_artifacts`) and the no-static-keys credential
contract are spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/02-agent-runtime-contract.md`
  (§Sandbox Lifecycle, §Session Lifecycle, §Error Contract),
  `06-delivery-plan.md` (P2 items 1-2, P2 gates),
  `docs/hermes-tokenrouter-credential-flow.md` (§核心合约: sandbox token
  rules), local config writeup
  `/Users/lifcc/Desktop/code/work/infra/her/hermes-local-docker-sandbox.md`
  (outside the repo)
- Code (verified this session): `tools/environments/base.py`
  (`BaseEnvironment` ABC, `ProcessHandle`, `get_sandbox_dir`,
  `init_session`, `cleanup`, `_run_bash`), `tools/environments/local.py`,
  `tools/environments/docker.py`, `tools/environments/daytona.py`,
  `tools/environments/modal.py`, `tools/environments/managed_modal.py`,
  `tools/environments/modal_utils.py`, `tools/environments/singularity.py`,
  `tools/environments/ssh.py`, `tools/environments/file_sync.py`,
  `tools/terminal_tool.py`, `tools/process_registry.py`, `docker/`
  (`entrypoint.sh`, `main-wrapper.sh`, `s6-rc.d/`, `cont-init.d/`,
  `stage2-hook.sh`, `SOUL.md`), `docker-compose.yml`

## Purpose & Scope

"The sandbox is a task computer, not an implementation detail"
(`02-agent-runtime-contract.md` §Sandbox Lifecycle). It is where the agent
runs commands, builds artifacts, and accumulates task files, with a
lifecycle that outlives a single websocket connection.

Credential rule: "The sandbox must not hold static provider keys. It
receives a short-lived, restricted token such as `HF_JWT_TOKEN`, then
TokenRouter handles credential exchange and provider policy" (§Sandbox
Lifecycle; details in `17-tokenrouter.md`).

Scope: environment backends, the session-level lifecycle verbs, artifact
restore, resource limits, and the credential boundary. Task file semantics
are `06-files-task-file-browser.md`; what runs inside (tools/skills) is out
of scope here.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Environment abstraction: ABC with process handles, bash execution, temp dirs, session init and cleanup | `tools/environments/base.py` (`BaseEnvironment`, `ProcessHandle`, `init_session`, `cleanup`) |
| Implemented | Local backend (host execution with sandbox dir convention) | `tools/environments/local.py`, `base.py` (`get_sandbox_dir`) |
| Implemented | Docker backend (container-isolated execution) | `tools/environments/docker.py` |
| Implemented | Remote/managed backends: Modal, managed Modal, Daytona, Singularity, SSH | `tools/environments/modal.py`, `managed_modal.py`, `daytona.py`, `singularity.py`, `ssh.py` |
| Implemented | Host<->environment file sync | `tools/environments/file_sync.py` |
| Implemented | Container packaging with s6 supervision, init hooks, entrypoint | `docker/` (`entrypoint.sh`, `s6-rc.d/`, `cont-init.d/`, `stage2-hook.sh`), `docker-compose.yml` |
| Implemented | Process tracking across tool calls | `tools/process_registry.py`, `tools/terminal_tool.py` |
| Documented config (not in repo) | Local Docker sandbox hardening recipe (memory/network limits, secret hygiene) | `hermes-local-docker-sandbox.md` at the workspace root |
| Specified, not built | `sandbox.create / attach / sleep / wake / recycle / restore_artifacts` operations | `02-agent-runtime-contract.md` §Sandbox Lifecycle; no such verbs exist over the environments layer (rg for `sandbox.create|sandbox_attach` etc. — no hits) |
| Specified, not built | Sandbox id in session state (`active sandbox id, if attached`) | `02-agent-runtime-contract.md` §Session Lifecycle |
| Specified, not built | Short-lived token injection (`HF_JWT_TOKEN`) replacing static env keys | `hermes-tokenrouter-credential-flow.md` §核心合约; today provider keys are server-side env/config (e.g. `ATLAS_API_KEY` via `plugins/video_gen/atlas/client.py` `resolve_credentials`) — server-side, but static |
| Specified, not built | Sleep/wake with artifact preservation across recycles | `02-agent-runtime-contract.md`; `06-delivery-plan.md` P2 gate "Running jobs survive worker/session interruption" |

## User Entry Points

No direct user surface. Reached via:

- Agent tool execution: terminal/file tools run inside the active
  environment (implemented; backend chosen by config).
- Session attach: resuming a task should re-attach its sandbox or restore
  artifacts (planned; `07-tasks-session-history.md` Open Question 4).
- Inspector/live panel showing sandbox status for the current run
  (planned; `01-product-surface.md` lists sandbox/task filesystem in the
  product shape).
- Admin/config: backend selection and limits in deployment config
  (`docker-compose.yml`, cli config).

## Feature List

| Feature | Status |
|---|---|
| Pluggable execution backends behind one ABC | Implemented |
| Per-session working dir / sandbox dir convention | Implemented (`get_sandbox_dir`, cwd markers in `base.py`) |
| Streamed process output with poll/kill/wait handles | Implemented (`ProcessHandle`, `_ThreadedProcessHandle`) |
| File sync in/out of environments | Implemented (`file_sync.py`) |
| Container isolation with resource limits | Implemented capability (Docker backend + compose); limit policy is deployment config, not product-enforced |
| Named lifecycle: create/attach as explicit gateway operations | Planned |
| Sleep/wake (pause billing/resources, preserve state) | Planned |
| Recycle with `restore_artifacts` (rebuild env, restore task files) | Planned |
| Sandbox id tracked in session state and shown in inspector | Planned |
| Short-lived scoped tokens instead of static keys inside the sandbox | Planned (TokenRouter-dependent) |
| Browser context store within the sandbox boundary | Planned (P2 item 4 in `06-delivery-plan.md`; browser tooling exists — `tools/browser_*` — but context persistence as product state does not) |

## State Machine

Planned product lifecycle (`02-agent-runtime-contract.md` §Sandbox
Lifecycle):

```text
(none) -> created            sandbox.create
created -> attached          sandbox.attach (bound to session)
attached -> sleeping         sandbox.sleep (idle policy or explicit)
sleeping -> attached         sandbox.wake
attached|sleeping -> recycled  sandbox.recycle (env destroyed)
recycled -> attached         sandbox.restore_artifacts (new env, restored files)
any -> failed                backend error -> `sandbox_unavailable`
```

Implemented today: environments are created per run/session by the tool
layer (`init_session`) and torn down via `cleanup`; there is no sleep/wake
or restore — a destroyed environment's state survives only through synced
files.

Rules:

- Recycling must never silently lose task files: restore_artifacts is the
  defined path back.
- A media job must not depend on sandbox liveness — jobs are durable in the
  Media Job Service, sandboxes are disposable.
- Sleep/wake transitions must be invisible to a queued approval (approvals
  pause the agent, not the sandbox contract).

## APIs & Events

Implemented (internal): `BaseEnvironment` interface — construction with
cwd/timeout/env, `_run_bash` execution, `init_session`, `cleanup`, temp dir
access; backend selection by configuration; file sync helpers.

Planned (gateway operations, verbatim from the runtime contract):
`sandbox.create`, `sandbox.attach`, `sandbox.sleep`, `sandbox.wake`,
`sandbox.recycle`, `sandbox.restore_artifacts`.

Planned events: sandbox state changes surface via `status.update` (the
event stream has no dedicated sandbox event; phase changes are high-level
status per `02-agent-runtime-contract.md` §Event Stream).

Credential injection (planned): environment env-var surface carries only
`HF_JWT_TOKEN`-class scoped tokens; static provider keys remain outside
(see `17-tokenrouter.md` four-phase flow).

## Data Model

Implemented: in-process environment objects with cwd, timeout, env map;
JSON stores for cwd markers/state under the sandbox dir
(`base.py` `_load_json_store`/`_save_json_store`); process registry entries.

Planned:

```text
sandboxes
- sandbox_id
- session_id (current binding; nullable when sleeping)
- backend: local | docker | modal | daytona | singularity | ssh
- state: created | attached | sleeping | recycled | failed
- task_files_root
- resource_profile (cpu/mem/net limits)
- created_at, last_active_at

sandbox_artifacts (for restore)
- sandbox_id
- manifest of synced paths -> object storage keys
- captured_at
```

## UI Behavior

- The inspector/live panel shows the current sandbox state (backend,
  attached/sleeping, last activity) for the active session — sandbox as
  visible task computer, not hidden infra.
- Wake latency is shown honestly (a waking sandbox renders "waking", not a
  frozen spinner).
- Recycle/restore is an explicit, confirmable action when user-initiated;
  automatic recycles (idle policy) must be visible in the task timeline.
- Shared sessions never expose sandbox file contents or env to viewers
  ("Shared conversations do not imply shared sandbox or credentials",
  `05-memory-marketplace-files.md` §Access Control).

## Permissions & Error Handling

- Sandbox operations are session-owner actions; service accounts (e.g.
  schedulers) need explicit scope.
- Typed error: `sandbox_unavailable` (`02-agent-runtime-contract.md`
  §Error Contract) — surfaced when attach/create/wake fails; the agent must
  report it, not degrade to pretending execution happened.
- Credential boundary checks (post-TokenRouter): the environment's env map
  must contain no static provider keys — acceptance test greps the sandbox
  env and mounted files ("Sandbox environment and mounted files contain no
  real provider key", `hermes-tokenrouter-credential-flow.md`
  §MVP 验收检查).
- Resource exhaustion inside the sandbox (OOM, disk) must map to visible
  tool errors with the backend's evidence attached, never to silent
  truncation of outputs.

## Acceptance Criteria

- Switching backend config (local -> docker) changes execution isolation
  without changing tool behavior (the ABC contract holds).
- Files written in a session are present after environment teardown via
  sync, and (post-P2) after recycle via `restore_artifacts`.
- A resumed session either re-attaches its sandbox or restores artifacts —
  and tells the user which happened.
- Killing the environment mid-媒体-job does not kill the job (durability
  boundary holds once durable jobs exist).
- With TokenRouter integrated: no static provider key is readable from
  inside any sandbox backend; expired tokens fail closed.
- `sandbox_unavailable` reaches the UI as a typed, actionable error.

## Non-Goals

- Multi-tenant container orchestration (K8s scheduling, autoscaling) in
  P0-P2 — single-deployment backends only.
- Sandbox as a security boundary against the deployment operator (it
  protects providers/credentials and isolates task execution; it is not a
  hostile-multitenant jail in MVP).
- GUI/VNC desktop streaming (the local browser/desktop bridge is P2 item 5,
  specified separately).
- Owning task file product semantics (Files component) or job durability
  (Media Job Service).

## Open Questions

1. Default backend for the hosted product: Docker-per-session vs
   Modal/managed — cost and cold-start tradeoffs are undecided.
2. Sleep implementation per backend: Docker pause vs checkpoint vs
   teardown+restore; which backends can honestly support `sleep` vs only
   `recycle`?
3. Idle policy: who decides sleep (gateway timer? cost budget?) and what is
   the default TTL?
4. `restore_artifacts` scope: full task root vs manifest-selected paths;
   where are artifacts staged (object storage keys per the planned
   `sandbox_artifacts` table)?
5. One sandbox per session, or shareable across a project's sessions
   (the runtime contract binds by session; product phrasing "task computer"
   suggests per-task)?
6. How the existing `tools/environments` config surface maps to per-session
   product choices — is backend ever user-visible/selectable?
