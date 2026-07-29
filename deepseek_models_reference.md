# DeepSeek API Models — Comprehensive Reference

> Compiled: June 2, 2026
> Source: Official DeepSeek API Docs (api-docs.deepseek.com) + HuggingFace Tech Report (DeepSeek-V4 Preview, Apr 24 2026)

---

## Available Models via API

Only **two** models are currently available via the DeepSeek API:

- **DeepSeek-V4-Flash** (`deepseek-v4-flash`) — Active
- **DeepSeek-V4-Pro** (`deepseek-v4-pro`) — Active
- `deepseek-chat` — **Deprecating Jul 24, 2026** (routes to V4-Flash non-thinking)
- `deepseek-reasoner` — **Deprecating Jul 24, 2026** (routes to V4-Flash thinking)

Base URLs:
- OpenAI format: `https://api.deepseek.com`
- Anthropic format: `https://api.deepseek.com/anthropic`

---

## 1. deepseek-v4-flash

### Architecture
- Total params: 284B
- Activated params: 13B
- Architecture: Mixture-of-Experts (MoE)
- Precision: FP4 (experts) + FP8 (other params)
- Novel attention: CSA (Compressed Sparse Attention) + HCA (Heavily Compressed Attention)

### What It's Best At
- High-speed reasoning and chat for everyday tasks
- Simple-to-moderate agentic and tool-use tasks
- Cost-efficient coding assistance
- Handling long contexts with minimal latency

### Key Strengths
- Extremely fast — smallest activated params of any comparable frontier model (13B)
- Cost leader — dramatically cheaper than Claude, GPT, Gemini equivalents
- 1M context window on par with the most expensive models
- Thinking mode (chain-of-thought reasoning) supported
- High concurrency — 2,500 concurrent requests (vs 500 for Pro)
- Tool calls, JSON mode, FIM completion all supported
- Context caching available
- Supports both OpenAI and Anthropic API formats

### Weaknesses
- Lower world knowledge ceiling than Pro (smaller param count)
- Significantly behind Pro on hard knowledge benchmarks (SimpleQA: 34.1 vs 57.9; HLE: 34.8 vs 37.7)
- Falls behind on complex agentic tasks (Terminal Bench: 56.9 vs 67.9)
- No vision/multimodal capabilities (text-only)
- Non-thinking mode has limited reasoning depth on complex problems

### Speed Tier
- Very Fast
- Concurrency limit: 2,500

### Cost Tier — Budget (cheapest frontier-class model available)

- Input (cache miss): $0.14/MTok
- Input (cache hit): $0.0028/MTok
- Output: $0.28/MTok

### Context Window
- 1M tokens (full support)
- Max output: 384K tokens (staged generation)

### Vision Capabilities: None

### Notable Benchmarks (V4-Flash Max mode)
- LiveCodeBench (Pass@1): 91.6 — Near frontier coding
- Codeforces Rating: 3052 — Competitive programming
- GPQA Diamond (Pass@1): 88.1 — PhD-level science
- SWE Verified (Resolved): 79.0 — Software engineering
- HMMT 2026 Feb (Pass@1): 94.8 — Hard math contest
- MMLU-Pro (EM): 86.2 — Broad knowledge
- SimpleQA-Verified (Pass@1): 34.1 — Factual knowledge (limited)
- HLE (Pass@1): 34.8 — Hard reasoning
- MRCR 1M (MMR): 78.7 — Long-context retrieval
- BrowseComp (Pass@1): 73.2 — Web browsing agent

---

## 2. deepseek-v4-pro

### Architecture
- Total params: 1.6T (1,600B)
- Activated params: 49B
- Architecture: Mixture-of-Experts (MoE)
- Precision: FP4 (experts) + FP8 (other params)
- Novel attention: CSA + HCA (same hybrid as Flash)

### What It's Best At
- Complex reasoning (math, STEM, coding competitions)
- Deep agentic tasks with tool-calling loops
- Knowledge-intensive retrieval and QA
- World-class long-context understanding
- SWE-bench and software engineering tasks

### Key Strengths
- Rivals top closed-source models — competes with GPT-5.4, Claude Opus 4.6, Gemini 3.1 Pro
- Best open-source model available (as of Apr 2026)
- 1M context with only 27% of single-token FLOPs vs DeepSeek-V3.2
- Only 10% KV cache vs V3.2 at full context
- World-class coding — #1 on LiveCodeBench (93.5), Codeforces (3206 rating)
- Strong agentic capabilities — SWE Verified (80.6%), BrowseComp (83.4%)
- Rich world knowledge — second only to Gemini-3.1-Pro among all models
- Thinking mode with configurable effort (High / Max)
- Context caching available

### Weaknesses
- No vision/multimodal capabilities (text-only)
- Lower concurrency than Flash (500 vs 2,500)
- Slower than Flash for simple queries (but faster per-token than Claude/GPT equivalents)
- Falls behind Gemini-3.1-Pro on pure knowledge benchmarks (MMLU-Pro, SimpleQA, GPQA)
- Trails Opus-4.6 and GPT-5.4 on some agentic benchmarks (Terminal Bench, Toolathlon)
- Apex (hard math benchmark) significantly behind GPT-5.4 xHigh and Gemini-3.1-Pro

