/* Onboarding - Company Brain setup wizard. */

import {
  el, card, button, pageHead, stepper, field, input, select, textarea, toast,
  setBusy, badge, dataTable, emptyState, chipSelect, kv, modal,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import {
  countryOptions, countryName, openContactModal, recordTitle, compactList,
} from './_page-utils.js';

const DOC_TYPES = [
  { value: 'product_catalog', label: 'Product catalog' },
  { value: 'technical_sheet', label: 'Technical sheet' },
  { value: 'price_list', label: 'Price list' },
  { value: 'past_sales', label: 'Past sales' },
  { value: 'past_customers', label: 'Past customers' },
  { value: 'current_contacts', label: 'Current contacts' },
  { value: 'distributor_list', label: 'Distributor list' },
  { value: 'certificate', label: 'Certificate' },
  { value: 'other', label: 'Other' },
];

export async function mount(root, ctx) {
  let disposed = false;
  let active = 0;
  const host = el('div', {});

  root.append(pageHead({
    title: 'Onboarding',
    sub: `Define ${db.company.name || 'your company'} deeply enough that the agent can research, score, and write like a sales teammate.`,
    actions: [
      button('Company Brain', { icon: 'brain', onClick: () => ctx.navigate('/app/company-brain') }),
      button('Integrations', { icon: 'plug', onClick: () => ctx.navigate('/app/integrations') }),
    ],
  }), host);

  const [status] = await Promise.all([
    call('onboarding.status'), call('company.getProfile'), call('company.getPositioning'),
    call('company.getSalesPreferences'), call('products.list'), call('documents.list'),
    call('leads.list'), call('contacts.list'), call('brain.get'),
  ]);
  if (status.status === 'not_started') await call('onboarding.start');
  active = db.onboarding.current_step || 0;

  function setActive(i) {
    active = Math.max(0, Math.min(db.onboarding.steps.length - 1, i));
    render();
  }

  async function mark(key, body = {}) {
    const route = {
      'company-identity': 'onboarding.updateCompanyIdentity',
      positioning: 'onboarding.updatePositioning',
      products: 'onboarding.updateProducts',
      'internal-sales-data': 'onboarding.updateInternalSalesData',
      'current-contacts': 'onboarding.updateCurrentContacts',
      'target-markets': 'onboarding.updateTargetMarkets',
      integrations: 'onboarding.updateIntegrations',
      'brain-review': 'onboarding.reviewBrain',
    }[key];
    if (route) await call(route, { body });
    toast(`${db.onboarding.steps.find(s => s.key === key)?.label || 'Step'} saved`, 'success');
    active = db.onboarding.current_step >= db.onboarding.steps.length ? active : db.onboarding.current_step;
    render();
  }

  async function render() {
    if (disposed) return;
    const steps = db.onboarding.steps.map(s => ({ label: s.label, done: s.status === 'done' }));
    host.replaceChildren(el('div', { class: 'ifz-wizard' },
      el('aside', { class: 'ifz-wizard-side' },
        card({ body: stepper(steps, active, { onStep: setActive }) }),
        el('div', { class: 'ifz-mt-4' }, card({
          title: 'Setup status',
          body: kv([
            ['Status', db.onboarding.status.replace(/_/g, ' ')],
            ['Completed', `${db.onboarding.steps.filter(s => s.status === 'done').length}/${db.onboarding.steps.length}`],
            ['Brain', db.brain.status.replace(/_/g, ' ')],
          ]),
        }))),
      el('main', { class: 'ifz-wizard-main' }, stepView(db.onboarding.steps[active]?.key))));
  }

  function stepView(key) {
    if (key === 'company-identity') return companyIdentity(mark);
    if (key === 'positioning') return positioning(mark);
    if (key === 'products') return productsStep(mark);
    if (key === 'internal-sales-data') return documentsStep(mark, 'internal-sales-data');
    if (key === 'current-contacts') return contactsStep(mark, ctx);
    if (key === 'target-markets') return targetMarketsStep(mark);
    if (key === 'integrations') return integrationsStep(mark, ctx);
    if (key === 'brain-review') return brainReviewStep(mark, ctx);
    return emptyState({ icon: 'upload', title: 'Step not found' });
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function companyIdentity(mark) {
  const company = db.company;
  const name = input({ value: company.name });
  const legal = input({ value: company.legal_name });
  const website = input({ value: company.website });
  const country = select(countryOptions(), { value: company.headquarters_country });
  const city = input({ value: company.city });
  const founded = input({ value: company.founded_year, type: 'number' });
  const industry = input({ value: company.industry });
  const employees = input({ value: company.employee_count });
  const model = input({ value: company.business_model });
  const lang = input({ value: company.main_language });
  const save = button('Save identity', { kind: 'primary', icon: 'check' });
  save.addEventListener('click', async () => {
    setBusy(save, true, 'Saving...');
    await call('company.updateProfile', { body: {
      name: name.value,
      legal_name: legal.value,
      website: website.value,
      headquarters_country: country.value,
      city: city.value,
      founded_year: Number(founded.value),
      industry: industry.value,
      employee_count: employees.value,
      business_model: model.value,
      main_language: lang.value,
    } });
    await mark('company-identity');
  });
  return card({
    title: 'Company identity',
    body: el('div', {},
      el('div', { class: 'ifz-form-row' }, field('Company name', name), field('Legal name', legal)),
      field('Website', website),
      el('div', { class: 'ifz-form-row' }, field('Headquarters country', country), field('City', city)),
      el('div', { class: 'ifz-form-row' }, field('Founded year', founded), field('Employee count', employees)),
      el('div', { class: 'ifz-form-row' }, field('Industry', industry), field('Main language', lang)),
      field('Business model', model),
      save),
  });
}

function positioning(mark) {
  const p = db.company.positioning;
  const sells = textarea({ value: p.what_company_sells, rows: 3 });
  const value = textarea({ value: p.main_value_proposition, rows: 3 });
  const quality = input({ value: p.quality_position });
  const price = input({ value: p.price_position });
  const market = input({ value: p.premium_or_mass_market });
  const diffs = textarea({ value: p.main_differentiators.join('\n'), rows: 4 });
  const certs = input({ value: p.certifications.join(', ') });
  const capacity = input({ value: p.manufacturing_capacity });
  const exportCap = input({ value: p.export_capacity });
  const delivery = textarea({ value: p.delivery_capabilities, rows: 2 });
  const support = textarea({ value: p.after_sales_support, rows: 2 });
  const save = button('Save positioning', { kind: 'primary', icon: 'check' });
  save.addEventListener('click', async () => {
    await call('company.updatePositioning', { body: {
      what_company_sells: sells.value,
      main_value_proposition: value.value,
      quality_position: quality.value,
      price_position: price.value,
      premium_or_mass_market: market.value,
      main_differentiators: diffs.value.split('\n').map(x => x.trim()).filter(Boolean),
      certifications: certs.value.split(',').map(x => x.trim()).filter(Boolean),
      manufacturing_capacity: capacity.value,
      export_capacity: exportCap.value,
      delivery_capabilities: delivery.value,
      after_sales_support: support.value,
    } });
    await mark('positioning');
  });
  return card({
    title: 'Positioning',
    body: el('div', {},
      field('What company sells', sells),
      field('Main value proposition', value),
      el('div', { class: 'ifz-grid cols-3' }, field('Quality position', quality), field('Price position', price), field('Market tier', market)),
      field('Differentiators', diffs, { hint: 'One per line.' }),
      el('div', { class: 'ifz-form-row' }, field('Certifications', certs), field('Manufacturing capacity', capacity)),
      el('div', { class: 'ifz-form-row' }, field('Export capacity', exportCap), field('After-sales support', support)),
      field('Delivery capabilities', delivery),
      save),
  });
}

function productsStep(mark) {
  const table = dataTable({
    columns: [
      { key: 'name', label: 'Product', render: p => recordTitle(p.name, p.category) },
      { key: 'moq', label: 'MOQ', render: p => p.moq || '-' },
      { key: 'price', label: 'Price band', render: p => p.price_band || '-' },
      { key: 'certs', label: 'Certifications', render: p => compactList(p.certifications || []) },
      { key: 'fit', label: 'Market fit', render: p => el('span', { class: 'cell-muted' }, (p.market_fit || []).map(f => `${countryName(f.country)} ${f.score}`).join(', ') || '-') },
    ],
    rows: db.products,
    empty: emptyState({ icon: 'file', title: 'No products yet' }),
  });
  return card({
    title: 'Product catalog',
    actions: [
      button('Add product', { icon: 'plus', onClick: () => openProductModal() }),
      button('Mark complete', { kind: 'primary', icon: 'check', onClick: () => mark('products') }),
    ],
    body: table,
    flush: true,
  });
}

function documentsStep(mark, key) {
  const uploadBtn = button('Add document record', { kind: 'primary', icon: 'upload', onClick: () => openDocumentModal() });
  const table = dataTable({
    columns: [
      { key: 'name', label: 'Document', render: d => recordTitle(d.name, d.type.replace(/_/g, ' ')) },
      { key: 'status', label: 'Status', render: d => badge(d.status) },
      { key: 'size', label: 'Size', render: d => `${d.size_kb} KB` },
      { key: 'uploaded', label: 'Uploaded', render: d => fmtDate(d.uploaded_at) },
      { key: 'action', label: '', width: '120px', render: d => d.demo_record
        ? button('Historical', { size: 'sm', disabled: true, title: 'Upload the original file to process it again.' })
        : button('Process', { size: 'sm', icon: 'refresh', onClick: async () => {
          const res = await call('documents.process', { params: { documentId: d.id } });
          toast('Document processing started', 'success', { actionLabel: 'Watch', onAction: () => location.hash = `#/app/agent-runs/${res.run_id}` });
        } }) },
    ],
    rows: db.documents,
  });
  return card({
    title: 'Internal sales data',
    actions: [uploadBtn, button('Mark complete', { icon: 'check', onClick: () => mark(key) })],
    body: el('div', {},
      el('p', { class: 'ifz-muted ifz-mb-4', style: { lineHeight: 1.55 } }, 'Track the files the Company Brain should learn from: past sales, customer lists, price lists, distributor lists, proposals, and certificates.'),
      table),
  });
}

function contactsStep(mark, ctx) {
  const contacts = db.contacts.slice(0, 8);
  return card({
    title: 'Current contacts',
    actions: [
      button('Add contact', { icon: 'plus', onClick: () => openContactModal({ onCreated: c => ctx.navigate(`/app/contacts/${c.id}`) }) }),
      button('Contacts table', { icon: 'contact', onClick: () => ctx.navigate('/app/contacts') }),
      button('Mark complete', { kind: 'primary', icon: 'check', onClick: () => mark('current-contacts') }),
    ],
    body: contacts.length
      ? dataTable({
          columns: [
            { key: 'name', label: 'Contact', render: c => recordTitle(c.name, c.title) },
            { key: 'company', label: 'Company', render: c => recordTitle(db.leads.find(l => l.id === c.lead_id)?.company_name || 'Unlinked', countryName(db.leads.find(l => l.id === c.lead_id)?.country)) },
            { key: 'email', label: 'Email', render: c => el('span', { class: 'ifz-mono' }, c.email || '-') },
            { key: 'status', label: 'Status', render: c => badge(c.email_status) },
          ],
          rows: contacts,
          onRowClick: c => ctx.navigate(`/app/contacts/${c.id}`),
        })
      : emptyState({ icon: 'contact', title: 'No contacts yet', hint: 'Upload a current contact list or add buyers manually.' }),
    flush: contacts.length > 0,
  });
}

function targetMarketsStep(mark) {
  const chips = chipSelect(Object.entries(countryOptions()).map(([_, item]) => item), db.company.sales_regions_target || []);
  return card({
    title: 'Target markets',
    body: el('div', {},
      field('Sales regions target', chips, { hint: 'The lead map and analytics use this to recommend markets.' }),
      el('div', { class: 'ifz-row wrap' },
        button('Save markets', { kind: 'primary', icon: 'check', onClick: async () => {
          await call('company.updateProfile', { body: { sales_regions_target: chips.getSelected() } });
          await mark('target-markets', { target_markets: chips.getSelected() });
        } }),
        button('Open Lead Map', { icon: 'map', onClick: () => location.hash = '#/app/lead-map' }))),
  });
}

function integrationsStep(mark, ctx) {
  const email = db.integrations.email[0];
  const whatsapp = db.integrations.whatsapp[0];
  return card({
    title: 'Integration setup',
    body: el('div', {},
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        setupPanel('Email', email ? 'connected' : 'not_connected', email?.mailbox || 'Connect in integrations'),
        setupPanel('WhatsApp', whatsapp ? 'connected' : 'not_connected', whatsapp?.display_phone_number || 'Business API setup'),
        setupPanel('LinkedIn', 'active', 'Manual workflow; no automated connection sending')),
      el('div', { class: 'ifz-row wrap' },
        button('Open integrations', { icon: 'plug', onClick: () => ctx.navigate('/app/integrations') }),
        button('Mark complete', { kind: 'primary', icon: 'check', onClick: () => mark('integrations') }))),
  });
}

function brainReviewStep(mark, ctx) {
  const complete = button('Complete onboarding', { kind: 'primary', icon: 'check', onClick: async () => {
    await mark('brain-review');
    await call('onboarding.complete');
    toast('Onboarding complete', 'success');
    ctx.navigate('/app/dashboard');
  } });
  return card({
    title: 'Company Brain review',
    body: el('div', {},
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        setupBlock('Brain status', kv([['Status', db.brain.status], ['Version', `v${db.brain.version}`], ['Approved', db.brain.approved_at ? fmtDate(db.brain.approved_at) : '-']])),
        setupBlock('Missing data', compactList(db.brain.sections.missing_data)),
        setupBlock('Actions', el('div', { class: 'ifz-col' }, button('Review Brain', { icon: 'brain', onClick: () => ctx.navigate('/app/company-brain') }), el('span', { class: 'ifz-muted ifz-small' }, 'Approve the brain, then complete onboarding.')))),
      el('div', { class: 'ifz-row wrap' },
        button('Rebuild Brain', { icon: 'refresh', onClick: async () => {
          const res = await call('brain.rebuild');
          toast('Brain rebuild started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
        } }),
        button('Approve Brain', { icon: 'check', onClick: async () => {
          if (!db.brain.id) { toast('Build the Company Brain first', 'warning'); return; }
          await call('brain.approve', { body: { snapshot_id: db.brain.id } });
          toast('Company Brain approved', 'success');
        } }),
        complete)),
  });
}

function openDocumentModal() {
  const file = input({
    type: 'file',
    accept: '.csv,.pdf,.doc,.docx,.xls,.xlsx,.txt,.json',
  });
  const type = select(DOC_TYPES, { value: 'current_contacts' });
  const save = button('Upload document', { kind: 'primary', icon: 'upload' });
  const maxBytes = Number(globalThis.window?.__HERMES_CONFIG__?.maxUploadBytes || 0);
  const limitHint = maxBytes
    ? `Maximum file size: ${Math.ceil(maxBytes / (1024 * 1024))} MB.`
    : 'The server will validate the file size.';
  const m = modal({
    title: 'Upload document',
    body: el('div', {},
      field('File', file, { required: true, hint: limitHint }),
      field('Document type', type)),
    actions: [button('Cancel', { onClick: () => m.close() }), save],
  });
  save.addEventListener('click', async () => {
    const selected = file.files?.[0];
    if (!selected) { toast('Choose a document to upload', 'warning'); return; }
    if (maxBytes && selected.size > maxBytes) {
      toast(`This file exceeds the ${Math.ceil(maxBytes / (1024 * 1024))} MB upload limit`, 'warning');
      return;
    }
    setBusy(save, true, 'Uploading…');
    try {
      const form = new FormData();
      form.append('document_type', type.value);
      form.append('file', selected, selected.name);
      await call('documents.upload', { body: form });
      toast('Document uploaded', 'success');
      m.close();
    } catch (err) {
      setBusy(save, false);
      toast(err.message || 'Document upload failed', 'error');
    }
  });
}

function openProductModal() {
  const name = input({ placeholder: 'Product name' });
  const category = input({ placeholder: 'Category' });
  const description = textarea({ rows: 3, placeholder: 'Short product description' });
  const moq = input({ placeholder: '50 units' });
  const price = input({ placeholder: 'EUR 100-150 FOB' });
  const save = button('Create product', { kind: 'primary', icon: 'plus' });
  const m = modal({
    title: 'Add product',
    body: el('div', {}, field('Name', name), field('Category', category), field('Description', description), field('MOQ', moq), field('Price band', price)),
    actions: [button('Cancel', { onClick: () => m.close() }), save],
  });
  save.addEventListener('click', async () => {
    if (!name.value.trim()) { toast('Product name is required', 'warning'); return; }
    await call('products.create', { body: { name: name.value.trim(), category: category.value.trim(), description: description.value.trim(), moq: moq.value.trim(), price_band: price.value.trim() } });
    toast('Product added', 'success');
    m.close();
  });
}

function fmtDate(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

function setupPanel(label, status, sub) {
  return el('div', { class: 'ifz-panellet ifz-col' },
    el('span', { class: 'ifz-overline' }, label),
    badge(status),
    el('span', { class: 'ifz-muted ifz-small' }, sub));
}

function setupBlock(label, body) {
  return el('div', { class: 'ifz-panellet' },
    el('div', { class: 'ifz-overline ifz-mb-4' }, label),
    body);
}
