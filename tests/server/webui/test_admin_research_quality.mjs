import assert from 'node:assert/strict';
import test, { afterEach } from 'node:test';

import { config } from '../../../server/webui/js/api.js';
import { installDom, resetDom } from './dom-shim.mjs';

const dom = installDom();

afterEach(() => {
  delete globalThis.fetch;
  config.authHeader = null;
  resetDom(dom);
});

const FIXTURE = {
  generated_at: 1787550000,
  warnings: [
    { code: 'thin_profile', company_id: 'cmp_a', message: 'Profile has no confirmed product range.' },
    { code: 'high_fact_reuse', fact_id: 'sf_1', message: 'A shared fact affects 8 customers.' },
    { code: 'source_change', source_id: 'bright-data', message: 'Source health changed to degraded.' },
  ],
  candidates: { supplied: 120, collapsed_rows: 9 },
  exclusions: { excluded_by_range: 17, cheap_verification_no_scope_signal: 13, ineligible: 4, rejected: 6 },
  facts: { shared_facts: 25, reused_facts: 11, max_consumers: 8 },
  profiles: { versions: 7, confirmed: 4, thin: 1 },
  labels: { history: 12, active: 5 },
  corrections: { previews: 2, applied: 1 },
  costs: { requests: 160, retries: 5, fresh_cache_hits: 42, negative_cache_hits: 8, failures: 3, tokens: 22000, cost: 18.75 },
  agentic: { companies: 14, pages: 38, elapsed_seconds: 84, budget_stops: 2 },
  contacts: { derived: 6 },
  operations: { cancellations: 1, provider_errors: 3 },
  sources: [{ source_id: 'bright-data', requests: 140, failures: 3, cache_hits: 36, status: 'degraded' }],
  outcomes: { by_band: [{ band: 'A', leads: 20, reply_rate: .25 }], by_label: [] },
};

test('admin quality view shows exclusions, reuse, source changes, and cost', async () => {
  const { renderResearchQuality } = await import('../../../server/webui/js/pages/admin.js');
  const node = renderResearchQuality(FIXTURE);
  dom.document.body.append(node);

  for (const label of [
    'Excluded candidates', 'Collapsed rows', 'Fact reuse', 'Source changes',
    'Requests', 'Tokens', 'Cost', 'Negative-cache hits', 'Budget stops',
  ]) assert.match(node.textContent, new RegExp(label, 'i'));
  assert.match(node.textContent, /Profile has no confirmed product range/);
  assert.match(node.textContent, /bright data/i);
  assert.equal(node.querySelectorAll('[role="alert"]').length, 3);
});

test('admin quality view has Turkish fixed labels while warning evidence stays intact', async () => {
  const { renderResearchQuality } = await import('../../../server/webui/js/pages/admin.js');
  const node = renderResearchQuality(FIXTURE, 'tr');

  assert.match(node.textContent, /Hariç tutulan adaylar/);
  assert.match(node.textContent, /İstekler/);
  assert.match(node.textContent, /Maliyet/);
  assert.match(node.textContent, /Profile has no confirmed product range/);
});

test('quality page loads through the admin-only API route', async () => {
  const requests = [];
  config.authHeader = 'Bearer admin';
  globalThis.fetch = async url => {
    requests.push(url);
    if (url === '/api/v1/admin/research/quality') return Response.json(FIXTURE);
    throw new Error(`unstubbed request: ${url}`);
  };
  const { mountResearchQuality } = await import('../../../server/webui/js/pages/admin.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  await mountResearchQuality(root, { navigate() {} });

  assert.deepEqual(requests, ['/api/v1/admin/research/quality']);
  assert.match(root.textContent, /Research quality/);
  assert.match(root.textContent, /Excluded candidates/);
});
