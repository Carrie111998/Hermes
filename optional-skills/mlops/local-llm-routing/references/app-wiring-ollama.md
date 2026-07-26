# Wiring a third-party app to local Ollama (OpenAI-compatible)

The Companion-Project session ported a paid-Anthropic app stack (AIRI / Mindcraft /
retro_buddy.py) to 100% free local Ollama. The repeatable recipe for ANY app that
expects an OpenAI-style key + base URL:

## The three env facts
- `OPENAI_API_KEY` = any dummy string (Ollama ignores auth), e.g. `ollama-local`.
- `OPENAI_BASE_URL` (or the app's "base_url" field) = `http://localhost:11434/v1`.
- Model name in the app's profile = the Ollama tag **verbatim** (`qwen2.5:3b`,
  `llama3.2-vision`, `nomic-embed-text`). Do NOT qualify it as an OpenAI model id.

Persist so the launcher subprocess inherits it:
```powershell
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "ollama-local", "User")
[Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", "http://localhost:11434/v1", "User")
```

## Mindcraft worked example
Mindcraft's profile schema is just `{name, model, embedding}` (verified against the
repo's `profiles/claude.json`):
```json
{ "name": "Vesper", "model": "qwen2.5:3b", "embedding": "nomic-embed-text" }
```
- `model` = any pulled Ollama text model. `qwen2.5:3b` is light enough for real-time
  play on an 11GB GPU; swap to `gemma2`/`mistral` freely.
- `embedding` = an Ollama embedding model (`nomic-embed-text` pulled once).
- The setup script must `ollama pull` both before launching, and set the two env vars
  in the generated `start-companion.bat` so `node main.js` inherits them.

### LAN port gotcha (cost the first run)
Mindcraft `settings.js` has `port: 55916`, but vanilla Minecraft "Open to LAN"
assigns a **RANDOM** port each time — you cannot force 55916. Two fixes:
- Set `port: -1` in `settings.js` → Mindcraft auto-scans for the open LAN port, OR
- Read the actual port from the Minecraft chat after "Open to LAN"
  (`Local game hosted on port 55923`) and use that.
Never tell the user to "set port 55916" — it silently fails.

## Vision integration (PyBoy / game-screen agents)
For screen-reading agents, use a vision model (`llama3.2-vision`, ~8GB VRAM min) and
the Ollama Python SDK 0.6.x multimodal shape (see `hermes-ollama-local` pitfalls):
`content` is a STRING, images passed as RAW BYTES via top-level `"images": [...]`.
Cold-load the first inference (2–3 min on 11GB) — budget it; don't declare failure.

## Model-choice cheat sheet (11GB VRAM class)
| Use | Ollama tag | Notes |
|-----|-----------|-------|
| Real-time game agent / chat | `qwen2.5:3b` | fast, light |
| Stronger local chat | `qwen2.5:14b` / `gemma2` | fits if RAM headroom |
| Screen-reading agent | `llama3.2-vision` | needs vision; slow cold-load |
| Embeddings (Mindcraft) | `nomic-embed-text` | pull once |
