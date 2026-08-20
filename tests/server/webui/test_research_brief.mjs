import assert from 'node:assert/strict';
import test, { afterEach, beforeEach } from 'node:test';

import { config } from '../../../server/webui/js/api.js';
import { resetReal } from '../../../server/webui/js/state.js';
import { installDom, resetDom, byText } from './dom-shim.mjs';

const dom = installDom();

// api.call feeds every response into the shared store, and the leadMap branch
// writes into db.leadMap. Without a store the page's own request throws.
beforeEach(() => { config.authHeader = null; resetReal(); });
afterEach(() => { delete globalThis.fetch; resetDom(dom); });

const nextTurn = () => new Promise(resolve => setImmediate(resolve));

const SECTORS = [{
  sector_id: 'household-appliances',
  name: 'Household appliances',
  buyer_types: ['importer', 'distributor', 'retailer', 'brand', 'wholesaler'],
}];

function baseResponses(overrides = {}) {
  return {
    '/api/v1/data-sources/catalog': [
      { source_id: 'ted', display_name: 'Tenders Electronic Daily', available: true },
      { source_id: 'brightdata-web', display_name: 'Bright Data Web Unlocker', available: true },
      { source_id: 'customer-list-corpus', display_name: 'Customer list corpus', available: false },
    ],
    '/api/v1/research/sectors': SECTORS,
    '/api/v1/research/configuration': { default_seller_countries: ['TR'] },
    // The endpoint answers with bare alpha-2 codes.
    '/api/v1/lead-map/selected-countries': ['DE', 'NL', 'RO'],
    ...overrides,
  };
}

function stubFetch(responses, posts = []) {
  globalThis.fetch = async (url, init = {}) => {
    if (init.method && init.method !== 'GET') {
      posts.push({ url, body: init.body ? JSON.parse(init.body) : null });
      return { ok: true, status: 201, headers: { get: () => 'application/json' },
               json: async () => ({ id: 'rc_new' }) };
    }
    const body = responses[url];
    return { ok: body !== undefined, status: body === undefined ? 404 : 200,
             headers: { get: () => 'application/json' },
             json: async () => (body === undefined ? {} : body) };
  };
}

async function mountBrief(responses, ctx = {}) {
  stubFetch(responses, ctx.posts);
  const { mount } = await import('../../../server/webui/js/pages/research-brief.js');
  const root = document.createElement('div');
  await mount(root, { navigate: path => ctx.navigated?.push(path), ...ctx });
  await nextTurn();
  return root;
}

test('the brief runs against sources the tenant already has, and says which', async () => {
  const root = await mountBrief(baseResponses());
  const text = root.textContent;
  assert.match(text, /Tenders Electronic Daily/);
  assert.match(text, /Bright Data Web Unlocker/);
  // Unavailable sources are not offered as if they would contribute.
  assert.doesNotMatch(text, /Customer list corpus/);
});

// Source access is admin-owned. A customer choosing providers would be
// choosing something they cannot install, enable, or pay for.
test('the customer is never asked to pick sources', async () => {
  const root = await mountBrief(baseResponses());
  assert.equal(byText(root, 'label, .ifz-label', 'Sources'), null);
  assert.doesNotMatch(root.textContent, /Install|Enable|Uninstall/);
});

test('running submits the weights, the markets and the sector buyer roles', async () => {
  const posts = [];
  const navigated = [];
  const root = await mountBrief(baseResponses(), { posts, navigated });

  const run = byText(root, 'button', 'Run lead search');
  assert.ok(run, 'the run action must exist');
  run.click();
  await nextTurn();
  await nextTurn();

  const create = posts.find(entry => entry.url.endsWith('/research-campaigns'));
  assert.ok(create, 'a campaign must be created');
  const cfg = create.body.config;
  assert.deepEqual(cfg.target_countries, ['DE', 'NL', 'RO']);
  assert.deepEqual(cfg.sector_ids, ['household-appliances']);
  assert.deepEqual(cfg.enabled_source_ids, ['ted', 'brightdata-web']);
  // Buyer roles come from the sector, not a free-text box: eligibility
  // intersects them with evidence, so an unpublished term rejects everyone.
  assert.deepEqual(cfg.buyer_types, SECTORS[0].buyer_types);
  assert.equal(Object.values(cfg.scoring.weights).reduce((a, b) => a + b, 0), 100);
  assert.equal(cfg.enrichment.research_each_lead, true);

  assert.ok(posts.some(entry => entry.url.endsWith('/rc_new/start')), 'the campaign must start');
  assert.deepEqual(navigated, ['/app/research']);
});

test('a tenant with no connected source cannot start a search that would find nothing', async () => {
  const root = await mountBrief(baseResponses({
    '/api/v1/data-sources/catalog': [
      { source_id: 'ted', display_name: 'Tenders Electronic Daily', available: false },
    ],
  }));
  const run = byText(root, 'button', 'Run lead search');
  assert.equal(run.disabled, true);
  assert.match(root.textContent, /administrator has to enable one/);
});

test('no markets means Setup, not an empty search', async () => {
  const posts = [];
  const root = await mountBrief(baseResponses({ '/api/v1/lead-map/selected-countries': [] }), { posts });
  assert.match(root.textContent, /No markets selected yet/);
  byText(root, 'button', 'Run lead search').click();
  await nextTurn();
  assert.equal(posts.length, 0, 'nothing may be created without a market');
});
