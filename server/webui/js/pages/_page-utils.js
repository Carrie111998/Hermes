import {
  el, badge, button, modal, field, input, select, textarea, setBusy, toast,
  blobDownload, csvDownload,
} from '../ui.js';
import { call } from '../api.js';
import { db } from '../mocks/db.js';
import { COUNTRY_NAMES, LANGUAGES, BUYER_INDUSTRIES } from '../catalog.js';

export const SEND_MODES = [
  { value: 'create_draft', label: 'Create drafts' },
  { value: 'approved_send', label: 'Approved send' },
];

export function countryName(code) {
  return COUNTRY_NAMES[code] || code || 'All markets';
}

export function leadFor(id) {
  return db.leads.find(l => l.id === id) || null;
}

export function contactFor(id) {
  return db.contacts.find(c => c.id === id) || null;
}

export function productFor(id) {
  return db.products.find(p => p.id === id) || null;
}

export function campaignFor(id) {
  return db.campaigns.find(c => c.id === id) || null;
}

export function ccRuleFor(id) {
  return db.ccRules.find(r => r.id === id) || db.ccRules.find(r => r.is_default) || null;
}

export function countryOptions({ includeAll = false } = {}) {
  const entries = Object.entries(COUNTRY_NAMES).map(([value, label]) => ({ value, label }));
  return includeAll ? [{ value: '', label: 'All markets' }, ...entries] : entries;
}

export function productOptions({ includeAll = false } = {}) {
  const entries = db.products.map(p => ({ value: p.id, label: p.name }));
  return includeAll ? [{ value: '', label: 'All products' }, ...entries] : entries;
}

export function languageOptions() {
  return LANGUAGES;
}

export function ccRuleOptions() {
  return db.ccRules.map(r => ({ value: r.id, label: r.name }));
}

export async function exportCsv(route) {
  const exp = await call(route, { body: { format: 'csv', filters: {} } });
  if (Array.isArray(exp?.rows)) {
    csvDownload(exp.filename, exp.rows);
    return;
  }
  if (!exp?.id) throw new Error('The export did not return a download id');
  const file = await call('exports.download', { params: { exportId: exp.id } });
  blobDownload(
    file.filename,
    file.blob,
    `Exported ${Number(exp.rows) || 0} rows to ${file.filename}`,
  );
}

