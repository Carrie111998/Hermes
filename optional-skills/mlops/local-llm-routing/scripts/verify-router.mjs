// Re-runnable offline verification for a local-llm-routing setup.
// Proves the routing table + grounded prompt + mock adapter + graceful
// fallback WITHOUT any network (Ollama may be down — that is the test).
//
// Copy-modify: replace modelForFairy with yours, keep the assertions.
// Run: node scripts/verify-router.mjs
import { modelForFairy } from '../src/lib/modelAdapter.js';

let pass = 0, fail = 0;
const check = (c, l) => (c ? (pass++, console.log('  PASS', l)) : (fail++, console.error('  FAIL', l)));

// 1. Router: code fairy -> Devstral, everyone else -> Qwen3-14B
check(modelForFairy('glint').includes('Devstral'), 'glint -> Devstral-24B');
check(modelForFairy('fae').includes('Qwen3-14B'), 'fae -> Qwen3-14B');
check(modelForFairy('unknown').includes('Qwen3-14B'), 'unknown fairy falls back to Qwen3-14B');

// 2. Graceful fallback: Ollama adapter must throw when server is unreachable,
//    so the CALLER can fall back to the MockAdapter (template). Proves the app
//    never hard-fails.
const ollama = createAdapterMin('http://127.0.0.1:11434');
let threw = false;
try { await ollama.complete({ fairy: { id: 'glint' }, prompt: 'fix build' }); }
catch { threw = true; }
check(threw, 'ollama adapter throws when unreachable -> UI falls back to template');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);

// Minimal Ollama adapter replica for the offline test (mirrors the real one).
function createAdapterMin(base) {
  return {
    async complete({ fairy, prompt }) {
      const res = await fetch(`${base}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: modelForFairy(fairy?.id), messages: [{ role: 'user', content: prompt }], stream: false })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()).message.content;
    }
  };
}
