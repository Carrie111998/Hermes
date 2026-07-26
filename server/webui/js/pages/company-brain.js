/* Company Brain - reviewable intelligence profile used by scoring/outreach. */

import {
  el, card, button, pageHead, badge, statCard, emptyState, toast, dataTable,
  kv, hbarList, textarea, field, modal, setBusy, fmt,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import { countryName, compactList, productFor } from './_page-utils.js';

export async function mount(root, ctx) {
  let disposed = false;
  const host = el('div', {});
  root.append(host);
  await Promise.all([call('products.list'), call('documents.list'), call('analytics.market')]);

  async function render() {
    const brain = await call('brain.get');
    if (disposed) return;

    const rebuildBtn = button('Rebuild', { icon: 'refresh', onClick: async () => {
      const res = await call('brain.rebuild');
      toast('Brain rebuild started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
      render();
    } });
    const approveBtn = button('Approve', { kind: 'primary', icon: 'check', onClick: async () => {
      if (!brain.id) { toast('Build the Company Brain first', 'warning'); return; }
      await call('brain.approve', { body: { snapshot_id: brain.id } });
      toast('Company Brain approved', 'success');
      render();
    } });
    const editBtn = button('Edit assumptions', { icon: 'edit', onClick: () => openBrainEdit(brain, render) });

    const marketRows = db.analytics.market.product_market_fit.filter(row => row.products?.length).map(row => ({
      country: row.country,
      products: row.products,
      best: row.products[0],
    }));

    const snapshots = await call('brain.snapshots');

    host.replaceChildren(
      pageHead({
        title: 'Company Brain',
        sub: 'The approved sales context that informs lead scoring, research prompts, and outreach generation.',
        actions: [button('Onboarding', { icon: 'upload', onClick: () => ctx.navigate('/app/onboarding') }), editBtn, rebuildBtn, approveBtn],
      }),
      el('div', { class: 'ifz-grid cols-4 ifz-mb-4' },
        statCard({ label: 'Status', value: brain.status.replace(/_/g, ' '), delta: brain.approved_at ? `approved ${fmt.ago(brain.approved_at)}` : 'review needed', deltaDir: brain.status === 'approved' ? 'up' : 'flat' }),
        statCard({ label: 'Version', value: `v${brain.version}`, delta: brain.built_at ? `built ${fmt.ago(brain.built_at)}` : 'not built', deltaDir: 'flat' }),
        statCard({ label: 'Products', value: String(db.products.length), delta: `${db.documents.filter(d => d.status === 'processed').length} docs processed`, deltaDir: 'up' }),
        statCard({ label: 'ICP roles', value: String(brain.sections.buyer_roles.length), delta: 'used in contact discovery', deltaDir: 'flat' })),
      el('div', { class: 'ifz-grid cols-2 ifz-mb-4' },
        sectionCard('Product understanding', brain.sections.product_understanding, 'file'),
        sectionCard('Ideal customer profile', brain.sections.ideal_customer_profile, 'target'),
        sectionCard('Market assumptions', brain.sections.market_assumptions, 'globe'),
        sectionCard('Sales arguments', brain.sections.sales_arguments, 'sparkle')),
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        card({
          title: 'Buyer roles',
          body: compactList(brain.sections.buyer_roles),
        }),
        card({
          title: 'Missing data',
          body: brain.sections.missing_data.length
            ? el('div', {}, brain.sections.missing_data.map(x => el('div', { class: 'ifz-actionrow' },
                el('span', { class: 'ifz-actionrow-icon' }, badge('warning', '!')),
                el('div', { class: 'ifz-actionrow-body' }, el('div', { class: 'ifz-actionrow-title' }, x)))))
            : emptyState({ icon: 'check', title: 'No gaps flagged' }),
        }),
        card({
          title: 'Top markets',
          body: hbarList(db.analytics.market.country_scores.slice(0, 6).map(c => ({ label: countryName(c.country), value: c.score }))),
        })),
      el('div', { class: 'ifz-grid cols-2' },
        card({
          title: 'Product-market fit',
          flush: true,
          body: dataTable({
            columns: [
              { key: 'country', label: 'Market', render: r => countryName(r.country) },
              { key: 'best', label: 'Best fit', render: r => productFor(r.best.product_id)?.name || r.best.name },
              { key: 'score', label: 'Score', render: r => el('span', { class: `ifz-score ${r.best.score >= 85 ? 'high' : 'mid'}` }, r.best.score) },
              { key: 'products', label: 'Also relevant', render: r => el('span', { class: 'cell-muted' }, r.products.slice(1).map(p => `${p.name} ${p.score}`).join(', ')) },
            ],
            rows: marketRows,
          }),
        }),
        card({
          title: 'Snapshot history',
          flush: true,
          body: dataTable({
            columns: [
              { key: 'version', label: 'Version', render: s => el('span', { class: 'ifz-strong' }, `v${s.version}`) },
              { key: 'note', label: 'Note', render: s => s.note },
              { key: 'approved', label: 'Approved', render: s => badge(s.approved ? 'approved' : 'pending') },
              { key: 'created', label: 'Created', render: s => fmt.ago(s.created_at) },
            ],
            rows: snapshots.items,
          }),
        })),
      el('div', { class: 'ifz-mt-4' }, card({
        title: 'Company profile in use',
        body: kv([
          ['Company', db.company.legal_name],
          ['Website', db.company.website],
          ['Headquarters', `${db.company.city}, ${countryName(db.company.headquarters_country)}`],
          ['Business model', db.company.business_model],
          ['Connected mailbox', db.company.sales_preferences.connected_mailbox],
          ['Target markets', db.company.sales_regions_target.map(countryName).join(', ')],
        ]),
      })));
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function sectionCard(title, items, iconName) {
  return card({
    title,
    body: items?.length
      ? el('div', {}, items.map(item => el('div', { class: 'ifz-actionrow' },
          el('span', { class: 'ifz-actionrow-icon' }, el('span', {}, iconName === 'target' ? 'ICP' : iconName === 'globe' ? 'MK' : iconName === 'file' ? 'PR' : 'SA')),
          el('div', { class: 'ifz-actionrow-body' },
            el('div', { class: 'ifz-actionrow-title' }, item)))))
      : emptyState({ icon: iconName, title: 'No notes yet' }),
  });
}

function openBrainEdit(brain, onSaved) {
  const assumptions = textarea({ value: brain.sections.market_assumptions.join('\n'), rows: 8 });
  const argumentsBox = textarea({ value: brain.sections.sales_arguments.join('\n'), rows: 6 });
  const missing = textarea({ value: brain.sections.missing_data.join('\n'), rows: 5 });
  const saveBtn = button('Save assumptions', { kind: 'primary', icon: 'check' });
  const m = modal({
    title: 'Edit Company Brain assumptions',
    wide: true,
    body: el('div', {},
      field('Market assumptions', assumptions, { hint: 'One line per assumption.' }),
      field('Sales arguments', argumentsBox, { hint: 'One line per reusable sales point.' }),
      field('Missing data', missing, { hint: 'Items shown during onboarding/review.' })),
    actions: [button('Cancel', { onClick: () => m.close() }), saveBtn],
  });
  saveBtn.addEventListener('click', async () => {
    setBusy(saveBtn, true, 'Saving...');
    await call('brain.update', { body: {
      market_assumptions: assumptions.value.split('\n').map(x => x.trim()).filter(Boolean),
      sales_arguments: argumentsBox.value.split('\n').map(x => x.trim()).filter(Boolean),
      missing_data: missing.value.split('\n').map(x => x.trim()).filter(Boolean),
    } });
    toast('Company Brain updated', 'success');
    m.close();
    if (onSaved) onSaved();
  });
}
