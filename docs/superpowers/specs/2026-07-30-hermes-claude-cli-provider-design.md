# Hermes-Native Claude CLI Provider Design

**Date:** 2026-07-30

**Status:** Approved approach; awaiting written-spec review

## Problem

Hermes currently treats an Anthropic OAuth credential as a direct Anthropic
Messages API credential. It sends the credential to `api.anthropic.com` while
adding Claude Code-shaped headers. Anthropic now classifies this request as
third-party usage, so a Claude Max account without extra-usage credit receives
HTTP 400 before inference begins.

The official `claude` executable on this host is separately authenticated with
the operator's Claude Max subscription and reports a first-party `claude.ai`
login. EchoGrid succeeds with that subscription because it launches the
official executable rather than replaying its OAuth credential.

## Goal

Add a `claude-cli` inference provider that uses the installed, authenticated
Claude Code executable while preserving Hermes as the agent runtime.

Hermes must continue to own:

- canonical conversation history and session lifecycle;
- Signal and desktop delivery;
- memory, skills, system instructions, and context compression;
- tool selection, approval, execution, and persistence;
- fallback selection and provider accounting;
- operator-visible model/provider receipts.

Claude CLI supplies only the next assistant decision: final text or one or more
Hermes tool requests.

## Non-Goals

- Do not remove the direct `anthropic` provider. It remains valid for Anthropic
  API keys and accounts with permitted extra usage.
- Do not copy, parse, refresh, or persist Claude Code OAuth credentials.
- Do not imitate Claude Code HTTP headers or call Anthropic's API directly from
  the new provider.
- Do not let Claude CLI execute its built-in terminal, filesystem, browser, or
  MCP tools.
- Do not replace Hermes sessions with Claude sessions or hide Claude's native
  session identity.
- Do not redesign the general provider abstraction.
- Do not import EchoGrid source as a dependency. Its subprocess pattern is
  evidence and a reference, not shared runtime code.

## Selected Architecture

### Provider identity

Introduce a distinct provider ID, `claude-cli`.

`claude-cli` means:

- provider class: local subprocess;
- billing/auth mode: Claude subscription through the official executable;
- model names: provider-native aliases such as `opus`, with the model receipt
  reported by Claude CLI when available;
- health check: `claude auth status`;
- inference command: `claude --print`;
- credentials: owned exclusively by Claude Code.

The existing `anthropic` provider continues to mean direct Anthropic API
transport. The two identities must never alias to each other in auth
resolution.

### Process boundary

Hermes launches Claude with a discrete argument array and `shell=False`.

The child environment:

- inherits the normal user environment needed to locate and run `claude`;
- removes `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN`, and Hermes provider-secret overrides;
- leaves Claude Code's own credential store untouched;
- sets no bypass-permission flags.

Claude is invoked with:

- `--print`;
- `--output-format json` for bounded, machine-readable completion;
- `--json-schema <schema>` for the Hermes decision envelope;
- `--tools ""` so Claude cannot execute built-in tools;
- `--session-id <uuid>` for a new provider-native session, or
  `--resume <uuid>` for an existing one;
- `--model <alias>` when the configured model is explicit;
- a bounded timeout and Windows-hidden child-process options.

Prompts and tool results go through stdin or a discrete argv value supported by
the CLI. They are never interpolated into a command string.

### Decision envelope

Claude returns one JSON object matching this logical shape:

```json
{
  "kind": "final",
  "text": "Response for the user"
}
```

or:

```json
{
  "kind": "tool_calls",
  "calls": [
    {
      "id": "provider-generated-stable-id",
      "name": "terminal",
      "arguments": {
        "command": "git status --short"
      }
    }
  ]
}
```

Rules:

- `kind=final` requires non-empty `text` and forbids `calls`.
- `kind=tool_calls` requires at least one call and forbids final text.
- Every call name must match a tool Hermes exposed for that turn.
- Every `arguments` value must validate against that Hermes tool's schema.
- Unknown fields, malformed JSON, duplicate call IDs, invalid tool names, and
  invalid arguments fail closed as provider protocol errors.
