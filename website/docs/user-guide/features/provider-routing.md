---
title: Provider Routing
description: Configure OpenRouter or Nous Portal provider preferences to optimize for cost, speed, or quality.
sidebar_label: Provider Routing
sidebar_position: 7
---

# Provider Routing

When using [OpenRouter](https://openrouter.ai) or [Nous Portal](/integrations/nous-portal) as your LLM provider, Hermes Agent supports **provider routing** — fine-grained control over which underlying AI providers handle your requests and how they're prioritized.

OpenRouter routes requests to many providers (e.g., Anthropic, Google, AWS Bedrock, Together AI). Provider routing lets you optimize for cost, speed, quality, or enforce specific provider requirements.

:::tip
Traffic routed through Nous Portal respects the same provider preferences — and Portal subscribers get 10% off token-billed providers.
:::

## Configuration

Add a `provider_routing` section to your `~/.hermes/config.yaml`:

```yaml
provider_routing:
  sort: "price"           # How to rank providers
  only: []                # Whitelist: only use these providers
  ignore: []              # Blacklist: never use these providers
  order: []               # Explicit provider priority order
  require_parameters: false  # Only use providers that support all parameters
  data_collection: null   # Control data collection ("allow" or "deny")
  quantizations: []       # Optional global quantization restriction
  allow_fallbacks: true   # Allow another eligible endpoint if routing fails
  model_overrides: {}     # Exact-model OpenRouter routing overrides
```

:::info
Provider routing only applies when using OpenRouter or Nous Portal. It has no effect with direct provider connections (e.g., connecting directly to the Anthropic API).
:::

## Options

### `sort`

Controls how OpenRouter ranks available providers for your request.

| Value | Description |
|-------|-------------|
| `"price"` | Cheapest provider first |
| `"throughput"` | Fastest tokens-per-second first |
| `"latency"` | Lowest time-to-first-token first |

```yaml
provider_routing:
  sort: "price"
```

### `only`

Whitelist of provider slugs. When set, **only** these providers will be used. All others are excluded. Use the lowercase slug shown by OpenRouter for each provider.

```yaml
provider_routing:
  only:
    - "anthropic"
    - "google"
```

### `ignore`

Blacklist of provider names. These providers will **never** be used, even if they offer the cheapest or fastest option.

```yaml
provider_routing:
  ignore:
    - "together"
    - "deepinfra"
```

### `order`

Explicit priority order. Providers listed first are preferred. Unlisted providers are used as fallbacks.

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
    - "amazon-bedrock"
```

### `require_parameters`

When `true`, OpenRouter will only route to providers that support **all** parameters in your request (like `temperature`, `top_p`, `tools`, etc.). This avoids silent parameter drops.

```yaml
provider_routing:
  require_parameters: true
```

### `data_collection`

Controls whether providers can use your prompts for training. Options are `"allow"` or `"deny"`.

```yaml
provider_routing:
  data_collection: "deny"
```

### `quantizations`

Restricts OpenRouter to endpoint quantizations such as `fp8`, `bf16`, or
`unknown`. It can be set globally or inside an exact-model override.

```yaml
provider_routing:
  quantizations: ["fp8"]
```

### `allow_fallbacks`

Controls whether OpenRouter may try another eligible endpoint when the requested
route is unavailable. `false` is meaningful and is preserved in the request.

```yaml
provider_routing:
  allow_fallbacks: false
```

## Model-Specific OpenRouter Routes

Use `model_overrides.openrouter` to constrain one exact OpenRouter model without
changing other models or direct-provider connections:

```yaml
provider_routing:
  sort: "price"
  allow_fallbacks: true
  model_overrides:
    openrouter:
      "deepseek/deepseek-v4-flash":
        only: ["baidu/fp8"]
        quantizations: ["fp8"]
        allow_fallbacks: false
```

The model key must match the active OpenRouter model ID exactly. Endpoint tags
come from OpenRouter and may include a variant suffix such as `baidu/fp8`; Hermes
stores the full returned tag rather than the display name.

Merge behavior is explicit:

- A missing field inherits the global `provider_routing` value.
- An empty list clears an inherited list restriction.
- Any blank string entry invalidates the entire list; ambiguous locks fail closed
  instead of silently narrowing the selected provider set.
- `false` remains `false`; it is not treated as missing.
- `null` omits that field from the request.
- Unknown routing keys are rejected by config validation.

### Automatic, Prefer, and Lock

Hermes Desktop exposes three modes for OpenRouter models:

- **Automatic** — no model-specific entry is stored. OpenRouter uses its normal
  behavior plus global routing defaults.
- **Prefer this endpoint** — stores `order`, `quantizations`, and
  `allow_fallbacks: true`.
- **Lock to this endpoint** — stores `only`, `quantizations`, and
  `allow_fallbacks: false`.

Selecting Automatic deletes the model's override entry entirely; it does not
save an object filled with empty lists.

Endpoint discovery is optional metadata. If discovery is unavailable, the model
picker remains usable and the Desktop manual editor accepts the exact provider
tag and quantization. A discovery failure never disables the selected model.
The same routing controls are available in Settings → Model and in profile
creation/setup; profile changes are saved to that profile rather than the active
profile. For OpenRouter, the model field accepts a typed `author/slug` ID (with
an optional `:suffix`) in addition to filtering the fetched suggestions, so new
or unlisted OpenRouter models remain usable.

:::warning Windows development builds and Desktop instance locking
When testing a Windows development or packaged Desktop build with an external
Python virtual environment, set all three variables in that build's environment:

```text
HERMES_HOME=%USERPROFILE%\.hermes-dev\openrouter-routing
HERMES_DESKTOP_HERMES_ROOT=C:\path\to\your\hermes-agent
HERMES_DESKTOP_PYTHON=%USERPROFILE%\.hermes\venvs\hermes-openrouter-dev\Scripts\python.exe
```

`HERMES_DESKTOP_HERMES_ROOT` must point to the fork root. If these variables are
not set, Desktop may fall back to bare system Python and fail imports such as
`yaml` even though the external virtual environment is correctly installed.

**Do not run the development/packaged Electron build alongside the production
Hermes Desktop app.** They share Electron's single-instance lock; close the
production app before launching the development build, and vice versa.
:::

## Practical Examples

### Optimize for Cost

Route to the cheapest available provider. Good for high-volume usage and development:

```yaml
provider_routing:
  sort: "price"
```

### Optimize for Speed

Prioritize low-latency providers for interactive use:

```yaml
provider_routing:
  sort: "latency"
```

### Optimize for Throughput

Best for long-form generation where tokens-per-second matters:

```yaml
provider_routing:
  sort: "throughput"
```

### Lock to Specific Providers

Ensure all requests go through a specific provider for consistency:

```yaml
provider_routing:
  only:
    - "anthropic"
```

### Avoid Specific Providers

Exclude providers you don't want to use (e.g., for data privacy):

```yaml
provider_routing:
  ignore:
    - "together"
    - "lepton"
  data_collection: "deny"
```

### Preferred Order with Fallbacks

Try your preferred providers first, fall back to others if unavailable:

```yaml
provider_routing:
  order:
    - "anthropic"
    - "google"
  require_parameters: true
```

## How It Works

Provider routing preferences are passed to OpenRouter or Nous Portal on agent chat requests and iteration-limit summaries via the `extra_body.provider` field. (`extra_body` is the OpenAI Python SDK argument; it becomes the top-level `provider` object in the JSON request.) Auxiliary tasks such as compression and title generation are configured independently under `auxiliary.<task>.extra_body`.

- **CLI mode** — configured in `~/.hermes/config.yaml`, loaded at startup
- **Gateway mode** — same config file, loaded when the gateway starts

The routing config is read from `config.yaml` and passed as parameters when creating the `AIAgent`:

```
providers_allowed  ← from provider_routing.only
providers_ignored  ← from provider_routing.ignore
providers_order    ← from provider_routing.order
provider_sort      ← from provider_routing.sort
provider_require_parameters ← from provider_routing.require_parameters
provider_data_collection    ← from provider_routing.data_collection
provider_quantizations      ← from provider_routing.quantizations
provider_allow_fallbacks    ← from provider_routing.allow_fallbacks
```

For OpenRouter, Hermes then overlays the exact active model entry from
`provider_routing.model_overrides.openrouter` before building the request's
`provider` object. Auxiliary models keep their independent routing configuration
and never inherit an override belonging to a different model.

:::tip
You can combine multiple options. For example, sort by price but exclude certain providers and require parameter support:

```yaml
provider_routing:
  sort: "price"
  ignore: ["together"]
  require_parameters: true
  data_collection: "deny"
```
:::

## Default Behavior

When no `provider_routing` section is configured (the default), the aggregator uses its own default routing logic, which generally balances cost and availability automatically.

:::tip Provider Routing vs. Fallback Models
Provider routing controls which **sub-providers behind OpenRouter or Nous Portal** handle your requests. For automatic failover to an entirely different provider when your primary model fails, see [Fallback Providers](/user-guide/features/fallback-providers).
:::
