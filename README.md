# Hermes Free-Tier Skills Library

A small, shareable collection of **Hermes Agent skills** that run models at
**$0 and uncapped** via local Ollama, with optional OpenRouter `:free` fallback.

## Why local-first (and why not just OpenRouter)

OpenRouter's `:free` models are $0 per token but **rate-limited per day** — a
naive "call all free models" burns the entire day's quota on one task and 429s.
These skills default to **Ollama** (`http://localhost:11434/v1`): truly free,
**no API key, no daily cap, unlimited runs**. OpenRouter is still available via
`--backend openrouter` for when you want bigger free models and have quota.

## Skills

| Skill | What it does | Key command |
|-------|--------------|-------------|
| `moa-free` | Mixture-of-Agents: fan a prompt across several models, synthesize into one answer. | `python moa_all.py "prompt"` |
| `model-advisor` | Task-aware router: classify prompt (code/creative/research/long/chat) and send to the best model for that job. | `python model_advisor.py "prompt"` |
| `local-llm-routing` | Per-agent local model routing: pick + wire HF GGUF models via Ollama with offline fallback. | `cp -r skills/local-llm-routing ~/.hermes/skills/mlops/` |

Both read `OPENROUTER_API_KEY` from the Hermes `.env` ONLY when using
`--backend openrouter`. Ollama needs no key. Cross-platform (Windows/Linux/macOS).

## Prereqs (local backend)

```bash
ollama pull qwen2.5:3b qwen2.5:0.5b llama3.2-vision   # or any models you like
# edit LOCAL_PROPOSERS / ROUTES_LOCAL in the scripts to match what you pulled
```

## Install (local)

```bash
cp -r skills/moa-free   ~/.hermes/skills/mlops/moa-free
cp -r skills/model-advisor ~/.hermes/skills/mlops/model-advisor
```

Or just run the scripts directly — they're standalone.

## Usage

```bash
# MOA (local Ollama by default)
python skills/moa-free/moa_all.py --list
python skills/moa-free/moa_all.py "your prompt"
python skills/moa-free/moa_all.py --backend openrouter "prompt"   # OR free models
python skills/moa-free/moa_all.py --run-queue   # replay prompts queued during cap

# Advisor (local Ollama by default)
python skills/model-advisor/model_advisor.py --show-routes
python skills/model-advisor/model_advisor.py "write a python yaml parser"
python skills/model-advisor/model_advisor.py --backend openrouter "prompt"
```

## Contributing

Add a folder under `skills/<category>/<name>/` with a `SKILL.md` (Hermes format)
and any script files. Keep scripts stdlib-only and backend-agnostic where
possible (Ollama default, optional cloud fallback). Update `library.json` + README.

## License

MIT — see LICENSE.
