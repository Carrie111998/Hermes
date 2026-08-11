# Implementation Plan — Per-Request Generation Parameters for the API Server

**Author:** Isabel (draft for Rob's review)
**Date:** 2026-08-11
**Fork:** `/opt/data/src/hermes-agent-param-fix/` (hermes 0.20.0)
**Goal:** Let the ISABEL stack's `chat-service` pass generation parameters (temperature, top_p, max_tokens, and arbitrary `extra_body`) into Hermes per-request, so the Modulator Engine's mood → temperature/max-token mapping can flow end-to-end into the actual LLM call.

---

## 1. The Discovery (why this is smaller than we thought)

Hermes **already has a per-request override channel**: `model_options`.

- The browser extension sends `model_options` in the request body.
- `_request_reasoning_config(model_options)` (api_server.py:268) translates `reasoning` / `reasoning_effort` into the agent's `reasoning_config`.
- `_request_service_tier(model_options)` (api_server.py:296) translates `service_tier` / `fast` into a per-request service tier.
- These flow into `_create_agent(...)` → agent kwargs → `request_overrides` → `build_api_kwargs()` → the actual API call.

So the plumbing for *per-request* overrides already exists and is proven. It's just scoped to reasoning and service tier. **We extend it to generation parameters.** No new architecture, no new channel — one more translator function and a merge.

**The gap confirmed:** the chat completions handler reads `messages`, `stream`, `model`, `system_prompt`, `title`, and `model_options` — but a grep for `temperature|top_p|max_tokens|extra_body` in `gateway/platforms/api_server.py` returns zero matches. Those fields are silently ignored today.

---

## 2. The Design

### 2.1 Request shape (what the stack sends)

Extend the existing `model_options` object (keeps one override channel, backward compatible):

```json
{
  "model": "gemma4:e2b",
  "messages": [...],
  "model_options": {
    "reasoning_effort": "high",
    "service_tier": "priority",
    "generation": {
      "temperature": 0.66,
      "top_p": 0.9,
      "max_tokens": 3994,
      "extra_body": {
        "num_ctx": 32768,
        "repeat_penalty": 1.1
      }
    }
  }
}
```

`generation` is a new sub-object inside the existing `model_options`. Absent → no change (backward compatible). Present → merged into the request.

### 2.2 The translator (new function, mirrors existing pattern)

```python
def _request_generation_params(model_options: Any) -> Optional[Dict[str, Any]]:
    """Translate model_options.generation into request_overrides.

    Mirrors _request_reasoning_config / _request_service_tier. Returns
    None when absent so callers can distinguish "not provided" from
    "explicitly cleared".
    """
    if not isinstance(model_options, dict):
        return None
    generation = model_options.get("generation")
    if not isinstance(generation, dict) or not generation:
        return None

    overrides: Dict[str, Any] = {}
    for key in ("temperature", "top_p", "max_tokens", "extra_body"):
        if key in generation:
            overrides[key] = generation[key]
    return overrides or None
```

### 2.3 The merge (in `_create_agent`, next to the existing translators)

At api_server.py:2680, next to:

```python
request_reasoning_config = _request_reasoning_config(model_options)
request_service_tier = _request_service_tier(model_options)
```

add:

```python
request_generation_params = _request_generation_params(model_options)
```

Then, where the agent kwargs are assembled (the block that currently applies `request_reasoning_config` and `request_service_tier`), add a merge into `request_overrides`:

```python
if request_generation_params:
    agent_kwargs.setdefault("request_overrides", {}).update(request_generation_params)
```

### 2.4 The flow (verified in source)

```
chat-service POST /v1/chat/completions
  → body["model_options"]["generation"] = {temperature, top_p, max_tokens, extra_body}
  → _request_generation_params() → dict
  → _create_agent() → agent_kwargs["request_overrides"] merged
  → AIAgent.request_overrides
  → build_api_kwargs() (agent/chat_completion_helpers.py)
  → transport (agent/transports/chat_completions.py)
  → actual HTTP call to Ollama / OpenAI-compatible endpoint
```

`request_overrides` is the established mechanism — `hermes_cli/runtime_provider.py:1042` already injects `extra_body` into it from custom-provider config, and `hermes_cli/models.py:2670` injects service-tier overrides. We're adding one more injection point, from the request body instead of config.

---

## 3. Files Touched

| File | Change |
|---|---|
| `gateway/platforms/api_server.py` | Add `_request_generation_params()`; call it in `_create_agent`; merge into `request_overrides` |
| `tests/gateway/test_api_server.py` | Add tests: generation params merge; absent → no change; invalid types → ignored; extra_body passthrough |

That's it. **Two files.** The rest of Hermes — skills, tools, MCP, FTS5 session recall, the whole agent loop — untouched.

---

## 4. Edge Cases & Decisions

1. **Precedence:** request-body `generation` should win over config-level `custom_providers.extra_body` (per-request is more specific). The merge order in `_create_agent` handles this — request overrides are applied after runtime kwargs.
2. **Validation:** keep it permissive like the existing translators — ignore unknown keys, don't 400 on a bad type. The provider will reject invalid values with its own error.
3. **`max_tokens` vs `max_completion_tokens`:** some providers (OpenAI) renamed it. Keep the key as sent; the transport already handles provider-specific naming where needed. For Ollama, `max_tokens` is correct.
4. **Streaming:** no special handling — overrides apply identically to stream and non-stream paths (both go through `_create_agent`).
5. **Security:** no new surface — same auth as every other API-server request. `extra_body` is passed through verbatim; the stack controls what it sends.

---

## 5. Testing

1. **Unit:** `_request_generation_params()` — present/absent/invalid shapes.
2. **Integration:** POST to the fork's API server with `model_options.generation`, assert the agent's `request_overrides` contains the values (mirror `tests/gateway/test_api_server.py` patterns).
3. **Live (optional, when stack is ready):** chat-service → Hermes fork → local Ollama; verify the Modulator's temperature actually lands in the Ollama request (check Ollama logs / response metadata).

---

## 6. Upstream Contribution

This is a clean, small, edge-expanding feature — exactly what Hermes' AGENTS.md says the project wants ("expand reach at the edges"). Plan:

1. Implement on the fork, test locally.
2. Open a PR to NousResearch/hermes-agent with the two-file diff.
3. If merged: the fork converges with upstream, maintenance cost → zero, and the stack gets a permanent, supported per-request generation-parameter channel.
4. If not merged: we hold the fork, rebase on upstream releases, and carry the ~30-line patch. Low burden either way.

---

## 7. What This Unlocks

- The Modulator Engine's `computeModulation(state)` output (temperature, max_tokens, prompt framing) flows into the *actual* generation call — the paper's Section 2.6 claim becomes literally true at the API boundary.
- The stack's `chat-service` can vary generation parameters per request, per mood, per task — without forking the agent runtime.
- The AR/VR agent presence work gets a clean interface: the stack decides *how* the agent speaks, Hermes handles *that* it speaks.

---

*Draft for Rob's review — not yet applied. The fork is clean at 0.20.0; the patch is ready to write when you give the word.*
