# Agent Plugin session capabilities

Portable Agent Plugins are normally untrusted model-tool packages. They do not
receive Hermes platform, profile, session, message, or active-turn identifiers.
An MCP server that must bridge one reviewed workflow into a private platform can
request an explicit `sessionCapability`, but the request is denied unless the
profile grants the exact installed package and server binding.

## Threat model

A plugin name, MCP tool description, toolset entry, Unix socket, or same-user
process is not an authority boundary. A sibling process may read same-user files
or connect to same-user sockets. A model may forge ordinary tool arguments. A
package update can change executable bytes while retaining its name.

Session capabilities therefore bind every call to host-owned identity and
current runtime state. They are intended for narrow fixed-workflow relays, not
generic phone, browser, terminal, or arbitrary tool proxies.

## Package request

An Agent Plugin requests capability metadata for one packaged MCP server in its
`plugin.json` extension:

```json
{
  "extensions": {
    "com.hermes": {
      "sessionCapability": ["workflows"]
    }
  }
}
```

The named server must be a valid packaged stdio MCP server. A Python server must
launch with `-I -S -B` and an in-package regular script. Bare ambient
executables, remote transports, symlinks, Python bytecode caches, and executable
content outside the approved package root fail closed.

## Profile grant

The profile grants one exact package/server binding and canonical package
digest:

```yaml
plugins:
  trusted_session_context:
    - binding: example-plugin:workflows
      digest: sha256:<canonical-package-digest>
```

Plugin discovery does not grant trust. Before every capability mint Hermes
rereads consent, rejects caches or symlinks, recomputes the package digest, and
checks the approved binding. Updating any included package byte changes the
digest and requires a new explicit grant.

## Per-call binding

For an authorised call Hermes mints a short-lived process-local HMAC capability
in MCP request `_meta`. The signed claims bind at least:

- audience and exact package/server binding;
- workflow name and canonical arguments;
- profile, platform, session, message, and tool-call identity;
- connection/turn sequence, expiry, and single-use nonce.

The receiving private relay must verify the complete claim, expected workflow
sequence, active turn, and replay ledger. It must not accept any of those values
from ordinary model arguments. A blank or nonmatching session cannot become a
proactive call.

Capabilities are process-local. Restarting Hermes invalidates every outstanding
capability. A capability for one package, server, workflow, payload, profile, or
turn cannot be reused for another.

## Cancellation and ambiguous outcomes

Standard MCP `notifications/cancelled` is bound to the exact request ID. The
stdio server, private relay, platform dispatcher, downstream MCP client, and
device server must propagate cancellation so delayed side effects do not keep
running silently.

Cancellation or transport loss after mutation handoff may have an ambiguous
outcome. Mutating workflows must use a stable operation identity derived from
trusted call identity and retry only with the byte-identical payload. An exact
historical receipt is success; a conflicting payload fails; an unprovable
outcome remains unknown rather than being relabeled as not committed.

## Operational checks

Before enabling a capability package:

1. run `hermes plugins doctor --ci <package>`;
2. verify the package is cache-free and symlink-free;
3. review its canonical digest and executable command;
4. add the exact digest-bound profile grant;
5. expose only the intended static MCP toolset to the target platform;
6. keep any raw downstream tools private and schema-pinned;
7. restart the gateway and inspect effective runtime tool discovery;
8. test cancellation, replay, expiry, wrong audience/binding/workflow/payload,
   package-update revocation, and ambiguous mutation recovery.

Do not use SOUL, skills, platform hints, tool descriptions, socket permissions,
or model-visible policy text as substitutes for these checks.
