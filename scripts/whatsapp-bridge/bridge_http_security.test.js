import assert from 'node:assert/strict';
import test from 'node:test';

import { hasValidBridgeToken } from './bridge_http_security.js';

test('accepts the exact bearer token and rejects missing or malformed credentials', () => {
  const expected = 'test-token-with-enough-entropy';

  assert.equal(hasValidBridgeToken(expected, `Bearer ${expected}`), true);
  assert.equal(hasValidBridgeToken(expected, ''), false);
  assert.equal(hasValidBridgeToken(expected, `Basic ${expected}`), false);
  assert.equal(hasValidBridgeToken(expected, 'Bearer wrong-token'), false);
});

test('keeps standalone bridges backward compatible when no token is configured', () => {
  assert.equal(hasValidBridgeToken('', ''), true);
  assert.equal(hasValidBridgeToken(undefined, ''), true);
});
