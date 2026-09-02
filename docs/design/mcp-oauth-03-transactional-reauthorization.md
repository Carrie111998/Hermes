# MCP OAuth Chunk 3 — Transactional Reauthorization

Status: design proposal (not yet implemented)
Depends on: unified lifecycle service (Chunk 2)
Fix target: GitHub issue #76590

## Purpose

Eliminate failed-reauthorization credential loss by running fresh OAuth flows against isolated in-memory staged state. The active credential remains unchanged until a complete replacement is validated and committed.

This chunk removes rollback rather than attempting to make rollback infer which files belong to which concurrent flow.

## Staged adapter

Add `StagedOAuthStorageAdapter`, implementing the asynchronous storage contract expected by the MCP SDK.

```python
class StagedOAuthStorageAdapter:
    def __init__(self, *, seed_client=None, seed_metadata=None): ...
    async def get_tokens(self) -> None: ...
    async def set_tokens(self, tokens) -> None: ...
    async def get_client_info(self): ...
    async def set_client_info(self, client) -> None: ...
    def load_oauth_metadata(self): ...
    def save_oauth_metadata(self, metadata) -> None: ...
    def build_validated_state(self, identity) -> LegacyOAuthState: ...
```

`get_tokens()` always returns `None` at flow start. Client and metadata may be seeded from active validated state to reuse a working dynamic registration and correct token endpoint. New SDK writes remain in the adapter.

No staged secret is written to disk or Keychain.

## Administrative lock

Introduce a cross-process per-profile/per-server lock under:

```text
HERMES_HOME/runtime/mcp-oauth-locks/<identity-digest>.admin.lock
```

The lifecycle service acquires it before loading seed state and holds it through authorization commit or abort. A competing explicit authorization, migration, or deletion returns `reauthorization_in_progress` after a short bounded wait.

Runtime reads remain available. Runtime refresh concurrency is fully revision-safe in Chunk 4; during this transitional chunk, the lifecycle service rechecks active token state immediately before commit and logs a safe warning if it changed.

## Flow

```text
1. Acquire administrative lock.
2. Load active state for seed client/metadata.
3. Construct staged adapter with no token.
4. Construct a non-cached OAuth provider using the staged adapter.
5. Run discovery, registration, browser callback, and token exchange.
6. Require a staged access token.
7. Probe authenticated MCP behavior.
8. Validate staged identity/client/metadata/token coherence.
9. Commit staged state through the compatibility backend.
10. Evict the cached runtime provider.
11. Release the lock.
```

The compatibility backend in this chunk still writes separate legacy records. Commit orders them to minimize inconsistency and occurs only after successful authorization. Chunk 5 replaces this with one atomic bundle.

## Failure behavior

Any exception or cancellation before commit:

- Discards the staged adapter.
- Leaves active files untouched.
- Releases callback/listener resources.
- Releases the administrative lock.
- Returns a typed lifecycle error.

The following APIs are removed from authorization paths:

- Pre-flow durable `manager.remove`.
- OAuth state snapshot for reauthorization.
- `restore(..., only_if_absent=True)`.
- Unconditional snapshot restore.

Compatibility snapshot helpers may remain only for unrelated migration code until Chunk 7.

## Provider construction seam

Refactor provider construction so the lifecycle service can supply an explicit storage adapter without inserting the staged provider into the runtime manager cache.

```python
def build_oauth_provider(
    identity,
    server_url,
    oauth_config,
    storage: OAuthStorageAdapter,
    interaction: OAuthInteraction,
) -> OAuthClientProvider: ...
```

The runtime manager calls the same builder with `ActiveOAuthStorageAdapter`.

## Commit validation

Commit requires:

- Non-empty access token.
- Supported token type.
- Valid expiry if supplied.
- Expected issuer/resource identity.
- Coherent client authentication method.
- Valid callback state and redirect URI.
- Successful configured authentication probe.

A public MCP server that never challenges the request does not count as OAuth-authorized without a staged token.

## Tests

Reuse the Chunk 0 failure matrix and invert the invariant:

- Failure after metadata discovery preserves active state.
- Failure after client registration preserves active state.
- Callback cancellation/timeout preserves active state.
- Token exchange failure preserves active state.
- Probe failure after token exchange preserves active state.
- Successful flow replaces state and evicts provider cache.
- Two CLI/dashboard/RPC reauthorization attempts serialize.
- Explicit deletion cannot enter while reauthorization holds the lock.
- Process termination before commit leaves active credentials usable.

## Demonstration

```text
OLD active bundle
  ├── staged client=PARTIAL
  ├── staged metadata=PARTIAL
  └── injected failure

Result: OLD active bundle unchanged
```

Then complete the same flow and show the active credentials change to `NEW` only after the authenticated probe succeeds.

## Non-goals

- Do not add the final versioned bundle format.
- Do not add Keychain.
- Do not solve stale refresh writes except for a pre-commit transitional check; Chunk 4 owns CAS.
- Do not retain rollback as a secondary mechanism.

## Completion criteria

- The Chunk 0 destructive scenarios all preserve active credentials.
- All three interactive surfaces use staged authorization.
- No pre-flow durable delete exists.
- Aborted flows create no durable partial client or metadata record.
- Issue #76590's failed-reauthorization path is demonstrably fixed.
