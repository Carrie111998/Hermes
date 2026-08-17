import assert from 'node:assert/strict';
import test from 'node:test';

import { installDom, resetDom } from './dom-shim.mjs';

const dom = installDom();
const { renderScoring, transferWeight } = await import('../../../server/webui/js/pages/research-scoring.js');

const DEFAULTS = {
  product_sector_fit: 25,
  buyer_channel_fit: 20,
  buying_intent: 15,
  market_coverage: 15,
  commercial_scale: 10,
  trade_activity: 10,
  contactability: 5,
};

test('plus five takes from the highest other weight', () => {
  const next = transferWeight(DEFAULTS, 'contactability', 5);

  assert.equal(next.product_sector_fit, 20);
  assert.equal(next.contactability, 10);
  assert.equal(Object.values(next).reduce((a, b) => a + b, 0), 100);
});

test('minus five gives to the lowest eligible other weight in dimension order', () => {
  const next = transferWeight(DEFAULTS, 'product_sector_fit', -5);

  assert.equal(next.product_sector_fit, 20);
  assert.equal(next.contactability, 10);
  assert.equal(Object.values(next).reduce((a, b) => a + b, 0), 100);
});

test('illegal five-point transfers leave the profile unchanged', () => {
  const atFloor = { ...DEFAULTS, contactability: 0, trade_activity: 15 };
  const atCeiling = { ...DEFAULTS, product_sector_fit: 100, buyer_channel_fit: 0, buying_intent: 0, market_coverage: 0, commercial_scale: 0, trade_activity: 0, contactability: 0 };

  assert.deepEqual(transferWeight(atFloor, 'contactability', -5), atFloor);
  assert.deepEqual(transferWeight(atCeiling, 'product_sector_fit', 5), atCeiling);
});

test('weight controls announce and highlight both dimensions after a transfer', () => {
  const updates = [];
  const root = renderScoring({ name: 'High precision', weights: DEFAULTS }, {
    onChange: value => updates.push(value),
  });
  dom.document.body.append(root);
  const rows = root.querySelectorAll('.ifz-weight-row');
  const source = rows.find(row => row.getAttribute('aria-label') === 'Product and sector fit');
  const recipient = rows.find(row => row.getAttribute('aria-label') === 'Contactability');
  const decrease = source.querySelectorAll('button').find(control => control.textContent === '-5');

  assert.ok(decrease, 'the control must use the explicit -5 label');
  decrease.click();

  assert.equal(source.classList.contains('changed'), true);
  assert.equal(recipient.classList.contains('changed'), true);
  assert.match(root.querySelector('[role="status"]').textContent, /Product and sector fit decreased.*Contactability increased/);
  assert.deepEqual(updates.at(-1).weights, {
    ...DEFAULTS, product_sector_fit: 20, contactability: 10,
  });
  resetDom(dom);
});
