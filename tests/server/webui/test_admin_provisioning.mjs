/* Browser-level regression tests for the admin provisioning surfaces.

   These drive the real page modules (pages/admin.js -> api.js -> adapters.js ->
   real-state.js) against a stubbed backend, so they fail if the request bodies
   drift away from what server/routes/admin.py + server/schemas.py accept:

     - CompanyCreate/CompanyPatch and UserCreate/UserPatch are extra="forbid",
       so `website`, `plan` and `name` must travel inside `data`.
     - UserCreate only knows the roles "admin" and "customer", and a user
       created without a password has no usable credential.
     - POST /admin/users/{id}/reset-password requires `password` and answers
       204 — there is no response body to read.

   They also cover the two UI-side failures that made those bugs invisible:
   admin responses not reaching the shared store (every user showed "Platform")
   and save buttons left spinning after a rejected request. */

import assert from 'node:assert/strict';
import test, { afterEach, beforeEach } from 'node:test';

import { installDom, resetDom, byText } from './dom-shim.mjs';

const dom = installDom();

const { db, resetReal } = await import('../../../server/webui/js/state.js');
const { config } = await import('../../../server/webui/js/api.js');
const admin = await import('../../../server/webui/js/pages/admin.js');

const COMPANY = {
  id: 'cmp_1',
  name: 'Acme Industries',
  legal_name: 'Acme Industries A.Ş.',
  status: 'active',
  users: 1,
  last_seen_at: '2026-08-01T10:00:00Z',
  data: { website: 'https://acme.test', plan: 'pilot' },
  created_at: '2026-07-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

const USER = {
  id: 'usr_1',
  email: 'ada@acme.test',
  role: 'customer',
  company_id: 'cmp_1',
  status: 'active',
  data: { name: 'Ada Lovelace' },
  created_at: '2026-07-01T10:00:00Z',
  updated_at: '2026-08-01T10:00:00Z',
};

/* ---------------- stub backend ---------------- */

function startBackend() {
  const requests = [];
  const handlers = new Map();
  const backend = {
    requests,
    on(method, path, handler) {
      handlers.set(`${method} ${path}`, handler);
      return backend;
    },
    /** Requests recorded for one route, in order. */
    to(method, path) {
      return requests.filter(r => r.method === method && r.path === path);
    },
    last() { return requests[requests.length - 1]; },
  };
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    const path = String(url).split('?')[0];
    const record = {
      method,
      path,
      url: String(url),
      body: init.body == null ? null : JSON.parse(init.body),
    };
    requests.push(record);
    const handler = handlers.get(`${method} ${path}`);
    assert.ok(handler, `unstubbed request: ${method} ${path}`);
    const result = handler(record) || {};
    const status = result.status ?? 200;
    return {
      ok: status < 400,
      status,
      headers: { get: () => null },
      async json() {
        if (result.payload === undefined) throw new Error('no body');
        return result.payload;
      },
    };
  };
  return backend;
}

/** A backend where both admin lists answer with the fixtures above. */
function backendWithFixtures() {
  return startBackend()
    .on('GET', '/api/v1/admin/companies', () => ({ payload: [COMPANY] }))
    .on('GET', '/api/v1/admin/users', () => ({ payload: [USER] }));
}

/* ---------------- page harness ---------------- */

function makeCtx() {
  const navigations = [];
  return {
    navigations,
    params: {},
    query: {},
    navigate: path => navigations.push(path),
  };
}

/** Let queued promise jobs (fetch -> json -> adapters -> render) run out. */
async function settle(rounds = 6) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
}

function mountPoint() {
  const root = dom.document.createElement('div');
  dom.document.body.append(root);
  return root;
}

function overlay() {
  return dom.document.querySelector('.ifz-overlay');
}

