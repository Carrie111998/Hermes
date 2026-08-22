# Codex Bridge Phase 1 Handoff

Status: accepted on 2026-08-22 for the authenticated local/API origin. The
feature remains disabled by default.

The architecture source of truth is
[`../CODEX-HERMES-MARROW-IMPLEMENTATION-PLAN.md`](../CODEX-HERMES-MARROW-IMPLEMENTATION-PLAN.md).
This file is the repo-local operational handoff for continuing work without
reconstructing Phase 1 from chat history.

## Shipped contract

```text
authenticated request
  -> Gateway capture
  -> one Codex thread
  -> compact public progress
  -> structured item/tool/requestUserInput boundary when needed
  -> durable prompt_id and needs_user state
  -> correlated reply after optional process restart
  -> resume the persisted Codex thread
  -> output_ready
  -> done with final result and workspace artifact paths
```

The bridge does not enter the Hermes `AIAgent` loop. It does not enqueue a
Kanban card, create a Hermes GPT worker, parse assistant prose for questions,
invent a user answer, or persist Codex reasoning.

Runtime contract:

- Python package: `openai-codex>=0.147,<0.149`.
- Accepted and live-tested version: `openai-codex==0.147.0` with its bundled
  `openai-codex-cli-bin==0.147.0` runtime.
- Canonical blocking request: `item/tool/requestUserInput` with
  `isBlocking=true`.
- Approval policy: deny all.
- Sandbox: `read-only` or `workspace-write`.
- Collaboration mode: `default` or `plan`.

## Phase 1-owned files

New files:

- `gateway/codex_bridge.py`
- `gateway/codex_bridge_http.py`
- `gateway/codex_bridge_local.py`
- `tests/gateway/test_codex_bridge.py`
- `tests/gateway/test_codex_bridge_http.py`
- `tests/gateway/test_codex_bridge_process_e2e.py`

Relevant edits in shared files:

- `gateway/run.py`: bridge mixin and authenticated pre-agent short circuit;
  legacy worker auto-dispatch gate.
- `gateway/platforms/api_server.py`: authenticated Codex task routes and
  capability advertisement.
- `hermes_cli/config_defaults.py`: default-off bridge and legacy-worker gates.
- `pyproject.toml` and `uv.lock`: optional pinned Codex SDK/runtime.
- `scripts/run_tests.sh`: forwards the two non-secret live-E2E opt-in flags
  through its hermetic environment.

Do not revert or reformat unrelated dirty files. In particular, the Kanban,
dashboard, plugin, website, and other CLI changes visible in the current
worktree are user-owned work outside this slice. The pre-existing untracked
`import-codex-auth.py` and `bin/` directory are not part of the Phase 1
acceptance surface.

Scoped `git diff --check` passes for every tracked Phase 1 file. A whole-tree
`git diff --check` currently reports pre-existing trailing whitespace in
`website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-stories.mdx`;
leave that unrelated dirty file untouched. Phase 1 source and test files are
still untracked until the user explicitly requests staging or a commit.

## Configuration

All behavioral settings belong in `config.yaml`; do not add them to `.env`.
The allowlist must be explicit and an empty allowlist fails closed.

```yaml
gateway:
  api_server:
    enabled: true
    host: 127.0.0.1
    port: 8642
    key: <strong-secret>

codex_bridge:
  enabled: true
  allowed_origins: [api_server]
  workspace_allowlist:
    - C:\\absolute\\approved\\workspace
  default_workspace: C:\\absolute\\approved\\workspace
  sandbox: read-only
  collaboration_mode: plan
  stale_recovery_seconds: 60

legacy_hermes_workers:
  auto_dispatch_enabled: false
```

Production rollout should begin with one workspace and keep the feature flag
off everywhere else.

## Authenticated HTTP surface

Every request requires `Authorization: Bearer ...` and
`X-Hermes-Session-Key`. Mutating calls also require `Idempotency-Key`.

- `POST /v1/codex/tasks` with `input` and `workspace` captures a task and
  returns `202` without holding the connection open for the Codex turn.
- `GET /v1/codex/tasks/{task_id}` returns the durable phase, public events,
  pending `prompt_id`/question, final result, and artifact paths.
