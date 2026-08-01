import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../../../server/webui/js/oauth-popup.js', import.meta.url),
  'utf8',
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { startEmailOAuth } = await import(moduleUrl);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function fakeTimers() {
  let nextId = 1;
  const intervals = new Map();
  const timeouts = new Map();
  return {
    api: {
      setInterval(fn) { const id = nextId++; intervals.set(id, fn); return id; },
      clearInterval(id) { intervals.delete(id); },
      setTimeout(fn) { const id = nextId++; timeouts.set(id, fn); return id; },
      clearTimeout(id) { timeouts.delete(id); },
    },
    async tickIntervals() {
      for (const fn of [...intervals.values()]) await fn();
      await Promise.resolve();
    },
    async fireTimeouts() {
      for (const [id, fn] of [...timeouts]) { timeouts.delete(id); fn(); }
      await Promise.resolve();
    },
    counts() { return { intervals: intervals.size, timeouts: timeouts.size }; },
  };
}

function harness(overrides = {}) {
  const listeners = new Map();
  const popup = {
    closed: false,
    closeCalls: 0,
    location: { urls: [], replace(url) { this.urls.push(url); } },
    close() { this.closed = true; this.closeCalls += 1; },
  };
  const windowRef = {
    location: { origin: 'https://app.example.test' },
    openCalls: [],
    open(...args) { this.openCalls.push(args); return popup; },
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) {
      if (listeners.get(type) === fn) listeners.delete(type);
    },
  };
  const timers = fakeTimers();
  const statuses = [];
  let connected = 0;
  let listResult = { items: [] };
  const startResult = {
    authorize_url: 'https://provider.example.test/authorize',
    expires_in: 600,
  };
  const options = {
    provider: 'google',
    windowRef,
    timerApi: timers.api,
    pollMs: 1000,
    startOAuth: async () => startResult,
    listIntegrations: async () => listResult,
    onConnected: () => { connected += 1; },
    onStatus: status => statuses.push(status),
    ...overrides,
  };
  return {
    options, popup, windowRef, timers, statuses, listeners,
    setListResult(value) { listResult = value; },
    connectedCount() { return connected; },
    message(event) { listeners.get('message')?.(event); },
  };
}

test('blocked popup reports blocked and never starts OAuth', async () => {
  let starts = 0;
  const h = harness({
    startOAuth: async () => { starts += 1; },
  });
  h.windowRef.open = () => null;
  const attempt = startEmailOAuth(h.options);
  await Promise.resolve();
  assert.equal(attempt, null);
  assert.equal(starts, 0);
  assert.deepEqual(h.statuses, [{ status: 'blocked' }]);
});

test('opens synchronously then navigates after the start request', async () => {
  const started = deferred();
  const h = harness({ startOAuth: () => started.promise });
  const attempt = startEmailOAuth(h.options);
  assert.equal(h.windowRef.openCalls.length, 1);
  assert.deepEqual(h.popup.location.urls, []);
  started.resolve({ authorize_url: 'https://provider.test/auth', expires_in: 30 });
  assert.equal(await attempt.ready, true);
  assert.deepEqual(h.popup.location.urls, ['https://provider.test/auth']);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
});

