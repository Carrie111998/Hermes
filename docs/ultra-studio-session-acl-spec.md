# Ultra Studio Session ACL P0 Spec

Status: implementation spec
Date: 2026-06-16

## Objective

Make API-server chat history safe for multi-user use by binding persisted
sessions and Responses API state to the server-resolved principal scope.

This is the P0 data-isolation layer that sits underneath the existing
`Principal` / `PolicyChecker` / sandbox-lease work. It does not implement the
future TokenRouter, Vault, quota, or billing control plane.

## Current Evidence

- `gateway.principal_headers` parses `tenant_id`, `workspace_id`,
  `project_id`, `user_id`, `roles`, and `sandbox_id` into a request scope.
- `gateway.api_agent_runner.run_agent_sync` binds that scope into the agent
  turn using `gateway.session_context`.
- `hermes_state.SessionDB` persists all sessions in one `sessions` table. It
  already has `user_id`, but no `tenant_id`, `workspace_id`, or `project_id`.
- API session endpoints currently read sessions by raw `session_id`; they do
  not check owner scope.
- Responses API chaining stores conversation history in `ResponseStore`; it
  currently has no owner check for `previous_response_id`, GET, DELETE, or
  named `conversation`.

## Chosen Architecture

P0 uses application-level ACL checks plus a small sidecar ownership table in
the existing `state.db`.

The sidecar is intentionally separate from `hermes_state.SessionDB` because
that module is already a large legacy file. The ownership table is managed by
`gateway.session_scope_store`, keyed by `session_id`, and can later be folded
into a dedicated session service without changing the HTTP contract.

The trusted identity source for this implementation is still the API server's
authenticated principal headers. A production deployment should replace those
headers with Gateway/TokenRouter-verified claims, but the storage and ACL
contracts remain the same.

## Boundary Map

- `gateway.principal_headers`: parse and validate request principal headers.
- `gateway.session_context`: bind a `Principal` into the current agent turn.
- `gateway.session_scope_store`: own the `api_session_scopes` sidecar table
  and scoped list filters.
- `gateway.session_acl`: compare request scope to persisted session/response
  scope and build deterministic scope fingerprints.
- `gateway.api_server_*`: enforce ACL at HTTP boundaries before reading
  history, mutating sessions, deleting sessions, forking sessions, or chaining
  Responses API state.

## Contracts

### Principal Scope

A multi-user request scope is complete only when it includes:

- `tenant_id`
- `workspace_id`
- `project_id`
- `user_id`

`roles` and `sandbox_id` are optional for session ACL.

If any multi-user identity header is present, the complete identity set is
required. Partial scopes are rejected before the agent turn starts.

### Session Ownership

New API-server sessions created under a principal bind a sidecar row with:

- `tenant_id`
- `workspace_id`
- `project_id`
- `user_id`

Session children created by compression or fork inherit the parent sidecar
scope when the caller does not provide one explicitly.

### Session Access

If a request has no multi-user scope, legacy single-user behavior is preserved.

If a request has a multi-user scope:

- session list filters by exact scope
- session get/messages/chat/stream/patch/delete/fork require exact scope match
- scoped requests cannot read legacy unscoped sessions
- scoped requests cannot read another user's scoped sessions
- denied existing sessions return `404` to avoid leaking session existence

### Default Session ID

The default `/v1/chat/completions` session id is derived from:

- system prompt
- first user message
- principal scope fingerprint

This prevents two users with the same first message from colliding into the
same deterministic API session id.

### Responses API State

Stored `ResponseStore` entries include principal scope. If a request has a
scope:

- `previous_response_id` must match the same scope
- `GET /v1/responses/{id}` must match the same scope
- `DELETE /v1/responses/{id}` must match the same scope
- named `conversation` mappings are internally prefixed by scope fingerprint

Legacy unscoped clients keep existing behavior.

### Idempotency Cache

For scoped requests, `Idempotency-Key` cache fingerprints include the
principal scope fingerprint. Two users with the same request body and the same
idempotency key must not share cached chat or Responses API results.

## Non-Goals

- No TokenRouter or JWT validation implementation in this P0.
- No provider-key vaulting.
- No quota or billing enforcement.
- No cross-service microservice split.
- No migration of historical unscoped sessions into tenants.

## Validation Matrix

- `api_session_scopes` stores and filters `tenant_id/workspace_id/project_id/user_id`.
- API user A cannot list, get, message, chat, fork, patch, or delete user B's
  sessions.
- API user A and B with the same first prompt get different default session ids.
- Responses API user A cannot chain, get, or delete user B's response state.
- Scoped users with the same `Idempotency-Key` do not share cached results.
- Legacy unscoped tests continue passing.