/** The control belonging to the field() labelled `label` (ignores the * mark). */
function control(scope, label) {
  const node = scope.querySelectorAll('label')
    .find(l => l.textContent.replace('*', '').trim() === label);
  assert.ok(node, `no field labelled "${label}"`);
  return scope.querySelector(`#${node.getAttribute('for')}`);
}

function fieldError(scope, label) {
  const node = scope.querySelectorAll('label')
    .find(l => l.textContent.replace('*', '').trim() === label);
  return node?.parentNode?.querySelector('.ifz-field-error')?.textContent || null;
}

function toastText(kind) {
  return dom.document.querySelectorAll('.ifz-toast')
    .filter(node => !kind || node.classList.contains(kind))
    .map(node => node.textContent)
    .join(' | ');
}

async function click(node) {
  assert.ok(node, 'element to click is missing');
  node.click();
  await settle();
}

beforeEach(() => {
  resetDom(dom);
  resetReal();
  config.authHeader = null;
});

afterEach(() => {
  delete globalThis.fetch;
});

/* ---------------- create company ---------------- */

test('create company puts website and plan in data, never at the top level', async () => {
  const backend = backendWithFixtures()
    .on('POST', '/api/v1/admin/companies', () => ({
      status: 201,
      payload: { ...COMPANY, id: 'cmp_2', name: 'Northwind', data: { website: 'https://northwind.test', plan: 'enterprise' } },
    }));
  const ctx = makeCtx();
  const root = mountPoint();

  await admin.mountCompanies(root, ctx);
  await click(byText(root, 'button', 'Create company'));

  const dialog = overlay();
  assert.ok(dialog, 'company modal did not open');
  control(dialog, 'Name').value = '  Northwind  ';
  control(dialog, 'Website').value = 'https://northwind.test';
  control(dialog, 'Plan').value = 'enterprise';

  await click(byText(dialog, 'button', 'Create company'));

  const [created] = backend.to('POST', '/api/v1/admin/companies');
  assert.deepEqual(created.body, {
    name: 'Northwind',
    legal_name: 'Northwind',
    data: { website: 'https://northwind.test', plan: 'enterprise' },
  });
  // Regression: these two keys are a 422 from CompanyCreate(extra="forbid").
  assert.equal('website' in created.body, false);
  assert.equal('plan' in created.body, false);

  assert.equal(overlay(), null, 'modal stayed open after a successful save');
  assert.match(toastText('success'), /Company saved/);
  assert.deepEqual(ctx.navigations.slice(-1), ['/admin/companies']);
});

test('editing a company sends no status field and keeps the profile in data', async () => {
  const backend = backendWithFixtures()
    .on('GET', '/api/v1/admin/companies/cmp_1', () => ({ payload: COMPANY }))
    .on('PATCH', '/api/v1/admin/companies/cmp_1', () => ({ payload: COMPANY }));
  const ctx = makeCtx();
  ctx.params = { companyId: 'cmp_1' };
  const root = mountPoint();

  await admin.mountCompanyDetail(root, ctx);
  await click(byText(root, 'button', 'Edit'));

  const dialog = overlay();
  // The modal is seeded from the flattened data envelope.
  assert.equal(control(dialog, 'Website').value, 'https://acme.test');
  assert.equal(control(dialog, 'Plan').value, 'pilot');

  control(dialog, 'Plan').value = 'enterprise';
  await click(byText(dialog, 'button', 'Save company'));

  const [patched] = backend.to('PATCH', '/api/v1/admin/companies/cmp_1');
  assert.deepEqual(patched.body, {
    name: 'Acme Industries',
    legal_name: 'Acme Industries A.Ş.',
    data: { website: 'https://acme.test', plan: 'enterprise' },
  });
  // CompanyPatch has no `status`; lifecycle changes use the activate/disable routes.
  assert.equal('status' in patched.body, false);
});

