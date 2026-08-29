---
sidebar_position: 9
title: "Run Hermes Locally with Ollama — Zero API Cost"
description: "Step-by-step guide to running Hermes Agent entirely on your own machine with Ollama and a tool-capable open-weight model, without cloud API keys or paid subscriptions"
---

# Run Hermes Locally with Ollama — Zero API Cost

## The Problem

Cloud LLM APIs charge per token. A heavy coding session can cost $5–20. For personal projects, learning, or privacy-sensitive work, that adds up — and you're sending every conversation to a third party.

## What This Guide Solves

You'll set up Hermes Agent running entirely on your own hardware, using [Ollama](https://ollama.com) as the model backend. No API keys, no subscriptions, no data leaving your machine. Once configured, Hermes works exactly like it does with OpenRouter or Anthropic — terminal commands, file editing, web browsing, delegation — but the model runs locally.

By the end, you'll have:

- Ollama serving one or more open-weight models
- Hermes connected to Ollama as a custom endpoint
- A working local agent that can edit files, run commands, and browse the web
- Optional: a Telegram/Discord bot powered entirely by your own hardware

## What You Need

| Component | Minimum | Recommended for agentic tool use |
|-----------|---------|----------------------------------|
| **RAM** | Enough for the selected model and context | 16+ GB for an 8B-class model; more for larger models or 64K context |
| **Storage** | The model download plus working space | 10+ GB free before pulling a model |
| **CPU / GPU** | A supported Ollama host | Apple Silicon, NVIDIA GPU, or a modern multi-core CPU |

:::tip Local inference can be slow
A local model must prefill Hermes' system prompt and tool schemas before it can respond. Keep the first model modest, verify tool calls with a small prompt, then move up in model size only when latency and memory are acceptable.
:::

## Step 1: Install Ollama

On macOS, install Ollama with Homebrew:

```bash
brew install ollama
```

