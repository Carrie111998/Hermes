# Hermes Subscription Routing and Desktop Design

## Goal

Configure the existing Hermes installation to use the operator's subscriptions
with the smallest possible footprint:

- Claude Max is the primary inference provider.
- ChatGPT through OpenAI Codex is the fallback provider.
- The existing Electron/React desktop application is packaged and reachable
  from a Hermes shortcut on the Windows desktop.

## Chosen approach

Use Hermes' existing `anthropic` OAuth provider, `openai-codex` OAuth provider,
fallback chain, and `hermes desktop` packaging command. Do not add or modify
provider adapters.

The operator explicitly accepts Hermes' current Anthropic OAuth implementation,
including its Claude Code-compatible request identity. Credentials remain owned
by Hermes and are not copied into source, documentation, the shortcut, or the
desktop package.

## Configuration flow

1. Authenticate Hermes' `anthropic` provider with OAuth.
2. Select `claude-opus-5`, verified on the operator's authenticated Anthropic
   model endpoint, as the primary model.
3. Authenticate Hermes' `openai-codex` provider with OAuth.
4. Add an OpenAI Codex subscription-backed model as the first fallback.
5. Preserve the existing Copilot credential pool; do not make it primary or add
   it to the fallback chain.

If an OAuth flow requires browser confirmation, the operator completes that
provider-owned confirmation. A failed or cancelled login leaves the previous
Hermes configuration intact.

## Runtime behavior

Hermes sends ordinary turns to the configured Anthropic primary. It invokes the
OpenAI Codex fallback only for failures Hermes already classifies as eligible,
such as rate limits, overload, or connection failures. Authentication errors
remain visible and must not silently retarget an unrelated provider.

The desktop app uses the same Hermes home, provider configuration, credentials,
sessions, skills, and backend as the CLI. It does not keep a second provider
configuration.

## Desktop delivery

Use `hermes desktop --build-only` to build the existing packaged Windows app.
Resolve the produced `Hermes.exe` from the build output, then create a Windows
desktop shortcut named `Hermes.lnk` that:

- targets that exact packaged executable;
- uses the packaged Hermes icon;
- starts in `D:\AI-Foundry`;
- contains no credentials or provider arguments.

Launching the shortcut starts the native Electron/React interface and its
headless Hermes backend.

## Verification

Verification is operational and uses the live installation:

1. `hermes auth status anthropic` reports logged in.
2. `hermes auth status openai-codex` reports logged in.
3. Hermes configuration reports Anthropic as primary and OpenAI Codex as the
   first fallback.
4. A real Hermes prompt succeeds through the primary provider.
5. A bounded fallback check confirms the fallback entry resolves and can answer
   without permanently damaging the primary configuration.
6. The packaged executable launches, reaches the backend, and can send a prompt.
7. The desktop shortcut launches the same packaged executable.

No claim of success is made from configuration files or build output alone.

## Failure and rollback

- OAuth cancellation: retain the prior provider state and report which login
  remains incomplete.
- Desktop build failure: retain CLI functionality and report the failing build
  stage; do not create a shortcut to a missing executable.
- Shortcut failure: remove or replace only the Hermes shortcut after resolving
  its exact target.
- Routing rollback: restore the captured pre-change Hermes configuration and
  fallback chain. Do not delete unrelated credentials.

## Non-goals

- Porting EchoGrid's Claude CLI or Codex CLI adapters.
- Rewriting Hermes provider architecture.
- Removing Hermes' existing Anthropic OAuth identity behavior.
- Changing EchoGrid.
- Adding Copilot or API-key billing to the selected route.
