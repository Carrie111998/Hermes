import test from 'node:test';
import assert from 'node:assert/strict';

import { createBaileysVersionResolver } from './baileys_version.js';

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  return {
    pending,
    setTimer(fn, delay) {
      const id = nextId++;
      pending.set(id, { fn, delay });
      return id;
    },
    clearTimer(id) {
      pending.delete(id);
    },
    fireOnly() {
      assert.equal(pending.size, 1, 'exactly one timeout must be pending');
      const [id, timer] = pending.entries().next().value;
      pending.delete(id);
      timer.fn();
    },
  };
}

test('version resolver fetches once and caches the successful version', async () => {
  const timers = fakeTimers();
  const latest = [2, 3000, 1043857760];
  let calls = 0;
  const resolveVersion = createBaileysVersionResolver({
    fetchVersion: async () => {
      calls += 1;
      return { version: latest, isLatest: true };
    },
    fallbackVersion: [2, 3000, 1035194821],
    timeoutMs: 5_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  const [first, second] = await Promise.all([resolveVersion(), resolveVersion()]);
  const third = await resolveVersion();

  assert.equal(calls, 1);
  assert.deepEqual(first, latest);
  assert.deepEqual(second, latest);
  assert.deepEqual(third, latest);
  assert.equal(timers.pending.size, 0);
});

test('version resolver bounds a hanging lookup and caches the fallback', async () => {
  const timers = fakeTimers();
  const fallback = [2, 3000, 1035194821];
  let calls = 0;
  const reasons = [];
  const resolveVersion = createBaileysVersionResolver({
    fetchVersion: () => {
      calls += 1;
      return new Promise(() => {});
    },
    fallbackVersion: fallback,
    timeoutMs: 5_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onFallback: (reason) => reasons.push(reason),
  });

  const firstPromise = resolveVersion();
  timers.fireOnly();
  const first = await firstPromise;
  const second = await resolveVersion();

  assert.equal(calls, 1);
  assert.deepEqual(first, fallback);
  assert.deepEqual(second, fallback);
  assert.deepEqual(reasons, ['timeout']);
});

test('version resolver keeps the cached fallback when the timed-out lookup settles late', async () => {
  const timers = fakeTimers();
  const fallback = [2, 3000, 1035194821];
  const reasons = [];
  let rejectFetch;
  const lateFetch = new Promise((_, reject) => { rejectFetch = reject; });
  const resolveVersion = createBaileysVersionResolver({
    fetchVersion: () => lateFetch,
    fallbackVersion: fallback,
    timeoutMs: 5_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onFallback: (reason) => reasons.push(reason),
  });

  const firstPromise = resolveVersion();
  await Promise.resolve();
  timers.fireOnly();
  assert.deepEqual(await firstPromise, fallback);

  // node:test fails this test automatically if the late rejection is unhandled.
  rejectFetch(new Error('late network failure'));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(await resolveVersion(), fallback);
  assert.deepEqual(reasons, ['timeout']);
  assert.equal(timers.pending.size, 0);
});

test('version resolver rejects malformed, unsafe, and non-latest version results', async () => {
  const fallback = [2, 3000, 1035194821];
  const cases = [
    { result: null, reason: 'invalid_version' },
    { result: { version: [2, 3000], isLatest: true }, reason: 'invalid_version' },
    { result: { version: [2, 3000, 1.5], isLatest: true }, reason: 'invalid_version' },
    { result: { version: [2, 3000, -1], isLatest: true }, reason: 'invalid_version' },
    { result: { version: [2, 3000, Number.MAX_SAFE_INTEGER + 1], isLatest: true }, reason: 'invalid_version' },
    { result: { version: [2, 3000, 1043857760], isLatest: false }, reason: 'not_latest' },
  ];

  for (const { result, reason } of cases) {
    const timers = fakeTimers();
    const reasons = [];
    const resolveVersion = createBaileysVersionResolver({
      fetchVersion: async () => result,
      fallbackVersion: fallback,
      timeoutMs: 5_000,
      setTimer: timers.setTimer,
      clearTimer: timers.clearTimer,
      onFallback: (value) => reasons.push(value),
    });

    assert.deepEqual(await resolveVersion(), fallback);
    assert.deepEqual(reasons, [reason]);
    assert.equal(timers.pending.size, 0);
  }
});

test('version resolver reports Baileys soft failures instead of treating bundled fallback as latest', async () => {
  const timers = fakeTimers();
  const fallback = [2, 3000, 1035194821];
  const reasons = [];
  const resolveVersion = createBaileysVersionResolver({
    fetchVersion: async () => ({
      version: fallback,
      isLatest: false,
      error: new Error('upstream unavailable'),
    }),
    fallbackVersion: fallback,
    timeoutMs: 5_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onFallback: (reason) => reasons.push(reason),
  });

  assert.deepEqual(await resolveVersion(), fallback);
  assert.equal(reasons.length, 1);
  assert.equal(reasons[0].message, 'upstream unavailable');
  assert.equal(timers.pending.size, 0);
});

test('version resolver contains lookup and fallback-reporting failures', async () => {
  const timers = fakeTimers();
  const fallback = [2, 3000, 1035194821];
  const resolveVersion = createBaileysVersionResolver({
    fetchVersion: async () => { throw new Error('network failed'); },
    fallbackVersion: fallback,
    timeoutMs: 5_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onFallback: () => { throw new Error('logger failed'); },
  });

  assert.deepEqual(await resolveVersion(), fallback);
  assert.equal(timers.pending.size, 0);
});
