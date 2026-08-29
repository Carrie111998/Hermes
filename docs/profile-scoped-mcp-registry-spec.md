# Profile-Scoped MCP Registry

## Goal

Make native MCP discovery and runtime connections safe in a multiplexed Hermes gateway. Each profile must load its own MCP configuration, resolve its own secrets, maintain its own connection lifecycle, and expose only its own MCP tools. Existing single-profile behavior must remain compatible.

## Current failure

MCP state is process-global. The first profile that performs discovery wins the shared registry/config slot. Other profiles can reuse that server definition. Observed consequence: Carol attempted to resolve `AUTHENTIK_ZUG_APP_PASSWORD`, even though Carol's config names `AUTHENTIK_CAROL_APP_PASSWORD`; Anton and Jonathon can fail with 401 for the same reason.

The service-account provider already supports profile-scoped secret lookup, but that is insufficient when the wrong profile's server config is selected before credential resolution.

## Scope

Implement this on the existing profile-scoped service-account work in this worktree/PR. Do not create, update, push, or merge an upstream PR without James's explicit approval.

### Required isolation key

Use a canonical profile identity, preferably the resolved profile `HERMES_HOME` path. Do not use only the server name. All profile-scoped state must be keyed by `(profile_home, server_name)` or by an equivalent immutable profile registry object:

- Loaded MCP server configuration.
- Config fingerprint and lazy-server metadata.
- `MCPServerTask` / transport/session instances.
- Reconnect, cooldown, parked, circuit-breaker, and error state.
- Registered tool ownership and lookup.
- Service-account auth provider and token cache path.
- Shutdown and cleanup bookkeeping.

## Behavioral requirements

1. A profile loads `mcp_servers` from its own `config.yaml`, never from the launch/default profile.
2. A profile's MCP connection uses only that profile's secret scope and `HERMES_HOME`.
3. Two profiles may use the same logical server name (`toolhive`) without sharing configuration, credentials, tokens, sessions, or errors.
4. Tool registration must not let profile A call profile B's MCP tools. If the existing global model tool registry cannot represent per-profile toolsets, add a profile-aware dispatch boundary or deterministic profile namespace that preserves the current public tool names where possible.
5. A failed connection in profile A must not park, suppress, or block profile B's connection.
6. Reconnects and lazy first-use connects must retain the originating profile identity.
7. Profile switching, branching, session resume, background tasks, and MCP event-loop handoffs must preserve both profile home and secret scope.
8. Single-profile CLI/TUI operation and existing static-header/OAuth modes must remain compatible.
9. No credential values, bearer tokens, or secret mappings may be logged.
10. Existing cache files must remain mode `0600` and live below the owning profile home.

## Design constraints

- Prefer a per-profile registry/container over adding more process-global maps.
- If process-global coordination is unavoidable, keys must include canonical profile identity and all callers must pass it explicitly.
- Avoid mutating process-global `HERMES_HOME` or `os.environ` as a substitute for context propagation.
- Preserve prompt-cache stability and existing MCP reconnect/parking semantics.
- Keep the public MCP tool API compatible unless a profile namespace is required to prevent a security boundary violation.
- Do not silently fall back to another profile's configuration or environment.

## Implementation checklist

- Trace every call to `_load_mcp_config`, discovery, registration, lazy connect, reconnect, tool dispatch, and shutdown.
- Introduce a small profile context/identity type or equivalent explicit parameters.
- Replace each unscoped MCP global map with profile-keyed state or a profile registry.
- Ensure `_run_on_mcp_loop` propagates both home override and secret scope, plus the profile identity used for config/registry lookup.
- Pass the canonical profile home explicitly to `build_service_account_auth`.
- Ensure tool dispatch resolves the server/task for the current profile, not just the logical server name.
- Define cleanup behavior when a profile session closes or a config fingerprint changes.
- Add assertions/diagnostics that report profile identity and server name, but never secrets.

## Tests required

### Unit and regression tests

- Profile A and B with the same server name load different configs.
- Profile A cannot resolve or borrow profile B's password environment variable.
- Profile A and B have independent `MCPServerTask` instances.
- Profile A's failure state does not park profile B.
- Profile A and B have independent config fingerprints and lazy metadata.
- Profile A and B receive independent token cache paths.
- Tool lookup/dispatch from profile A cannot reach profile B's task.
- Reconnect preserves the original profile identity.
- Branch/resume/background task propagation preserves home and secret scope.
- Single-profile mode remains compatible.
- Existing OAuth and static-header MCP modes remain unchanged.

### Real-path integration test

Create two temporary profile homes with different `config.yaml` and `.env` values, using the same MCP server name. Run real imports and the actual discovery/connection path. Assert:

- Each profile sends its own credential to the test server.
- Each profile registers only its own tools.
- A failed profile does not affect the other.
- Cache files are created only beneath the corresponding profile homes.
- No credential values appear in captured logs.

### Live validation plan

After local tests pass, restart the live gateway only from a separate shell. Trigger one harmless MCP operation from each of `zug`, `anton`, `carol`, and `jonathon`. Verify each profile gets the expected tool set and cache path. This is validation only; do not alter cluster state.

## Acceptance criteria

- The four affected personas can authenticate to ToolHive concurrently from the live multiplexed gateway.
- Carol never attempts `AUTHENTIK_ZUG_APP_PASSWORD`.
- Jonathon no longer receives a 401 caused by another profile's config or cache.
- At least two same-named MCP servers can coexist in one process with distinct configs and sessions.
- Focused and full relevant tests pass.
- No upstream PR is created or modified without James's explicit approval.
- The final report identifies changed files, tests run, live validation results, and any remaining limitations.

## Existing PR Review Comments to Address

Also address the existing review feedback on PR #97628:

- Install profile secret scope on gateway startup discovery and alternate-profile dashboard probes.
- Preserve HTTP support, but prevent cross-origin credential redirects at the token endpoint; clarify the TLS/HTTP documentation.
- Make service-account cache filenames collision-resistant and namespaced separately from browser OAuth caches. Bind cached tokens to the relevant MCP/token URL, client ID, username, and scope; invalidate on identity changes.
- Complete the service-account CLI branch with explicit arguments and validation, or remove the unreachable branch and document the supported config/dashboard path.
- Use a strict dashboard service-account model with forbidden extra fields; persist only the explicit env-var-name contract.
- Cover short-lived tokens, refresh-token retention when refresh responses omit a replacement token, and delayed 401 responses racing with a concurrent refresh.
- Describe the current implementation accurately as Authentik-compatible rather than implying generic OAuth client-credentials support unless generic support is actually implemented.

Do not weaken the profile-isolation acceptance criteria while addressing these comments. Do not create or modify an upstream PR without James's explicit approval.
