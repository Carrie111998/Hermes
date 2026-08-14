---
title: "Recovering Mcp Tunnel Account Context — Use when MCP tunnel setup fails before daemon traffic"
sidebar_label: "Recovering Mcp Tunnel Account Context"
description: "Use when MCP tunnel setup fails before daemon traffic"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Recovering Mcp Tunnel Account Context

Use when MCP tunnel setup fails before daemon traffic.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/autonomous-ai-agents/recovering-mcp-tunnel-account-context` |
| Path | `optional-skills/autonomous-ai-agents/recovering-mcp-tunnel-account-context` |
| Version | `0.1.0` |
| Author | Wade Packard (brandonwadepackard-cell), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `MCP`, `Tunnel`, `OpenAI`, `Recovery`, `Read-Only` |
| Related skills | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Recovering MCP Tunnel Account Context Skill

Recover secure MCP tunnels that are locally healthy but fail in the ChatGPT
control plane. This procedure preserves the working rollback path, protects
credentials, and proves a replacement end to end.

## When to Use

- Connector creation fails with `424`, upstream `401`, or an active-
  organization-context error.
- The managed runtime is ready but a connector attempt produces zero daemon
  traffic.
- ChatGPT and the OpenAI Platform may use different accounts, organizations,
  or workspaces.

Do not use when the request reaches the daemon. Debug the MCP server or managed
runtime instead.

## Prerequisites

- A current `tunnel-client` installation and access to its managed-runtime
  status and logs.
- Hermes with the restricted command `hermes mcp serve --read-only`.
- Access to the ChatGPT workspace and OpenAI Platform organization intended to
  own the new tunnel. The user completes login, OAuth, and 2FA privately.
- An intact old tunnel/runtime/profile to preserve as rollback.

## How to Run

Use `terminal` for current CLI help, sanitized status, and log correlation. Use
`browser_navigate` for the official ChatGPT and Platform account surfaces, and
`web_search` or `web_extract` only for official OpenAI documentation. Do not
show or execute syntax until every subcommand, flag, and key-source form is
verified from those current sources.

## Quick Reference

| Evidence | Work next |
|---|---|
| Request reached daemon | Local MCP/runtime path |
| Zero daemon traffic plus organization-context error | Identity chain |
| Aligned identity chain plus same error | Capture once, then escalate |

A Platform usage balance and a ChatGPT subscription are separate. A new
account matters because it can split the identity chain, not because debt
travels through the tunnel.

## Deployment Ownership

Account-context recovery and same-alias maintenance are different operations.
During account-context recovery, keep the old runtime running and create a
distinct replacement. Do not unload the healthy rollback runtime before the
replacement has passed connector, discovery, harmless-read, and post-cutover
gates.

For same-alias maintenance, first identify the mechanism that owns that exact
alias. If a service manager owns it, stop or unload only that exact service
immediately before a verified manual launcher is used, never run both owners
concurrently, and restore normal supervision immediately afterward.

Process/state metadata is authoritative only for the mechanism that created
it. A service-owned process plus its live health probe may disagree with stale
manual-launcher metadata. Local runtime health is not end-to-end proof.

## Procedure

### 1. Freeze rollback

Record the old alias, tunnel/profile paths, and sanitized runtime state. Do not
copy or export profiles, environment files, cookies, tokens, or key material.
Keep the old runtime running.

**Done when:** the old runtime remains healthy and untouched.

### 2. Classify one captured failure

Record a timestamp and log offsets, submit one connector attempt, and correlate
the sanitized connector response with daemon traffic. Zero daemon traffic plus
the organization-context error is a control-plane signal; daemon traffic sends
the investigation back to the local MCP/runtime path.

**Done when:** evidence, not local readiness alone, identifies the failing side.

### 3. Align the identity chain

Side by side, compare the masked ChatGPT account and selected workspace with
the masked Platform account and active organization. Matching displayed email
text is insufficient: the account must be offered the intended ChatGPT
workspace and be authorized for the Platform organization.

**Done when:** one account context owns or can access every new resource.

### 4. Create the replacement in parallel

Create a distinct tunnel under the aligned context and use only its new IDs;
never reuse an old workspace ID. Create the runtime key through the official
picker and store it through an environment/provider wizard or hidden input in
an owner-only file. Never print it or place it in chat, argv, logs, or archives.

Executable syntax is allowed only after live verification with `terminal` help
or official documentation. A handoff, memory, prior attempt, or agent answer
is not proof. If syntax remains unverified, describe the operation without a
code block and mark it unresolved.

**Done when:** distinct tunnel, alias, profile, and key reference exist while
the old runtime stays live.

### 5. Start restricted Hermes

Run the verified replacement flow with `hermes mcp serve --read-only`. If the
managed-runtime command owns the process, require its process and state fields,
live health/readiness, and no current error. If a service manager owns it,
require the service-owned process plus its live health probe; do not substitute
stale metadata from a different launcher.

**Done when:** the replacement runtime is ready before connector creation.

### 6. Attach once

From the aligned ChatGPT workspace, create and link the connector through the
official UI. Capture only a sanitized result.

**Done when:** connector creation succeeds and link status is `ACTIVE`.

### 7. Prove the read-only boundary

Trigger discovery and one harmless read call. Require daemon receipt,
successful response posting, and exactly this tool set:

- `attachments_fetch`
- `channels_list`
- `conversation_get`
- `conversations_list`
- `events_poll`
- `events_wait`
- `messages_read`
- `permissions_list_open`

Every tool must advertise `readOnlyHint=true` and `destructiveHint=false`.
Require `messages_send` and `permissions_respond` absent from discovery. Do not
invoke a mutating tool merely to test that it is absent.

**Done when:** exact discovery and a real read round trip both pass.

### 8. Cut over safely

Stop only the precisely identified old runtime. Preserve its tunnel,
connector, profile, alias, and key reference for rollback. Recheck the new
runtime and repeat one harmless read after the old runtime stops. This is the
first point at which recovery may stop or unload the healthy rollback service.

**Done when:** the replacement remains ready and independent after cutover.

## Pitfalls

- Blaming plan gating before identity proof.
- Adding organization headers to the local daemon for a pre-daemon failure.
- Calling remembered syntax an example. Users copy examples; omit unverified
  commands.
- Assuming shell variables make an unverified secret workflow safe.
- Declaring success from HTTP `200`, visible tools, or runtime readiness alone.
- Opening support before testing one matched identity chain.

## Verification

Recovery requires every gate: aligned identity chain, ready restricted runtime,
connector success, `ACTIVE` link, correlated daemon traffic, exact read-only
tool discovery, mutating tools absent, a harmless read round trip, post-cutover
readiness, and rollback artifacts preserved.

If a matched identity chain still fails before daemon traffic, stop rebuilding.
Escalate one sanitized captured attempt with timestamp and request/correlation
identifier; include no credentials or full account identifiers.
