# MCP OAuth Chunk 1 — Shared Credential Store Facade

Status: design proposal (not yet implemented)
Depends on: Chunk 0 behavioral harness
Delivery plan: [`../plans/2026-09-01-mcp-oauth-credential-store-delivery-plan.md`](../plans/2026-09-01-mcp-oauth-credential-store-delivery-plan.md)

## Purpose

Introduce one backend-neutral persistence API while preserving the existing `mcp-tokens/` layout and runtime behavior. This is an ownership refactor, not yet the rollback fix or storage-format migration.

At completion, callers no longer know how token, client, and metadata records are stored. The compatibility backend remains responsible for the existing files.

## Package slice

Create the initial package:

```text
tools/mcp_oauth_store/
├── __init__.py
├── models.py
├── errors.py
├── base.py
├── factory.py
└── legacy_file_backend.py
```

Later chunks add lifecycle, staging, versioned bundles, Keychain, and migration modules.

## Domain objects

`OAuthIdentity` contains canonical profile home, server name, and normalized MCP URL. `LegacyOAuthState` represents the current optional token/client/metadata records without pretending they are atomically coherent.

```python
@dataclass(frozen=True)
class OAuthIdentity:
    profile_home: Path
    server_name: str
    server_url: str


@dataclass(frozen=True)
class LegacyOAuthState:
    tokens: OAuthToken | None
    client: OAuthClientInformationFull | None
    metadata: OAuthMetadata | None
    cimd_rejected: bool = False
```

This transitional model must not become the final bundle schema.

## Store interface for this chunk

```python
class OAuthCredentialStore(Protocol):
    backend_name: str

    def load_state(self, identity: OAuthIdentity) -> LegacyOAuthState: ...
    def set_tokens(self, identity: OAuthIdentity, tokens: OAuthToken) -> None: ...
    def set_client(self, identity: OAuthIdentity, client: ClientInfo) -> None: ...
    def set_metadata(self, identity: OAuthIdentity, metadata: OAuthMetadata) -> None: ...
    def mark_cimd_rejected(self, identity: OAuthIdentity) -> None: ...
    def delete(self, identity: OAuthIdentity) -> bool: ...
```

The interface temporarily permits independent record writes to maintain compatibility. These methods are removed or made private when Chunk 5 introduces coherent bundles.

## Legacy backend

`LegacyFileOAuthCredentialStore` ports the current safe filename, JSON validation, absolute-expiry compatibility, permissions, atomic writes, and CIMD marker behavior from `HermesTokenStorage`.

Paths remain:

```text
HERMES_HOME/mcp-tokens/<safe-server>.json
HERMES_HOME/mcp-tokens/<safe-server>.client.json
HERMES_HOME/mcp-tokens/<safe-server>.meta.json
HERMES_HOME/mcp-tokens/<safe-server>.cimd-off
```

Profile resolution uses the identity's explicit canonical home or `get_hermes_home()` at the factory boundary. No module-level path may capture the wrong profile.

## Factory

The initial factory always returns the legacy file backend. It still reads the future configuration location so later backend addition does not require surface rewiring.

```python
def get_oauth_credential_store(
    *, hermes_home: Path | None = None,
) -> OAuthCredentialStore: ...
```

Unknown configured backend values return a typed `backend_unavailable` error; they do not silently choose files.

## Call-site migration

Replace direct durable operations in:

- `tools/mcp_oauth.py` storage callbacks.
- `tools/mcp_oauth_manager.py` durable-state checks and disk watching.
- `hermes_cli/mcp_config.py` token presence, login, and remove paths.
- Dashboard MCP OAuth code.
- TUI gateway MCP OAuth session code.
- Startup and diagnostics that inspect `mcp-tokens/`.

During this chunk, `HermesTokenStorage` may remain as an MCP SDK adapter, but it delegates every durable operation to the store. It no longer constructs paths itself.

## Typed errors

Introduce stable safe codes:

- `credential_not_found`
- `backend_unavailable`
- `backend_timeout`
- `credential_corrupt`
- `identity_mismatch`
- `deletion_failed`

Errors contain safe identity and backend context, never token payloads.

## Contract tests

Parameterize tests over the store factory:

- Token/client/metadata round trip.
- Missing optional records.
- CIMD marker round trip.
- Explicit deletion removes every legacy artifact.
- Profile isolation.
- Server-name path traversal resistance.
- POSIX directory and file permissions.
- Corrupt record reporting without automatic deletion.
- Atomic replacement of each compatibility record.

## Demonstration

1. Seed legacy files under a temporary profile.
2. Load the MCP provider through production startup code.
3. Refresh a fake token and observe the same legacy file update.
4. Load from a second process/profile-aware entry point.
5. Show that no storage format changed and no reauthorization occurred.

## Non-goals

- Do not add staged reauthorization.
- Do not fix destructive rollback by adding new rollback rules.
- Do not introduce bundle revisions or CAS.
- Do not add Keychain.
- Do not migrate user files.

## Completion criteria

- Production callers use the store facade for durable OAuth state.
- `HermesTokenStorage` is an adapter, not a persistence implementation.
- Existing integrations and files remain compatible.
- Backend contract tests pass with isolated profiles.
- The Chunk 0 harness still demonstrates the baseline failure.
