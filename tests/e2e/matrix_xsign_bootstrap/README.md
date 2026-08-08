# Matrix cross-signing bootstrap — E2E test

Self-contained end-to-end test for the auto-bootstrap behavior in the Matrix
adapter, `plugins/platforms/matrix/adapter.py` (this moved out of the old
gateway-builtin location when Matrix became a plugin).
Spins up a real Continuwuity homeserver
in Docker, registers a fresh bot, runs the patched bootstrap path
against it, and asserts:

1. Cross-signing keys get published with **unpadded** base64 keyids
   (the bug this PR fixes — padded keyids are silently rejected by
   matrix-rust-sdk in Element).
2. On a second startup with the same crypto store, bootstrap is
   skipped.
3. When `MATRIX_RECOVERY_KEY` is set, the existing recovery-key path
   takes precedence and no fresh bootstrap happens.

## Run

```bash
# from repo root
docker compose -f tests/e2e/matrix_xsign_bootstrap/docker-compose.yml up -d
python tests/e2e/matrix_xsign_bootstrap/test_bootstrap.py
docker compose -f tests/e2e/matrix_xsign_bootstrap/docker-compose.yml down -v
```

The `down -v` step removes the persistent volume so the next run gets
a fresh homeserver — important because Continuwuity's one-time admin
registration token is only valid before the first user is created.

## Port

The compose binds Continuwuity to `127.0.0.1:26167` by default. Override
with `HOMESERVER_HOST_PORT=NNNNN docker compose up -d` if that port is
busy locally.

## What the test exercises

The test mirrors the bootstrap snippet from
`MatrixAdapter.connect()` in `plugins/platforms/matrix/adapter.py` (the
"if MATRIX_RECOVERY_KEY else get_own_cross_signing_public_keys /
generate_recovery_key" branch) inline so it runs without importing the
entire hermes gateway and its many dependencies. The copy lives in this
test's own `_connect_with_bootstrap()` helper — that name exists **only
here**, not in production. **If `connect()` diverges from the mirrored
snippet, this test must be updated to match.** A small price for not
requiring the full hermes-agent runtime in CI.

> **Known gap:** nothing mechanically enforces the mirror. The production
> bootstrap has already moved once (module → plugin, and out of a dedicated
> method into `connect()`) without this test noticing, so treat drift here
> as likely rather than hypothetical.

## Skipped when

- `mautrix` Python package is not installed
- The homeserver isn't reachable at `$E2E_MATRIX_HS` (default
  `http://127.0.0.1:26167`)
