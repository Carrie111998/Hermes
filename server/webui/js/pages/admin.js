/* Administrator surfaces backed by the real tenant-aware API. */

import {
  el, card, button, pageHead, statCard, dataTable, badge, emptyState, toast,
  input, select, field, modal, setBusy, kv, fmt, hbarList,
} from '../ui.js';
import { call } from '../api.js';
import { db, emit } from '../mocks/db.js';
import { updateSession } from '../session.js';
import { countryName, recordTitle, providerLabel } from './_page-utils.js';

const ADMIN_TABS = [
  ['/admin/dashboard', 'Dashboard'],
  ['/admin/companies', 'Companies'],
  ['/admin/users', 'Users'],
  ['/admin/agent-runs', 'Runs'],
  ['/admin/analytics', 'Analytics'],
  ['/admin/integrations', 'Integrations'],
  ['/admin/errors', 'Errors'],
  ['/admin/logs', 'Logs'],
  ['/admin/data-sources', 'Data sources'],
];

function adminNav(ctx, activePath) {
  return el('div', { class: 'ifz-admin-tabs ifz-mb-4' },
    ADMIN_TABS.map(([path, label]) => el('button', {
      class: `ifz-filter-chip${path === activePath ? ' on' : ''}`,
      onclick: () => ctx.navigate(path),
    }, label)));
}

function withAdmin(root, ctx, title, sub, activePath, body, actions = []) {
  root.replaceChildren(
    pageHead({
      title,
      sub,
      actions: [button('App', { icon: 'dashboard', onClick: () => ctx.navigate('/app/today') }), ...actions],
    }),
    adminNav(ctx, activePath),
    body);
}

export async function mountDashboard(root, ctx) {
  const [companies, users, errors, logs, runs] = await Promise.all([
    call('admin.companies.list'),
    call('admin.users.list'),
    call('admin.errors'),
    call('admin.logs', { query: { limit: 8 } }),
    call('agentRuns.list').catch(() => ({ items: [], total: 0 })),
  ]);
  withAdmin(root, ctx, 'Admin Dashboard', 'Customer workspace operations for interfaze-agent.', '/admin/dashboard',
    el('div', {},
      el('div', { class: 'ifz-grid cols-4 ifz-mb-4' },
        statCard({ label: 'Companies', value: String(companies.total), delta: `${companies.items.filter(c => c.status === 'active').length} active`, deltaDir: 'up' }),
        statCard({ label: 'Users', value: String(users.total), delta: `${users.items.filter(u => u.status === 'active').length} active`, deltaDir: 'flat' }),
        statCard({ label: 'Agent runs', value: String(runs.total), delta: `${runs.items.filter(r => r.status === 'running').length} running`, deltaDir: 'flat' }),
        statCard({ label: 'Warnings', value: String(errors.total), delta: 'run health', deltaDir: errors.total ? 'flat' : 'up' })),
      el('div', { class: 'ifz-grid cols-2' },
        card({
          title: 'Recent customers',
          flush: true,
          body: companiesTable(companies.items, ctx, false),
        }),
        card({
          title: 'Recent admin log',
          flush: true,
          body: logsTable(logs.items),
        }))));
}

export async function mountCompanies(root, ctx) {
  const res = await call('admin.companies.list');
  withAdmin(root, ctx, 'Companies', 'Admin-managed customer companies. No open signup in the MVP.', '/admin/companies',
    card({ flush: true, body: companiesTable(res.items, ctx, true) }),
    [button('Create company', { kind: 'primary', icon: 'plus', onClick: () => openCompanyModal(null, () => ctx.navigate('/admin/companies')) })]);
}

