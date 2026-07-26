---
name: model-advisor
description: "Use when user wants prompt-based model routing or 'advisor'."
version: 0.1.0
author: Hermes Agent community
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [routing, advisor, model-selection, free-models]
---

# Model-Advisor: Task-Aware Model Router (local-first, truly free)

Classifies your prompt by task type and routes the real work to the BEST model
for that job (not one fixed default). This is the model-switching-by-need
pattern, shipped as a portable skill.

DEFAULT BACKEND = OLLAMA (http://localhost:11434/v1) — TRULY free and UNCAPPED,
no API key. OPTIONAL `--backend openrouter` uses OpenRouter's $0/token `:free`
models (rate-limited per day; the script queues + reports reset on 429).

## When to use
- User says "advisor", "route this", "pick the best model for this", or wants
  automatic model selection by task type instead of a fixed default.
- NOT for normal turns; it burns 1 free OR call on the chosen model.

## Files
- `model_advisor.py` — the router (stdlib only, no pip).

## Usage
```
python model_advisor.py --show-routes
python model_advisor.py "write a python yaml parser"          # local Ollama (default)
python model_advisor.py --backend openrouter "prompt"        # OR free models
python model_advisor.py --llm-classify "prompt"              # LLM classify (local or OR)
python model_advisor.py --queue-only "prompt"
python model_advisor.py --run-queue
```

## Routing table (free only, strength-ordered)
code     -> north-mini-code > nemotron-super > gpt-oss-20b
creative -> nemotron-ultra > gemma-4-31b > ling-3.0-flash
research -> ling-3.0-flash > nemotron-ultra > gemma-4-31b
long     -> nemotron-ultra (1M ctx)
chat     -> gemma-4-31b > gpt-oss-20b > nemotron-nano-30b

## Classifier
- Default: local keyword heuristic (FREE, instant, 0 quota burn).
- --llm-classify: cheap free model tags task (burns 1 quota; better nuance).

## Quota-aware
Probes OpenRouter free cap first (same daily cap as moa). If capped, queues the
prompt and prints exact UTC reset; re-run with --run-queue after reset.

## Pitfalls
- Free quota resets midnight UTC daily.
- Add $10 to OpenRouter -> 1000 free req/day, unlocks advisor + moa fully.
- Local classification is keyword-based; --llm-classify is smarter but costs a call.
- Does NOT change Hermes' internal model.default; it's an external router script.
