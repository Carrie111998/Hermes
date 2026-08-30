# Live Profile-Scoped MCP Discovery: Investigation and Fix

## Mission

Fix the remaining production-path failure in the profile-scoped MCP work. The v3 build is running in the live Hermes gateway, but a fresh Jonathon Matrix session had no ToolHive MCP tool exposed. Do not declare the feature complete until a fresh profile session exposes the registered ToolHive tools or the exact live connection failure is visible and correctly profile-scoped.

## Current evidence

- Live systemd service is running:
  `/home/wynnj/.hermes/versions/hermes-agent-profile-mcp-auth-v3/.venv/bin/python -m hermes_cli.main gateway run`
- Source imports in that process resolve to the v3 worktree.
- The live gateway was restarted onto commit `f8d704fc1`.
- Local focused tests pass: `158 passed` with `-W error::RuntimeWarning`.
- Fresh Jonathon session was stored in `/home/wynnj/.hermes/profiles/jonathon/state.db` with `profile_name=jonathon` and a new session id.
- Jonathon config contains `mcp_servers.toolhive`, HTTPS URL, `auth: service_account`, and profile-specific `password_env`.
- A direct read-only v3 probe under Jonathon's `HERMES_HOME` reports:

```json
[{"name":"toolhive","transport":"http","tools":0,"connected":false,"disabled":false,"status":"configured"}]
```

- The fresh Jonathon model response said no ToolHive tool was exposed and no connection error was returned.
- The system journal contains no ToolHive discovery/401 line for that fresh v3 session.
- Jonathon's old `cache/mcp_schema_cache.json` contains ToolHive schemas, but its related tool-discovery cache references the prior v2 install path. Treat this as evidence of stale cache state, not proof of root cause.
- Earlier v2 live tests did reach ToolHive and parked it with HTTP 401. That is a separate symptom from the v3 fresh-session result where no discovery/error appears.

## Hard constraints

- Do not restart, stop, reload, or otherwise mutate `hermes-gateway.service`.
- Do not change live systemd drop-ins, live profile config, profile credentials, cluster state, or external services.
- Do not clear, delete, or rewrite live caches/state as part of investigation. Use temporary copies or read-only probes.
- Do not print credentials, tokens, `.env` contents, authorization headers, or secret values. Environment variable names are acceptable; values are not.
- Do not push, open, modify, or merge a Forgejo/GitHub PR without James's explicit approval.
- Do not reset or discard existing worktree changes.
- Do not add Claude attribution or Co-Authored-By trailers.
- Preserve the active main Matrix session and all unrelated profiles.

## Phase 1: Reproduce the exact live seam locally

Create a tight, red-capable regression test using real imports and temporary profile homes. It must exercise the same path as the Matrix gateway, not only direct `mcp_tool` helpers:

1. Construct a session through the gateway profile/session creation path with `profile_name=jonathon` or an equivalent profile selector.
2. Verify the in-memory session dict has the expected `profile_home` before `_start_agent_build` runs.
3. Run `_start_agent_build` in its normal deferred-build thread.
4. Observe `HERMES_HOME`, secret scope, startup coordinator key, MCP config loading, status, registration, and the model tool snapshot at each boundary.
5. Use a fake MCP HTTP server or deterministic mocked transport that records the profile/config identity, returns a valid initialize/tools response, and does not require real credentials.
6. Assert that the resulting agent exposes the fake server tool. Assert the failure mode seen live if the test is red before the fix.

The test must distinguish these hypotheses:

- H1: `profile_home` is absent or wrong at agent build time.
- H2: startup discovery runs under the wrong `HERMES_HOME`/secret scope.
- H3: discovery completes but model tool definitions are snapshotted before registration.
- H4: stale cache paths or cache fingerprints suppress registration or load incompatible data.
- H5: process-global native tool registry or MCP profile registry drops the profile's registration.
- H6: the live Matrix profile session uses a different build path than the tested TUI/CLI path.

Record the observation that falsifies each hypothesis. Do not patch until the failing seam is identified.

## Phase 2: Trace all relevant code

Read and trace completely enough to cite exact functions/lines in:

- `tui_gateway/methods_session.py` profile resolution and session creation.
- `tui_gateway/server.py` `_start_agent_build`, `_make_agent`, `_load_cfg`, profile-home binding, and agent tool snapshot construction.
- `hermes_cli/mcp_startup.py` per-profile coordinator, `_has_configured_mcp_servers`, `start_background_mcp_discovery`, wait/join/retry behavior, and scope replay.
- `tui_gateway/entry.py` `ensure_mcp_discovery_started`, `wait_for_mcp_discovery`, and late refresh.
- `tools/mcp_tool.py` config loading, status, registration, schema cache, `MCPServerTask`, and profile dispatch.
- `tools/mcp_profile.py` profile registry ownership and context propagation.
- Any model/tool registry code that snapshots tools into `AIAgent`.

For every boundary record:

- Current profile name and resolved profile home.
- Current `get_hermes_home()` result.
- Discovery coordinator key and thread.
- MCP status and number of registered tools.
- Cache path/fingerprint, without exposing secret data.
- Point at which tool definitions enter the AIAgent.

Use temporary diagnostic logging or test hooks only; remove temporary logging before completion.

## Phase 3: Implement the smallest root-cause fix

If the evidence confirms a bug, implement the smallest fix that:

- Causes the real Matrix/deferred gateway path to discover the selected profile's MCP config.
- Keeps `profile_home`, secret scope, and profile registry identity together across every thread/event-loop boundary.
- Ensures discovery finishes or schedules a safe late refresh before the first model turn's tool snapshot.
- Invalidates or namespaces caches by source/install/config fingerprint where needed; never use stale schemas as live connection state.
- Never places profile secrets in process-global `os.environ`.
- Does not make a failed profile suppress or poison another profile.
- Preserves single-profile CLI/TUI behavior.
- Preserves prompt-cache invariants: no mid-conversation system prompt/toolset mutation. Late refresh is allowed only before the first model turn, or must use the existing safe mechanism.
- Does not silently fall back to another profile's MCP config or credentials.
- Fails closed with a precise non-secret error if profile identity cannot be established.

If the model's process-global tool registry cannot safely expose same-name MCP tools for simultaneous profiles, implement an explicit profile-aware dispatch/snapshot boundary or fail closed. Do not claim isolation merely because runtime tasks are separate.

## Required tests

Add/update tests for:

1. Exact deferred gateway session creation -> agent build -> MCP discovery path.
2. Profile home is present and correct before discovery and agent construction.
3. Two profiles with same server name and distinct configs discover independently through that path.
4. Fake MCP tool appears in the constructed agent tool snapshot.
5. Discovery failure is visible as a precise status/error rather than silent MCP absence.
6. Stale v2 cache data cannot suppress a v3 discovery or tool registration.
7. Schema cache and connection status are separated correctly.
8. A parked/failed profile does not affect another profile.
9. Reconnect/lazy first-use preserves originating profile identity.
10. Existing startup coordinator, service-account, profile-registry, secret-scope, multiplex, lazy/late-refresh, and slash-worker tests remain green.
11. No `RuntimeWarning` or unawaited coroutine warning.

Tests must avoid real production credentials and external state. Use temp homes and deterministic fakes for new regression coverage.

## Validation

Run the targeted regression first, then:

```bash
uv run pytest -W error::RuntimeWarning -q \
  tests/hermes_cli/test_mcp_startup_profile_scope.py \
  tests/tools/test_mcp_service_account.py \
  tests/tools/test_mcp_profile_registry.py \
  tests/agent/test_secret_scope.py \
  tests/gateway/test_multiplex_adapter_session_key_namespace.py \
  tests/gateway/test_multiplex_background_task_scope.py \
  tests/test_tui_mcp_late_refresh.py \
  tests/tui_gateway/test_slash_worker_mcp_discovery.py \
  tests/test_tui_gateway_server.py

uv run python -m compileall -q hermes_cli/mcp_startup.py tui_gateway/entry.py \
  tui_gateway/server.py tui_gateway/methods_session.py tools/mcp_profile.py \
  tools/mcp_tool.py tools/mcp_service_account.py

git diff --check
```

Run a final read-only probe against a temporary copy of a profile home to show the resulting status and cache paths without secrets. Do not run `hermes mcp test` against production; that command has previously obscured the actual gateway path.

### Known-red, pre-existing, unrelated to this branch

`tests/test_tui_gateway_server.py::test_model_options_preserves_canonical_custom_row_after_agent_init` fails on any developer machine that has an active Claude Code login. It is **not** caused by the profile-scoped MCP work and must not be "fixed" as part of it.

Root cause — the test is not hermetic against ambient host credentials:

- `hermes_cli/inventory.py:753` — `_filter_explicit_provider_rows` keeps an `anthropic` row when `_anthropic_oauth_credentials_present()` is true, *before* it consults `is_provider_explicitly_configured`.
- `hermes_cli/inventory.py:670` → `agent/anthropic_adapter.py:1088` — that probe reads `Path.home() / ".claude" / ".credentials.json"`.
- `tests/conftest.py:477-486` — `_hermetic_environment` redirects `HERMES_HOME` but **deliberately does not redirect `HOME`** (subprocess tests depend on a stable `HOME`). So the real `~/.claude/.credentials.json` is visible inside the test.
- The test stubs `hermes_cli.auth.is_provider_explicitly_configured` but never stubs `_anthropic_oauth_credentials_present`, so the `anthropic` row survives the explicit-only filter and `canonical_order` sorts it ahead of the custom row: got `['anthropic', 'custom:local-ollama']`, expected `['custom:local-ollama']`.

Evidence:

- Bisected to `4ec57d56a` (2026-08-22, `fix(inventory): keep Anthropic OAuth logins visible in desktop pickers`), which added the OAuth bypass branch. On this host: `4ec57d56a^` → **passed**; `4ec57d56a` → **failed**. The test itself dates from `9fed768b5` (2026-07-22) and was never updated for the new branch.
- Pristine `f8d704fc1` with **zero** working-tree changes, checked out in a separate detached worktree: same single failure, `1 failed, 615 passed` — byte-identical to the result with this branch's changes applied.
- Stubbing only `hermes_cli.inventory._anthropic_oauth_credentials_present` → `False` makes the test pass unchanged, isolating the ambient credential as the sole cause.
- The sibling suites added with `4ec57d56a` (`tests/hermes_cli/test_inventory.py:263,294`) do stub that probe and are green, which is why CI — with no Claude Code login — never sees this.

Fixing it belongs to whoever owns `4ec57d56a`: stub `_anthropic_oauth_credentials_present` in the TUI-gateway test the same way `test_inventory.py` already does. Out of scope here.

## Completion report

Report exactly:

- Confirmed root cause, with file/function references and evidence.
- Why the earlier local two-profile tests passed while the live path failed.
- Files changed and design rationale.
- Tests added/changed and exact results.
- Cache behavior and any remaining tool-registry limitations.
- Explicit confirmation that no service was restarted, no live config/cache/cluster was mutated, no credentials were exposed, and no PR was created or modified.
