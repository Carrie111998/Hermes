"""Apply fallback-chain expansion to the runtime Hermes config.yaml.

Adds more OpenRouter free models (deepseek-r1, llama-4, mistral-small-3.2)
plus commented-out OpenCode Go/Zen placeholders so the user can activate
them by uncommenting OPENCODE_*_API_KEY in .env.

Idempotent: if the new tier-1 comment banner is already present, exits 0.
"""
from pathlib import Path
import sys

CONFIG = Path.home() / "AppData" / "Local" / "hermes" / "config.yaml"

OLD = """fallback_providers:
  - provider: openrouter
    model: nvidia/nemotron-3-ultra-550b-a55b:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: nvidia/nemotron-3-super-120b-a12b:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: qwen/qwen3-coder:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: google/gemma-4-31b-it:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: google/gemma-4-26b-a4b-it:free
    base_url: "https://openrouter.ai/api/v1"
"""

NEW = """fallback_providers:
  # ── Tier 1: OpenRouter free agentic / coding models ─────────────────
  # Walked one-by-one.  Each entry inherits OPENROUTER_API_KEY from the
  # primary model stanza.  Image-capable models (gemma-4-31b) kept early
  # so Discord image input keeps working through failover.
  - provider: openrouter
    model: nvidia/nemotron-3-ultra-550b-a55b:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: nvidia/nemotron-3-super-120b-a12b:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: qwen/qwen3-coder:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: google/gemma-4-31b-it:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: google/gemma-4-26b-a4b-it:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: deepseek/deepseek-r1:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: meta-llama/llama-4-70b-instruct:free
    base_url: "https://openrouter.ai/api/v1"
  - provider: openrouter
    model: mistralai/mistral-small-3.2-24b-instruct:free
    base_url: "https://openrouter.ai/api/v1"
  # ── Tier 2: OpenCode Go ($10/mo subscription, GLM-5/Kimi K2/MiMo) ────
  # Uncomment OPENCODE_GO_API_KEY in .env to activate. Cheap insurance
  # once the free OpenRouter pool is rate-limited.
  # - provider: opencode-go
  #   model: glm-5
  #   base_url: "https://opencode.ai/zen/go/v1"
  # - provider: opencode-go
  #   model: kimi-k2.5
  #   base_url: "https://opencode.ai/zen/go/v1"
  # ── Tier 3: OpenCode Zen (pay-as-you-go, GPT/Claude/Gemini) ──────────
  # Uncomment OPENCODE_ZEN_API_KEY in .env to activate.
  # - provider: opencode-zen
  #   model: gemini-3-flash
  #   base_url: "https://opencode.ai/zen/v1"
"""

SENTINEL = "Tier 1: OpenRouter free agentic"

text = CONFIG.read_text(encoding="utf-8")
if SENTINEL in text:
    print("[SKIP] fallback chain already expanded")
    sys.exit(0)
if OLD not in text:
    print("[WARN] current fallback block did not match expected shape")
    sys.exit(1)
CONFIG.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
print(f"[ OK ] expanded fallback chain in {CONFIG}")
