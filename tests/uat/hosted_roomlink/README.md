# RoomLink two-host UAT

This manual harness validates the boundary unit tests cannot prove: one host
admits a scoped peer run, the home coordinator exits while it is running, and
a fresh home process recovers exactly one reply before revoking the route.

The harness uses synthetic agents, isolated containers, temporary state, a
dedicated API key, and a temporary HTTPS route. It must never reuse a
production state directory or API key.

## Required assertions

1. The target advertises the current RoomLink protocol over HTTPS.
2. Invitation scope binds room, home, authority epoch, member, installation,
   and target profile.
3. The first home process persists the remote receipt, then exits before the
   peer completes.
4. A fresh home process reloads the link and publishes one remote reply.
5. Repeating status/replay does not duplicate that reply.
6. A second remote turn is stopped by exact task/generation and reaches a
   durable cancelled state before teardown.
7. Revocation succeeds before temporary state and network routes are removed.
8. The original network route configuration is restored byte-for-byte.

`home_runner.py` prints the following only after assertions 1–6 pass:

```text
UAT_OK remote_reply=1 restart_recovered=1 stop_acknowledged=1 scoped_route_revoked=1
```

Keep environment-specific hostnames, ports, credentials, and cleanup commands
in the private execution record rather than this repository.
