# Profile Routing for Inbound Messages

> **Audience:** Gateway operators and contributors
> **Related:** [Session Lifecycle](session-lifecycle.md), `docs/design/profile-builder.md`

## Security boundary

Hermes serves exactly one profile in each gateway process. The profile name,
home directory, credentials, session store, durable event store, and background
workers are frozen when that process starts.

`gateway.multiplex_profiles: true` is no longer a supported deployment mode.
Gateway startup fails closed when the setting is present. The same restriction
applies when the API server or webhook adapter is embedded directly: they must
not start a listener for several profiles in one process.

The former in-process `profile_routes` mechanism and `/p/<profile>/...` HTTP
routes are therefore obsolete. Do not configure or expose them.

## Supported topology

Run one managed gateway service for every profile:

```bash
hermes gateway install
hermes gateway start

hermes -p coder gateway install
hermes -p coder gateway start
```

Each process exposes only its platform's native routes. For example, a webhook
process serves `/webhooks/<route>`, and an API-server process serves
`/v1/chat/completions`; Hermes does not add a profile prefix.

If several profiles must be reachable through one public address, put a thin
external ingress in front of their isolated processes. The ingress may route by
hostname, port, or an operator-owned path, but it must remove that routing
prefix and forward to the selected profile process's native endpoint. It must
not merge profile state or credentials.

Connection-oriented platforms such as Discord and Telegram should use a
separate bot credential for each profile process. A single platform credential
must not be consumed concurrently by multiple gateways.

This topology preserves process-level isolation and gives each profile an
independent crash, restart, secret, and persistence boundary.
