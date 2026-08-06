---
sidebar_position: 15
title: "Kanban Review Runner Operations"
description: "Fail-closed health, Linear/GitHub/CodeRabbit/Slack shadow validation, and staged rollout controls"
---

# Kanban review runner operations

The review runner is a bounded, script-only boundary for the Linear → GitHub → CodeRabbit → QA → human-review workflow. It is inert by default, never uses an LLM, and never merges, approves, pushes, updates branches, or mutates Linear.

The current production-readiness status is **conditional / blocked for live delivery**:

- Linear, GitHub, CodeRabbit, and Slack acknowledgement reads have typed, allowlisted MCP boundaries.
- `dry-run` performs no writes. `shadow` performs no provider writes and may persist only immutable local reconciliation or acknowledgement audit rows.
- Restricted GitHub review-comment and Slack message/reply transports are available behind independent `delivery_enabled: false` gates. They are not reviewer-request, approval, merge, branch, file, or channel-management authority.
- `production_ready` remains false until the exact private GitHub PR/head and Slack channel/thread probes pass with the production credentials and the operator approves each destination. Do not create a production cron or enable gateway routing from this runbook.

## Authority and safety contract

| System | Authority | Allowed by the current runner | Never inferred or allowed |
|---|---|---|---|
| Linear | Issue intent, title, status, project context, attachments | OAuth MCP reads and resource probes | Webhook delivery, issue mutation, PR head authority |
| GitHub | PR identity, open/draft state, exact head SHA, checks, reviews, review comments | Four explicitly allowlisted MCP reads; exact-head `COMMENT` review receipt when separately enabled | Merge, approval, branch update, push, reviewer request, file write |
| CodeRabbit | Review evidence observed through GitHub | Exact-head classification | A green status as proof that no actionable comments exist |
| Slack | Notification route and acknowledgement evidence only | Allowlisted thread/reaction/text reads; exact stored channel/thread message delivery when separately enabled | Approval authority, merge authority, destination discovery, channel management |
| Kanban | Work ownership, exact-head gate, local audit/outbox state | Bounded local reconciliation/audit writes in shadow mode | Automatic follow-up creation or recursive scheduling |

A Slack `approve`, `lgtm`, or approval-looking reaction is stored only as acknowledgement evidence. GitHub remains the human decision source. Merge remains human-only.

## Required operator inputs

Do not guess any identifier. Obtain and approve all of the following before enabling a provider:

1. Private GitHub repository in exact `owner/name` form.
2. Slack staging channel ID (not a channel name).
3. Slack acknowledgement user IDs.
4. Linear team name/key and a known issue ID for the resource probe.
5. GitHub and Slack credentials in approved credential storage.
6. A known private PR/head and, for Slack acknowledgement testing, an existing approved staging thread.

If any value is unavailable, leave the corresponding provider disabled. Health will report the missing allowlist rather than discovering or guessing it.

## Credential policy

Secrets belong in the active Hermes home's `.env` (`~/.hermes/.env` for the default profile, or the selected profile's `.env`) or an approved external secret source. The review runner refuses plaintext GitHub or Slack tokens in `config.yaml` and emits only redacted failure type/kind diagnostics.

Use references in the MCP server configuration:

```yaml
mcp_servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${env:GITHUB_PERSONAL_ACCESS_TOKEN}
  slack:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-slack"]
    env:
      SLACK_BOT_TOKEN: ${env:SLACK_BOT_TOKEN}
      SLACK_TEAM_ID: T_APPROVED_WORKSPACE
```

Do not paste secret values into logs, Kanban comments, health output, runbooks, or test fixtures.

## Disabled-by-default policy

```yaml
kanban:
  review_runner:
    enabled: false
    gateway_enabled: false
    mode: dry-run
    timeout_seconds: 120
    lease_seconds: 180
    max_items_per_run: 50
    retry_ceiling: 3
    provider_timeout_seconds: 15
    providers:
      github:
        enabled: false
        delivery_enabled: false
        adapter: disabled
        mcp_server: github
        repositories: []
        coderabbit_logins: ["coderabbitai[bot]", "coderabbitai"]
      slack:
        enabled: false
        delivery_enabled: false
        adapter: disabled
        mcp_server: slack
        channel_ids: []
        acknowledgement_user_ids: []
```

For an approved read-only staging run, set `enabled: true`, `mode: shadow`, select `adapter: mcp`, enable only the providers being tested, populate their exact allowlists, and keep both `delivery_enabled` values false. Do not enable `gateway_enabled` for a local operator probe.

