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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
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

function activeResultPair() {
  return [
    ...responseFor('/api/v1/research-campaigns/rc_1/results?view=active'),
    {
      id: 'result_second', organization_id: 'org_3', company_name: 'Beacon AT',
      verdict: 'review', fit_score: 67, evidence_confidence: .71,
      country: 'AT', buyer_role: 'Importer', source_count: 1,
      reasons: ['priority_band_b'], conflicting_claims: [], missing_evidence: ['second_source'],
    },
  ];
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
  assert.equal(root.querySelector('tr[role="button"]'), null);
  const companyControl = root.querySelector('button[aria-label="Inspect evidence for Atlas DE"]');
  assert.ok(companyControl);
  assert.equal(companyControl.getAttribute('type'), 'button');

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

test('only observed and estimated-range claims render as supporting evidence', async () => {
  globalThis.fetch = async url => {
    if (url === '/api/v1/research/results/result_active/claims') {
      return Response.json([
        { id: 'observed', field: 'buyer_role', value: 'Distributor', status: 'observed', confidence: .92, evidence: [] },
        { id: 'estimated', field: 'employee_count', value: '50 to 100', status: 'estimated_range', confidence: .64, evidence: [] },
        { id: 'unknown', field: 'annual_revenue', value: null, status: 'unknown', confidence: 0, evidence: [] },
        { id: 'na', field: 'store_count', value: null, status: 'not_applicable', confidence: 0, evidence: [] },
        { id: 'conflict', field: 'country', value: ['DE', 'AT'], status: 'conflicted', confidence: .55, evidence: [] },
      ]);
    }
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  const supporting = byText(root, 'h3', 'Supporting claims').parentNode.textContent;
  const neutral = byText(root, 'h3', 'Unknown or not applicable').parentNode.textContent;
  const conflicting = byText(root, 'h3', 'Conflicting claims').parentNode.textContent;

  assert.match(supporting, /Buyer role/);
  assert.match(supporting, /Employee count/);
  assert.doesNotMatch(supporting, /Annual revenue|Store count/);
  assert.match(neutral, /Annual revenue/);
  assert.match(neutral, /Store count/);
  assert.match(conflicting, /Country/);
  dispose?.();
});

test('company selection uses a native button inside an unmodified table row', async () => {
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') {
      return Response.json([
        ...responseFor(url),
        {
          id: 'result_second', organization_id: 'org_3', company_name: 'Beacon AT',
          verdict: 'review', fit_score: 67, evidence_confidence: .71,
          country: 'AT', buyer_role: 'Importer', source_count: 1,
          reasons: ['priority_band_b'], conflicting_claims: [], missing_evidence: ['second_source'],
        },
      ]);
    }
    if (url === '/api/v1/research/results/result_second/claims') return Response.json([]);
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  const control = root.querySelector('button[aria-label="Inspect evidence for Beacon AT"]');
  const row = control?.closest('tr');
  assert.ok(control);
  assert.equal(control.getAttribute('type'), 'button');
  assert.equal(row?.hasAttribute('role'), false);
  assert.equal(row?.hasAttribute('tabindex'), false);

  control.click();
  await nextTurn();

  assert.match(root.querySelector('.ifz-results-detail').textContent, /Beacon AT/);
  assert.ok(requests.includes('/api/v1/research/results/result_second/claims'));
  dispose?.();
});

test('a late campaign response cannot replace the visible campaign or its export context', async () => {
  const requests = [];
  const lateA = deferred();
  const campaigns = [
    { id: 'rc_a', name: 'Campaign A', status: 'succeeded', config: { target_countries: ['DE'], sector_ids: ['appliances'], buyer_types: ['distributor'], enabled_source_ids: ['fixture'] } },
    { id: 'rc_b', name: 'Campaign B', status: 'succeeded', config: { target_countries: ['AT'], sector_ids: ['lighting'], buyer_types: ['importer'], enabled_source_ids: ['fixture'] } },
  ];
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url, method: init.method || 'GET' });
    if (url === '/api/v1/research-campaigns') return Response.json(campaigns);
    if (url === '/api/v1/research-campaigns/rc_a/results?view=active') return lateA.promise;
    if (url === '/api/v1/research-campaigns/rc_b/results?view=active') return Response.json([{
      id: 'result_b', organization_id: 'org_b', company_name: 'Visible B', verdict: 'review',
      fit_score: 72, evidence_confidence: .75, country: 'AT', buyer_role: 'Importer', source_count: 1,
      reasons: ['priority_band_b'], conflicting_claims: [], missing_evidence: [],
    }]);
    if (url === '/api/v1/research/results/result_b/claims') return Response.json([]);
    if (url === '/api/v1/research-campaigns/rc_b/export?view=active') {
      return new Response('id,verdict\nresult_b,review\n', {
        status: 200,
        headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="research-rc_b-active.csv"' },
      });
    }
    throw new Error(`unstubbed request: ${init.method || 'GET'} ${url}`);
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const mounting = mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  const picker = root.querySelector('select[aria-label="Active research brief"]');
  picker.value = 'rc_b';
  picker.dispatchEvent({ type: 'change' });
  await nextTurn();
  await nextTurn();

  lateA.resolve(Response.json([{
    id: 'result_a', organization_id: 'org_a', company_name: 'Late A', verdict: 'strong_fit',
    fit_score: 95, evidence_confidence: .95, country: 'DE', buyer_role: 'Distributor', source_count: 2,
    reasons: ['a_band'], conflicting_claims: [], missing_evidence: [],
  }]));
  const dispose = await mounting;
  await nextTurn();

  assert.match(root.textContent, /Visible B/);
  assert.doesNotMatch(root.textContent, /Late A/);
  const exportButton = byText(root, 'button', 'Export active');
  exportButton.click();
  await nextTurn();
  assert.ok(requests.some(request =>
    request.method === 'POST'
      && request.url === '/api/v1/research-campaigns/rc_b/export?view=active'));
  assert.ok(dom.document.body.querySelector('a[download="research-rc_b-active.csv"]'));
  dispose?.();
});