test('a rejected company save restores the button instead of stranding "Saving…"', async () => {
  backendWithFixtures().on('POST', '/api/v1/admin/companies', () => ({
    status: 422,
    payload: { detail: [{ msg: 'Extra inputs are not permitted' }] },
  }));
  const root = mountPoint();

  await admin.mountCompanies(root, makeCtx());
  await click(byText(root, 'button', 'Create company'));

  const dialog = overlay();
  control(dialog, 'Name').value = 'Northwind';
  const save = byText(dialog, 'button', 'Create company');
  await click(save);

  assert.ok(overlay(), 'modal closed on a failed save');
  assert.equal(save.disabled, false);
  assert.equal(save.textContent, 'Create company');
  assert.match(toastText('error'), /Extra inputs are not permitted/);
});

test('a company save with no name never reaches the backend', async () => {
  const backend = backendWithFixtures();
  const root = mountPoint();

  await admin.mountCompanies(root, makeCtx());
  await click(byText(root, 'button', 'Create company'));

  const dialog = overlay();
  await click(byText(dialog, 'button', 'Create company'));

  assert.deepEqual(backend.to('POST', '/api/v1/admin/companies'), []);
  assert.equal(fieldError(dialog, 'Name'), 'Enter a company name');
});

/* ---------------- create user ---------------- */

test('the users table resolves each company from the synced admin store', async () => {
  backendWithFixtures();
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());

  // Regression: admin.companies.list was never mirrored into db.admin, so this
  // column fell back to "Platform" for every customer user.
  assert.deepEqual(db.admin.companies.map(c => c.id), ['cmp_1']);
  const cells = root.querySelectorAll('td').map(td => td.textContent);
  assert.ok(cells.includes('Acme Industries'), `company column was ${JSON.stringify(cells)}`);
  assert.equal(cells.includes('Platform'), false);
});

test('the user role select offers only the roles the backend accepts', async () => {
  backendWithFixtures();
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Create user'));

  const dialog = overlay();
  const roles = control(dialog, 'Role').querySelectorAll('option');
  assert.deepEqual(roles.map(o => o.getAttribute('value')), ['customer', 'admin']);
  assert.deepEqual(roles.map(o => o.textContent), ['Customer', 'Administrator']);
});

test('create user sends a usable password and keeps the display name in data', async () => {
  const backend = backendWithFixtures().on('POST', '/api/v1/admin/users', () => ({
    status: 201,
    payload: { ...USER, id: 'usr_2', email: 'grace@acme.test', data: { name: 'Grace Hopper' } },
  }));
  const ctx = makeCtx();
  const root = mountPoint();

  await admin.mountUsers(root, ctx);
  await click(byText(root, 'button', 'Create user'));

  const dialog = overlay();
  control(dialog, 'Name').value = 'Grace Hopper';
  control(dialog, 'Email').value = 'grace@acme.test';
  control(dialog, 'Company').value = 'cmp_1';
  control(dialog, 'Temporary password').value = 'correct-horse-battery';

  await click(byText(dialog, 'button', 'Create user'));

  const [created] = backend.to('POST', '/api/v1/admin/users');
  assert.deepEqual(created.body, {
    email: 'grace@acme.test',
    role: 'customer',
    company_id: 'cmp_1',
    password: 'correct-horse-battery',
    data: { name: 'Grace Hopper' },
  });
  // `name` is not a users column and UserCreate forbids unknown keys.
  assert.equal('name' in created.body, false);
  assert.equal(overlay(), null);
  assert.match(toastText('success'), /User saved/);
});

test('create user refuses to submit without a password long enough to be usable', async () => {
  const backend = backendWithFixtures();
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Create user'));

  const dialog = overlay();
  control(dialog, 'Email').value = 'grace@acme.test';
  control(dialog, 'Company').value = 'cmp_1';
  control(dialog, 'Temporary password').value = 'short';
  await click(byText(dialog, 'button', 'Create user'));

  assert.deepEqual(backend.to('POST', '/api/v1/admin/users'), []);
  assert.equal(fieldError(dialog, 'Temporary password'), 'Use at least 10 characters');
});