export async function mountCompanyDetail(root, ctx) {
  let company;
  try {
    company = await call('admin.companies.get', { params: { companyId: ctx.params.companyId } });
  } catch {
    withAdmin(root, ctx, 'Company not found', null, '/admin/companies', emptyState({ icon: 'building', title: 'Company not found' }));
    return;
  }
  const users = (await call('admin.users.list')).items.filter(u => u.company_id === company.id);
  const statusPanel = card({ body: kv([['Status', company.status], ['Plan', company.plan], ['Users', users.length]]) });
  const profilePanel = card({ body: kv([['Legal name', company.legal_name], ['Website', company.website], ['Created', fmt.ago(company.created_at)]]) });
  const actionPanel = card({
    body: el('div', { class: 'ifz-col' },
      button('Activate', { icon: 'check', onClick: () => setCompanyStatus(company.id, 'admin.companies.activate', ctx) }),
      button('Disable', { icon: 'ban', onClick: () => setCompanyStatus(company.id, 'admin.companies.disable', ctx) }),
      button('Suspend', { kind: 'danger', icon: 'ban', onClick: () => setCompanyStatus(company.id, 'admin.companies.suspend', ctx) })),
  });
  const usersPanel = card({
    title: 'Users',
    flush: users.length > 0,
    body: users.length ? usersTable(users, ctx) : emptyState({ icon: 'contact', title: 'No users assigned' }),
  });
  withAdmin(root, ctx, company.name, 'Admin customer detail and lifecycle actions.', '/admin/companies',
    el('div', {},
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' }, statusPanel, profilePanel, actionPanel),
      usersPanel),
    [
      button('Open workspace', { kind: 'primary', icon: 'dashboard', onClick: () => {
        updateSession({ company: { id: company.id, name: company.name } });
        db.company = { ...db.company, id: company.id, name: company.name };
        emit('company', db.company);
        ctx.navigate('/app/today');
      } }),
      button('Edit', { icon: 'edit', onClick: () => openCompanyModal(company, () => ctx.navigate(`/admin/companies/${company.id}`)) }),
    ]);
}

export async function mountUsers(root, ctx) {
  const res = await call('admin.users.list');
  withAdmin(root, ctx, 'Users', 'Admin-provisioned users; customers cannot invite users in MVP.', '/admin/users',
    card({ flush: true, body: usersTable(res.items, ctx) }),
    [button('Create user', { kind: 'primary', icon: 'plus', onClick: () => openUserModal(null, () => ctx.navigate('/admin/users')) })]);
}

export async function mountAgentRuns(root, ctx) {
  const res = await call('agentRuns.list');
  withAdmin(root, ctx, 'Admin Agent Runs', 'Run audit for the selected workspace.', '/admin/agent-runs',
    card({ flush: true, body: dataTable({
      columns: [
        { key: 'label', label: 'Run', render: r => recordTitle(r.label, r.type.replace(/_/g, ' ')) },
        { key: 'status', label: 'Status', render: r => badge(r.status) },
        { key: 'progress', label: 'Progress', render: r => `${r.progress}%` },
        { key: 'created', label: 'Started', render: r => fmt.ago(r.created_at) },
      ],
      rows: res.items,
      onRowClick: r => ctx.navigate(`/admin/agent-runs/${r.id}`),
    }) }));
}

export async function mountAnalytics(root, ctx) {
  const dash = await call('dashboard.summary');
  withAdmin(root, ctx, 'Admin Analytics', 'Operational health for the selected workspace.', '/admin/analytics',
    el('div', { class: 'ifz-grid cols-2' },
      card({ title: 'Best markets', body: hbarList(dash.market.best_countries.map(c => ({ label: countryName(c.country), value: c.score }))) }),
      card({ title: 'Top industries', body: hbarList(dash.market.top_industries, { suffix: ' leads' }) }),
      card({ title: 'Pipeline', body: kv([
        ['Leads found', dash.sales.leads_found],
        ['Contacts found', dash.sales.contacts_found],
        ['Emails sent', dash.sales.emails_sent],
        ['Replies', dash.sales.replies],
      ]) }),
      card({ title: 'Recommended actions', body: dash.recommended_actions.map(a => el('div', { class: 'ifz-actionrow' },
        el('div', { class: 'ifz-actionrow-body' }, el('div', { class: 'ifz-actionrow-title' }, a.title), el('div', { class: 'ifz-actionrow-sub' }, a.sub)),
        button('Open', { size: 'sm', onClick: () => ctx.navigate(a.href) }))) })));
}