test('a late claim response cannot replace evidence loaded for the visible campaign', async () => {
  const lateAClaim = deferred();
  let claimRequests = 0;
  globalThis.fetch = async url => {
    if (url === '/api/v1/research-campaigns') return Response.json([
      { id: 'rc_a', name: 'Campaign A', status: 'succeeded', config: {} },
      { id: 'rc_b', name: 'Campaign B', status: 'succeeded', config: {} },
    ]);
    if (url === '/api/v1/research-campaigns/rc_a/results?view=active') return Response.json([{
      id: 'shared_result', organization_id: 'org_a', company_name: 'Campaign A company', verdict: 'review',
      fit_score: 60, evidence_confidence: .6, country: 'DE', buyer_role: 'Distributor', source_count: 1,
      reasons: [], conflicting_claims: [], missing_evidence: [],
    }]);
    if (url === '/api/v1/research-campaigns/rc_b/results?view=active') return Response.json([{
      id: 'shared_result', organization_id: 'org_b', company_name: 'Campaign B company', verdict: 'review',
      fit_score: 80, evidence_confidence: .8, country: 'AT', buyer_role: 'Importer', source_count: 1,
      reasons: [], conflicting_claims: [], missing_evidence: [],
    }]);
    if (url === '/api/v1/research/results/shared_result/claims') {
      claimRequests += 1;
      if (claimRequests === 1) return lateAClaim.promise;
      return Response.json([{
        id: 'claim_b', field: 'market_note', value: 'Visible B claim',
        status: 'observed', confidence: .8, evidence: [],
      }]);
    }
    throw new Error(`unstubbed request: GET ${url}`);
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const mounting = mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  await nextTurn();
  const picker = root.querySelector('select[aria-label="Active research brief"]');
  picker.value = 'rc_b';
  picker.dispatchEvent({ type: 'change' });
  await nextTurn();
  await nextTurn();

  lateAClaim.resolve(Response.json([{
    id: 'claim_a', field: 'market_note', value: 'Late A claim',
    status: 'observed', confidence: .6, evidence: [],
  }]));
  const dispose = await mounting;
  await nextTurn();

  const detail = root.querySelector('.ifz-results-detail').textContent;
  assert.match(detail, /Campaign B company/);
  assert.match(detail, /Visible B claim/);
  assert.doesNotMatch(detail, /Late A claim/);
  dispose?.();
});

test('an earlier A claim success cannot overwrite a newer A selection after A to B to A', async () => {
  const firstAClaim = deferred();
  let aClaimRequests = 0;
  globalThis.fetch = async url => {
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') {
      return Response.json(activeResultPair());
    }
    if (url === '/api/v1/research/results/result_active/claims') {
      aClaimRequests += 1;
      if (aClaimRequests === 1) return firstAClaim.promise;
      return Response.json([{
        id: 'fresh_a', field: 'market_note', value: 'Fresh A evidence',
        status: 'observed', confidence: .9, evidence: [],
      }]);
    }
    if (url === '/api/v1/research/results/result_second/claims') return Response.json([]);
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const mounting = mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  await nextTurn();
  root.querySelector('button[aria-label="Inspect evidence for Beacon AT"]').click();
  await nextTurn();
  root.querySelector('button[aria-label="Inspect evidence for Atlas DE"]').click();
  await nextTurn();
  assert.match(root.querySelector('.ifz-results-detail').textContent, /Fresh A evidence/);

  firstAClaim.resolve(Response.json([{
    id: 'late_a', field: 'market_note', value: 'Late A evidence',
    status: 'observed', confidence: .4, evidence: [],
  }]));
  const dispose = await mounting;
  await nextTurn();

  const detail = root.querySelector('.ifz-results-detail').textContent;
  assert.match(detail, /Fresh A evidence/);
  assert.doesNotMatch(detail, /Late A evidence/);
  dispose?.();
});

test('an earlier A claim error cannot replace a newer A selection after A to B to A', async () => {
  const firstAClaim = deferred();
  let aClaimRequests = 0;
  globalThis.fetch = async url => {
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') {
      return Response.json(activeResultPair());
    }
    if (url === '/api/v1/research/results/result_active/claims') {
      aClaimRequests += 1;
      if (aClaimRequests === 1) return firstAClaim.promise;
      return Response.json([{
        id: 'fresh_a', field: 'market_note', value: 'Fresh A evidence',
        status: 'observed', confidence: .9, evidence: [],
      }]);
    }
    if (url === '/api/v1/research/results/result_second/claims') return Response.json([]);
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const mounting = mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  await nextTurn();
  root.querySelector('button[aria-label="Inspect evidence for Beacon AT"]').click();
  await nextTurn();
  root.querySelector('button[aria-label="Inspect evidence for Atlas DE"]').click();
  await nextTurn();
  assert.match(root.querySelector('.ifz-results-detail').textContent, /Fresh A evidence/);

  firstAClaim.reject(new Error('late A failure'));
  const dispose = await mounting;
  await nextTurn();

  const detail = root.querySelector('.ifz-results-detail').textContent;
  assert.match(detail, /Fresh A evidence/);
  assert.doesNotMatch(detail, /Evidence could not be loaded/);
  dispose?.();
});

test('an export completion is silent after the selected result changes', async () => {
  const lateExport = deferred();
  globalThis.fetch = async (url, init = {}) => {
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') {
      return Response.json(activeResultPair());
    }
    if (url === '/api/v1/research/results/result_second/claims') return Response.json([]);
    if (url === '/api/v1/research-campaigns/rc_1/export?view=active' && init.method === 'POST') {
      return lateExport.promise;
    }
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  byText(root, 'button', 'Export active').click();
  root.querySelector('button[aria-label="Inspect evidence for Beacon AT"]').click();
  await nextTurn();
  lateExport.resolve(new Response('id,verdict\nresult_active,strong_fit\n', {
    status: 200,
    headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="stale-active.csv"' },
  }));
  await nextTurn();
  await nextTurn();

  assert.match(root.querySelector('.ifz-results-detail').textContent, /Beacon AT/);
  assert.equal(dom.document.body.querySelector('a[download="stale-active.csv"]'), null);
  assert.doesNotMatch(dom.document.body.querySelector('.ifz-toasts')?.textContent || '', /Downloaded/);
  dispose?.();
});

test('an export error is silent after the selected result changes', async () => {
  const lateExport = deferred();
  globalThis.fetch = async (url, init = {}) => {
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') {
      return Response.json(activeResultPair());
    }
    if (url === '/api/v1/research/results/result_second/claims') return Response.json([]);
    if (url === '/api/v1/research-campaigns/rc_1/export?view=active' && init.method === 'POST') {
      return lateExport.promise;
    }
    return Response.json(responseFor(url));
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  byText(root, 'button', 'Export active').click();
  root.querySelector('button[aria-label="Inspect evidence for Beacon AT"]').click();
  await nextTurn();
  lateExport.reject(new Error('late export failure'));
  await nextTurn();
  await nextTurn();

  assert.match(root.querySelector('.ifz-results-detail').textContent, /Beacon AT/);
  assert.doesNotMatch(
    dom.document.body.querySelector('.ifz-toasts')?.textContent || '',
    /could not be prepared/,
  );
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
    if (url === '/api/v1/research-campaigns/rc_1/export?view=rejected') {
      return new Response('id,verdict\nresult_rejected,reject\n', {
        status: 200,
        headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="research-rc_1-rejected.csv"' },
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
  const rejectedTab = byText(root, 'button', 'Rejected');
  rejectedTab.click();
  await nextTurn();
  await nextTurn();
  const rejectedExport = byText(root, 'button', 'Export rejected');
  rejectedExport.click();
  await nextTurn();
  await nextTurn();

  assert.ok(requests.some(request =>
    request.method === 'POST'
      && request.url === '/api/v1/research-campaigns/rc_1/export?view=rejected'));
  const downloads = dom.document.body.querySelectorAll('a[download]')
    .map(link => link.getAttribute('download'));
  assert.ok(downloads.includes('research-rc_1-active.csv'));
  assert.ok(downloads.includes('research-rc_1-rejected.csv'));
  dispose?.();
});

test('a running campaign brings in new results without a reload', async (t) => {
  // The results endpoint never gated on completion, so a campaign's leads were
  // already arriving one at a time — the page just told the customer to reload
  // to see them. A five-hundred-company run showed nothing for its whole
  // duration unless somebody kept pressing refresh.
  t.mock.timers.enable({ apis: ['setInterval'] });
  let leads = [{
    id: 'result_first', organization_id: 'org_1', company_name: 'Atlas DE',
    verdict: 'strong_fit', fit_score: 91, evidence_confidence: .88,
    country: 'DE', buyer_role: 'Distributor', source_count: 2,
    reasons: ['a_band'], conflicting_claims: [], missing_evidence: [],
  }];
  globalThis.fetch = async url => {
    if (url === '/api/v1/research-campaigns') return Response.json([{
      id: 'rc_1', name: 'DACH appliance distributors', status: 'running',
      config: { target_countries: ['DE'], sector_ids: ['household-appliances'] },
      updated_at: 1786900000,
    }]);
    if (url === '/api/v1/research-campaigns/rc_1/results?view=active') return Response.json(leads);
    if (url.includes('/claims')) return Response.json([]);
    throw new Error(`unstubbed request: ${url}`);
  };
  const { mount } = await import('../../../server/webui/js/pages/research-results.js');
  const root = dom.document.createElement('main');
  dom.document.body.append(root);

  const dispose = await mount(root, { query: {}, params: {}, navigate() {} });
  await nextTurn();
  assert.match(root.textContent, /Atlas DE/);
  assert.doesNotMatch(root.textContent, /Beacon AT/);

  leads = [...leads, {
    id: 'result_second', organization_id: 'org_3', company_name: 'Beacon AT',
    verdict: 'review', fit_score: 67, evidence_confidence: .71,
    country: 'AT', buyer_role: 'Importer', source_count: 1,
    reasons: ['priority_band_b'], conflicting_claims: [], missing_evidence: [],
  }];
  t.mock.timers.tick(5000);
  await nextTurn();
  await nextTurn();

  assert.match(root.textContent, /Beacon AT/);
  // The company the customer had open stays open; a new arrival must not steal
  // the evidence panel out from under them.
  assert.match(root.textContent, /Atlas DE/);
  dispose?.();
});

test('a finished campaign is not polled, and disposing stops the polling', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
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
  const afterMount = requests.length;

  // rc_1 is 'succeeded' in the shared stub, so there is nothing to wait for.
  t.mock.timers.tick(20000);
  await nextTurn();
  assert.equal(requests.length, afterMount);

  dispose?.();
  t.mock.timers.tick(20000);
  await nextTurn();
  assert.equal(requests.length, afterMount);
});