test('a customer user without a company is caught before the request', async () => {
  const backend = backendWithFixtures();
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Create user'));

  const dialog = overlay();
  control(dialog, 'Email').value = 'grace@acme.test';
  control(dialog, 'Company').value = '';
  control(dialog, 'Temporary password').value = 'correct-horse-battery';
  await click(byText(dialog, 'button', 'Create user'));

  assert.deepEqual(backend.to('POST', '/api/v1/admin/users'), []);
  assert.equal(fieldError(dialog, 'Company'), 'Customer users need a company');
});

test('editing a user sends no password and keeps the button usable after a failure', async () => {
  const backend = backendWithFixtures().on('PATCH', '/api/v1/admin/users/usr_1', () => ({
    status: 409,
    payload: { detail: 'Email already exists' },
  }));
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Edit'));

  const dialog = overlay();
  assert.equal(control(dialog, 'Email').value, 'ada@acme.test');
  assert.equal(control(dialog, 'Name').value, 'Ada Lovelace');
  control(dialog, 'Email').value = 'taken@acme.test';

  const save = byText(dialog, 'button', 'Save user');
  await click(save);

  const [patched] = backend.to('PATCH', '/api/v1/admin/users/usr_1');
  assert.deepEqual(patched.body, {
    email: 'taken@acme.test',
    role: 'customer',
    company_id: 'cmp_1',
    data: { name: 'Ada Lovelace' },
  });
  // UserPatch forbids `password`; resets go through the reset-password route.
  assert.equal('password' in patched.body, false);
  assert.equal(save.disabled, false);
  assert.equal(save.textContent, 'Save user');
  assert.ok(overlay(), 'modal closed on a failed save');
});

/* ---------------- password reset ---------------- */

test('password reset collects a password and survives the 204 answer', async () => {
  const backend = backendWithFixtures()
    .on('POST', '/api/v1/admin/users/usr_1/reset-password', () => ({ status: 204 }));
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Reset'));

  const dialog = overlay();
  assert.ok(dialog, 'reset modal did not open');
  control(dialog, 'New password').value = 'correct-horse-battery';
  await click(byText(dialog, 'button', 'Set password'));

  const [reset] = backend.to('POST', '/api/v1/admin/users/usr_1/reset-password');
  // Regression: the request used to carry no body at all (ResetPassword requires
  // one) and the page then read `.message` off the 204's null response.
  assert.deepEqual(reset.body, { password: 'correct-horse-battery' });
  assert.equal(overlay(), null);
  assert.match(toastText('success'), /Password updated for ada@acme\.test/);
  assert.equal(toastText('error'), '');
});

test('password reset validates length before calling the backend', async () => {
  const backend = backendWithFixtures();
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Reset'));

  const dialog = overlay();
  control(dialog, 'New password').value = 'short';
  await click(byText(dialog, 'button', 'Set password'));

  assert.deepEqual(backend.to('POST', '/api/v1/admin/users/usr_1/reset-password'), []);
  assert.equal(fieldError(dialog, 'New password'), 'Use at least 10 characters');
  assert.ok(overlay());
});

test('a rejected password reset leaves the dialog usable', async () => {
  backendWithFixtures().on('POST', '/api/v1/admin/users/usr_1/reset-password', () => ({
    status: 409,
    payload: { detail: 'User is not bound to Supabase Auth' },
  }));
  const root = mountPoint();

  await admin.mountUsers(root, makeCtx());
  await click(byText(root, 'button', 'Reset'));

  const dialog = overlay();
  control(dialog, 'New password').value = 'correct-horse-battery';
  const save = byText(dialog, 'button', 'Set password');
  await click(save);

  assert.ok(overlay(), 'modal closed on a failed reset');
  assert.equal(save.disabled, false);
  assert.equal(save.textContent, 'Set password');
  assert.match(toastText('error'), /User is not bound to Supabase Auth/);
});
