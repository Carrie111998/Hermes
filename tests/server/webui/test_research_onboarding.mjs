import assert from 'node:assert/strict';
import test, { afterEach } from 'node:test';

import { installDom, resetDom, byText } from './dom-shim.mjs';

const dom = installDom();
afterEach(() => resetDom(dom));

test('setup requires explicit confirmation of derived products and emphasis', async () => {
  const { researchProfileStep } = await import('../../../server/webui/js/pages/setup.js');
  const view = researchProfileStep({
    derived: true,
    confirmed: false,
    products: [{ name: 'Vana', english_name: 'Valve', emphasis: 1 }],
  });

  assert.match(view.textContent, /Confirm research profile/);
  assert.match(view.textContent, /Valve/);
  assert.match(view.textContent, /emphasis/i);
  assert.equal(byText(view, 'button', 'Confirm research profile').disabled, false);
});