For other platforms, use the installer from [ollama.com](https://ollama.com/download). Start the local server if it is not already running:

```bash
ollama serve
```

Verify it in another terminal:

```bash
ollama --version
curl http://localhost:11434/api/tags   # Should return {"models":[]}
```

## Step 2: Pull a Tool-Capable Model

Hermes uses structured tool calls for file operations, terminal commands, and browsing. Choose a model marked **Tools** in the [Ollama model library](https://ollama.com/search?c=tools); do not assume that every chat model can invoke tools.

For example, Qwen 3 includes tool-use support and has an 8B variant that is a practical starting point on many developer machines:

```bash
ollama pull qwen3:8b
```

Use the exact identifier shown by `ollama list` in the commands below. Before configuring Hermes, verify that the OpenAI-compatible endpoint accepts it:

```bash
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "Say hello"}],
    "max_tokens": 50
  }'
```

:::info Multiple models
You can pull several models and switch between models that are already configured in Hermes. Ollama loads the selected model on demand and unloads idle models automatically.
:::

## Step 3: Configure Hermes

Run the Hermes setup wizard and select **Custom Endpoint**, then enter:

- **Base URL:** `http://localhost:11434/v1`
- **API Key:** Leave empty (Ollama does not require one on loopback)
- **Model:** the exact identifier from `ollama list`, such as `qwen3:8b`

Or configure the same endpoint non-interactively:

```bash
hermes config set model.provider custom
hermes config set model.base_url http://localhost:11434/v1
hermes config set model.default qwen3:8b
```

The resulting `~/.hermes/config.yaml` is:

```yaml
model:
  default: "qwen3:8b"
  provider: "custom"
  base_url: "http://localhost:11434/v1"
```

## Step 4: Start Using Hermes

```bash
hermes
```

That's it. You're now running a fully local agent. Try it out:

```
You: List all Python files in this directory and count the lines of code in each

You: Read the README.md and summarize what this project does

You: Create a Python script that fetches the weather for Ho Chi Minh City
```

Hermes will use the terminal tool, file operations, and your local model — no cloud calls.

## Step 5: Tune for Agentic Work

### Set the Context Window

Hermes needs at least 64,000 tokens for full agentic work. Configure that context on an Ollama-derived model, then point Hermes at the new model name:

```bash
cat >/tmp/Modelfile <<'EOF'
FROM qwen3:8b
PARAMETER num_ctx 64000
EOF
ollama create qwen3-8b-64k -f /tmp/Modelfile
hermes config set model.default qwen3-8b-64k
```

A 64K context consumes substantially more memory. If it does not fit, choose a smaller model, reduce the enabled Hermes toolsets with `hermes tools`, or use a hosted provider.

### Keep a Gateway Model Loaded

For a persistent gateway bot, keep the active model loaded:

```bash
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen3-8b-64k","keep_alive":"24h"}'
```

Inspect loaded models and their processor split with:

```bash
ollama ps
```

## Step 6: Run as a Gateway Bot (Optional)

Once the CLI works, the same custom model endpoint is used by Hermes gateways. Configure the platform through `hermes setup` or the existing platform configuration, then start it:

```bash
hermes gateway
```

## Step 7: Configure a Fallback (Optional)

A local model can struggle with complex requests. Add a configured cloud fallback with `hermes fallback add`; Hermes uses it only when the primary model fails.

## Troubleshooting

### "Connection refused" on startup

Ollama isn't running. Start it:

```bash
sudo systemctl start ollama
# or
ollama serve
```

### Slow responses

- **Check model size vs RAM:** If your model needs more RAM than available, it swaps to disk. Use a smaller model or add RAM.
- **Check `ollama ps`:** If no GPU layers are offloaded, responses are CPU-bound. This is normal for CPU-only servers.
- **Reduce context:** Large conversations slow down inference. Use `/compress` regularly, or set a lower compression threshold in config.

### Slow first response (prefill)

Hermes sends a fixed payload on every API call — the system prompt plus the tool schemas for all enabled tools — before any of your conversation content. On CPU-only or low-VRAM setups, processing that prompt (the *prefill* phase) dominates the first turn: the model can sit silent for minutes while it works through the prompt, then generate at its normal pace. This is expected behaviour, not a hang. The [Mac local-LLM guide](./local-llm-on-mac.md#timeouts) documents the same effect — during prefill on large contexts, local models may produce no output for minutes while processing the prompt — and Hermes automatically raises its stream read timeout from 120s to 1800s for local endpoints (`HERMES_STREAM_READ_TIMEOUT`).

What helps:

- **Keep the model loaded** — Ollama unloads idle models after 5 minutes, adding a full reload before the next prefill. Use the one-request `keep_alive` example in [Step 5](#keep-a-gateway-model-loaded), or configure the Ollama server according to its documentation.
- **Set an appropriate request timeout** — configure a provider-wide or model-specific `timeout_seconds` value under `providers` in `config.yaml`; see [Configuration](/user-guide/configuration).
- **Measure and trim the fixed prompt** — run `hermes prompt-size` for a byte breakdown of the system prompt and tool schemas, then disable unused toolsets with `hermes tools` and uninstall skills you don't need with `hermes skills`.
- **Use GPU offloading** — inspect it with `ollama ps`.

### Model doesn't follow tool calls

Models without tool-call support produce plain text instead of structured function calls. Solutions:

- **Use a model marked as tool-capable** in the [Ollama library](https://ollama.com/search?c=tools) and verify it with the OpenAI-compatible request in [Step 2](#step-2-pull-a-tool-capable-model).
- **Hermes has auto-repair** — it detects malformed tool calls and attempts to fix them automatically.
- **Set up a fallback** — use `hermes fallback add` so an already configured provider can handle a primary-model failure.

If the model prints raw JSON like `{"name": "web_search", ...}` in its reply instead of actually running the tool, that's usually the *server*, not the model — tool calling isn't enabled or the tool-call format isn't parsed. See the per-server fix table in [Tool calls appear as text instead of executing](/integrations/providers#tool-calls-appear-as-text-instead-of-executing) (llama.cpp needs `--jinja`, vLLM needs `--enable-auto-tool-choice --tool-call-parser hermes`, and so on).

### Context window errors

The default Ollama context (2048 tokens) is too small for agentic work. See [Set the Context Window](#set-the-context-window) to increase it.

## Cost Comparison

Here's what running locally saves compared to cloud APIs, based on a typical coding session (~100K tokens input, ~20K tokens output):

| Provider | Cost per Session | Monthly (daily use) |
|----------|-----------------|---------------------|
| Anthropic Claude Sonnet | ~$0.80 | ~$24 |
| OpenRouter (GPT-4o) | ~$0.60 | ~$18 |
| **Ollama (local)** | **$0.00** | **$0.00** |

Your only cost is electricity — roughly $0.01–0.05 per session depending on hardware.

## What Works Well Locally

- **File editing and code generation** — models 9B+ handle this well
- **Terminal commands** — Hermes wraps the command, runs it, reads output regardless of model
- **Web browsing** — the browser tool does the fetching; the model just interprets results
- **Cron jobs and scheduled tasks** — work identically to cloud setups
- **Multi-platform gateway** — Telegram, Discord, Slack all work with local models

## What's Better with Cloud Models

- **Very complex multi-step reasoning** — 70B+ or cloud models like Claude Opus are noticeably better
- **Long context windows** — cloud models offer 100K–1M tokens; local runtimes often default below Hermes' 64K minimum unless you configure them
- **Speed on large responses** — cloud inference is faster than CPU-only local for long generations

The sweet spot: use local for everyday tasks, set up a cloud fallback for the hard stuff.
