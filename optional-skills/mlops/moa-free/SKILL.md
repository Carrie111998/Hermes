---
name: moa-free
description: "Use when user says 'moa' a prompt; synthesizes free OpenRouter models."
version: 0.1.0
author: Hermes Agent community
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [MOA, OpenRouter, free-models, synthesis, agent]
---

# MOA-Free: Quota-Aware Mixture of Agents (Free Tier)

Fan a prompt across several FREE OpenRouter chat models in parallel, then
synthesize their outputs into one final answer with a free aggregator.

WHY QUOTA-AWARE (critical): OpenRouter enforces a DAILY free-model request cap.
A naive "call all 12 free models" burns the entire day's quota on ONE task and
429s instantly. This skill runs a SMALL strength-ordered proposer set (default 3)
to conserve the daily budget, probes quota first, and QUEUES the prompt if
capped instead of failing.

## When to use
- User says "moa <prompt>", "run moa on this", "mix models on this".
- User wants higher-quality/free output and can tolerate 30-90s latency.
- NOT for normal interactive turns — keep a single good free model as default.

## Files
- `moa_all.py` — the runner (stdlib only, no pip).

## Usage
```
python moa_all.py --list
python moa_all.py "your prompt here"
python moa_all.py --proposers 4 "prompt"
python moa_all.py --queue-only "prompt"
python moa_all.py --run-queue
```
Run from this skill directory (or pass an absolute path). The script reads
OPENROUTER_API_KEY from the Hermes `.env` automatically.

## Behavior
1. probe_quota() fires one tiny call. On 200 quota available. On 429 parse
   X-RateLimit-Reset (ms), report exact UTC reset, then queue.
2. If available: run min(proposers, len(PROPOSERS)) in a ThreadPool (5 workers),
   then aggregate with nvidia/nemotron-3-ultra-550b-a55b:free.
3. If aggregator 429s -> fall back to printing the single best proposer.

## Strength-ordered free chat proposers (8; non-chat excluded)
1. nvidia/nemotron-3-ultra-550b-a55b:free   (strongest, 1M ctx)
2. nvidia/nemotron-3-super-120b-a12b:free
3. google/gemma-4-31b-it:free
4. inclusionai/ling-3.0-flash:free
5. openai/gpt-oss-20b:free
6. nvidia/nemotron-3-nano-30b-a3b:free
7. poolside/laguna-s-2.1:free
8. cohere/north-mini-code:free

## To unlock full MOA-all
Add $10 credit to OpenRouter once -> 1000 free requests/day (~80 MOA-all runs).
Without it, daily free calls are capped; the script queues and waits for reset.

## Pitfalls
- Reset is midnight UTC daily.
- :free slugs can be pruned/rate-limited without notice — always probe first.
- Do NOT set --proposers higher than ~4 on a free account; you'll exhaust quota.
- Aggregator call also counts against the daily free cap — script reserves 1.
