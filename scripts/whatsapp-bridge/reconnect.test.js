import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createCachedVersionResolver,
  createConnectionWatchdog,
  createReconnectScheduler,
  reconnectDelayForReason,
  withTimeout,
} from './reconnect.js';

test('withTimeout rejects a stalled operation', async () => {
  const never = new Promise(() => {});

  await assert.rejects(
    withTimeout(never, 10, 'version discovery'),
    /version discovery timed out after 10ms/,
  );
});

test('version resolver caches the first successful discovery', async () => {
  let calls = 0;
  const resolveVersion = createCachedVersionResolver(async () => {
    calls += 1;
    return { version: [2, 3000, 1] };
  }, { timeoutMs: 50 });

  assert.deepEqual(await resolveVersion(), [2, 3000, 1]);
  assert.deepEqual(await resolveVersion(), [2, 3000, 1]);
  assert.equal(calls, 1);
});

test('version resolver falls back instead of wedging when discovery stalls', async () => {
  const resolveVersion = createCachedVersionResolver(
    async () => new Promise(() => {}),
    { timeoutMs: 10 },
  );

  assert.equal(await resolveVersion(), null);
});

test('close code 428 uses the bounded normal reconnect path', () => {
  assert.equal(reconnectDelayForReason(428), 3000);
  assert.equal(reconnectDelayForReason(515), 1000);
});

test('reconnect scheduler deduplicates close events and surfaces start failure', async () => {
  const callbacks = [];
  const errors = [];
  let starts = 0;
  const scheduler = createReconnectScheduler({
    setTimer: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    clearTimer: () => {},
    onError: (error) => errors.push(error),
  });

  scheduler.schedule(async () => {
    starts += 1;
    throw new Error('socket start failed');
  }, 3000);
  scheduler.schedule(async () => {
    starts += 1;
  }, 3000);

  assert.equal(callbacks.length, 1);
  await callbacks[0]();
  assert.equal(starts, 1);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /socket start failed/);
});

test('reconnect scheduler coalesces one retry while start is in flight', async () => {
  const callbacks = [];
  let releaseStart;
  let starts = 0;
  const scheduler = createReconnectScheduler({
    setTimer: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    clearTimer: () => {},
  });

  assert.equal(scheduler.schedule(async () => {
    starts += 1;
    await new Promise((resolve) => { releaseStart = resolve; });
  }, 3000), true);

  const firstAttempt = callbacks[0]();
  await Promise.resolve();
  assert.equal(starts, 1);
  assert.equal(scheduler.schedule(async () => { starts += 1; }, 3000), false);
  assert.equal(scheduler.schedule(async () => { starts += 100; }, 3000), false);
  assert.equal(callbacks.length, 1);

  releaseStart();
  await firstAttempt;
  assert.equal(callbacks.length, 2);
  await callbacks[1]();
  assert.equal(starts, 2);
});

test('reconnect scheduler cancel drops a retry queued during start', async () => {
  const callbacks = [];
  let releaseStart;
  const scheduler = createReconnectScheduler({
    setTimer: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    clearTimer: () => {},
  });

  scheduler.schedule(
    async () => new Promise((resolve) => { releaseStart = resolve; }),
    3000,
  );
  const firstAttempt = callbacks[0]();
  await Promise.resolve();
  scheduler.schedule(async () => {}, 3000);
  scheduler.cancel();

  releaseStart();
  await firstAttempt;
  assert.equal(callbacks.length, 1);
});

test('connection watchdog fails a socket that never opens or closes', () => {
  const callbacks = [];
  const cleared = [];
  const watchdog = createConnectionWatchdog({
    setTimer: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    clearTimer: (id) => cleared.push(id),
  });
  let timedOut = false;

  watchdog.arm(() => {
    timedOut = true;
  }, 30_000);
  assert.equal(callbacks.length, 1);
  callbacks[0]();
  assert.equal(timedOut, true);

  watchdog.arm(() => {}, 30_000);
  watchdog.cancel();
  assert.deepEqual(cleared, [2]);
});