- Hermes converts a valid decision into its existing internal assistant/text
  or tool-call representation. The existing conversation loop remains
  responsible for execution and subsequent model turns.

### Prompt and tool protocol

On the first Claude turn for a Hermes session, Hermes sends:

- the stable Hermes system prompt;
- the current tool catalog and JSON schemas;
- the conversation transcript needed to reconstruct the current turn;
- the decision-envelope contract;
- the current user message.

On subsequent calls, Hermes resumes the same Claude session and sends only the
new semantic delta:

- the next user message; or
- Hermes tool results keyed by the prior decision's call IDs; or
- an explicit context-compression/reset frame.

The tool catalog is immutable within a Claude provider session. If Hermes must
change toolsets immediately, compress context, or rebuild its system prompt,
the adapter starts a new Claude session and bootstraps it from Hermes's
canonical transcript. This follows Hermes's existing prompt-cache invariant:
stable conversations keep a stable provider-native session; invalidation is
explicit rather than silent.

### Durable session mapping

Store a provider attachment for each Hermes session:

```json
{
  "provider": "claude-cli",
  "hermes_session_id": "20260730_010103_73299221",
  "claude_session_id": "uuid",
  "model_requested": "opus",
  "model_reported": "claude-opus-5",
  "tool_catalog_fingerprint": "sha256:...",
  "system_prompt_fingerprint": "sha256:...",
  "last_success_at": 1785390000.0
}
```

Requirements:

- The mapping is profile-scoped under `get_hermes_home()`.
- Raw Claude credentials are never stored in the mapping.
- A Hermes session has at most one active Claude attachment.
- Resume uses the recorded Claude session ID only when the provider, model,
  tool fingerprint, and system-prompt fingerprint still match.
- Missing, corrupt, or incompatible mappings fail closed to a new Claude
  session bootstrapped from Hermes's canonical transcript.
- Session reset, deletion, and profile isolation must include the attachment
  lifecycle.

The implementation should use Hermes's durable SQLite state where a suitable
provider-attachment seam exists. If no compatible seam exists, use one
profile-scoped JSON store with atomic replace and a process lock; do not add
provider data to the legacy `sessions.json` mirror.

### Failure and fallback behavior

Map failures into Hermes's existing provider error classes:

- executable missing or not spawnable: unreachable;
- `claude auth status` not logged in: authentication required;
- subscription limit message: quota exhausted;
- timeout: transient provider timeout;
- nonzero exit without a quota/auth signature: provider execution failure;
- malformed or schema-invalid output: provider protocol failure;
- stale/missing Claude session on resume: retry once with a newly bootstrapped
  Claude session, then fail normally;
- cancellation or Hermes `/stop`: terminate the exact child process and its
  owned process tree.

Only after the bounded provider-specific retry may Hermes enter its normal
fallback chain. The user sees a fallback banner only when `claude-cli`
actually fails, not on every successful turn.

Errors and logs may include:

- executable version;
- exit code;
- duration;
- redacted provider/session identifiers;
- classified failure reason.

They must not include credentials, complete environment dumps, or raw prompts.

### Provider selection and UI

Add `claude-cli` to Hermes's provider registry and model picker with a clear
label such as **Claude Code (subscription)**.

Selection requirements:

- health and auth state come from `claude auth status`;
- no Hermes OAuth login is offered for this provider;
- the UI reports the configured alias and, after a successful call, the
  provider-reported active model when available;
- `anthropic` remains labeled as the direct API/extra-usage route;
- fallback receipts distinguish `claude-cli` from `anthropic`;
- auxiliary model selection must not silently route `claude-cli` work through
  direct Anthropic OAuth.

For the operator's profile, the intended final order is:

1. `claude-cli / opus`
2. `openai-codex / gpt-5.6-sol`

The profile is changed only after the new provider passes unit, integration,
desktop, and live subscription verification.

## Components

The implementation should keep responsibilities separated:

