---
name: local-llm-routing
description: Pick and wire local Hugging Face GGUF models into a local-first agent/app with per-agent model routing and offline fallback. Ollama-served, no cloud billing.
version: 1.0.0
author: Hermes session
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [local-llm, ollama, huggingface, gguf, model-routing, agent-systems, offline-fallback, apache-2.0]
---

# Local LLM Routing for Agent Systems

Use this skill when wiring local, private, no-billing LLMs into an app or multi-agent
system: choosing which Hugging Face GGUF model(s), serving them through Ollama, routing
different agents/skills to different models, and degrading gracefully when no model is
running. Complements `llama-cpp` (which covers quant selection + native llama.cpp server).

## When to use

- User wants local-first / private / no-API-key / no per-token billing.
- An app has multiple agents or skills that benefit from different models (e.g. a code
  specialist vs a generalist).
- You need the app to NEVER break when the local model server is down.
- Money/commercial intent is in play -> licensing matters (prefer Apache-2.0).

## Decision framework (RAM first, task second, license third)

1. **RAM/VRAM budget first.** Pick the largest quant that fits. Q4_K_M is the default
   quality/size sweet spot. See RAM tiers in references/model-selection-2026.md.
2. **Task second.** Code-heavy agents -> a coding specialist (Devstral). Generalist
   chat/creative/tone -> Qwen3 family. Reasoning-only -> gpt-oss-20B.
3. **License third.** If the product is or may be monetized, avoid the Llama community
   license; prefer Apache-2.0 (Qwen3, Devstral, gpt-oss, Gemma, Phi-4).

## Serving via Ollama (no cloud, no keys)

Ollama pulls GGUF directly from Hugging Face with the `hf.co/` shorthand:

```bash
# install: https://ollama.com/download (Windows installer)
ollama run hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M
ollama run hf.co/unsloth/Devstral-Small-2505-GGUF:Q4_K_M
# optional low-RAM fallback
ollama run hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M
```

Ollama serves at `http://localhost:11434`. Only outbound traffic is to localhost, and only
when the user explicitly enables local-LLM mode.

## Per-agent routing pattern

Map each agent/skill id to a model tag. One generalist (Qwen3-14B) covers most fairies;
route the code specialist (e.g. `glint`) to Devstral. Example `FAIRY_MODEL_MAP`:

```js
const FAIRY_MODEL_MAP = {
  glint: 'hf.co/unsloth/Devstral-Small-2505-GGUF:Q4_K_M',
  fae:   'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  // ...every other fairy -> Qwen3-14B
};
export const modelForFairy = (id) => FAIRY_MODEL_MAP[id] || FAIRY_MODEL_MAP.fae;
```

This is the v0.5 "Model routing per skill" slice of most agent-OS roadmaps.

## Wiring a third-party app to local Ollama (OpenAI-compatible)
Many apps (Mindcraft, OpenWebUI, AnythingLLM, custom Python) take an OpenAI-style
key + base URL. Point them at Ollama with NO auth and NO cloud:
- Set `OPENAI_API_KEY` to any dummy string (Ollama ignores it), e.g. `ollama-local`.
- Set `OPENAI_BASE_URL` (or the app's "base_url") to `http://localhost:11434/v1`.
- In the app's model/profile field, use the Ollama tag verbatim (`qwen2.5:3b`,
  `llama3.2-vision`, `nomic-embed-text`). The profile is usually just
  `{"name": "<bot>", "model": "<ollama-tag>", "embedding": "<ollama-embed-tag>"}`.
Persist via `[Environment]::SetEnvironmentVariable(...)` (PowerShell) or export so the
launcher subprocess inherits it. Worked example + Mindcraft LAN-port gotcha + vision
integration in references/app-wiring-ollama.md.

## Offline fallback adapter (graceful degradation)

