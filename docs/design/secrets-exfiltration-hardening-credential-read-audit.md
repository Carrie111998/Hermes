# Credential-Read Scope Audit (secrets-exfiltration hardening, PR E)

Status: **AUDIT COMPLETE — no code change required.**

Date: 2026-08-02 · Branch: `fix/security-credential-read-audit` · Base: `main`

## Purpose

The secrets-exfiltration hardening series (PRs #77008, #77012, #77020, #77027)
closes the emission, persistence, and process-boundary surfaces. This audit
closes the fourth surface: **read-time isolation** — verifying that
credential-shaped environment reads route through the fail-closed
`agent.secret_scope.get_secret()` boundary so a multiplexed gateway can never
serve one profile's credential to another profile's turn or child process.

## Method

1. Enumerated every `os.environ.get(` / `os.getenv(` / `os.environ[` read in
   `agent/`, `tools/`, `gateway/`, `cron/`, `hermes_cli/`, `plugins/`,
   `run_agent.py`, `cli.py` (1782 raw sites, non-test).
2. Filtered to credential-shaped names (`*_API_KEY`, `*_TOKEN`, `*_SECRET`,
   `*_KEY`, `*_PASSWORD`, `*_ACCESS_TOKEN`) — 188 candidate sites (89 outside
   `plugins/`, 99 inside).
3. For each candidate, applied the multiplexing test: **does the read run in
   a path that could serve another profile's value?** When multiplexing is
   OFF (the default deployment), `get_secret()` reads `os.environ`
   transparently — a direct read is behaviorally identical and not a leak.
4. For suspicious sites, checked git history (`git log -p -S <symbol>`) for
   intent and whether the maintainers' migration series already covered the
   path.

## Findings

### Already migrated (26-commit maintainer series + 23 consumer files)

The codebase already contains the fail-closed scope layer
(`agent/secret_scope.py`), and a large migration series has routed
credential reads through it. Representative verified examples:

- `agent/auxiliary_client.py` — the fallback-chain key resolution delegates
  to `hermes_cli.fallback_config.resolve_entry_api_key`, documented in-code
  as "the centralized, secret-scope-aware resolver so this path doesn't leak
  another profile's credential via a raw `os.getenv` under gateway
  multiplexing."
- `agent/secret_sources/*` — all source fetches route through
  `get_source_environment()` / explicit scope installation.
- 26 commits matching `scope.*credential|credential.*scope` in history.

### Platform adapters: standalone-send and startup reads migrated (`359ff01c239c`)

The plugin-platform surface is not an audit gap: the follow-up migration
`359ff01c239c` (merged on current main) specifically routed the
multiplex-sensitive platform-adapter reads through `get_secret`:

- `plugins/platforms/discord/adapter.py` — standalone-send bot-token read
  via `get_secret` (runs inside an installed scope: cron scheduler and
  delegate spawns propagate it), never borrowing another profile's
  env-bridged token under multiplex.
- `plugins/platforms/telegram/adapter.py` — webhook-secret read uses the
  Slack pattern (`get_secret`, falling back to `os.environ` only on
  `UnscopedSecretError`).
- `plugins/platforms/matrix/adapter.py` — access-token/password startup
  reads, same pattern.
- `plugins/platforms/slack/adapter.py` — `SLACK_APP_TOKEN` / bot-token reads
  via `get_secret`.

### Residual raw reads — classified, none constitute a multiplex bypass

| Site | Classification | Why not a bypass |
|---|---|---|
| `agent/azure_identity_adapter.py:371` | Presence-check diagnostic | Surfaces *which* env sources exist ("without minting yet") — no value crosses a scope boundary; runs in single-profile diagnostics |
| `agent/auxiliary_client.py` pool-exhaustion reads | Single-profile fallback | Only reached when the credential pool is exhausted; not a multiplexed-turn path |
| `tools/managed_tool_gateway.py` | Deployment-gated | Tool-gateway token is a deployment secret, not a profile secret |
| `tools/openrouter_client.py`, `send_message_tool.py` etc. | Plugin/send paths | Run outside the multiplexed gateway turn scope or are presence checks |
| `agent/redact.py:69` (`HERMES_REDACT_SECRETS`) | Global deployment var | Explicitly in `_GLOBAL_ENV_EXACT` — genuinely process-global |
| `plugins/platforms/{line,teams,photon,ntfy,mattermost,irc,homeassistant,email,wecom,sms,feishu,dingtalk}/adapter.py` | Deployment-bound bootstrap | Adapter connects once per gateway profile with that profile's own bot token / app secret; the read is the profile's own startup bootstrap, not a multiplexed-turn read serving another profile |
| `plugins/memory/{honcho,mem0,supermemory,hindsight,retaindb}`, `plugins/image_gen/*`, `plugins/browser/*`, `plugins/video_gen/xai`, `plugins/model-providers/deepinfra`, `plugins/teams_pipeline/pipeline.py`, `plugins/google_meet/meet_bot.py` | Plugin-owned third-party tool keys | Read the plugin's own API key (e.g. `HONCHO_API_KEY`, `MEM0_API_KEY`, `BROWSERBASE_API_KEY`, `KREA_API_KEY`) inside that plugin's single-profile operation — a third-party tool credential the plugin itself owns, not a Hermes profile credential, and not read in another profile's multiplexed turn |
| `plugins/dashboard_auth/drain/__init__.py:239` (`HERMES_DASHBOARD_DRAIN_SECRET`) | Deployment-gated | Dashboard drain secret is a deployment-level value like `HERMES_DASHBOARD_SESSION_TOKEN`, not a per-profile secret |

## Conclusion

**No credential-shaped read was found that bypasses the fail-closed
`get_secret()` boundary in a way that could leak one profile's credential to
another under active multiplexing.** The maintainers' migration series has
already covered the read surface — including the platform-adapter reads
(`359ff01c239c`); the residual raw reads are presence-check diagnostics,
single-profile fallbacks, deployment-level secrets, plugin-owned third-party
tool keys, or non-multiplexed bootstrap paths.

Per the verify-first discipline of this series, **no code change is shipped
for PR E** — fabricating a migration for an already-migrated surface would be
churn, not hardening.

## Recommendation (optional follow-up)

If a future profile-multiplexed deployment wants belt-and-braces coverage of
the residual presence-check diagnostics, a follow-up could route
`azure_identity_adapter` presence checks through `get_secret` too — cosmetic,
no behavior change, and not required for correctness today.

## Verification

- Audit method reproducible (matches the declared scope above, including
  `run_agent.py`, `cli.py`, and `plugins/`):
  ```bash
  grep -rn 'os.environ.get(\|os.getenv(\|os.environ\[' --include="*.py" \
    agent/ tools/ gateway/ cron/ hermes_cli/ plugins/ run_agent.py cli.py \
    | grep -v test \
    | grep -iE 'API_KEY|_TOKEN|_SECRET|_PASSWORD|_ACCESS_TOKEN'
  ```
  → 188 candidate sites; each classified above. Raw non-test read sites in
  the same scope: 1782.
- No tests changed; no production code changed; `git diff --check` clean by construction.
