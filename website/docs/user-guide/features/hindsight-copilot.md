---
sidebar_position: 4.5
title: "Hindsight with GitHub Copilot"
description: "Use your GitHub Copilot subscription as the LLM backend for Hindsight local embedded memory."
---

# Hindsight with GitHub Copilot

Hindsight local embedded mode can use GitHub Copilot as its LLM backend. Hermes exposes this as the `copilot` choice in `hermes memory setup` and maps it to Hindsight's native `github-copilot` provider.

This path requires **Hindsight 0.9.2 or newer**, the first Hindsight release with the native GitHub Copilot provider. Hermes installs that minimum automatically when you select Copilot during local embedded setup.

## Why the native provider matters

GitHub Copilot is not a generic OpenAI-compatible API key endpoint. Hindsight's native provider uses the official Copilot SDK and its authentication/runtime behavior instead of sending ordinary OpenAI requests to `api.githubcopilot.com`. That keeps Copilot-specific request metadata, model handling, authentication, and session isolation inside the supported Hindsight integration.

Hermes therefore does **not** pass `HINDSIGHT_LLM_API_KEY` or an OpenAI-style base URL when Copilot is selected.

## Authentication

The Hindsight daemon inherits the same GitHub authentication sources Hermes already understands. In priority order, Hermes can resolve:

- `COPILOT_GITHUB_TOKEN`
- `GH_TOKEN`
- `GITHUB_TOKEN`
- `gh auth token`

When Hermes can resolve a raw GitHub token, it exposes it to the embedded Hindsight daemon as `COPILOT_GITHUB_TOKEN`. Hindsight can also use a signed-in Copilot/GitHub CLI account supported by the official Copilot SDK.

Classic `ghp_*` personal access tokens are not supported by the Hermes Copilot auth path. Use the normal Hermes/Copilot login flow, a supported OAuth/GitHub App token, or a fine-grained token with the required Copilot permission.

## Setup

Run:

```bash
hermes memory setup
```

Then choose:

1. **Hindsight**
2. **Local Embedded**
3. **copilot**
4. Keep the default Copilot model or enter another model available to your Copilot plan

The default model follows Hindsight's native provider default: `gpt-5.6-terra`.

No separate Hindsight LLM API key is requested for Copilot.

## Resulting embedded profile

Hermes materializes the Hindsight profile with values equivalent to:

```text
HINDSIGHT_API_LLM_PROVIDER=github-copilot
HINDSIGHT_API_LLM_MODEL=gpt-5.6-terra
```

It intentionally does not write `HINDSIGHT_API_LLM_API_KEY` or `HINDSIGHT_API_LLM_BASE_URL` for this provider.

## Troubleshooting

If Copilot authentication is unavailable, first verify that Hermes itself can use Copilot. For environment-based auth, confirm one of the supported GitHub token variables is available to the Hermes process. For CLI auth, verify `gh auth token` or your Copilot CLI sign-in works under the same OS user that runs Hermes.

If Hindsight reports that `github-copilot` is unknown, upgrade the embedded runtime to Hindsight 0.9.2 or newer and rerun `hermes memory setup`.
