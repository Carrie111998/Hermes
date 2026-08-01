import test from 'node:test';
import assert from 'node:assert/strict';

import { createReconnectScheduler } from './reconnect_scheduler.js';

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
    async fireOnly() {
      assert.equal(pending.size, 1, 'exactly one timer must be pending');
      const [id, timer] = pending.entries().next().value;
      pending.delete(id);
      return timer.fn();
    },
    async runOnly() {
      await this.fireOnly();
    },
  };
}

test('reconnect scheduler coalesces pending starts into one timer', () => {
  const timers = fakeTimers();
  const scheduler = createReconnectScheduler({
    start: async () => {},
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(120_000);
  scheduler.schedule(3_000);

  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 3_000);
});

test('reconnect scheduler never replaces an earlier timer with a later one', () => {
  const timers = fakeTimers();
  const scheduler = createReconnectScheduler({
    start: async () => {},
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(3_000);
  scheduler.schedule(120_000);

  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 3_000);
});

test('reconnect scheduler catches a rejected start and schedules one retry', async () => {
  const timers = fakeTimers();
  const errors = [];
  let attempts = 0;
  const scheduler = createReconnectScheduler({
    start: async () => {
      attempts += 1;
      throw new Error('auth read failed');
    },
    retryDelayMs: 3_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onError: (error) => errors.push(error.message),
  });

  scheduler.schedule(0);
  await timers.runOnly();

  assert.equal(attempts, 1);
  assert.deepEqual(errors, ['auth read failed']);
  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 3_000);
});

test('reconnect scheduler retries even when error reporting throws', async () => {
  const timers = fakeTimers();
  const scheduler = createReconnectScheduler({
    start: async () => { throw new Error('auth read failed'); },
    retryDelayMs: 3_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    onError: () => { throw new Error('logger failed'); },
  });

  scheduler.schedule(0);
  await timers.runOnly();

  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 3_000);
});

test('reconnect scheduler coalesces a request arriving during a failed start', async () => {
  const timers = fakeTimers();
  let rejectStart;
  const scheduler = createReconnectScheduler({
    start: () => new Promise((_, reject) => { rejectStart = reject; }),
    retryDelayMs: 3_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(0);
  const activeStart = timers.fireOnly();
  scheduler.schedule(1_000);
  rejectStart(new Error('handshake setup failed'));
  await activeStart;

  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 1_000);
});

test('reconnect scheduler prefers retry delay over a later request after failure', async () => {
  const timers = fakeTimers();
  let rejectStart;
  const scheduler = createReconnectScheduler({
    start: () => new Promise((_, reject) => { rejectStart = reject; }),
    retryDelayMs: 3_000,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(0);
  const activeStart = timers.fireOnly();
  scheduler.schedule(5_000);
  rejectStart(new Error('handshake setup failed'));
  await activeStart;

  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 3_000);
});

test('reconnect scheduler drops stale requests after a successful start', async () => {
  const timers = fakeTimers();
  let resolveStart;
  const scheduler = createReconnectScheduler({
    start: () => new Promise((resolve) => { resolveStart = resolve; }),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(0);
  const activeStart = timers.fireOnly();
  scheduler.schedule(1_000);
  resolveStart();
  await activeStart;

  assert.equal(timers.pending.size, 0);
});

test('reconnect scheduler can schedule a fresh start after success', async () => {
  const timers = fakeTimers();
  let attempts = 0;
  const scheduler = createReconnectScheduler({
    start: async () => { attempts += 1; },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });

  scheduler.schedule(0);
  await timers.runOnly();
  assert.equal(attempts, 1);
  assert.equal(timers.pending.size, 0);

  scheduler.schedule(120_000);
  assert.equal(timers.pending.size, 1);
  assert.equal([...timers.pending.values()][0].delay, 120_000);
  await timers.runOnly();
  assert.equal(attempts, 2);
  assert.equal(timers.pending.size, 0);
});
