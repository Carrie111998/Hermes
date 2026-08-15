/**
 * Unit tests for the reconnect scheduling and version resolution guards.
 *
 * Regression tests for the reconnect-wedge trap: startSocket() awaits
 * network I/O (fetchLatestBaileysVersion has no AbortSignal) before it
 * creates a socket, and the close handler used to re-enter it via a bare
 * `setTimeout(startSocket, ...)`. A rejection was unhandled and a stalled
 * fetch left the bridge permanently disconnected while its HTTP server
 * kept answering 503 — observed in the field as a bridge that logged
 * "Reconnecting in 3s..." once and then went silent for 27+ hours.
 *
 * These tests avoid importing bridge.js because that file starts an HTTP
 * server and Baileys socket at module load. Keep the helper module pure.
 */

import { strict as assert } from 'node:assert';

import {
  createReconnectScheduler,
  createVersionResolver,
  createReconnectBackoff,
} from './bridge_helpers.js';

const tick = () => new Promise(resolve => setImmediate(resolve));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// -- createReconnectScheduler ---------------------------------------------

// A rejecting start function is caught and rescheduled at the retry delay;
// a subsequent success stops the retry chain.
{
  const timers = [];
  const logs = [];
  let attempts = 0;
  const startFn = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('boom');
  };

  const schedule = createReconnectScheduler(startFn, {
    retryDelayMs: 5000,
    log: line => logs.push(line),
    setTimeoutFn: (fn, ms) => timers.push({ fn, ms }),
  });

  schedule(3000);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 3000);

  timers[0].fn();
  await tick();
  await tick();

  assert.equal(attempts, 1);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /Reconnect failed \(boom\)/);
  assert.equal(timers.length, 2, 'rejection must schedule a retry');
  assert.equal(timers[1].ms, 5000);

  timers[1].fn();
  await tick();
  await tick();

  assert.equal(attempts, 2);
  assert.equal(timers.length, 2, 'success must not schedule another attempt');
  assert.equal(logs.length, 1);
}

// A synchronous throw from the start function is contained the same way as
// an async rejection.
{
  const timers = [];
  const logs = [];
  const schedule = createReconnectScheduler(
    () => { throw new Error('sync boom'); },
    {
      retryDelayMs: 1000,
      log: line => logs.push(line),
      setTimeoutFn: (fn, ms) => timers.push({ fn, ms }),
    },
  );

  schedule(0);
  timers[0].fn();
  await tick();
  await tick();

  assert.equal(logs.length, 1);
  assert.match(logs[0], /sync boom/);
  assert.equal(timers.length, 2);
}

// -- createVersionResolver ------------------------------------------------

// A successful fetch returns and caches the version.
{
  const resolveVersion = createVersionResolver(
    async () => ({ version: [2, 3000, 99] }),
    { log: () => {} },
  );
  assert.deepEqual(await resolveVersion(), [2, 3000, 99]);
}

// A fetch that never settles resolves within the timeout bound instead of
// pending forever; before any success there is no cache, so the resolver
// yields null (callers fall back to the Baileys default).
{
  const logs = [];
  const resolveVersion = createVersionResolver(
    () => new Promise(() => {}),
    { timeoutMs: 20, log: line => logs.push(line) },
  );
  assert.equal(await resolveVersion(), null);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /version fetch timed out/);
  assert.match(logs[0], /library default/);
}

// After one success, later failures fall back to the cached version.
{
  const logs = [];
  let calls = 0;
  const resolveVersion = createVersionResolver(
    async () => {
      calls += 1;
      if (calls === 1) return { version: [2, 3000, 42] };
      throw new Error('network down');
    },
    { timeoutMs: 20, log: line => logs.push(line) },
  );
  assert.deepEqual(await resolveVersion(), [2, 3000, 42]);
  assert.deepEqual(await resolveVersion(), [2, 3000, 42]);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /network down/);
  assert.match(logs[0], /cached version/);
}

// The losing timeout timer is cleared after a fast success, so the resolver
// does not hold the event loop open for the full timeout window.
{
  const resolveVersion = createVersionResolver(
    async () => ({ version: [2, 3000, 1] }),
    { timeoutMs: 60_000, log: () => {} },
  );
  const before = Date.now();
  await resolveVersion();
  await sleep(10);
  assert.ok(Date.now() - before < 1000);
}

console.log('bridge.reconnect.test.mjs: all assertions passed');

// -- createReconnectBackoff ------------------------------------------------

// Exponential sequence with no jitter (random -> 0.5 keeps the midpoint).
{
  const backoff = createReconnectBackoff({ random: () => 0.5 });
  const delays = [3000, 6000, 12000, 24000, 48000, 96000, 192000, 300000, 300000];
  for (const expected of delays) {
    assert.equal(backoff.next(), expected, `expected ${expected}ms`);
  }
}

// Cap holds at maxMs and does not grow past it.
{
  const backoff = createReconnectBackoff({ maxMs: 10000, random: () => 0.5 });
  for (let i = 0; i < 10; i += 1) {
    assert.ok(backoff.next() <= 10000, 'delay must never exceed maxMs');
  }
}

// Jitter bounds: random() in [0, 1) must keep every delay within ±jitterFactor.
{
  const baseMs = 3000, maxMs = 30000;
  const factor = 0.2;
  // worst cases: random() -> 0 (min) and random() -> 0.999... (max)
  for (const r of [0, 0.999999]) {
    const backoff = createReconnectBackoff({ baseMs, maxMs, jitterFactor: factor, random: () => r });
    for (let i = 0; i < 50; i += 1) {
      const d = backoff.next();
      assert.ok(d >= 0, `delay must not be negative (got ${d})`);
      // every delay must be within ±20% of SOME exponential step <= maxMs,
      // where the capped step is included and repeats once the cap is hit
      let ok = false;
      let exp = baseMs;
      while (true) {
        const step = Math.min(exp, maxMs);
        const lo = step * (1 - factor), hi = step * (1 + factor);
        if (d >= lo && d <= hi) { ok = true; break; }
        if (step >= maxMs) break; // every later step is the cap; checked once
        exp *= 2;
      }
      assert.ok(ok, `delay ${d} outside ±20% of any exponential step`);
    }
  }
}

// reset() restarts the sequence.
{
  const backoff = createReconnectBackoff({ random: () => 0.5 });
  assert.equal(backoff.next(), 3000);
  assert.equal(backoff.next(), 6000);
  backoff.reset();
  assert.equal(backoff.next(), 3000, 'reset must restart from baseMs');
}

// Deterministic per-call jitter: same random() -> same delay for a fixed step.
{
  const a = createReconnectBackoff({ random: () => 0.1 });
  const b = createReconnectBackoff({ random: () => 0.1 });
  assert.equal(a.next(), b.next());
}

console.log('createReconnectBackoff: all assertions passed');
