# MCP OAuth Chunk 7 — Migration, Diagnostics, and Cleanup

Status: design proposal (not yet implemented)
Depends on: file bundle and Apple Keychain backends (Chunks 5–6)
Finalizes: shared OAuth credential-store rollout

## Purpose

Safely move valid legacy credentials into the configured backend, expose non-secret operational diagnostics, and remove obsolete persistence and rollback paths after every production caller uses the shared architecture.

## Migration sources

Per configured MCP server and active profile:

```text
HERMES_HOME/mcp-tokens/<server>.json
HERMES_HOME/mcp-tokens/<server>.client.json
HERMES_HOME/mcp-tokens/<server>.meta.json
HERMES_HOME/mcp-tokens/<server>.cimd-off
```

Migration never scans unrelated profiles. Server enumeration comes from profile configuration plus exact known legacy mappings.

## Migration commands

Proposed CLI:

```bash
hermes mcp credentials status [server]
hermes mcp credentials migrate --to file [server]
hermes mcp credentials migrate --to apple-keychain [server]
hermes mcp credentials resolve-conflict <server> --keep destination|legacy|reauthorize
```

Names may align with existing CLI conventions, but operations remain CLI/lifecycle functions rather than new model-visible tools.

## Migration algorithm

Under the credential's administrative lock:

```text
1. Read all legacy artifacts without modifying them.
2. Validate token, client, metadata, MCP URL, and issuer relationships.
3. Convert relative expiry using legacy mtime only through documented compatibility rules.
4. Construct the versioned bundle.
5. Load destination.
6. Resolve empty/equivalent/conflicting destination state.
7. Write destination.
8. Read back and validate complete equality.
9. Record non-secret migration completion.
10. Securely remove legacy secret-bearing artifacts.
```

Migration completion is idempotent. A crash before verified destination write leaves legacy files untouched. A crash after verification but before cleanup is resumed by recognizing an equivalent destination bundle.

## Conflict policy

Destination and legacy states are:

- `destination_empty`: migrate automatically after validation.
- `equivalent`: verify destination and finish legacy cleanup.
- `conflicting`: stop with `migration_conflict`.
- `legacy_invalid`: preserve artifacts, report corruption, require reauthorization or explicit cleanup.
- `destination_invalid`: fail closed; do not overwrite automatically.

Timestamps never choose a winner. Explicit resolution options:

- Keep destination, then remove legacy.
- Replace destination with validated legacy.
- Reauthorize into destination, then remove legacy.

## Cleanup semantics

After destination read-back verification, remove exact legacy files. Because portable secure deletion cannot be guaranteed on modern filesystems, documentation must say "remove" rather than promise physical overwriting.

No broad glob or recursive delete is used. Cleanup validates the exact profile token directory and sanitized server artifact names.

## Diagnostic projection

```python
@dataclass(frozen=True)
class OAuthCredentialStatus:
    server_name: str
    profile_display: str
    backend: str
    present: bool
    expiration_state: str
    expires_at: datetime | None
    refreshable: bool
    issuer_binding: str
    revision_prefix: str | None
    migration_state: str
    reauthorization_state: str
    error_code: str | None
```

Diagnostics never include access token, refresh token, client secret, authorization code, full revision, or raw provider response.

Example:

```text
Server: todoist
Profile: default
Backend: apple-keychain
Credential: present
Expiration: refresh_due
Expires: 2026-09-01T18:30:00Z
Refreshable: yes
Issuer binding: valid
Migration: complete
Reauthorization: idle
```

## Automatic startup behavior

- Missing backend setting: continue legacy-compatible behavior; report migration availability.
- Configured destination empty with valid legacy state: run verified migration according to configuration policy.
- Conflict: use configured valid destination, report conflict, do not delete either state.
- Locked secure backend: fail closed without using legacy plaintext as an implicit fallback.
- Background contexts never prompt for migration conflict resolution.

## Production cleanup

After call-site audit and migration coverage, remove:

- Durable path logic from `HermesTokenStorage`.
- Snapshot/restore APIs used for reauthorization.
- Manager methods that combine eviction and durable deletion.
- Surface-specific OAuth persistence and rollback.
- Token-file mtime semantic watching.
- Direct legacy artifact reads outside migration/compatibility modules.
- Documentation claiming MCP OAuth tokens always live in `mcp-tokens/`.

Keep a time-bounded legacy reader only if the supported upgrade window requires it. Mark it with an explicit removal release/migration version rather than leaving permanent dual behavior.

## Downgrade behavior

Hermes never silently exports Keychain credentials to plaintext for an older release. Downgrade options are:

- Explicitly migrate to the file backend with a security warning.
- Reauthorize using the older release.

Migration metadata remains non-secret and allows newer Hermes to diagnose prior backend selection after reinstall or rollback.

## Tests

- Every valid combination of legacy token/client/metadata.
- Legacy refresh response without refresh token.
- Corrupt legacy JSON and identity mismatch.
- Destination write/read-back failure preserves legacy.
- Crash/resume before and after destination verification.
- Equivalent destination completes cleanup.
- Conflicting destination requires explicit choice.
- Exact deletion targets only selected server/profile.
- Locked Keychain does not fall back to legacy file at runtime.
- Diagnostics redact all secrets.
- Repository behavioral tests prove no non-migration production path reads legacy files.

## Demonstration

```text
legacy token files
    │
    ├── migrate to Keychain
    ├── read-back verify
    ├── remove exact legacy files
    └── restart CLI/TUI/gateway without reauthorization
```

Also demonstrate a conflicting destination remains untouched and produces actionable status without exposing credential values.

## Non-goals

- Do not auto-select between conflicting valid credentials.
- Do not promise forensic secure erasure.
- Do not add model-visible credential tools.
- Do not retain permanent dual-write compatibility.

## Completion criteria

- Migration is verified, idempotent, profile-safe, and resumable.
- Diagnostics are useful without exposing secrets.
- Keychain-to-plaintext downgrade is explicit.
- Direct legacy storage manipulation is confined to migration/temporary compatibility code.
- All obsolete rollback and ambiguous durable-delete APIs are removed.
