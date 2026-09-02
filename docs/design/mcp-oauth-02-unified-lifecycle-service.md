# MCP OAuth Chunk 2 — Unified Lifecycle Service

Status: design proposal (not yet implemented)
Depends on: shared store facade (Chunk 1)
Delivery plan: [`../plans/2026-09-01-mcp-oauth-credential-store-delivery-plan.md`](../plans/2026-09-01-mcp-oauth-credential-store-delivery-plan.md)

## Purpose

Replace surface-owned authorization policy with one `OAuthLifecycleService`. CLI, dashboard, Desktop/TUI RPC, runtime refresh, reconnect, status, and explicit deletion use the same service and typed outcomes.

This chunk centralizes control flow but deliberately preserves legacy reauthorization persistence until Chunk 3 introduces staging.

## New modules

```text
tools/mcp_oauth_store/
├── lifecycle.py
├── interaction.py
├── diagnostics.py
└── sdk_adapter.py
```

## Lifecycle interface

```python
class OAuthLifecycleService:
    def load_for_runtime(self, identity, expected_issuer=None) -> StoredState | None: ...
    async def authorize(self, identity, oauth_config, interaction) -> AuthorizationResult: ...
    async def persist_refresh(self, identity, loaded, response) -> StoredState: ...
    def delete(self, identity, *, revoke_remote=False) -> DeletionResult: ...
    def status(self, identity) -> OAuthCredentialStatus: ...
```

In this chunk `StoredState` wraps legacy state. Later it becomes the revisioned bundle.

## Interaction abstraction

Authorization UI and callback transport implement:

```python
class OAuthInteraction(Protocol):
    async def publish_authorization_url(self, url: str) -> None: ...
    async def wait_for_callback(self, *, timeout: float) -> AuthorizationCodeResult: ...
    def report_progress(self, event: OAuthProgressEvent) -> None: ...
```

Concrete adapters:

- `LoopbackCLIInteraction`: browser open plus local callback waiter.
- `DashboardOAuthInteraction`: dashboard URL publication and HTTP callback delivery.
- `RPCOAuthInteraction`: Desktop local callback relay through TUI gateway RPC.

The interaction never receives stored tokens or client secrets.

## Authorization result

```python
@dataclass(frozen=True)
class AuthorizationResult:
    status: Literal["authorized", "cancelled", "failed"]
    tools: tuple[OAuthToolSummary, ...]
    credential_status: OAuthCredentialStatus
    error: MCPOAuthCredentialError | None
```

No caller infers success merely because MCP initialization or `tools/list` returned successfully. `authorized` requires a persisted token.

## Surface changes

### CLI

`_reauth_oauth_server` becomes presentation around `authorize`. It no longer owns provider construction, timeout policy, token verification, or error humanization.

### Dashboard

Dashboard flow storage retains only session/progress state. Its worker invokes `authorize` with `DashboardOAuthInteraction`.

### Desktop/TUI RPC

RPC start, callback, and poll methods manage transport session IDs and invoke the same service with `RPCOAuthInteraction`.

### Runtime and reconnect

Runtime loading and refresh call the service. Transport parking calls `MCPOAuthManager.evict`, which is defined as memory-only.

### Deletion

Only `OAuthLifecycleService.delete` invokes durable store deletion. Server configuration removal and remote token revocation are separate reported results.

## Provider manager boundary

`MCPOAuthManager` owns cached provider entries, in-flight 401 coordination, and provider reconstruction. It no longer exposes a method whose name `remove` ambiguously means both memory eviction and credential deletion.

Use explicit methods:

```python
manager.evict(identity)             # memory only
lifecycle.delete(identity)          # durable credentials
```

## Error translation

Protocol and backend errors become stable lifecycle codes before reaching surfaces. Presentation layers may add instructions but cannot choose destructive recovery.

Background contexts map interaction-required states to `reauthorization_required`; they never start a browser.

## Tests

- One table-driven authorization outcome suite runs through each interaction adapter.
- Success, cancellation, timeout, invalid state, registration error, token exchange error, and public unauthenticated MCP response yield identical lifecycle semantics.
- CLI, dashboard, and RPC surface tests verify presentation and transport only.
- Transient reconnect proves durable state remains present after `evict`.
- Explicit delete proves memory cache and durable state are handled separately.

## Demonstration

Run the same fake OAuth peer through CLI, dashboard, and RPC adapters. Show identical lifecycle event order and final credential status:

```text
authorization_url → callback → token_obtained → probe → authorized
```

For a timeout, show all three return `authorization_timeout` and the same safe guidance.

## Non-goals

- Do not yet eliminate the current snapshot/delete/restore internals.
- Do not add staged storage.
- Do not change the legacy file format.
- Do not add revision/CAS behavior.
- Do not add Keychain.

## Completion criteria

- Every surface calls the lifecycle service.
- Interaction adapters own no durable credential logic.
- Provider eviction and credential deletion are unambiguous and separate.
- Success always requires token persistence.
- Chunk 0 failure remains reproducible through the unified service, setting up Chunk 3.