## Deterministic preflight

Run health before any resource read:

```bash
hermes kanban --board <board> review-runner health --json
```

Health may start only the explicitly selected MCP servers and discovers only the tools required by the selected read and delivery gates. Prompt and resource utility schemas are disabled for this boundary. If a selected server was already registered in the process with any additional tool, health fails closed rather than relying only on the per-call allowlist. It does not read a repository/channel resource and does not call a provider write tool.

Required signals:

- `configuration.valid=true`
- `providers.timeout_bounded=true`
- selected provider `read_registered=true`
- selected provider credential preflight `ready=true`
- `configuration.external_writes_enabled=false` while delivery gates are disabled
- `readiness.live_ready=false` while any enabled provider lacks its delivery gate/transport
- `readiness.production_ready=false`
- no `adapter_setup_failed:*` blocker

Health also returns an operator checklist, provider limitations, backlog counts, lease state, and the exact shadow command. Its diagnostics include a validated exception class and allowlisted failure kind only; unrecognized kinds become `unknown`, and provider exception text is not emitted.

## Linear OAuth and polling path

Linear OAuth connectivity is not event delivery. Validate a team and known issue using a read-only resource probe:

```bash
hermes kanban --board <board> linear-mcp health \
  --team <team-name-or-key> \
  --issue-id <issue-id> \
  --json
```

Required signals:

- `status=ready`
- `oauth_configured=true`
- `stages.configured=true`
- `stages.connected=true`
- `stages.discovered=true`
- `stages.resource_authorized=true`
- `stages.write_enabled=false`

The defined polling command is the same probe scheduled as a no-agent, read-only process (recommended cadence: every five minutes). Snapshot ingestion into the Linear coordinator is not implemented in this boundary, so production activation remains blocked until that ingestion path is reviewed. Do not treat OAuth as a webhook and do not create a production cron from this runbook while that blocker remains.

## GitHub, CodeRabbit, and Slack shadow validation

First probe one explicit resource route. This command is read-only: it does not
open the board database and reports `external_provider_writes=false` and
`local_writes=false`.

```bash
hermes kanban --board <board> review-runner staging \
  --repository <owner/name> \
  --pr-number <number> \
  --expected-head-sha <40-character-sha> \
  --channel-id <approved-channel-id> \
  --thread-ts <approved-existing-thread-ts> \
  --json
```

The GitHub arguments are mandatory. The Slack arguments are mandatory together
when the Slack provider is enabled. The command fails closed on a repository,
PR, head, channel, or thread mismatch; it never substitutes a resource.

Use a known Linear issue already linked to authoritative local coordinator rows:

```bash
hermes kanban --board <board> review-runner run \
  --mode shadow \
  --linear-issue-id <issue-id> \
  --json
```

Shadow mode may insert immutable local reconciliation and acknowledgement audit rows. It does not process outbound GitHub or Slack intents and performs no external write.

Validate:

1. GitHub returns the approved private repository and full exact head SHA.
2. Status/check evidence is for that same head.
3. CodeRabbit evidence is classified as `pending`, `clean`, `actionable`, `no_actionable_comments`, `skipped`, `paused`, `rate_limited`, `unavailable`, or `stale` without collapsing those states.
4. Slack reads remain inside the configured channel and user allowlists.
5. Rerunning the same input reuses deterministic reconciliation/audit identities.
6. Timeouts, permission failures, rate limits, missing tools, malformed provider data, and head changes fail closed.

A successful process exit is not sufficient: inspect `errors`, reconciliation findings, and exact-head evidence.

## Known provider limitations and gates

### GitHub MCP null `commit_id`

Some GitHub MCP review/review-comment responses may return a null `commit_id`. The adapter refuses the entire snapshot instead of skipping that evidence. Health reports this policy as `fail_closed`.

### Authoritative review-thread resolution

The configured GitHub MCP comment surface does not expose authoritative thread-resolution state. Current-head root comments are therefore treated conservatively as unresolved/actionable. A PR with such comments cannot be cleared by this adapter. Production approval remains blocked until an authoritative resolution source is added and reviewed.

### Slack staging authorization

The runner requires both an exact channel allowlist and exact acknowledgement user allowlist before it registers the Slack read adapter. It never lists channels, broadens membership, or searches for a substitute destination. An existing sent outbox thread is required to prove thread/reaction read access.

### Live delivery

Delivery is gated independently from provider reads:

