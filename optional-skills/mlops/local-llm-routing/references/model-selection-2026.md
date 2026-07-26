# Local LLM Model Selection — 2026 (verified via web search 2026-07-06)

## RAM tiers -> config
| Free RAM | Court config | Notes |
|----------|--------------|-------|
| ≤8 GB | Qwen3-8B for all | Works, weaker code |
| 12–16 GB | Qwen3-14B for all | Sweet spot |
| 24 GB+ (or dGPU) | Qwen3-14B + Devstral-24B (code only) | Best quality |

## Recommended picks (HF GGUF, Q4_K_M)
- **Court/generalist (8 fairies):** `unsloth/Qwen3-14B-GGUF` — Apache-2.0, ~9 GB. Best
  all-around local family: instruction-following, coding, creative/tone, multilingual.
- **Code specialist (Glint):** `unsloth/Devstral-Small-2505-GGUF` — Apache-2.0, ~14 GB.
  #1 open-source coding-agent model (46.8% SWE-bench Verified). Text-only, 128K context.
  Finetuned from Mistral Small 3.1, uses the OpenHands scaffold. Ideal for
  scaffold/repair/refactor agent tasks.
- **Low-RAM fallback:** `unsloth/Qwen3-8B-GGUF` — Apache-2.0, ~5.5 GB. HumanEval 76.0.
  Runs on almost any laptop.

## Why NOT the others
- **Llama 3.3 8B** — solid all-rounder, but Llama community license is NOT Apache-2.0 ->
  avoid when commercial/monetization intent exists.
- **Phi-4 / Phi-4-mini** — excellent reasoning at 3–4 GB, but weak at long creative /
  art-direction prompts (Stella-style fairies suffer).
- **Gemma 3/4** — multimodal (good for image vision LATER), but smaller context and
  Google license; weaker general local fit today.
- **gpt-oss-20B / 120B** — Apache-2.0 reasoning beast, good for risk/logic (Selena),
  but softer at coding than Qwen3/Devstral.

## Pull via Ollama (no API key, no billing)
```bash
ollama run hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M
ollama run hf.co/unsloth/Devstral-Small-2505-GGUF:Q4_K_M
ollama run hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M   # optional low-RAM fallback
```
Ollama serves at http://localhost:11434 (OpenAI-style /api/chat).

## Caveats
- All `"latest"` npm deps + no lockfile = reproducibility risk before v0.5+; pin for CI.
- Could NOT run live `npm run build` in sandbox (no network -> `npm install` blocked).
  Verify the adapter layer with a node test script instead (see template).
- HF Inference API is explicitly AVOIDED here because it bills per token; local-first ==
  Ollama/llama.cpp only.
