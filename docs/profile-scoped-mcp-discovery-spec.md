# Profile-Scoped MCP Discovery: Investigation and Development

## Objective

Make multiplexed Hermes MCP discovery select and retain the correct profile configuration. The live gateway may remain on the canary during development, but this work must not interrupt, restart, or mutate the active main session.

## Observed failure

The profile-scoped MCP registry work passes local tests, including a two-profile synthetic integration test, but the live multiplexed gateway still parks ToolHive with HTTP 401 for multiple personas. Carol reported:

```text
MCP service-account 'toolhive': environment variable 'AUTHENTIK_ZUG_APP_PASSWORD' is not set or is empty
```

Carol's own config names `AUTHENTIK_CAROL_APP_PASSWORD`. Direct Authentik token exchange succeeds for Anton and Jonathon, and fresh tokens authenticate directly to ToolHive. Therefore the remaining failure is config/discovery selection, not the credential or ToolHive endpoint.

The likely remaining process-global coordinator is in `hermes_cli/mcp_startup.py`:

```python
_mcp_discovery_started = False
```

`tools/mcp_tool.py` also has `_load_mcp_config()` and discovery/registration entry points that must be traced. `tui_gateway/entry.py` documents a known limitation that the first profile building an agent wins the MCP discovery slot. The new `tools/mcp_profile.py` scopes runtime state, but startup discovery may still be one-shot and profile-global.

## Constraints

- Work in the current Hermes worktree and build on commit `02f39e700` plus its uncommitted/follow-up changes.
- Do not reset, checkout another worktree, or discard existing changes.
- Do not restart or stop any service, especially `hermes-gateway.service`.
- Do not change the live gateway configuration or cluster.
- Do not push, create, update, or merge any upstream or Forgejo PR without James's explicit approval.
- Do not add Claude attribution or Co-Authored-By trailers.
- Do not print credentials, tokens, secret values, or full `.env` contents.
- Preserve the active main Matrix session: no gateway restart, no shared SQLite/state mutation, and no process-global environment mutation.

## Phase 1: Investigation

Trace and document the complete production path for a profile-specific agent build:

1. Profile/session selection and `profile_home` binding.
2. `hermes_cli.mcp_startup.start_background_mcp_discovery` and `_mcp_discovery_started` behavior.
3. `tui_gateway.entry.ensure_mcp_discovery_started` and the agent-build path.
4. Context propagation into discovery threads and the dedicated MCP event loop.
5. `tools.mcp_tool._load_mcp_config()` and environment interpolation.
6. `_discover_all`, `_discover_and_register_server`, `_connect_server`, and `MCPServerTask` construction.
7. Native global tool registry registration and tool dispatch.
8. Lazy reconnect/first-use behavior after a parked initial connection.

For every process-global variable or one-shot gate, classify it as:

- Must become profile-scoped.
- Can remain process-global coordination only if it carries explicit profile identity.
- Must be split into a process-wide coordinator plus per-profile work state.

Build a tight reproduction that uses two temporary profile homes with the same server name (`toolhive`) but different `password_env` values, then exercises the actual startup/discovery entry point—not only direct helper calls. The reproduction must fail before the fix if the startup coordinator collapses profiles.

## Phase 2: Implementation

Implement the smallest architecture that fixes the demonstrated root cause:

- Each profile can initiate discovery independently, or a shared coordinator must queue independent profile jobs keyed by canonical profile home.
- Discovery must load config from the selected profile home.
- Each discovery job must carry profile home, profile identity, secret scope, and registry ownership across thread/event-loop boundaries.
- A failure/park/cooldown for profile A must not suppress profile B.
- Repeated calls for the same profile must deduplicate safely.
- Shutdown must close all profile discovery tasks without orphaning them.
- Existing single-profile CLI/TUI behavior remains compatible.
- Existing OAuth, static-header, stdio, lazy, reconnect, and service-account behavior remains compatible.
- The process-global model tool registry must not permit profile A to dispatch to profile B's MCP task. If public tool names collide, use an explicit profile-aware dispatch boundary or fail closed; never silently choose the first profile.
- Do not solve this by unioning all profile secrets into `os.environ`.

If the investigation proves the current public tool registry cannot support simultaneous same-name tools safely, document the limitation and implement a fail-closed behavior plus a clear migration path rather than pretending isolation is complete.

## Required tests

Add or update tests for:

1. Two profiles with the same server name enter the real startup discovery entry point independently.
2. Startup discovery for profile A reads A's config; profile B reads B's config.
3. Profile A and B use different `password_env` names and cannot borrow one another's secret.
4. The one-shot discovery gate does not suppress a second profile.
5. Concurrent discovery jobs retain their own profile scope and home.
6. Repeated discovery for one profile deduplicates without affecting another.
7. A parked/failed A does not park or suppress B.
8. Lazy first-use and reconnect retain originating profile identity.
9. Tool dispatch cannot cross profile boundaries.
10. Shutdown cleans up all profile registries/tasks.
11. Single-profile startup remains compatible.
12. Existing service-account/OAuth/header/stdio tests pass.

Use real imports and temporary profile homes for integration coverage. Treat `RuntimeWarning` as an error and ensure no unawaited coroutine warnings remain.

## Validation commands

Run at minimum:

```bash
uv run pytest -W error::RuntimeWarning -q \
  tests/tools/test_mcp_service_account.py \
  tests/tools/test_mcp_profile_registry.py \
  tests/agent/test_secret_scope.py \
  tests/gateway/test_multiplex_adapter_session_key_namespace.py \
  tests/gateway/test_multiplex_background_task_scope.py \
  tests/test_tui_mcp_late_refresh.py \
  tests/tui_gateway/test_slash_worker_mcp_discovery.py

uv run python -m compileall -q tools/mcp_profile.py tools/mcp_tool.py tools/mcp_service_account.py

git diff --check
```

Run additional targeted tests created for the startup coordinator. Report exact counts and failures.

## Completion report

Report:

- Root cause with exact file/function references.
- Process-global state retained, split, or made profile-scoped.
- Files changed.
- Tests added/changed.
- Exact commands and results.
- Any remaining model-tool-registry or live-gateway limitations.
- Explicit statement that no service was restarted, no cluster changed, no push occurred, and no PR was created or modified.
