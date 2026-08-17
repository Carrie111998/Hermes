import assert from 'node:assert/strict';
import test, { afterEach, beforeEach } from 'node:test';

import { db, resetReal } from '../../../server/webui/js/state.js';
import { call, config } from '../../../server/webui/js/api.js';
import { installDom, resetDom, byText } from './dom-shim.mjs';

const dom = installDom();

beforeEach(() => {
  resetReal();
  config.authHeader = null;
});

afterEach(() => {
  delete globalThis.fetch;
  resetDom(dom);
});

test('product import sends the selected file as multipart and refreshes the shared catalog', async () => {
  let request;
  globalThis.fetch = async (url, init) => {
    request = { url, ...init };
    return Response.json({
      imported: 1,
      products: [{ id: 'prd_1', product_name: 'Built-in oven', category: 'Ovens' }],
    }, { status: 201 });
  };
  const file = new Blob(['product_name,category\nBuilt-in oven,Ovens\n'], { type: 'text/csv' });
  const body = new FormData();
  body.append('file', file, 'catalog.csv');

  const result = await call('products.import', { body });

  assert.equal(request.method, 'POST');
  assert.equal(request.url, '/api/v1/products/import');
  assert.equal(request.body, body);
  assert.equal(request.headers['Content-Type'], undefined);
  assert.equal(result.imported, 1);
  assert.deepEqual(db.products, result.products);
});

test('catalog import shows row errors without clearing the selected file', async () => {
  globalThis.history = { replaceState() {} };
  globalThis.location = { pathname: '/', search: '' };
  globalThis.requestAnimationFrame = callback => callback();
  globalThis.fetch = async (url, init = {}) => {
    if (url === '/health') return Response.json({});
    if (url === '/api/v1/products/import') {
      assert.ok(init.body instanceof FormData);
      return Response.json({ detail: { errors: [{ row_number: 3, field: 'product_name', message: 'Field required' }] } }, { status: 422 });
    }
    if ([
      '/api/v1/products', '/api/v1/documents', '/api/v1/contacts', '/api/v1/leads',
      '/api/v1/company-brain/snapshots', '/api/v1/integrations/email', '/api/v1/integrations/whatsapp',
      '/api/v1/cc-rules', '/api/v1/lead-map/selected-countries', '/api/v1/lead-map/countries',
    ].includes(url)) return Response.json([]);
    if ([
      '/api/v1/company/profile', '/api/v1/company/positioning', '/api/v1/company/sales-preferences',
      '/api/v1/company/email-templates', '/api/v1/company-brain',
    ].includes(url)) return Response.json({});
    if (url === '/api/v1/onboarding/status') return Response.json({ status: 'in_progress', completed_steps: [] });
    throw new Error(`unstubbed request: ${init.method || 'GET'} ${url}`);
  };
  const { mount } = await import('../../../server/webui/js/pages/setup.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);
  const dispose = await mount(root, { query: { section: 'products' }, params: {}, navigate() {} });
  const picker = root.querySelector('input[type="file"]');
  const selected = new Blob(['product_name\n\n'], { type: 'text/csv' });
  picker.files = [selected];
  picker.dispatchEvent({ type: 'change' });

  const upload = byText(root, 'button', 'Import catalog');
  upload.click();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(picker.files[0], selected);
  assert.match(root.textContent, /Row 3.*product name.*Field required/i);
  dispose();
});
