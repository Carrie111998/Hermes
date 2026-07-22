import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';

import { listenWithRetry } from './bridge_helpers.js';

const silentLog = { warn: () => {}, error: () => {} };

function addrInUse() {
  return Object.assign(new Error('listen EADDRINUSE: address already in use 127.0.0.1:3000'), {
    code: 'EADDRINUSE',
  });
}

test('listenWithRetry retries EADDRINUSE and succeeds once the port frees up', async () => {
  let attempts = 0;
  const listenFn = () => {
    attempts += 1;
    const server = new EventEmitter();
    setImmediate(() => {
      if (attempts < 3) server.emit('error', addrInUse());
      else server.emit('listening');
    });
    return server;
  };

  await new Promise((resolve, reject) => {
    listenWithRetry(listenFn, {
      retries: 5,
      delayMs: 1,
      log: silentLog,
      onListening: () => resolve(),
      onFatal: (err) => reject(err),
    });
  });
  assert.equal(attempts, 3);
});

test('listenWithRetry gives up after exhausting retries', async () => {
  let attempts = 0;
  const listenFn = () => {
    attempts += 1;
    const server = new EventEmitter();
    setImmediate(() => server.emit('error', addrInUse()));
    return server;
  };

  const err = await new Promise((resolve, reject) => {
    listenWithRetry(listenFn, {
      retries: 2,
      delayMs: 1,
      log: silentLog,
      onListening: () => reject(new Error('should not listen')),
      onFatal: (e) => resolve(e),
    });
  });
  assert.equal(err.code, 'EADDRINUSE');
  assert.equal(attempts, 3); // initial try + 2 retries
});

test('listenWithRetry treats non-EADDRINUSE errors as fatal immediately', async () => {
  let attempts = 0;
  const listenFn = () => {
    attempts += 1;
    const server = new EventEmitter();
    setImmediate(() => server.emit('error', Object.assign(new Error('denied'), { code: 'EACCES' })));
    return server;
  };

  const err = await new Promise((resolve) => {
    listenWithRetry(listenFn, {
      retries: 5,
      delayMs: 1,
      log: silentLog,
      onFatal: (e) => resolve(e),
    });
  });
  assert.equal(err.code, 'EACCES');
  assert.equal(attempts, 1);
});