### Speed Tier
- Fast (competitive with GPT-5.4, faster than Claude Opus)
- Concurrency limit: 500

### Cost Tier — Mid-range (dramatically cheaper than Claude/GPT flagship)

- Input (cache miss): $0.435/MTok
- Input (cache hit): $0.003625/MTok
- Output: $0.87/MTok

### Context Window
- 1M tokens (full support)
- Max output: 384K tokens (staged generation)

### Vision Capabilities: None

### Notable Benchmarks (V4-Pro Max mode)
- LiveCodeBench (Pass@1): 93.5 — #1 vs all frontier models
- Codeforces Rating: 3206 — #1 competitive coding
- SWE Verified (Resolved): 80.6 — Ties Gemini-3.1-Pro
- SWE Multilingual (Resolved): 76.2 — ~Claude Opus level
- GPQA Diamond (Pass@1): 90.1 — PhD science
- MMLU-Pro (EM): 87.5 — Broad knowledge
- SimpleQA-Verified (Pass@1): 57.9 — Factual knowledge (below Gemini)
- Chinese SimpleQA (Pass@1): 84.4 — Near Gemini-3.1-Pro (85.9)
- HLE (Pass@1): 37.7 — Hard reasoning
- HMMT 2026 Feb (Pass@1): 95.2 — Math contest
- IMOAnswerBench (Pass@1): 89.8 — Olympic math
- Apex Shortlist (Pass@1): 90.2 — #1 extreme math
- MRCR 1M (MMR): 83.5 — Long-context retrieval
- CorpusQA 1M (ACC): 62.0 — Long-context QA
- Terminal Bench 2.0 (Acc): 67.9 — Agentic CLI tasks
- BrowseComp (Pass@1): 83.4 — Web browsing agent
- MCPAtlas Public (Pass@1): 73.6 — MCP tool use
- SWE Pro (Resolved): 55.4 — Harder SWE benchmark
- GDPval-AA (Elo): 1554 — Agent arena
- Toolathlon (Pass@1): 51.8 — Tool orchestration

---

## Shared Features (Both Models)

### Thinking Mode
- Non-Thinking: Fast, direct responses
- Thinking High: Chain-of-thought reasoning (default)
- Thinking Max: Maximum reasoning budget for hardest problems

Effort control: OpenAI `reasoning_effort: "high"/"max"`, Anthropic `output_config: {effort: "high/max"}`

### Supported Features
- JSON Output: Yes
- Tool Calls: Yes
- Multi-round Chat: Yes
- Chat Prefix Completion (Beta): Yes
- FIM Completion (Beta): Non-thinking only
- Context Caching: Yes
- Anthropic API Format: Yes
- Streaming: Yes
- Temperature/top_p/penalties: Not supported in thinking mode (silently ignored)

---

## Comparison to Claude and Gemini Equivalents

### Pricing Comparison (per 1M tokens)

- DeepSeek V4-Flash: $0.14 input / $0.0028 cached / $0.28 output / 1M ctx
- Gemini 2.5 Flash: $0.15 input / - cached / $0.60 output / 1M ctx
- Claude 3.5 Haiku: $0.80 input / - cached / $4.00 output / 200K ctx
- DeepSeek V4-Pro: $0.435 input / $0.0036 cached / $0.87 output / 1M ctx
- Gemini 3.1 Pro: $1.25 input / $0.07 cached / $5.00 output / 1M ctx
- Claude 4 Sonnet: $3.00 input / - cached / $15.00 output / 200K ctx
- Claude 4 Opus: $15.00 input / - cached / $75.00 output / 200K ctx

### Key Differentiators

Where DeepSeek wins:
1. Cost — 10-50x cheaper than Claude Opus; 3-6x cheaper than Gemini Pro
2. Context caching — near-zero on cache hits ($0.0028/MTok)
3. Context length — 1M standard (vs 200K Claude)
4. Coding — #1 Codeforces and LiveCodeBench
5. Open-source — MIT license, weights available
6. Concurrency — Flash handles 2,500 concurrent

Where Claude wins:
1. Vision — native image understanding (DS V4 has none)
2. Agentic coding — Opus 4.6 leads SWE Verified (80.8%) and SWE Multilingual
3. Long-context retrieval — MRCR 1M: Claude 92.9 vs V4-Pro 83.5
4. Safety/alignment — Constitutional AI

Where Gemini wins:
1. World knowledge — Gemini 3.1 Pro leads SimpleQA (75.6%), MMLU-Pro (91.0%), GPQA (94.3%)
2. Multimodal — native image/audio/video
3. Apex — Gemini 60.9% vs V4-Pro 38.3% (hardest math)
4. Toolathlon — agentic tool orchestration

---

## Key Takeaways

1. Only 2 API models exist: deepseek-v4-flash and deepseek-v4-pro. Old names retire Jul 24, 2026.
2. Neither has vision — both are pure text.
3. Both have 1M context — best-in-class alongside Gemini.
4. V4-Flash is the cost king — $0.14/$0.28 per MTok. Cheapest frontier-quality API.
5. V4-Pro is the coding champion — #1 LiveCodeBench/Codeforces.
6. Thinking mode is default — both ship with CoT reasoning enabled.
7. Context caching is transformative — cache-hit pricing is effectively free.
