---
name: bitwarden-secrets
description: Authoritative Bitwarden Secrets Manager (bws) protocol for Hermes — encrypted-only cache, no plaintext at rest, masked output, child-process env hygiene.
version: 1.0.0
author: Axl Ibiza
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [security, secrets, bitwarden, bws, secrets-manager, credential-hygiene, exfiltration-prevention]
    related_skills: [hermes-agent]
---

# Bitwarden Secrets Manager — Hermes Secrets-Handling Protocol

The authoritative protocol for operating Bitwarden Secrets Manager (`bws`) inside Hermes. It encodes the post-hardening guarantees: **secrets are never persisted as plaintext, never emitted into status lines or logs, and never inherited by child processes**. If you are configuring, rotating, debugging, or auditing the Bitwarden integration, follow this skill.

## When to Use

- Configuring or re-configuring `secrets.bitwarden` (setup, token rotation, region/project changes).
- Debugging "Bitwarden rejected the machine-account access token", `bws exited 1`, timeouts, or checksum mismatches.
- Auditing whether any secret path can leak: disk cache, stderr, logs, subprocess environments.
- Answering "is my Bitwarden secret safe in Hermes?" — the answer must be grounded in this protocol's invariants, not vibes.

## Protocol invariants (non-negotiable)

1. **No plaintext at rest, ever.** The disk cache is always AES-GCM encrypted (`~/.hermes/cache/bws_cache.enc.json`), keyed off the bootstrap token. There is no plaintext write branch. `encrypted_cache.enabled: false` means **memory-only** — it disables disk persistence entirely; it never re-enables plaintext.
2. **Legacy plaintext is destroyed.** A pre-hardening `~/.hermes/cache/bws_cache.json` is re-encrypted and removed on first read — including in memory-only mode. Verify it does not exist after any run.
3. **No secret values in output.** Secret-source error, remediation-hint, warning, and conflict lines are masked before reaching stderr. `RedactingFormatter` masks opaque credential values in all log output (shape-based regex + exact-value pass). If you see a raw secret value in any status line or log, that is a regression — stop and fix, do not route around.
4. **No credentials in child environments.** `BWS_ACCESS_TOKEN` (under its exact configured name) and every `*_PASSWORD` are stripped from spawned children on every surface: terminal (`build_subprocess_env`) and non-terminal (browser worker, ACP executor, computer-use driver, TUI/Node host). The only child that receives the token is the `bws` CLI itself, explicitly, never by inheritance.
5. **Fail-closed read scope.** Credential-shaped environment reads route through `agent.secret_scope.get_secret()`. The credential-read audit (#77031) documents every read path; any new env read of credential-shaped names must go through the gate.
6. **The gate is tested.** The end-to-end no-exfiltration test (#77039) pins invariant 3+4: a loaded secret's name and value never surface in stdout, stderr, or formatted output. Run it as part of any change to this integration.

## Quick Reference

| Task | Command |
|---|---|
| Interactive setup | `hermes secrets bitwarden setup` |
| Non-interactive setup | `hermes secrets bitwarden setup --access-token "$BWS_ACCESS_TOKEN" --server-url https://vault.bitwarden.eu --project-id <uuid>` |
| Status / validation | `hermes secrets bitwarden status` |
| Rotate token (masked prompt) | `hermes secrets bitwarden token` |
| Rotate token (non-interactive) | `hermes secrets bitwarden token --access-token 0.…` |
| Dry-run sync | `hermes secrets bitwarden sync` |
| Apply to current shell | `hermes secrets bitwarden sync --apply` |
| Install binary only | `hermes secrets bitwarden install` |
| Disable (keeps config) | `hermes secrets bitwarden disable` |
| Config keys | `secrets.bitwarden.*` in `~/.hermes/config.yaml` |
| Bootstrap token | `BWS_ACCESS_TOKEN` in `~/.hermes/.env` |

## Procedure

### 1. Setup

1. In the Bitwarden web app: create a machine account, grant it Read access to a project, create a never-expiring (or dated) access token (starts with `0.`). Bitwarden shows the token once — copy it immediately.
2. Run `hermes secrets bitwarden setup`. It installs the pinned `bws` binary (SHA-256 verified) into `~/.hermes/bin/`, prompts for the token (hidden input), records the region (`BWS_SERVER_URL`), lists projects, test-fetches, and flips `enabled: true`.
3. Confirm with `hermes secrets bitwarden status`.

### 2. Rotation

`hermes secrets bitwarden token` probes the new token against Bitwarden **before** writing anything — a rejected token leaves the current `.env` untouched. On success it stores the token, clears fetch caches, and warns if the configured project is invisible to the new machine account. Prefer this over editing `.env` by hand.

### 3. Verification after any change

1. `hermes secrets bitwarden status` — token presence, binary version, project, region all correct.
2. Plaintext check: `ls ~/.hermes/cache/bws_cache*` — must show only `bws_cache.enc.json` (or nothing in memory-only mode). Any `bws_cache.json` is a legacy-plaintext regression.
3. Child-env check: spawn a probe child and confirm `BWS_ACCESS_TOKEN` and `*_PASSWORD` are absent from its environment (see Pitfalls for the exact check).
4. If touching the integration code: run the no-exfiltration gate and the bitwarden secret-source test module.

## Pitfalls

- **Never echo the token.** Do not paste `BWS_ACCESS_TOKEN` values into chat, logs, or issue reports. Use the masked rotation path.
- **Region mismatch masquerades as rejection.** An EU token hitting the US identity endpoint fails as `invalid_client`. Check `secrets.bitwarden.server_url` before assuming revocation.
- **`bws` must be pinned, not "latest".** Hermes downloads a pinned version (v2.0.0 at time of writing) with checksum verification. If you need a newer version, that is a repo PR, not a runtime auto-upgrade.
- **Do not store `BWS_ACCESS_TOKEN` as a secret inside the project.** Hermes refuses to overwrite its own bootstrap token even with `override_existing: true` — the secret is silently skipped.
- **A failed cache write must never block a fetch.** The encrypted-cache write is best-effort by design. A cache failure surfaces as a warning, never as a startup failure.
- **Memory-only mode still destroys legacy plaintext.** `encrypted_cache.enabled: false` never reads or writes plaintext; it still removes a leftover `bws_cache.json`. Do not "temporarily" re-enable plaintext — there is no such option.
- **If a raw secret value appears in any status line, warning, or log: that is a security regression in the masking layer, not a cosmetic issue.** Quarantine the output, reproduce, and file a fix against `agent/redact.py` / `hermes_cli/env_loader.py`.

## Verification

- [ ] `hermes secrets bitwarden status` reports valid token + project + region.
- [ ] Only `bws_cache.enc.json` (or nothing) exists under `~/.hermes/cache/`; no plaintext `bws_cache.json`.
- [ ] A spawned child process (terminal and non-terminal surfaces) sees neither `BWS_ACCESS_TOKEN` nor any `*_PASSWORD` unless explicitly passthrough-registered.
- [ ] A simulated fetch error/warning emits masked output (`***`) — no raw secret values on stderr.
- [ ] `tests/test_secrets_exfiltration.py` (no-exfiltration gate) and `tests/test_bitwarden_secrets.py` pass.