Always ship a `MockAdapter` (template/canned output, zero network) and fall back to it
when the Ollama request throws (server unreachable). The app must never hard-fail. See
templates/model-adapter.js for a copy-modify `MockAdapter` + `OllamaAdapter` pair plus the
`/api/chat` call shape (stream:false, OpenAI-style messages).

```js
try {
  return await ollama.complete({ fairy, prompt });
} catch {
  return mock.complete({ fairy, prompt }); // template whisper, app still works
}
```

### Third adapter class: arbitrary local HTTP `/chat` server
A discovered local model server may speak a simpler protocol than Ollama — e.g.
`POST /chat { message } -> { reply }` (seen with `crystal-ai` on `:8765`, a
LiteRT-LM `gemma` model). Treat it as a first-class backend behind Ollama, not a
one-off:

```js
export class HttpChatAdapter {
  constructor({ base = 'http://127.0.0.1:8765' } = {}) { this.base = base; this.name = 'httpchat'; }
  async complete({ fairy, prompt, memoryContext = '' } = {}) {
    const sys = systemPromptForFairy(fairy);
    const message = memoryContext ? `${sys}\n\nUser: ${prompt}` : `${sys}\n\n${prompt}`;
    let res;
    try {
      res = await fetch(`${this.base}/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message })
      });
    } catch (err) { throw new Error(`server unreachable at ${this.base}: ${err.message}`); }
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0,200)}`);
    const data = await res.json();
    if (!data?.reply) throw new Error('empty reply');
    return data.reply.trim();
  }
}
export function createAdapter(kind = 'mock', opts = {}) {
  if (kind === 'ollama') return new OllamaAdapter(opts);
  if (kind === 'httpchat') return new HttpChatAdapter(opts); // <-- discovered local server
  return new MockAdapter(opts);
}
```

Wire it so the UI can pick `ollama` / `httpchat` / `mock` (default mock offline,
Ollama primary, httpchat as a zero-install second local backend already running).

### Live-probe test (hits the real running model)
Unlike Ollama (assert "throws when down"), a live local server should be *probed*:
if it's up, assert a real reply; if down, assert clean degradation. This proves the
protocol end-to-end without hard-failing in CI where the server isn't present:

```js
const a = createAdapter('httpchat');
let ok = false, degraded = false;
try { const r = await a.complete({ fairy: { name: 'Fae' }, prompt: 'hi' }); ok = typeof r === 'string' && r.length > 0; }
catch (e) { degraded = /unreachable|ENOENT|ECONNREFUSED|fetch failed/i.test(e.message); }
check(ok || degraded, 'httpchat returns reply OR degrades cleanly when absent');
```

### Probing localhost from the sandbox
`web_extract`/`web_search` on `http://127.0.0.1:...` return
"Blocked: URL targets a private or internal network address" (correct sandbox
behavior). Inspect a local server with **terminal `curl`**:
`curl -s --max-time 5 http://127.0.0.1:PORT/health` then grep the served HTML
for `fetch('/...')` to learn its routes before POSTing.

## Grounded system prompts

Carry each agent's identity + forbidden guardrails into the system prompt so the model
cannot claim irreversible actions (no file writes, posts, spend, or submissions). This
preserves the consent model of local-first companion apps.

## Verify without a server (no network)

Assert the routing table + fallback path in a node script (`node scripts/test-...mjs`).
Proves correctness offline and in CI. See templates/model-adapter.js for the adapter shape
and references/model-selection-2026.md for the assertions to encode.

## 2026 recommended stack (verified this session via web search)

| Role | Model | HF GGUF | RAM (Q4_K_M) | License |
|------|-------|---------|--------------|---------|
| Generalist court | Qwen3-14B | `unsloth/Qwen3-14B-GGUF` | ~9 GB | Apache-2.0 |
| Code specialist | Devstral-Small-2505 | `unsloth/Devstral-Small-2505-GGUF` | ~14 GB | Apache-2.0 |
| Low-RAM fallback | Qwen3-8B | `unsloth/Qwen3-8B-GGUF` | ~5.5 GB | Apache-2.0 |