1. **Claude CLI process client**
   - command construction;
   - sanitized environment;
   - timeout, cancellation, and Windows process handling;
   - stdout/stderr collection;
   - auth/version probes.

2. **Decision protocol**
   - JSON Schema;
   - prompt frames;
   - strict parsing and tool-schema validation;
   - translation to Hermes's existing response objects.

3. **Session attachment store**
   - Hermes-to-Claude session mapping;
   - profile isolation;
   - fingerprint compatibility;
   - atomic persistence and lifecycle cleanup.

4. **Provider integration**
   - registry/model-picker entry;
   - agent initialization;
   - primary and fallback call paths;
   - error classification and usage receipts;
   - auxiliary-client behavior.

These units must be independently testable. The process client must accept an
injectable executable path so tests can spawn a deterministic fixture process
instead of mocking subprocess behavior.

## Testing Strategy

All implementation follows red-green-refactor TDD and uses
`scripts/run_tests.sh`, never direct `pytest`.

### Process client tests

- builds a discrete argv array with `shell=False`;
- strips Anthropic/Hermes credential overrides from the child environment;
- preserves the remaining environment;
- runs the auth and version probes;
- captures valid JSON;
- classifies missing executable, nonzero exit, quota text, timeout, and
  cancellation;
- terminates the owned process tree on Windows.

### Protocol tests

- accepts valid final text;
- accepts valid single and parallel tool calls;
- rejects malformed JSON, unknown fields, empty output, duplicate IDs, unknown
  tools, and schema-invalid arguments;
- converts decisions into the same internal response types used by existing
  providers;
- emits deterministic first-turn and resume frames.

### Session tests

- creates and resumes one Claude attachment per Hermes session;
- isolates profiles;
- rejects mismatched tool/system fingerprints;
- bootstraps from canonical history after stale-session failure;
- removes or invalidates attachments during session lifecycle operations;
- never writes credentials or raw prompts.

### Provider integration tests

- a fixture executable completes a text-only Hermes turn;
- a fixture executable requests a Hermes tool, consumes its result, and
  completes the turn;
- fallback activates only after classified CLI failure;
- auxiliary calls do not leak into direct Anthropic OAuth;
- desktop and gateway receipts identify `claude-cli`;
- an existing `anthropic` API-key configuration continues to use the direct
  provider unchanged.

### Guarded live test

With an explicit live-test flag:

1. verify `claude auth status` reports a logged-in `claude.ai` account;
2. create a fresh Hermes session using `claude-cli / opus`;
3. obtain the exact response `HERMES_CLAUDE_CLI_OK`;
4. perform one harmless Hermes-owned tool call and return its result;
5. resume the same Claude session for a second message;
6. verify logs and usage receipts show `claude-cli`, not `anthropic`;
7. verify no fallback banner and no direct `api.anthropic.com` request from
   Hermes;
8. verify the existing Signal and desktop paths can use the provider.

## Acceptance Criteria

The feature is accepted only when:

- Hermes can complete and resume a Claude Opus conversation through the
  official CLI using the current Claude Max login;
- Hermes remains the owner of tools, approvals, memory, sessions, delivery,
  and fallback;
- Claude CLI built-in tools are disabled;
- Hermes neither reads nor transmits Claude OAuth credentials;
- provider/tool/session failures are bounded, classified, and cancellable;
- direct `anthropic` API access remains backward compatible;
- GPT-5.6 fallback still works;
- relevant focused suites and the repository's required broader gates pass;
- a guarded live CLI, desktop, and Signal verification succeeds on the exact
  candidate commit.

## Rollout

Ship `claude-cli` as a new explicit provider. Do not silently reinterpret
existing `anthropic` configurations.

After verification:

1. change the operator profile primary to `claude-cli / opus`;
2. keep `openai-codex / gpt-5.6-sol` as fallback;
3. restart the gateway and desktop backend;
4. start a new desktop session and a new Signal session;
5. verify no routine fallback notification appears;
6. preserve the prior configuration values in the normal Hermes backup path
   so rollback is straightforward.