- `POST /v1/codex/tasks/{task_id}/reply` with `prompt_id` and `answer` resumes
  the waiting thread.

Task reads and replies are bound to the authenticated origin conversation.
Prompt IDs are bound to their task. Request and reply idempotency collisions
with different payloads are rejected. Duplicate valid requests/replies do not
create a second execution.

## Persistence and recovery

The profile-aware database is
`<HERMES_HOME>/codex_bridge/state.db`. It stores task/thread/origin/workspace
mapping, compact public events, pending questions, correlated replies, final
results, artifact paths, process ownership, and request fingerprints. It does
not store private reasoning. Schema initialization includes additive migration
for Phase 1 columns.

At `needs_user`, the Codex SDK stream is stopped. A later valid reply claims the
durable reply once and resumes the stored thread ID. A stale process owner may
be recovered after `stale_recovery_seconds`; concurrent fresh owners cannot
both execute.

## Verification commands

Per repo policy, use `scripts/run_tests.sh`, not direct pytest. Use a distinct
repo-local `--basetemp` per invocation on Windows.

```bash
HERMES_CODEX_BRIDGE_LIVE_E2E=1 scripts/run_tests.sh \
  tests/gateway/test_codex_bridge.py -q \
  --basetemp=.pytest-phase1-live

scripts/run_tests.sh tests/gateway/test_codex_bridge_http.py -q \
  --basetemp=.pytest-phase1-http

HERMES_CODEX_BRIDGE_PROCESS_E2E=1 scripts/run_tests.sh \
  tests/gateway/test_codex_bridge_process_e2e.py -q \
  --basetemp=.pytest-phase1-process

scripts/run_tests.sh tests/test_packaging_metadata.py -q \
  --basetemp=.pytest-phase1-packaging
scripts/run_tests.sh tests/gateway/test_config.py -q \
  --basetemp=.pytest-phase1-config
scripts/run_tests.sh tests/gateway/test_api_server.py -q \
  --basetemp=.pytest-phase1-api
```

The live tests require an existing local Codex login/runtime. They must skip,
not fake E2E, when that capability is unavailable.

## Acceptance evidence

Latest acceptance run through the mandatory hermetic wrapper:

| Test file | Result |
| --- | ---: |
| `tests/gateway/test_codex_bridge.py` with live SDK flag | 17 passed |
| `tests/gateway/test_codex_bridge_http.py` | 2 passed |
| `tests/gateway/test_codex_bridge_process_e2e.py` with live process flag | 1 passed |
| `tests/test_packaging_metadata.py` | 7 passed |
| `tests/gateway/test_config.py` | 61 passed |
| `tests/gateway/test_api_server.py` | 109 passed |
| **Total** | **197 passed, 0 failed** |

Confirmed by automated behavior tests:

- Workspace allowlist and default-off feature gate.
- One executor for duplicate requests.
- Different-payload idempotency collision rejection.
- Compact 2-4 update boundary.
- Durable task/thread mapping and stale recovery.
- Structured question/options preservation and durable `prompt_id`.
- Same-origin validation and prompt/task binding.
- Reply consumption exactly once and duplicate-reply suppression.
- Final result and artifact persistence/delivery.
- No reasoning event persistence.

Confirmed with the real pinned runtime:

- SDK start and resume on the same Codex thread.
- App-server schema includes `item/tool/requestUserInput`.
- A real structured blocking request transitions to `needs_user` without a
  fabricated answer.
- Full Gateway process termination and restart between question and reply.
- Wrong-origin reply rejection, one-time correct reply, then
  `output_ready -> done`.
- No workspace mutation while stopped at the read-only question boundary.
- A separate workspace-write run returned a real changed-file artifact path.

## Deliberately not in Phase 1

- Telegram or other messaging-channel rollout.
- Kanban event/card projection.
- Marrow policy hooks or protected-action authorization.
- Multi-channel hardening, scheduler routing, and production SLO monitoring.

The smallest next action is a one-workspace Phase 1 pilot. Phase 2 may then
project these already-durable events into Kanban without putting Kanban on the
execution critical path.
