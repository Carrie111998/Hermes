# MCP OAuth Chunk 6 — Apple Keychain Backend

Status: design proposal (not yet implemented)
Depends on: versioned bundle store protocol (Chunk 5)
Platform: macOS

## Purpose

Provide a secure MCP OAuth storage backend available to every local Hermes Python surface: CLI, TUI, gateway, cron, Desktop-launched gateway, and background runtime. It must not depend on Electron `safeStorage`.

## Keychain item model

Store each complete versioned credential envelope as a generic-password item:

```text
Service: com.nousresearch.hermes.mcp-oauth.v1
Account: <identity-digest>
Label:   Hermes MCP OAuth — <server display name>
Secret:  UTF-8 JSON versioned bundle envelope
```

The stable service/account identity does not depend on Hermes application signing or install path. Profile isolation is encoded in the identity digest and validated inside the envelope.

## Backend interface

`AppleKeychainOAuthCredentialStore` implements the same contract suite as the file backend:

```python
load
create
compare_and_swap
replace_authorized
delete
administrative_lock
```

Cross-process administrative and mutation locks remain profile-scoped files under `HERMES_HOME/runtime/mcp-oauth-locks/`. They contain no secrets and coordinate Keychain callers consistently with the file backend.

## Access implementation

Initial implementation may invoke `/usr/bin/security` with argument arrays, no shell, bounded timeouts, and a minimal environment.

Secret-handling rule:

- Secret JSON must not appear in process arguments, logs, or exception text.
- If `security` cannot perform a required non-interactive write with secret input through stdin, implement that operation with Security.framework bindings instead of passing `-w <secret>`.

The backend adapter hides this choice from lifecycle callers.

## Operations

### Load

- Query exact service/account.
- Bound output size.
- Parse and validate the envelope.
- Verify identity, schema, and revision.
- Map missing item to `credential_not_found`.

### Create and replacement

- Hold mutation lock.
- Verify create/revision precondition.
- Replace the generic-password payload.
- Read back the exact item.
- Verify identity, revision, and complete bundle equality.
- Report uncertain write outcomes rather than assuming success.

### Delete

- Hold administrative and mutation locks.
- Delete exact service/account only.
- Report local deletion independently from optional remote revocation.

## Configuration

```yaml
mcp:
  oauth:
    credential_store: apple-keychain
```

`auto` on macOS selects the same backend. An unavailable, locked, denied, or interaction-required Keychain returns a typed error. There is no plaintext fallback after selection.

Missing configuration remains on compatibility file behavior until explicit migration policy changes it.

## Availability probe

The factory performs a non-destructive probe that distinguishes:

- Supported and accessible.
- Login Keychain locked.
- User interaction required.
- `security`/framework unavailable.
- Permission denied.
- Operation timeout.

The probe must not create a persistent test credential during ordinary startup. Platform integration tests use isolated test items.

## Headless and background behavior

Background callers use bounded subprocess/framework operations. If Keychain access would prompt or hang:

- Return `backend_locked` or `interaction_required`.
- Preserve existing Keychain item.
- Do not start browser authorization.
- Do not create file credentials.

User guidance directs the operator to unlock the login Keychain or select/migrate to an explicitly chosen backend.

## Desktop boundary

Desktop renderer and plugins never receive bundle data. The local Python gateway accesses Keychain directly. Desktop RPC carries only authorization progress and callback parameters.

Remote gateways use the backend configured on the remote host; Desktop's local Keychain is not copied to the remote machine.

## Tests

### Backend contract

Run in a unique service namespace or account prefix:

- Create/load/replace/delete.
- CAS conflict across processes.
- Identity isolation.
- Read-back verification failure.
- Locked/denied/timeout mapping.
- No file fallback.

Cleanup deletes only exact test items created by the run.

### Cross-surface integration

- Authenticate via CLI, consume via TUI.
- Authenticate via Desktop, consume via gateway.
- Refresh via cron, reload in a live runtime session.
- Restart Hermes and load without reauthorization.
- Locked Keychain produces actionable failure without token loss.

### Secret exposure

- Captured subprocess arguments contain no bundle or token.
- Logs and errors contain no secrets.
- Debug export excludes Keychain payloads.
- Renderer-facing RPC payloads contain no secrets.

## Demonstration

Configure `apple-keychain`, authorize a fake/local MCP through CLI, restart the gateway, and call the MCP from TUI. Verify:

- Keychain exact item exists.
- No `mcp-tokens/<server>.json` or plaintext bundle exists.
- Refresh persists to Keychain.
- A second process observes the new revision.

## Non-goals

- Do not use Electron `safeStorage`.
- Do not add Windows or Linux secure backends here.
- Do not silently migrate without verified destination write.
- Do not make Keychain contents model-visible.

## Completion criteria

- All local Hermes Python surfaces share Keychain credentials.
- Backend contract tests pass on macOS.
- Secure-backend failures are typed and fail closed.
- No secret is exposed through argv, logs, UI, or fallback files.
- Ordinary Hermes updates do not change Keychain item identity.
