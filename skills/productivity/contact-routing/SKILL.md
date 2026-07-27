---
name: contact-routing
description: "Resolve a person to a verified, purpose-specific outbound route before messaging; use when the user asks to contact, notify, DM, email, or hand off to someone by name."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [contacts, messaging, routing, safety]
---

# Contact Routing

Use Hermes's profile-scoped contact registry to distinguish four separate facts:

1. **Identity** — who the person is.
2. **Reachability** — whether a configured gateway currently exposes the destination.
3. **Route preference** — which route the user designated for this purpose.
4. **Authorization** — whether the requested communication may be sent.

Never infer one from another. A generated channel-directory entry or platform allowlist is not a preferred route.

## When to Use

- The user asks to contact, notify, DM, email, or hand off to a person by name.
- Several platform identities or destinations could refer to the same person.
- The communication purpose determines which route should be used.
- A discovered or allowlisted destination must not be mistaken for route preference.

## Prerequisites

- Initialize `$HERMES_HOME/contacts.yaml` with `hermes contacts init`.
- Record only source-backed identity and route information.
- Configure the relevant messaging/mail adapter separately; the registry stores no credentials.

## Procedure

1. Identify the communication purpose explicitly, such as `internal`, `external_work`, or `urgent`.
2. Resolve without sending:

   ```bash
   hermes contacts resolve "Person" --purpose purpose_name
   ```

   Use `--route route-key` only when the user or durable workflow selected that exact route.
3. Stop on any status other than `ok`, including unknown/ambiguous contacts, missing routes, stale endpoints, stale directory caches, or directory mismatches.
4. A successful resolution still reports `authorization_check: required`. Confirm the user authorized the communication and that no platform-specific identity check remains.
5. Re-run with `--show-destination` only when preparing the authorized delivery.
6. Send through the existing messaging or mailbox tool; the resolver never sends.
7. Read back the destination, sender identity, message ID, and content when the platform supports it.

## Registry maintenance

Initialize and inspect the active profile's registry:

```bash
hermes contacts init
hermes contacts validate
hermes contacts list
hermes contacts show "Person"
```

The registry lives at `$HERMES_HOME/contacts.yaml`. Keep it owner-readable, source identities and route preferences, mark stale/unverified routes honestly, and avoid storing credentials or message history in it.

## Pitfalls

Do not automatically merge contacts based on similar names or endpoints. Do not promote every discovered destination into the registry. Ask for clarification when identity, purpose, preferred route, or authority is materially ambiguous.

## Verification

- `hermes contacts validate` reports `status: ok`.
- Resolution reports the expected contact ID, route key, and live-check state. A directory match is accepted only when the generated cache is at most ten minutes old.
- Resolution output always reports `send_performed: false` and `authorization_check: required`.
- After an authorized send, the platform readback matches the resolved destination and expected sender identity.
