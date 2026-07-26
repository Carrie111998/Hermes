// COPY-MODIFY adapter pair for a local-first agent/agent-OS app.
// Verified pattern (local-first agent systems, 2026-07-06): routes each agent to a
// local Ollama model and falls back to a template when Ollama is unreachable.
// No API keys, no cloud, only localhost outbound traffic when local-LLM enabled.

const OLLAMA_BASE = 'http://localhost:11434';

// fairyId -> model tag (HF GGUF served by Ollama)
export const FAIRY_MODEL_MAP = {
  glint: 'hf.co/unsloth/Devstral-Small-2505-GGUF:Q4_K_M', // code specialist
  fae:   'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  stella: 'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  lyra:  'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  selena: 'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  hope:  'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  moss:  'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  nova:  'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
  rune:  'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M',
};
export const modelForFairy = (id) => FAIRY_MODEL_MAP[id] || FAIRY_MODEL_MAP.fae;

// Grounded system prompt: identity + forbidden guardrails so the model never
// claims irreversible actions (file writes, posts, spend, submissions).
export function systemPromptForFairy(fairy) {
  if (!fairy) return 'You are a helpful assistant.';
  const parts = [
    `You are ${fairy.name}, the ${fairy.title} of this local-first companion.`,
    fairy.sacredPurpose ? `Sacred purpose: ${fairy.sacredPurpose}` : '',
    fairy.strengths?.length ? `Strengths: ${fairy.strengths.join(', ')}.` : '',
    fairy.weaknesses?.length ? `Weaknesses to guard: ${fairy.weaknesses.join(', ')}.` : '',
    fairy.forbidden?.length
      ? `Forbidden without explicit approval: ${fairy.forbidden.join('; ')}.`
      : '',
    'Warm, grounded, slightly mystical voice. Concise and actionable. Suggest actions but never claim to have performed irreversible ones.',
  ];
  return parts.filter(Boolean).join('\n');
}

export function buildMessages(fairy, userText, memoryContext = '') {
  const user = memoryContext ? `Memory:\n${memoryContext}\n\nSignal:\n${userText}` : userText;
  return [
    { role: 'system', content: systemPromptForFairy(fairy) },
    { role: 'user', content: user || '' },
  ];
}

// Offline default + safety net. Zero network.
export class MockAdapter {
  constructor(latency = 120) { this.latency = latency; this.name = 'mock'; }
  async complete({ fairy, prompt } = {}) {
    await new Promise((r) => setTimeout(r, this.latency));
    return `${fairy?.name || 'Fairy'} senses: ${(prompt || '').slice(0, 140)}`;
  }
}

// Local Ollama adapter. Throws if server unreachable -> caller falls back.
export class OllamaAdapter {
  constructor({ base = OLLAMA_BASE } = {}) { this.base = base; this.name = 'ollama'; }
  async complete({ fairy, prompt, model, temperature = 0.7, memoryContext = '', signal } = {}) {
    const modelTag = model || modelForFairy(fairy?.id);
    const messages = buildMessages(fairy, prompt || '', memoryContext);
    let res;
    try {
      res = await fetch(`${this.base}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: modelTag, messages, stream: false, options: { temperature } }),
        signal,
      });
    } catch (err) {
      throw new Error(`Ollama unreachable at ${this.base}: ${err.message}`);
    }
    if (!res.ok) throw new Error(`Ollama HTTP ${res.status}`);
    const data = await res.json();
    const content = data?.message?.content;
    if (!content) throw new Error('Ollama returned empty content');
    return content.trim();
  }
}

export function createAdapter(kind = 'mock', opts = {}) {
  return kind === 'ollama' ? new OllamaAdapter(opts) : new MockAdapter();
}

// Usage with graceful fallback:
// try { return await ollama.complete({ fairy, prompt }); }
// catch { return mock.complete({ fairy, prompt }); }

// Minimal offline test (node scripts/test-model-router.mjs):
//   1. assert modelForFairy('glint').includes('Devstral')
//   2. assert modelForFairy('fae').includes('Qwen3-14B')
//   3. assert systemPromptForFairy({...}).includes('Forbidden')
//   4. const mock = createAdapter('mock'); assert((await mock.complete({fairy:{name:'Fae'},prompt:'x'})).startsWith('Fae senses:'))
//   5. const o = createAdapter('ollama'); let threw=false;
//      try { await o.complete({fairy:{id:'glint'},prompt:'x'}); } catch { threw=true; }
//      assert(threw)  // proves UI can fall back when no server
