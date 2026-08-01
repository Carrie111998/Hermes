/* Analytics - sales pipeline + market intelligence. */

import {
  el, card, button, pageHead, statCard, hbarList, barChart, dataTable, badge,
  toast, tabs, fmt,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import { countryName, exportCsv, productFor } from './_page-utils.js';

export async function mount(root, ctx) {
  let disposed = false;
  let active = ctx.query.tab || 'sales';
  const host = el('div', {});

  root.append(pageHead({
    title: 'Analytics',
    sub: 'Sales pipeline metrics and market intelligence from your Company Brain and outreach data.',
    actions: [
      button('Export analytics', { icon: 'download', onClick: async () => {
        await exportCsv('exports.analytics');
      } }),
      button('Refresh', { kind: 'primary', icon: 'refresh', onClick: async () => {
        const run = await call('agentRuns.create', { body: { type: 'analytics_refresh', label: 'Refresh analytics' } });
        toast('Analytics refresh started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${run.id}`) });
      } }),
    ],
  }), host);

  async function render() {
    const [dash, pipeline, market] = await Promise.all([
      call('dashboard.summary'),
      call('analytics.pipeline'),
      call('analytics.market'),
    ]);
    if (disposed) return;
    const tabBar = tabs([
      { key: 'sales', label: 'Sales pipeline' },
      { key: 'market', label: 'Market intelligence' },
      { key: 'sources', label: 'Sources & exports' },
    ], active, key => { active = key; render().catch(console.error); });

    const stats = el('div', { class: 'ifz-grid cols-6 ifz-mb-4' },
      statCard({ label: 'Leads found', value: fmt.num(dash.sales.leads_found), delta: '+ live scans', deltaDir: 'up' }),
      statCard({ label: 'Contacts found', value: fmt.num(dash.sales.contacts_found), delta: 'buyer people', deltaDir: 'up' }),
      statCard({ label: 'Emails sent', value: fmt.num(dash.sales.emails_sent), delta: 'draft-first', deltaDir: 'flat' }),
      statCard({ label: 'Replies', value: fmt.num(dash.sales.replies), delta: `${dash.sales.emails_sent ? Math.round((dash.sales.replies / dash.sales.emails_sent) * 100) : 0}% rate`, deltaDir: dash.sales.replies ? 'up' : 'flat' }),
      statCard({ label: 'Interested', value: fmt.num(dash.sales.interested), delta: 'sample/order signal', deltaDir: dash.sales.interested ? 'up' : 'flat' }),
      statCard({ label: 'WhatsApp', value: fmt.num(dash.sales.whatsapp_messages), delta: 'approval flow', deltaDir: 'flat' }));

    host.replaceChildren(tabBar, stats,
      active === 'sales' ? salesView(pipeline)
        : active === 'market' ? marketView(market, ctx)
          : sourcesView(market));
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function salesView(pipeline) {
  return el('div', { class: 'ifz-grid cols-2' },
    card({
      title: 'Pipeline funnel',
      body: hbarList(pipeline.funnel.map(f => ({ label: f.stage, value: f.value })), { suffix: '' }),
    }),
    card({
      title: 'Lead status mix',
      flush: true,
      body: dataTable({
        columns: [
          { key: 'status', label: 'Status', render: r => badge(r.status) },
          { key: 'count', label: 'Count', render: r => el('span', { class: 'ifz-strong' }, r.count) },
          { key: 'share', label: 'Share', render: r => {
            const total = pipeline.leads_by_status.reduce((sum, x) => sum + x.count, 0) || 1;
            return `${Math.round((r.count / total) * 100)}%`;
          } },
        ],
        rows: pipeline.leads_by_status,
      }),
    }),
    card({
      title: 'Emails sent weekly',
      body: barChart({ labels: pipeline.emails_sent_weekly.labels, values: pipeline.emails_sent_weekly.values, color: 'var(--accent)' }),
    }),
    card({
      title: 'Replies weekly',
      body: barChart({ labels: pipeline.replies_weekly.labels, values: pipeline.replies_weekly.values, color: 'var(--success)' }),
    }));
}

function marketView(market, ctx) {
  return el('div', {},
    el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
      card({
        title: 'Country opportunity',
        actions: button('Open map', { size: 'sm', icon: 'map', onClick: () => ctx.navigate('/app/lead-map') }),
        body: hbarList(market.country_scores.map(c => ({ label: countryName(c.country), value: c.score }))),
      }),
      card({
        title: 'Top buyer industries',
        body: hbarList(market.top_industries, { suffix: ' leads' }),
      }),
      card({
        title: 'Source performance',
        body: hbarList(market.source_performance, { suffix: ' leads' }),
      })),
    card({
      title: 'Product-market fit matrix',
      flush: true,
      body: dataTable({
        columns: [
          { key: 'country', label: 'Market', render: r => countryName(r.country) },
          { key: 'primary', label: 'Primary fit', render: r => {
            const top = r.products[0];
            return el('div', {}, el('div', { class: 'cell-strong' }, productFor(top.product_id)?.name || top.name), el('div', { class: 'cell-muted ifz-small' }, `Score ${top.score}`));
          } },
          { key: 'secondary', label: 'Secondary products', render: r => el('span', { class: 'cell-muted' }, r.products.slice(1).map(p => `${p.name} ${p.score}`).join(', ')) },
          { key: 'leads', label: 'Leads', render: r => db.leads.filter(l => l.country === r.country).length },
          { key: 'contacts', label: 'Contacts', render: r => {
            const leadIds = db.leads.filter(l => l.country === r.country).map(l => l.id);
            return db.contacts.filter(c => leadIds.includes(c.lead_id)).length;
          } },
        ],
        rows: market.product_market_fit,
      }),
    }));
}

function sourcesView(market) {
  const dataSources = [
    { type: 'web_search', label: 'Web directories', status: 'enabled' },
    { type: 'exhibitor_lists', label: 'Trade fair exhibitors', status: 'enabled' },
    { type: 'company_registries', label: 'Company registries', status: 'enabled' },
    { type: 'linkedin_reference', label: 'LinkedIn references', status: 'manual' },
  ];
  return el('div', { class: 'ifz-grid cols-2' },
    card({
      title: 'Data source health',
      flush: true,
      body: dataTable({
        columns: [
          { key: 'label', label: 'Source', render: s => s.label },
          { key: 'status', label: 'Status', render: s => badge(s.status === 'manual' ? 'pending' : 'active', s.status) },
          { key: 'leads', label: 'Leads', render: s => market.source_performance.find(x => x.label.toLowerCase().includes(s.label.split(' ')[0].toLowerCase()))?.value || db.leads.filter(l => l.source === s.type).length },
        ],
        rows: dataSources,
      }),
    }),
    card({
      title: 'Export surfaces',
      body: el('div', {},
        exportRow('Leads CSV', 'exports.leads'),
        exportRow('Contacts CSV', 'exports.contacts'),
        exportRow('Research CSV', 'exports.research'),
        exportRow('Outreach CSV', 'exports.outreach'),
        exportRow('Analytics CSV', 'exports.analytics')),
    }));
}

function exportRow(label, route) {
  return el('div', { class: 'ifz-actionrow' },
    el('div', { class: 'ifz-actionrow-body' },
      el('div', { class: 'ifz-actionrow-title' }, label),
      el('div', { class: 'ifz-actionrow-sub' }, 'Generate and download a tenant-scoped CSV')),
    button('Export', { size: 'sm', icon: 'download', onClick: async () => {
      await exportCsv(route);
    } }));
}