export async function mountIntegrations(root, ctx) {
  const [email, whatsapp, sources] = await Promise.all([
    call('emailIntegrations.list'),
    call('whatsapp.integrations'),
    call('dataSources.list'),
  ]);
  withAdmin(root, ctx, 'Integration Health', 'Provider status for the selected workspace.', '/admin/integrations',
    el('div', { class: 'ifz-grid cols-2' },
      card({
        title: 'Email providers',
        flush: true,
        body: dataTable({
          columns: [
            { key: 'provider', label: 'Provider', render: i => providerLabel(i.provider) },
            { key: 'mailbox', label: 'Mailbox', render: i => i.mailbox },
            { key: 'status', label: 'Status', render: i => badge(i.status) },
            { key: 'test', label: 'Last test', render: i => i.last_test?.at ? fmt.ago(i.last_test.at) : '-' },
          ],
          rows: email.items,
          empty: emptyState({ icon: 'plug', title: 'No email integrations' }),
        }),
      }),
      card({ title: 'WhatsApp Business', body: whatsapp.items.length ? kv([
        ['Business', whatsapp.items[0].business_name],
        ['Status', whatsapp.items[0].status],
        ['Template', whatsapp.items[0].template_status],
      ]) : emptyState({ icon: 'whatsapp', title: 'Not connected' }) }),
      card({ title: 'Data sources', flush: true, body: dataSourcesTable(sources.items) })));
}

export async function mountErrors(root, ctx) {
  const res = await call('admin.errors');
  withAdmin(root, ctx, 'Errors', 'Non-secret warnings and failed-run incidents.', '/admin/errors',
    card({ flush: true, body: dataTable({
      columns: [
        { key: 'level', label: 'Level', render: e => badge(e.level === 'warning' ? 'pending' : 'active', e.level) },
        { key: 'area', label: 'Area', render: e => e.area },
        { key: 'message', label: 'Message', render: e => e.message },
        { key: 'at', label: 'Time', render: e => fmt.ago(e.at) },
      ],
      rows: res.items,
      empty: emptyState({ icon: 'check', title: 'No errors' }),
    }) }));
}

export async function mountLogs(root, ctx) {
  const res = await call('admin.logs', { query: { limit: 100 } });
  withAdmin(root, ctx, 'Logs', 'Activity-derived admin logs with secret-safe messages only.', '/admin/logs',
    card({ flush: true, body: logsTable(res.items) }));
}

export async function mountDataSources(root, ctx) {
  const res = await call('dataSources.catalog');
  const rows = res.items || res;
  withAdmin(root, ctx, 'Data Sources',
    'Provider catalog, access state, health, licensing and tenant evidence lifecycle.', '/admin/data-sources',
    el('div', { class: 'ifz-research-stack' },
      card({ body: el('div', { class: 'ifz-grid cols-3' },
        statCard({ label: 'Cataloged sources', value: String(rows.length), delta: 'global catalog', deltaDir: 'flat' }),
        statCard({ label: 'Available', value: String(rows.filter(source => source.available).length), delta: 'tenant-ready', deltaDir: 'up' }),
        statCard({ label: 'Needs access', value: String(rows.filter(source => !source.available && source.health !== 'retired').length), delta: 'explicitly gated', deltaDir: 'flat' })) }),
      card({ flush: true, body: researchSourcesTable(rows, ctx) }),
      card({ title: 'Lifecycle semantics', body: el('div', { class: 'ifz-grid cols-3' },
        el('div', {}, el('strong', {}, 'Disable'), el('p', { class: 'ifz-hint' }, 'Stops future collection. Existing evidence remains active.')),
        el('div', {}, el('strong', {}, 'Uninstall'), el('p', { class: 'ifz-hint' }, 'Removes the adapter. Historical evidence and source metadata remain.')),
        el('div', {}, el('strong', {}, 'Purge evidence'), el('p', { class: 'ifz-hint' }, 'Deletes this tenant’s raw and normalized evidence, then recalculates affected leads and scores.'))) })),
  );
}