Devstral = #1 open-source coding-agent model (46.8% SWE-bench Verified), text-only — ideal
for scaffold/repair/refactor. Qwen3-14B = best all-around local family, commercial-safe.
Full RAM tiers + why-nots (Llama license, Phi-4 creative weakness, Gemma context, gpt-oss)
are in references/model-selection-2026.md.

## References

- **[references/model-selection-2026.md](references/model-selection-2026.md)** — RAM tiers,
  the verified 2026 picks, and explicit why-nots for Llama/Phi-4/Gemma/gpt-oss.
- **[templates/model-adapter.js](templates/model-adapter.js)** — copy-modify
  `MockAdapter` + `OllamaAdapter` + routing map + grounded system-prompt builder.
- **[references/app-wiring-ollama.md](references/app-wiring-ollama.md)** — point any
  OpenAI-compat app (Mindcraft etc.) at local Ollama: dummy key, `:11434/v1`,
  profile shape, LAN `port:-1` gotcha, vision cold-load.

## Overlap note

`llama-cpp` is the lower layer (quant choice, native llama.cpp `llama-server`). Use it when
you want the llama.cpp server directly or need quant/imatrix guidance. This skill sits
above it: Ollama serving + multi-model routing + offline fallback for agent apps. They are
complementary, not competing.

## Windows/MSYS path gotcha (learned the hard way)
On Windows the MSYS bash layer doubles a leading slash: writing `/c/Users/x/a.js`
can land at `C:\c\Users\x\a.js`. When you `write_file` then need `terminal`/`node`
to run it, PREFER native Windows paths (`C:\Users\x\a.js`) for the run step, or
sync from the doubled dir (`/c/c/Users/...`) to the real one. Node's ESM import
resolves relative to the *real* repo, so a file stranded in `C:\c\...` shows as
"Cannot find module". Fix: `cp /c/c/Users/x/repo/src/lib/* /c/Users/x/repo/src/lib/`.
This burned ~6 tool calls in one session — encode it now.

## VRAM-contention root cause (why a local LLM "crashes" under load)
A single llama.cpp/Ollama server on a small GPU (e.g. RTX 2080 Ti 11GB) is fine.
Stacking MULTIPLE heavy servers (e.g. five 27B Fable instances on 8919–8923) eats
all VRAM and the models die mid-generation — looks like "the model is broken" but
is purely contention. **Rule:** run ONE instance per model; if you need redundancy,
use a watchdog that restarts a single instance on death (see below) rather than
spawning duplicates. A 27B IQ4_XS model is too heavy to serve reliably even alone
on 11GB VRAM under sustained generation — keep it as an OPTIONAL bonus and let a
smaller stable model (mistral 7B via Ollama) carry the load.

### Killing orphaned background model servers on Windows
Hermes `terminal(background=true)` launches leave python/llama processes that
`taskkill /F /T /PID x` may REFUSE (owned by another session / protected parent).
`wmic` terminate works where taskkill fails:
```bash
# find listeners:  netstat -ano | grep ":89[12][0-9]\|:314[0-4]" | grep LISTENING
for p in 8108 25416 12856; do wmic process where "ProcessId=$p" call terminate; done
```
Then re-launch ONE instance and verify with `curl -s http://127.0.0.1:PORT/v1/models`.
This cleared five stacked Fable servers + three ARIA cores in one pass.

### Self-healing watchdog pattern (avoid the port war entirely)
Don't fight orphaned twins — outlive them: launch on a FRESH port, and for the
single instance you DO keep, run a tiny watchdog that polls `/v1/models` and
`subprocess.Popen`s a replacement if it's down (no kill of other servers, no
port contention). Verified: `fable_watchdog.py` kept one Fable alive while the
heavy model died repeatedly under load.

## Re-runnable verification
`scripts/verify-router.mjs` asserts the routing table + graceful-fallback path with
zero network. Drop it next to the adapter and run `node scripts/verify-router.mjs`.
