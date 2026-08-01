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

class FakeNode {
  constructor(tagName = null, text = '') {
    this.nodeType = tagName ? 1 : 3;
    this.tagName = tagName?.toUpperCase();
    this._text = text;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.attributes = new Map();
    this.className = '';
    this.classList = {
      add: (...names) => {
        const current = new Set(this.className.split(/\s+/).filter(Boolean));
        for (const name of names) current.add(name);
        this.className = [...current].join(' ');
      },
    };
  }

  append(...children) {
    for (const child of children) {
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  replaceChildren(...children) {
    this.childNodes = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'class') this.className = String(value);
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  dispatchEvent(event) {
    event.target ||= this;
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }

  click() { this.dispatchEvent({ type: 'click' }); }
  focus() {}

  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes = this.parentNode.childNodes.filter(node => node !== this);
    this.parentNode = null;
  }

  get textContent() {
    return this.nodeType === 3
      ? this._text
      : this.childNodes.map(child => child.textContent).join('');
  }

  set textContent(value) {
    this._text = String(value);
    if (this.nodeType === 1) {
      this.childNodes = [new FakeNode(null, this._text)];
      this.childNodes[0].parentNode = this;
    }
  }
}

function nodesMatching(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    for (const child of node.childNodes || []) visit(child);
  }
  visit(root);
  return matches;
}

function fakeDocument() {
  const listeners = new Map();
  return {
    body: new FakeNode('body'),
    createElement: tag => new FakeNode(tag),
    createElementNS: (_namespace, tag) => new FakeNode(tag),
    createTextNode: text => new FakeNode(null, String(text)),
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
}

function fakeBrowserWindow() {
  const listeners = new Map();
  const popup = {
    closed: false,
    location: { href: null, replace(url) { this.href = url; } },
    close() { this.closed = true; },
  };
  return {
    popup,
    location: { origin: 'https://app.example.test' },
    openCalls: [],
    open(...args) { this.openCalls.push(args); return popup; },
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
    postOAuth(data) {
      listeners.get('message')?.({
        origin: this.location.origin,
        source: popup,
        data,
      });
    },
  };
}

async function flushUntil(predicate, message) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail(message);
}

test('Google Connect starts provider OAuth and refreshes Integrations once on success', async () => {
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const originalSetInterval = globalThis.setInterval;
  const documentRef = fakeDocument();
  const windowRef = fakeBrowserWindow();
  const requests = [];
  let emailLists = 0;
  let dispose;

  globalThis.document = documentRef;
  globalThis.window = windowRef;
  globalThis.setTimeout = (...args) => {
    const timer = originalSetTimeout(...args);
    timer.unref?.();
    return timer;
  };
  globalThis.setInterval = (...args) => {
    const timer = originalSetInterval(...args);
    timer.unref?.();
    return timer;
  };
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, method: options.method || 'GET' });
    if (url === '/health') return Response.json({ agent_runs_enabled: false });
    if (url === '/api/v1/integrations/email') {
      emailLists += 1;
      return Response.json([]);
    }
    if (url === '/api/v1/integrations/whatsapp'
        || url === '/api/v1/linkedin/actions'
        || url === '/api/v1/data-sources') return Response.json([]);
    if (url === '/api/v1/integrations/email/oauth/google/start') {
      return Response.json({
        authorize_url: 'https://accounts.example.test/authorize',
        expires_in: 600,
      });
    }
    return Response.json({ message: `Unexpected request: ${options.method} ${url}` }, { status: 404 });
  };

  try {
    const { resetReal } = await import('../../../server/webui/js/mocks/db.js');
    const { mount } = await import('../../../server/webui/js/pages/integrations.js');
    resetReal();
    const root = new FakeNode('main');
    dispose = await mount(root, { navigate() {} });

    const connectButtons = nodesMatching(
      root,
      node => node.tagName === 'BUTTON' && node.textContent === 'Connect',
    );
    assert.equal(connectButtons.length, 4);
    connectButtons[0].click();

    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(windowRef.openCalls.length, 1);
    assert.equal(windowRef.popup.location.href, 'https://accounts.example.test/authorize');
    assert.deepEqual(
      requests.filter(request => request.url.includes('/integrations/email/') && request.method === 'POST'),
      [{ url: '/api/v1/integrations/email/oauth/google/start', method: 'POST' }],
    );

    windowRef.postOAuth({
      type: 'interfaze:oauth',
      provider: 'google',
      status: 'connected',
    });
    await flushUntil(() => emailLists === 2, 'Integrations did not refresh after OAuth success');

    assert.equal(emailLists, 2);
    assert.equal(nodesMatching(
      documentRef.body,
      node => node.tagName === 'SPAN' && node.textContent === 'Google Workspace connected',
    ).length, 1);
  } finally {
    dispose?.();
    globalThis.document = originalDocument;
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.setInterval = originalSetInterval;
  }
});