export async function waitForRun(runOrId, { timeoutMs = 15000, intervalMs = 120 } = {}) {
  const runId = typeof runOrId === 'string' ? runOrId : runOrId?.run_id || runOrId?.id;
  if (!runId) return runOrId;
  const deadline = Date.now() + timeoutMs;
  let current = typeof runOrId === 'object' ? runOrId : null;
  while (Date.now() < deadline) {
    current = await call('agentRuns.get', { params: { runId } });
    if (['completed', 'failed', 'cancelled'].includes(current.status)) return current;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw new Error('Agent run is still working. Open Agent Runs to continue watching it.');
}

export function leadOptions({ includeEmpty = false } = {}) {
  const entries = db.leads.map(l => ({ value: l.id, label: `${l.company_name} - ${countryName(l.country)}` }));
  return includeEmpty ? [{ value: '', label: 'No company selected' }, ...entries] : entries;
}

export function compactList(items, { empty = 'None', class: cls = 'ifz-taglist' } = {}) {
  if (!items || !items.length) return el('span', { class: 'ifz-muted' }, empty);
  return el('div', { class: cls }, items.map(item => el('span', { class: 'ifz-tag' }, item)));
}

export function recordTitle(title, sub) {
  return el('div', {},
    el('div', { class: 'cell-strong' }, title || '-'),
    sub ? el('div', { class: 'cell-muted ifz-small' }, sub) : null);
}

export function metricCard(label, value, sub, tone = 'flat') {
  return el('div', { class: 'ifz-card ifz-stat' },
    el('div', { class: 'ifz-overline' }, label),
    el('div', { class: 'ifz-stat-value' }, value),
    sub ? el('div', { class: `ifz-stat-delta ${tone}` }, sub) : null);
}

export function messageContext(message) {
  const lead = leadFor(message.lead_id);
  const contact = contactFor(message.contact_id);
  const campaign = campaignFor(message.campaign_id);
  return { lead, contact, campaign };
}

export function emailPreview(message) {
  const { lead, contact, campaign } = messageContext(message);
  return el('div', { class: 'ifz-email-preview' },
    el('div', { class: 'ifz-email-meta' },
      el('div', {}, el('b', {}, 'To: '), el('span', {}, contact?.email || contact?.name || 'No contact selected')),
      el('div', {}, el('b', {}, 'From: '), el('span', {}, db.company.sales_preferences.connected_mailbox)),
      message.cc?.length ? el('div', {}, el('b', {}, 'CC: '), el('span', {}, message.cc.join(', '))) : null,
      el('div', {}, el('b', {}, 'Lead: '), el('span', {}, lead ? `${lead.company_name} / ${countryName(lead.country)}` : 'Manual')),
      campaign ? el('div', {}, el('b', {}, 'Campaign: '), el('span', {}, campaign.name)) : null,
      message.subject ? el('div', { class: 'ifz-email-subject' }, message.subject) : null),
    el('div', { class: 'ifz-email-body' }, message.body || 'No message body yet.'));
}

export function openMessageReviewModal(message, { onUpdated, title = 'Review message' } = {}) {
  let current = message;
  const subjectInput = input({ value: current.subject || '', placeholder: 'Subject' });
  const bodyInput = textarea({ value: current.body || '', rows: 14 });
  const statusNode = el('span', {}, badge(current.status));
  const previewHost = el('div', {}, emailPreview(current));

  async function refresh(updated) {
    current = updated || await call('messages.get', { params: { messageId: current.id } });
    statusNode.replaceChildren(badge(current.status));
    subjectInput.value = current.subject || '';
    bodyInput.value = current.body || '';
    previewHost.replaceChildren(emailPreview(current));
    if (onUpdated) onUpdated(current);
  }

  const saveBtn = button('Save edits', { icon: 'edit' });
  const regenBtn = button('Regenerate', { icon: 'refresh' });
  const approveBtn = button('Approve', { kind: 'primary', icon: 'check' });
  const draftBtn = button('Create draft', { icon: 'mail' });
  const sendBtn = button('Send now', { kind: 'primary', icon: 'send' });
  const repliedBtn = button('Mark replied', { icon: 'check' });

  const m = modal({
    title,
    wide: true,
    body: el('div', {},
      el('div', { class: 'ifz-row between ifz-mb-4' },
        el('div', { class: 'ifz-muted ifz-small' }, 'Edits are saved to this revision. Delivery still requires explicit approval.'),
        statusNode),
      field('Subject', subjectInput),
      field('Body', bodyInput),
      el('div', { class: 'ifz-row wrap ifz-mb-4' }, saveBtn, regenBtn, approveBtn, draftBtn, sendBtn, repliedBtn),
      previewHost),
    actions: [button('Close', { onClick: () => m.close() })],
  });

  saveBtn.addEventListener('click', async () => {
    setBusy(saveBtn, true, 'Saving...');
    try {
      const updated = await call('messages.update', { params: { messageId: current.id }, body: { subject: subjectInput.value, body: bodyInput.value } });
      toast('Message saved', 'success');
      await refresh(updated);
    } finally { setBusy(saveBtn, false); }
  });
  regenBtn.addEventListener('click', async () => {
    setBusy(regenBtn, true, 'Regenerating...');
    try {
      const result = await call('messages.regenerate', { params: { messageId: current.id } });
      if (result?.type) {
        const completed = await waitForRun(result);
        const updated = completed.output_ref
          ? await call('messages.get', { params: { messageId: completed.output_ref } })
          : await call('messages.get', { params: { messageId: current.id } });
        toast('Message regenerated', 'success');
        await refresh(updated);
      } else {
        toast('Message regenerated', 'success');
        await refresh(result);
      }
    } finally { setBusy(regenBtn, false); }
  });
  approveBtn.addEventListener('click', async () => {
    const updated = await call('messages.approve', { params: { messageId: current.id } });
    toast('Message approved', 'success');
    await refresh(updated);
  });
  draftBtn.addEventListener('click', async () => {
    const updated = await call('messages.createDraft', { params: { messageId: current.id } });
    toast('Draft created in mailbox', 'success');
    await refresh(updated);
  });
  sendBtn.addEventListener('click', async () => {
    const updated = await call('messages.send', { params: { messageId: current.id } });
    toast('Message marked sent', 'success');
    await refresh(updated);
  });
  repliedBtn.addEventListener('click', async () => {
    const updated = await call('messages.markReplied', { params: { messageId: current.id } });
    toast('Reply recorded', 'success');
    await refresh(updated);
  });
}

export function openContactModal({ leadId = '', onCreated } = {}) {
  const leadSelect = select(leadOptions({ includeEmpty: true }), { value: leadId });
  const nameInput = input({ placeholder: 'Anna Muller', required: true });
  const titleInput = input({ placeholder: 'Purchasing Manager' });
  const emailInput = input({ placeholder: 'anna@example.com', type: 'email' });
  const phoneInput = input({ placeholder: '+49 170 1234567' });
  const linkedinInput = input({ placeholder: 'https://www.linkedin.com/in/...' });
  const createBtn = button('Create contact', { kind: 'primary', icon: 'plus' });

  const m = modal({
    title: 'Add contact manually',
    body: el('div', {},
      field('Company', leadSelect),
      field('Full name', nameInput, { required: true }),
      field('Title', titleInput),
      field('Email', emailInput),
      field('Phone', phoneInput),
      field('LinkedIn URL', linkedinInput)),
    actions: [button('Cancel', { onClick: () => m.close() }), createBtn],
  });

  createBtn.addEventListener('click', async () => {
    if (!nameInput.value.trim()) { toast('Name is required', 'warning'); return; }
    setBusy(createBtn, true, 'Creating...');
    try {
      const contact = await call('contacts.create', { body: {
        lead_id: leadSelect.value || null,
        full_name: nameInput.value.trim(),
        title: titleInput.value.trim(),
        email: emailInput.value.trim(),
        phone: phoneInput.value.trim(),
        linkedin_url: linkedinInput.value.trim(),
      } });
      m.close();
      toast(`Contact created: ${contact.name}`, 'success');
      if (onCreated) onCreated(contact);
    } catch (err) {
      toast(err.message || 'Could not create contact', 'error');
      setBusy(createBtn, false);
    }
  });
}

export function openCampaignModal({ onCreated } = {}) {
  const nameInput = input({ value: 'New export campaign' });
  const countrySelect = select(countryOptions({ includeAll: true }), { value: 'NL' });
  const productSelect = select(productOptions(), { value: db.products[0]?.id });
  const languageSelect = select(languageOptions(), { value: 'en' });
  const modeSelect = select(SEND_MODES, { value: 'create_draft' });
  const ccSelect = select(ccRuleOptions(), { value: db.company.sales_preferences.default_cc_rule_id });
  const createBtn = button('Create campaign', { kind: 'primary', icon: 'plus' });

  const m = modal({
    title: 'Create outreach campaign',
    body: el('div', {},
      field('Campaign name', nameInput),
      el('div', { class: 'ifz-form-row' },
        field('Market', countrySelect),
        field('Language', languageSelect)),
      field('Product', productSelect),
      el('div', { class: 'ifz-form-row' },
        field('Send mode', modeSelect, { hint: 'Draft mode remains default for safety.' }),
        field('CC rule', ccSelect))),
    actions: [button('Cancel', { onClick: () => m.close() }), createBtn],
  });

  createBtn.addEventListener('click', async () => {
    setBusy(createBtn, true, 'Creating...');
    try {
      const camp = await call('campaigns.create', { body: {
        name: nameInput.value.trim() || 'New export campaign',
        lead_ids: db.leads.filter(lead => !countrySelect.value || lead.country === countrySelect.value).map(lead => lead.id),
        country: countrySelect.value || null,
        product_id: productSelect.value,
        language: languageSelect.value,
        send_mode: modeSelect.value,
        cc_rule_id: ccSelect.value,
      } });
      m.close();
      toast('Campaign created', 'success');
      if (onCreated) onCreated(camp);
    } catch (err) {
      toast(err.message || 'Could not create campaign', 'error');
      setBusy(createBtn, false);
    }
  });
}

export function leadCompanySummary(lead) {
  if (!lead) return 'No linked lead';
  return `${lead.company_name} - ${countryName(lead.country)}${lead.city ? ` / ${lead.city}` : ''}`;
}

export function industryOptions() {
  return BUYER_INDUSTRIES.map(x => ({ value: x, label: x }));
}

export function providerLabel(provider) {
  return ({
    google: 'Google Workspace',
    microsoft: 'Microsoft 365',
    stub: 'Local test mailbox',
    zoho: 'Zoho Mail',
    smtp: 'Generic SMTP',
  })[provider] || provider;
}

export function integrationLogo(text) {
  return el('span', { class: 'ifz-integration-logo' }, text);
}

export function actionButton(label, iconName, onClick, kind = '') {
  return button(label, { icon: iconName, kind, onClick });
}