function researchSourcesTable(rows, ctx) {
  return dataTable({
    columns: [
      { key: 'display_name', label: 'Source', render: source => recordTitle(source.display_name, source.publisher) },
      { key: 'categories', label: 'Capabilities', render: source => (source.categories || []).join(', ') },
      { key: 'jurisdiction', label: 'Jurisdiction', render: source => (source.jurisdiction || []).join(', ') || 'Global' },
      { key: 'access_tier', label: 'Access', render: source => source.access_tier.replace(/_/g, ' ') },
      { key: 'health', label: 'Health', render: source => badge(source.health) },
      { key: 'state', label: 'State', render: source => source.enabled ? badge('active', 'enabled') : source.installed ? badge('pending', 'disabled') : badge('neutral', 'not installed') },
      { key: 'actions', label: 'Actions', width: '260px', render: source => el('div', { class: 'ifz-row' },
        source.installed
          ? button(source.enabled ? 'Disable' : 'Enable', { size: 'sm', onClick: async () => {
              await call(source.enabled ? 'dataSources.disable' : 'dataSources.enable', { params: { sourceId: source.source_id } });
              toast(source.enabled ? 'Future collection stopped; historical evidence remains active.' : 'Source enabled for tenant campaigns.', 'success');
              ctx.navigate('/admin/data-sources');
            } })
          : button('Install', { size: 'sm', disabled: source.health === 'retired', onClick: async () => {
              await call('dataSources.install', { params: { sourceId: source.source_id } });
              toast('Adapter installed; enable it when access is ready.', 'success');
              ctx.navigate('/admin/data-sources');
            } }),
        source.installed ? button('Uninstall', { kind: 'ghost', size: 'sm', onClick: async () => {
          await call('dataSources.uninstall', { params: { sourceId: source.source_id } });
          toast('Adapter removed. Historical evidence and source metadata remain.', 'success');
          ctx.navigate('/admin/data-sources');
        } }) : null,
        button('Purge', { kind: 'danger', size: 'sm', onClick: () => openPurgeSource(source, () => ctx.navigate('/admin/data-sources')) })) },
    ],
    rows,
    empty: emptyState({ icon: 'database', title: 'No provider definitions are installed' }),
  });
}

async function openPurgeSource(source, afterPurge) {
  const impact = await call('dataSources.impact', { params: { sourceId: source.source_id } });
  const confirmation = input({ placeholder: source.display_name, autocomplete: 'off' });
  const purge = button('Purge evidence', { kind: 'danger', disabled: true });
  confirmation.addEventListener('input', () => { purge.disabled = confirmation.value !== source.display_name; });
  const m = modal({
    title: `Purge ${source.display_name}`,
    wide: true,
    body: el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-policy-lock' }, 'This deletes this tenant’s raw and normalized evidence, then recalculates affected leads and scores.'),
      el('div', { class: 'ifz-grid cols-4' },
        statCard({ label: 'Campaigns', value: String(impact.campaigns) }),
        statCard({ label: 'Organizations', value: String(impact.organizations) }),
        statCard({ label: 'Claims', value: String(impact.claims) }),
        statCard({ label: 'Leads at risk', value: String(impact.leads_may_lose_qualification) })),
      field(`Type “${source.display_name}” to confirm`, confirmation, { required: true,
        hint: `${(impact.storage_bytes / 1024 / 1024).toFixed(2)} MB of local snapshots are included in this impact preview.` })),
    actions: [button('Cancel', { kind: 'ghost', onClick: () => m.close() }), purge],
  });
  purge.addEventListener('click', async () => {
    setBusy(purge, true, 'Purging…');
    try {
      await call('dataSources.purge', { params: { sourceId: source.source_id }, body: { confirmation: confirmation.value } });
      m.close();
      toast('Evidence purged. Affected leads require recalculation.', 'success');
      afterPurge?.();
    } catch (error) {
      toast(error.message, 'error');
      setBusy(purge, false);
    }
  });
}

function companiesTable(rows, ctx, actions) {
  return dataTable({
    columns: [
      { key: 'name', label: 'Company', render: c => recordTitle(c.name, c.website) },
      { key: 'status', label: 'Status', render: c => badge(c.status === 'access_pending' ? 'pending' : c.status) },
      { key: 'plan', label: 'Plan', render: c => el('span', { class: 'ifz-tag' }, c.plan) },
      { key: 'users', label: 'Users', render: c => c.users },
      { key: 'seen', label: 'Last seen', render: c => c.last_seen_at ? fmt.ago(c.last_seen_at) : '-' },
      actions ? { key: 'actions', label: '', width: '90px', render: c => button('Open', { size: 'sm', onClick: () => ctx.navigate(`/admin/companies/${c.id}`) }) } : null,
    ].filter(Boolean),
    rows,
    onRowClick: c => ctx.navigate(`/admin/companies/${c.id}`),
  });
}

