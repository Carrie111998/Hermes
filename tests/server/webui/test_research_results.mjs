import assert from 'node:assert/strict';
import test, { afterEach, beforeEach } from 'node:test';

import { config } from '../../../server/webui/js/api.js';
import { installDom, resetDom, byText } from './dom-shim.mjs';

const dom = installDom();

beforeEach(() => {
  config.authHeader = null;
  globalThis.URL.createObjectURL = () => 'blob:research-export';
  globalThis.URL.revokeObjectURL = () => {};
});

afterEach(() => {
  delete globalThis.fetch;
  resetDom(dom);
});

function nextTurn() {
  return new Promise(resolve => setImmediate(resolve));
}

function responseFor(url) {
  if (url === '/api/v1/research-campaigns') return [{
    id: 'rc_1', name: 'DACH appliance distributors', status: 'succeeded',
    config: {
      target_countries: ['DE', 'AT'], sector_ids: ['household-appliances'],
      buyer_types: ['distributor'], enabled_source_ids: ['fixture-directory'],
    },
    updated_at: 1786900000,
  }];
  if (url === '/api/v1/research-campaigns/rc_1/results?view=active') return [{
    id: 'result_active', organization_id: 'org_1', company_name: 'Atlas DE',
    verdict: 'strong_fit', fit_score: 91, evidence_confidence: 0.88,
    country: 'DE', buyer_role: 'Distributor', source_count: 2,
    reasons: ['a_band_with_official_and_independent_evidence'],
    conflicting_claims: [], missing_evidence: [],
  }];
  if (url === '/api/v1/research-campaigns/rc_1/results?view=rejected') return [{
    id: 'result_rejected', organization_id: 'org_2', company_name: 'Northstar DE',
    verdict: 'reject', fit_score: 28, evidence_confidence: 0.61,
    country: 'DE', buyer_role: 'Retailer', source_count: 1,
    reasons: ['buyer_role'], conflicting_claims: [], missing_evidence: ['second_source'],
  }];
  if (url === '/api/v1/research/results/result_active/claims') return [{
    id: 'claim_1', field: 'buyer_role', value: 'Distributor', status: 'observed',
    confidence: 0.92, evidence: [{
      provenance_url: 'https://atlas.example.test', source_id: 'fixture-directory',
      retrieved_at: 1786900000, snapshot_id: 'snap_1', raw_hash: 'a'.repeat(64),
    }],
  }];
  if (url === '/api/v1/research/results/result_rejected/claims') return [];
  throw new Error(`unstubbed request: ${url}`);
}

test('Rejected results are fetched only after the Rejected tab is selected', async () => {
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();

  assert.ok(requests.includes('/api/v1/research-campaigns/rc_1/results?view=active'));
  assert.equal(requests.some(url => url.includes('view=rejected')), false);
  assert.match(root.textContent, /Atlas DE/);
  assert.match(root.textContent, /Why this verdict/);
  assert.match(root.textContent, /https:\/\/atlas\.example\.test|Open source/);

  const rejectedTab = byText(root, 'button', 'Rejected');
  assert.ok(rejectedTab);
  rejectedTab.click();
  await nextTurn();
  await nextTurn();

  assert.equal(
    requests.filter(url => url === '/api/v1/research-campaigns/rc_1/results?view=rejected').length,
    1,
  );
  assert.match(root.textContent, /Northstar DE/);
  assert.equal(root.querySelector('[role="tab"][aria-selected="true"]').textContent, 'Rejected');
  dispose?.();
});

test('each tab exports its own server-filtered result view', async () => {
  const requests = [];
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url, method: init.method || 'GET' });
    if (url === '/api/v1/research-campaigns/rc_1/export?view=active') {
      return new Response('id,verdict\nresult_active,strong_fit\n', {
        status: 200,
        headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="research-rc_1-active.csv"' },
      });
    }
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  const exportButton = byText(root, 'button', 'Export active');
  assert.ok(exportButton);
  exportButton.click();
  await nextTurn();
  await nextTurn();

  assert.ok(requests.some(request =>
    request.method === 'POST'
      && request.url === '/api/v1/research-campaigns/rc_1/export?view=active'));
  dispose?.();
});
