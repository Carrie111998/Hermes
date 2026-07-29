import test from 'node:test';
import assert from 'node:assert/strict';

import { createInboxReceiptBuffer, createInboxSweepController } from './inbox_sweep.js';

function fakeTimers() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();

  function setTimer(fn, delay) {
    const id = nextId++;
    pending.set(id, { at: now + delay, fn });
    return id;
  }

  function clearTimer(id) {
    pending.delete(id);
  }

  function advance(ms) {
    const target = now + ms;
    while (true) {
      const due = [...pending.entries()]
        .filter(([, timer]) => timer.at <= target)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      pending.delete(due[0]);
      now = due[1].at;
      due[1].fn();
    }
    now = target;
  }

  return { pending, setTimer, clearTimer, advance, now: () => now };
}

test('sweep window is fixed and intentional close preserves anchored reconnect', () => {
  const timers = fakeTimers();
  let closes = 0;
  let reconnects = 0;
  const sweep = createInboxSweepController({
    reconnectIntervalMs: 120_000,
    windowMs: 3_000,
    setTimeoutFn: timers.setTimer,
    clearTimeoutFn: timers.clearTimer,
    closeSocket: () => { closes += 1; },
    reconnect: () => { reconnects += 1; },
  });

  sweep.connected();
  timers.advance(2_999);
  sweep.receivedInbound();
  assert.equal(closes, 0);
  timers.advance(1);
  assert.equal(closes, 1);
  sweep.closed({ intentional: true, reason: 428 });
  timers.advance(116_999);
  assert.equal(reconnects, 0);
  timers.advance(1);
  assert.equal(reconnects, 1);
});

test('unintentional close replaces anchored timer with bounded retry', () => {
  const timers = fakeTimers();
  let reconnects = 0;
  const sweep = createInboxSweepController({
    reconnectIntervalMs: 120_000,
    setTimeoutFn: timers.setTimer,
    clearTimeoutFn: timers.clearTimer,
    closeSocket: () => {},
    reconnect: () => { reconnects += 1; },
  });

  sweep.connected();
  timers.advance(500);
  sweep.closed({ intentional: false, reason: 515 });
  assert.deepEqual([...timers.pending.values()].map(({ at }) => at - timers.now()), [1_000]);
  timers.advance(1_000);
  assert.equal(reconnects, 1);
});

test('repeated connected calls replace timers and stop clears them', () => {
  const timers = fakeTimers();
  const sweep = createInboxSweepController({
    reconnectIntervalMs: 120_000,
    setTimeoutFn: timers.setTimer,
    clearTimeoutFn: timers.clearTimer,
    closeSocket: () => {},
    reconnect: () => {},
  });

  sweep.connected();
  assert.equal(timers.pending.size, 2);
  sweep.connected();
  assert.equal(timers.pending.size, 2);
  sweep.stop();
  assert.equal(timers.pending.size, 0);
});

test('receipt buffer is bounded, ordered, idempotent and isolates delivery failures', () => {
  const delivered = [];
  const errors = [];
  const buffer = createInboxReceiptBuffer({
    maxEntries: 3,
    deliver: (receipt) => {
      if (receipt.id === 'bad') throw new Error('delivery failed');
      delivered.push(receipt.id);
    },
    onDeliveryError: (error, receipt) => errors.push([error.message, receipt.id]),
  });

  assert.equal(buffer.capture({ id: 'first' }), true);
  assert.equal(buffer.capture({ id: 'bad' }), true);
  assert.equal(buffer.capture({ id: 'last' }), true);
  assert.equal(buffer.capture({ id: 'overflow' }), false);
  buffer.release();
  buffer.release();

  assert.deepEqual(delivered, ['first', 'last']);
  assert.deepEqual(errors, [['delivery failed', 'bad']]);
  assert.equal(buffer.size, 0);
});
