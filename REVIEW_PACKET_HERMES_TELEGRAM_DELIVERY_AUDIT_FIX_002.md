# HERMES-TELEGRAM-DELIVERY-VERIFICATION-001 implementation review packet

## Boundary

Review only the uncommitted correction in this worktree against the current
`origin/main` base. Do not inspect or request Telegram message bodies,
credentials, tokens, raw chat or user identifiers, or runtime files. Do not
edit files, send Telegram traffic, restart services, or perform deployment.
Return exactly one first-line verdict: `PASS`, `REVISE`, or `BLOCKED`, followed
by concise scope-level reasons.

## Defect and correction

The normal GatewayRunner streaming path built outbound metadata from the
session source only. That preserved topic/thread routing but dropped the
opaque inbound Telegram delivery-correlation metadata before constructing the
stream consumer. A visible response could therefore be delivered while the
Telegram adapter's metadata-only outbound audit had no correlation to record.

The correction changes the stream setup in `gateway/run.py` to construct
metadata through the existing source/event-data helper, passing the already
available event metadata explicitly. It also forwards that metadata through
the proxy delegation boundary. This retains existing thread metadata and
copies only the allowed opaque Telegram correlation fields. No message body,
credential, token, raw chat id, or user identity is added.

The regression tests in `tests/gateway/test_run_progress_topics.py` and
`tests/gateway/test_proxy_mode.py` verify that streaming send/edit metadata
retain the originating thread metadata and opaque correlation field, and that
the proxy delegation boundary receives event metadata instead of dropping it.

## Review questions

1. Does the correction cover the ordinary streaming final-response path that
   previously lost correlation metadata?
2. Does it preserve existing topic/thread routing and avoid sensitive-data
   expansion?
3. Is the regression test appropriate and is the correction narrow enough?

## Local verification

- Focused Gateway suites: `53 passed` plus the real proxy-stream regression.
- Broader affected Gateway suites: `225 passed`.
- Ruff and `py_compile`: PASS.
- `git diff --check`: PASS.

Implementation review does not authorize commit, merge, DGX deployment,
restart, Telegram state mutation, or a second user-visible test.
