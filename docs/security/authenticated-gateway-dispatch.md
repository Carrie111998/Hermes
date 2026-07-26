# Authenticated gateway dispatch

Authenticated gateway dispatch is a host-issued capability for plugin commands and model tools that must run only for an explicitly authorized inbound gateway event.

It is not a generic authentication mechanism and is not available to CLI, TUI, local/synthetic plugin dispatch, delegated agents, Codex app-server turns, or model-supplied arguments. Installed plugin code remains trusted host code; this capability is not a sandbox against a malicious plugin.

## Registering protected plugin entry points

Plugin commands and tools opt in independently:

```python
context.register_command(
    "admin-command",
    handle_command,
    requires_authenticated_gateway=True,
)

context.register_tool(
    name="admin_tool",
    schema={...},
    handler=handle_tool,
    requires_authenticated_gateway=True,
)
```

A command registration requires a command capability. A protected tool requires a tool capability. The two capability kinds are not interchangeable.

Protected tool handlers receive provenance only through the host-owned keyword argument:

```python
def handle_tool(args, *, tool_context):
    platform = tool_context.platform
```

Protected command handlers receive the corresponding host-owned `command_context` keyword. Model arguments and command text cannot create or replace these values.

## Capability contract

A capability is:

- created only by `gateway.authenticated_dispatch`;
- sealed against direct construction, copying, mutation, and lookalike objects;
- bound to one exact `dispatch_kind` (`command` or `tool`);
- bound to the inbound platform, user, chat, optional thread, and triggering event message ID;
- valid only while its issuer-owned lease is live;
- rejected if source and event message IDs disagree;
- revoked deterministically on scope exit or before an accepted busy-turn prompt mutation.

Validation is identity-based and lease-based, not field-shape-based. An object with matching attributes is not authority.

Lease admission and revocation are atomic. Revocation immediately prevents new admissions and returns without waiting; already-admitted uses may finish under their call-specific claims.

## Tool execution boundary

For protected tools, authorization occurs immediately after model arguments are parsed and any `tool_call` bridge wrapper is resolved. Unauthorized calls are denied before:

- tool-request middleware;
- plugin pre-tool hooks;
- guardrails and approval checks;
- checkpoints or activity publication;
- tool start, progress, complete, or post-tool callbacks;
- handler entry.

The registry repeats authorization at dispatch as defense in depth.

Only the host transport names `authenticated_gateway_context` and `tool_context` are recursively reserved in protected model schemas and protected model arguments. Domain fields such as `platform` or `message_id` remain valid application data and do not confer authority.

## Model-visible surface

Protected tool definitions are included in provider payloads only while a live tool lease is bound to the current native agent turn. They are also removed from deferred `tool_search` and `tool_describe` catalogs outside that scope.

Command capabilities, stale capabilities, and absent capabilities do not expose protected model tools. Filtering is per request and does not mutate the agent's cached tool snapshot or system prompt.

## Host integration

Gateway integrations must pass the actual normalized inbound `MessageEvent.message_id` when issuing a protected command or tool turn. Reply anchors, thread roots, quoted-message IDs, and delivery-routing metadata must never be used as authorization provenance.

The generic host APIs are in `gateway/authenticated_dispatch.py`:

- `issue_authenticated_gateway_dispatch(...)`
- `authenticated_gateway_turn(...)`
- `bind_authenticated_gateway_dispatch(...)`
- `validate_authenticated_gateway_command_dispatch(...)`
- `validate_authenticated_gateway_tool_dispatch(...)`
- `use_authenticated_gateway_tool_dispatch(...)`
- `revoke_authenticated_gateway_dispatch(...)`

Callers should normally use the context managers rather than manually coordinating issue, bind, use, and revoke operations.