function usersTable(rows, ctx) {
  return dataTable({
    columns: [
      { key: 'name', label: 'User', render: u => recordTitle(u.name, u.email) },
      { key: 'role', label: 'Role', render: u => el('span', { class: 'ifz-tag' }, u.role) },
      { key: 'company', label: 'Company', render: u => db.admin.companies.find(c => c.id === u.company_id)?.name || 'Platform' },
      { key: 'status', label: 'Status', render: u => badge(u.status) },
      { key: 'last', label: 'Last login', render: u => u.last_login_at ? fmt.ago(u.last_login_at) : '-' },
      { key: 'actions', label: '', width: '160px', render: u => el('div', { class: 'ifz-row' },
        button('Edit', { size: 'sm', icon: 'edit', onClick: () => openUserModal(u, () => ctx.navigate('/admin/users')) }),
        button('Reset', { size: 'sm', onClick: async () => { const res = await call('admin.users.resetPassword', { params: { userId: u.id } }); toast(res.message, 'success'); } })) },
    ],
    rows,
  });
}

function logsTable(rows) {
  return dataTable({
    columns: [
      { key: 'area', label: 'Area', render: l => el('span', { class: 'ifz-tag' }, l.area) },
      { key: 'message', label: 'Message', render: l => l.message },
      { key: 'at', label: 'Time', render: l => fmt.ago(l.at) },
    ],
    rows,
    empty: emptyState({ icon: 'clock', title: 'No logs' }),
  });
}

function dataSourcesTable(rows) {
  return dataTable({
    columns: [
      { key: 'label', label: 'Source', render: s => recordTitle(s.label, s.type.replace(/_/g, ' ')) },
      { key: 'status', label: 'Status', render: s => badge(s.status === 'enabled' ? 'active' : 'pending', s.status) },
      { key: 'actions', label: '', width: '100px', render: s => button('Test', { size: 'sm', onClick: async () => {
        const res = await call('dataSources.test', { params: { sourceId: s.id } });
        toast(`Source test passed (${res.latency_ms}ms)`, 'success');
      } }) },
    ],
    rows,
  });
}

function openCompanyModal(company, afterSave) {
  const name = input({ value: company?.name || '' });
  const legal = input({ value: company?.legal_name || '' });
  const website = input({ value: company?.website || '' });
  const plan = select(['trial', 'pilot', 'demo', 'enterprise'], { value: company?.plan || 'trial' });
  const save = button(company ? 'Save company' : 'Create company', { kind: 'primary', icon: 'check' });
  const m = modal({
    title: company ? 'Edit company' : 'Create company',
    body: el('div', {}, field('Name', name), field('Legal name', legal), field('Website', website), field('Plan', plan)),
    actions: [button('Cancel', { onClick: () => m.close() }), save],
  });
  save.addEventListener('click', async () => {
    setBusy(save, true, 'Saving...');
    const body = { name: name.value, legal_name: legal.value || name.value, website: website.value, plan: plan.value };
    if (company) await call('admin.companies.update', { params: { companyId: company.id }, body });
    else await call('admin.companies.create', { body });
    toast('Company saved', 'success');
    m.close();
    afterSave();
  });
}

function openUserModal(user, afterSave) {
  const name = input({ value: user?.name || '' });
  const email = input({ value: user?.email || '', type: 'email' });
  const role = select(['admin', 'support', 'owner', 'sales'], { value: user?.role || 'owner' });
  const company = select([{ value: '', label: 'Platform' }, ...db.admin.companies.map(c => ({ value: c.id, label: c.name }))], { value: user?.company_id || '' });
  const save = button(user ? 'Save user' : 'Create user', { kind: 'primary', icon: 'check' });
  const m = modal({
    title: user ? 'Edit user' : 'Create user',
    body: el('div', {}, field('Name', name), field('Email', email), field('Role', role), field('Company', company)),
    actions: [button('Cancel', { onClick: () => m.close() }), save],
  });
  save.addEventListener('click', async () => {
    setBusy(save, true, 'Saving...');
    const body = { name: name.value, email: email.value, role: role.value, company_id: company.value || null };
    if (user) await call('admin.users.update', { params: { userId: user.id }, body });
    else await call('admin.users.create', { body });
    toast('User saved', 'success');
    m.close();
    afterSave();
  });
}

async function setCompanyStatus(companyId, route, ctx) {
  await call(route, { params: { companyId } });
  toast('Company status updated', 'success');
  ctx.navigate(`/admin/companies/${companyId}`);
}
