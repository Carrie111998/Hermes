# MCP OAuth Chunk 5 — Versioned Atomic File Bundles

Status: design proposal (not yet implemented)
Depends on: revision-safe refresh (Chunk 4)
Supersedes: independent live token/client/metadata files for the `file` backend

## Purpose

Replace independently written legacy OAuth artifacts with one versioned, atomically replaced credential bundle. Readers must observe a coherent old or new state, never a token paired with unrelated client or metadata.

## Final file layout

```text
HERMES_HOME/mcp-credentials/
├── v1/
│   └── <identity-digest>.json
└── index.v1.json
```

The bundle file is authoritative. The optional index contains only non-secret diagnostic mapping and is rebuildable.

The identity digest is SHA-256 over a canonical, length-delimited encoding of profile identity, server name, and normalized server URL. It prevents path traversal and ambiguous concatenation.

## Envelope schema

```json
{
  "schema_version": 1,
  "revision": "random-128-bit-hex",
  "bundle": {
    "identity": {
      "profile_id": "...",
      "server_name": "todoist",
      "server_url": "https://ai.todoist.net/mcp"
    },
    "issuer": "https://todoist.com",
    "protected_resource": {},
    "authorization_server": {},
    "client": {},
    "tokens": {
      "access_token": "...",
      "refresh_token": "...",
      "token_type": "Bearer",
      "scopes": [],
      "accepted_at_utc": "...",
      "expires_at": "..."
    },
    "created_at": "...",
    "updated_at": "...",
    "cimd_rejected": false
  }
}
```

Pydantic or equivalent typed validation enforces field types, URL/issuer identity, maximum sizes, and schema version before use.

## Backend API

Replace compatibility record methods with bundle operations:

```python
class FileOAuthCredentialStore:
    def load(identity) -> StoredBundle | None: ...
    def create(identity, bundle) -> StoredBundle: ...
    def compare_and_swap(identity, expected_revision, bundle) -> StoredBundle: ...
    def replace_authorized(identity, bundle) -> StoredBundle: ...
    def delete(identity) -> bool: ...
```

`replace_authorized` requires the administrative lock. `compare_and_swap` takes the mutation lock and verifies revision.

## Atomic write protocol

Under the mutation lock:

1. Load and validate the current envelope when revision checking is required.
2. Serialize the complete new envelope in memory.
3. Create a random same-directory temporary file with `O_EXCL`, mode `0600`.
4. Write, flush, and `fsync` the temporary file.
5. Atomically replace the destination with `os.replace`.
6. `fsync` the parent directory on POSIX where supported.
7. Remove any leftover temporary file after failure.

Directory mode is `0700`; `secure_parent_dir` must preserve existing repository security invariants.

## Read protocol

Known-identity reads require no directory scan:

1. Resolve identity digest.
2. Read a bounded payload.
3. Decode JSON.
4. Validate schema and identity.
5. Validate issuer binding against runtime expectation.
6. Return immutable bundle plus revision.

Corrupt or unsupported bundles produce typed errors and are not deleted or overwritten automatically.

## Revision watching

Replace token-file mtime watching with revision watching:

- Cached provider entry remembers the loaded revision.
- Before an auth-sensitive request, the active adapter loads the current revision or uses a backend revision probe.
- Revision change evicts/rebuilds the provider.
- Mtime may optimize the file backend but is never the semantic revision.

## Legacy compatibility in this chunk

On load, if no bundle exists but legacy files do, invoke the migration reader to construct a validated in-memory bundle. The backend may write the new bundle only through the verified migration procedure.

During the rollout window, legacy files are not updated after a new bundle becomes authoritative. A non-secret migration marker prevents ambiguous dual writes.

## Index design

`index.v1.json` is optional and contains:

- Identity digest.
- Profile display identifier.
- Server display name.
- Schema version.
- Backend-visible status timestamp.

It contains no URL query credentials, tokens, client secrets, authorization codes, or metadata response bodies. Diagnostics can enumerate configured MCP servers without relying on the index.

## Crash and concurrency tests

- Reader loop during replacement sees only complete envelopes.
- Crash before `os.replace` preserves old destination.
- Crash after `os.replace` exposes valid new destination.
- Two CAS writers from one revision yield one winner.
- Delete versus CAS is serialized by mutation lock.
- Temporary files have `0600` from creation.
- Directory has `0700`.
- Parent `fsync` is attempted on supported POSIX systems.
- Unsupported schema and corrupt payload remain intact for diagnosis.
- Profile and server identity mismatches fail closed.

## Demonstration

Continuously load and validate a credential in one process while another performs hundreds of revisions. Record that every observed token, client ID, issuer, metadata endpoint, and revision belongs to one known complete generation.

## Non-goals

- Do not add encryption to the file backend; it is owner-only plaintext by explicit selection.
- Do not add Apple Keychain in this chunk.
- Do not silently resolve conflicting legacy and bundle credentials.
- Do not support arbitrary user-provided bundle paths.

## Completion criteria

- File backend uses one authoritative bundle per identity.
- CAS and explicit replacement are atomic.
- Provider invalidation uses revisions.
- Existing valid legacy credentials remain usable through verified migration.
- Independent live OAuth record writes are no longer used by the file backend.
