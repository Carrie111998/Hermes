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

# Model-Advisor: Task-Aware Free-Model Router

Classifies your prompt by task type and routes the real work to the BEST free
OpenRouter model for that job (not one fixed default). This is the
model-switching-by-need pattern, shipped as a portable skill.

## When to use
- User says "advisor", "route this", "pick the best model for this", or wants
  automatic model selection by task type instead of a fixed default.
- NOT for normal turns; it burns 1 free OR call on the chosen model.

## Files
- `model_advisor.py` — the router (stdlib only, no pip).

## Usage
```
python model_advisor.py --show-routes
python model_advisor.py "write a python yaml parser"      # local classify
python model_advisor.py --llm-classify "prompt"           # LLM classify (1 quota)
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
