# MCP OAuth Chunk 4 — Expiration and Refresh Concurrency

Status: design proposal (not yet implemented)
Depends on: transactional reauthorization (Chunk 3)
Architecture sections: expiration policy, refresh flow, and concurrency model

## Purpose

Make access-token expiration and refresh deterministic across restarts and safe across concurrent Hermes processes. A stale refresher must not overwrite credentials produced by a newer refresh or reauthorization.

## Token time model

Extend the token record with:

```python
@dataclass(frozen=True)
class OAuthTokenRecord:
    access_token: str
    refresh_token: str | None
    token_type: str
    scopes: tuple[str, ...]
    accepted_at_utc: datetime
    expires_at: datetime | None
```

When a response supplies `expires_in`:

```text
expires_at = accepted_at_utc + expires_in
```

Both timestamps are persisted. UTC wall time determines validity across restarts. Monotonic time measures in-process waits, request deadlines, and retry delays.

## Expiration classification

```python
class TokenExpirationState(Enum):
    VALID = "valid"
    REFRESH_DUE = "refresh_due"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
```

For known expiration:

```text
token_lifetime = expires_at - accepted_at_utc
refresh_window = min(60 seconds, token_lifetime × 10%)
refresh_due_at = expires_at - refresh_window
```

- Before `refresh_due_at`: `valid`.
- At/after `refresh_due_at` but before `expires_at`: `refresh_due`.
- At/after `expires_at`: `expired`.
- No trustworthy expiry: `unknown`.

Invalid or non-positive lifetimes are immediately expired; their safety window is zero.

## Transitional revision envelope

Chunk 5 introduces one physical bundle, but Chunk 4 needs logical revisions first. Store a non-secret revision alongside legacy state or in a compatibility manifest managed only by the backend.

```python
@dataclass(frozen=True)
class StoredState:
    state: LegacyOAuthState
    revision: str
```

Every successful credential mutation creates a random 128-bit revision. The revision contains no token-derived material.

## Compare-and-swap API

Add to the store facade:

```python
def compare_and_swap_tokens(
    identity: OAuthIdentity,
    *,
    expected_revision: str,
    tokens: OAuthTokenRecord,
) -> StoredState: ...
```

Under a short cross-process mutation lock, the backend reads the current revision, rejects a mismatch, writes the merged state, assigns a new revision, and releases the lock.

The versioned bundle backend generalizes this to whole-bundle CAS in Chunk 5.

## Refresh coordination

Within one process, the provider manager continues to deduplicate refresh/401 work per server. Cross-process correctness comes from CAS, not the in-process future map.

Refresh algorithm:

```text
1. Load state + revision.
2. Classify expiration.
3. If refresh is required, call the token endpoint.
4. Merge response with loaded token record.
5. Preserve old refresh_token if response omitted it.
6. CAS using loaded revision.
7. On conflict, reload and evaluate newer state.
```

Conflict handling:

- Different valid access token: retry the resource request once with it.
- Newer refreshable but expired state: one bounded refresh retry.
- Identity/issuer change: return typed mismatch/reauthorization result.
- Missing credential: return `credential_not_found`; do not recreate silently.

## Request behavior

- `valid`: send request.
- `refresh_due`: refresh before request when refreshable.
- `expired`: refresh before request or return `reauthorization_required`.
- `unknown`: send request; recover from rejection once.

An authentication rejection causes one coordinated reload. If it finds a newer valid token, retry once. Otherwise refresh once when possible. There is no recursive or unbounded 401 loop.

## Authorization interaction

Explicit successful reauthorization holds the administrative lock and intentionally replaces active credentials. A refresh started from the previous revision loses CAS after reauthorization commits.

A failed staged reauthorization never changes the revision, so concurrent refresh proceeds normally.

## Provider response rules

- Omitted `refresh_token`: retain loaded refresh token.
- Returned rotated refresh token: replace it atomically with access token.
- `invalid_grant`: return `reauthorization_required`, preserve stored state for diagnostics.
- HTTP 5xx/timeout: preserve state and return retryable failure.
- Malformed response: preserve state and return typed invalid-response error.

## Tests

Use an injectable UTC clock and real temporary backend:

- `expires_in` converts to exact `expires_at`.
- Long-lived token uses 60-second window.
- Short-lived token uses ten-percent window.
- Boundary instants classify correctly.
- Unknown lifetime remains usable until rejection.
- Omitted refresh token is preserved.
- Rotated refresh token replaces old token.
- Two processes refresh one revision; one CAS wins.
- Stale refresh loses to successful reauthorization.
- Refresh during failed reauthorization succeeds.
- 401 recovery retries at most once.
- Wall-clock adjustment does not change monotonic timeout behavior.

## Demonstration

Start two worker processes with the same expired credential and revision. Release both token-endpoint responses together. Show one commit succeeds, one receives `revision_conflict`, and both subsequent requests use the winning token.

## Non-goals

- Do not yet migrate to a single physical bundle.
- Do not add provider-specific refresh-window configuration without a demonstrated provider requirement.
- Do not delete credentials on `invalid_grant`.
- Do not add Keychain.

## Completion criteria

- Expiration behavior survives restart.
- Proactive refresh follows the documented safety window.
- Stale writes cannot replace newer credentials.
- Refresh-token rotation and omission are correct.
- Authentication rejection recovery is bounded.