test('closing the popup while start is pending reports cancellation', async () => {
  const started = deferred();
  const h = harness({ startOAuth: () => started.promise });
  const attempt = startEmailOAuth(h.options);
  h.popup.closed = true;
  started.resolve({ authorize_url: 'https://provider.test/auth', expires_in: 30 });
  assert.equal(await attempt.ready, false);
  assert.deepEqual(h.statuses, [{ status: 'cancelled' }]);
  assert.deepEqual(h.popup.location.urls, []);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('start failure closes the blank popup and releases resources', async () => {
  const error = new Error('OAuth is not configured');
  const h = harness({ startOAuth: async () => { throw error; } });
  const attempt = startEmailOAuth(h.options);
  assert.equal(await attempt.ready, false);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.statuses, [{ status: 'start_failed', error }]);
  assert.equal(h.listeners.has('message'), false);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('accepts only the expected origin window provider type and status', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  const valid = { type: 'interfaze:oauth', provider: 'google', status: 'connected' };
  h.message({ origin: 'https://evil.test', source: h.popup, data: valid });
  h.message({ origin: h.windowRef.location.origin, source: {}, data: valid });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, provider: 'microsoft' } });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, type: 'other' } });
  h.message({ origin: h.windowRef.location.origin, source: h.popup,
    data: { ...valid, status: 'unknown' } });
  assert.equal(h.connectedCount(), 0);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
  h.message({ origin: h.windowRef.location.origin, source: h.popup, data: valid });
  assert.equal(h.connectedCount(), 1);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('polling fallback completes once and ignores a later message race', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  h.setListResult({ items: [{ provider: 'google', status: 'connected' }] });
  await h.timers.tickIntervals();
  h.message({
    origin: h.windowRef.location.origin,
    source: h.popup,
    data: { type: 'interfaze:oauth', provider: 'google', status: 'connected' },
  });
  assert.equal(h.connectedCount(), 1);
  assert.equal(h.popup.closeCalls, 1);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('transient polling failures do not abort the bounded attempt', async () => {
  const h = harness({ listIntegrations: async () => { throw new Error('temporary'); } });
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  await h.timers.tickIntervals();
  assert.deepEqual(h.statuses, []);
  assert.deepEqual(h.timers.counts(), { intervals: 1, timeouts: 1 });
});

test('manual popup close reports cancellation and cleans up', async () => {
  const h = harness();
  const attempt = startEmailOAuth(h.options);
  await attempt.ready;
  h.popup.closed = true;
  await h.timers.tickIntervals();
  assert.deepEqual(h.statuses, [{ status: 'cancelled' }]);
  assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
});

test('callback cancellation and failure notify once without closing the popup', async () => {
  for (const status of ['cancelled', 'failed']) {
    const h = harness();
    const attempt = startEmailOAuth(h.options);
    await attempt.ready;
    h.message({
      origin: h.windowRef.location.origin,
      source: h.popup,
      data: { type: 'interfaze:oauth', provider: 'google', status },
    });
    assert.deepEqual(h.statuses, [{ status }]);
    assert.equal(h.popup.closeCalls, 0);
    assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
  }
});

test('expiry reports expired and explicit disposal is silent', async () => {
  const expired = harness();
  const expiringAttempt = startEmailOAuth(expired.options);
  await expiringAttempt.ready;
  await expired.timers.fireTimeouts();
  assert.deepEqual(expired.statuses, [{ status: 'expired' }]);
  assert.deepEqual(expired.timers.counts(), { intervals: 0, timeouts: 0 });

  const disposed = harness();
  const disposedAttempt = startEmailOAuth(disposed.options);
  await disposedAttempt.ready;
  disposedAttempt.cancel();
  assert.deepEqual(disposed.statuses, []);
  assert.deepEqual(disposed.timers.counts(), { intervals: 0, timeouts: 0 });
  assert.equal(disposed.listeners.has('message'), false);
});

test('invalid start responses fail closed and clean up', async () => {
  const invalidResults = [
    { authorize_url: '', expires_in: 600 },
    { authorize_url: 'https://provider.test/auth', expires_in: 0 },
  ];
  for (const result of invalidResults) {
    const h = harness({ startOAuth: async () => result });
    const attempt = startEmailOAuth(h.options);
    assert.equal(await attempt.ready, false);
    assert.equal(h.popup.closeCalls, 1);
    assert.equal(h.statuses.length, 1);
    assert.equal(h.statuses[0].status, 'start_failed');
    assert.equal(h.statuses[0].error.message, 'OAuth start returned an invalid response');
    assert.deepEqual(h.timers.counts(), { intervals: 0, timeouts: 0 });
    assert.equal(h.listeners.has('message'), false);
  }
});