- GitHub delivery registers only `create_pull_request_review` plus the existing review readback. It sends `event=COMMENT` with the immutable full head SHA and a deterministic receipt marker. `request_reviewer` fails closed because that write tool is not exposed.
- Slack delivery registers only channel history/thread readback plus post/reply. It uses the immutable outbox channel/thread route and a deterministic receipt marker.
- Both transports search provider readback for the marker before retrying an ambiguous side effect. A receipt must preserve the exact head (GitHub) or exact channel/thread (Slack).
- Enabling one provider's delivery does not enable the other. If both providers are enabled, `live_ready` requires both delivery transports.

`mode: live` does not create authority. Candidates are skipped when the required snapshot + delivery transport pair is absent, and delivery remains disabled by default.

## Timeout, retry, and idempotency behavior

- Each MCP call has a bounded provider timeout; the runner also has a larger wall-clock deadline and lease.
- A run stops before starting a provider call that cannot fit inside the remaining deadline.
- Read failures fail the pass and are retried only by a later operator/scheduler invocation.
- Outbox retry ceilings, per-intent attempt leases, typed transient/rate-limit categories, and provider readback markers remain the delivery idempotency boundary.
- Never reduce safety by retrying an unknown side effect in-process.

## Rollout sequence

1. Keep runner, providers, and both delivery gates disabled.
2. Add credential references and exact allowlists.
3. Set `enabled: true`, `mode: shadow`, and enable one read provider with `adapter: mcp`; leave its `delivery_enabled: false`.
4. Run health, the deterministic `staging` resource probe, and the Linear OAuth resource probe. Stop on any auth, permission, head, channel, or thread failure.
5. Run one known Linear issue in shadow and repeat the same input to prove deterministic/idempotent local results.
6. Resolve every health blocker and known provider limitation.
7. Obtain explicit approval for the exact provider, repository/PR or channel/thread, production integration, cron routing, gateway routing, and live-delivery window.
8. Enable exactly one provider's `delivery_enabled: true` while remaining in `mode: shadow`; rerun health and staging. Verify the discovered tool allowlist contains only the documented restricted delivery tools.
9. Change to `mode: live` only for the approved destination/window. Inspect the provider receipt and outbox attempt before enabling a second destination.
10. Deploy code, restart the gateway only if gateway-hosted code changed, then rerun health and staging verification.

This repository does not perform steps 7–10 automatically.

## Rollback

1. Set `kanban.review_runner.mode: shadow` (or `dry-run`) and both provider `delivery_enabled` values to `false`.
2. Set `kanban.review_runner.enabled: false`, then set both provider `enabled` values to `false` and adapters to `disabled`.
3. Disable/remove the external cron or gateway route using the same operator surface that created it.
4. Restart the gateway only if its loaded code/config requires it.
5. Run health and confirm `external_writes_enabled=false`, both `write_registered=false`, `live_ready=false`, and no active runner lease.
6. Preserve reconciliation, gate, outbox, and acknowledgement rows for audit. Do not delete or rewrite history as rollback.

The runner reports `hermes gateway restart` and `hermes gateway status` as operator commands; it never executes a restart itself.

## Failure diagnosis

| Signal | Meaning | Operator response |
|---|---|---|
| `credential readiness is blocked` | Missing/unresolved env reference or plaintext secret in config | Move the secret to approved storage; keep provider disabled |
| `required MCP tools were not discovered` | Wrong server/package/tool surface or connection failure | Run `hermes mcp test <server>` and verify package/version/config |
| `permission` / `auth` | Credential lacks resource access or is invalid | Fix least-privilege access; do not broaden allowlists speculatively |
| `rate_limited` | Provider quota/backoff | Stop manual loops; retry on a later scheduled pass |
| `commit_id is unavailable` | Exact-head evidence cannot be established | Keep the gate blocked; use an authoritative provider that supplies commit identity |
| `actionable_review_thread` | Current-head unresolved comment exists (or cannot be proven resolved) | Resolve in GitHub and re-read through an authoritative resolution source |
| `head mismatch` / `stale` | Evidence targets an older PR head | Supersede old intent/evidence and rerun on the current full SHA |
| `adapter_not_registered` | Provider enabled without a complete safe read/delivery pair | Keep live mode disabled and correct config/implementation |
| runner lease held | Another bounded pass owns the board lease | Wait for completion/expiry; do not bypass the lease |

Never paste raw provider errors into tickets or chat without redaction.
