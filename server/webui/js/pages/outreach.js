/* Outreach - campaigns, approval queue, CC rules, and message review. */

import {
  el, card, badge, dataTable, button, fmt, pageHead, emptyState, toast, tabs,
  statCard, input, select, field, modal, setBusy, kv, timeline,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import {
  countryName, leadFor, contactFor, productFor, ccRuleFor, recordTitle,
  openCampaignModal, openMessageReviewModal, productOptions, countryOptions,
  languageOptions, ccRuleOptions, SEND_MODES, emailPreview, waitForRun, exportCsv,
} from './_page-utils.js';

export async function mountList(root, ctx) {
  let disposed = false;
  let activeTab = ctx.query.tab || (ctx.query.message ? 'messages' : 'campaigns');
  let openedMessage = false;
  const host = el('div', {});

  root.append(pageHead({
    title: 'Outreach',
    sub: 'Campaigns, message queue, approval controls, and market-specific CC rules.',
    actions: [
      button('Export outreach', { icon: 'download', onClick: async () => {
        await exportCsv('exports.outreach');
      } }),
      button('New campaign', { kind: 'primary', icon: 'plus', onClick: () => openCampaignModal({ onCreated: c => ctx.navigate(`/app/outreach/campaigns/${c.id}`) }) }),
    ],
  }), host);

  await Promise.all([
    call('leads.list'), call('contacts.list'), call('products.list'), call('ccRules.list'),
    call('company.getSalesPreferences'),
  ]);

  async function render() {
    if (disposed) return;
    const tabBar = tabs([
      { key: 'campaigns', label: 'Campaigns' },
      { key: 'messages', label: `Message queue (${db.messages.filter(m => ['draft_generated', 'approved'].includes(m.status)).length})` },
      { key: 'cc', label: 'CC rules' },
    ], activeTab, key => { activeTab = key; render().catch(console.error); });

    if (activeTab === 'campaigns') host.replaceChildren(tabBar, await campaignsView(ctx));
    else if (activeTab === 'messages') host.replaceChildren(tabBar, await messagesView(ctx));
    else host.replaceChildren(tabBar, await ccRulesView());

    if (ctx.query.message && !openedMessage) {
      const message = db.messages.find(m => m.id === ctx.query.message);
      if (message) {
        openedMessage = true;
        openMessageReviewModal(message, { onUpdated: () => render().catch(console.error) });
      }
    }
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

async function campaignsView(ctx) {
  const [campaignsRes, messagesRes] = await Promise.all([call('campaigns.list'), call('messages.list')]);
  const campaigns = campaignsRes.items.slice().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  const messages = messagesRes.items;

  const table = dataTable({
    columns: [
      { key: 'name', label: 'Campaign', render: c => recordTitle(c.name, `${countryName(c.country)} / ${productFor(c.product_id)?.name || 'All products'}`) },
      { key: 'status', label: 'Status', render: c => badge(c.status) },
      { key: 'mode', label: 'Mode', render: c => el('span', { class: 'ifz-tag' }, c.send_mode === 'approved_send' ? 'approved send' : 'draft mode') },
      { key: 'queue', label: 'Queue', render: c => {
        const queue = messages.filter(m => m.campaign_id === c.id);
        const ready = queue.filter(m => ['draft_generated', 'approved'].includes(m.status)).length;
        return el('span', {}, `${ready}/${queue.length} ready`);
      } },
      { key: 'replies', label: 'Replies', render: c => el('span', { class: 'ifz-strong' }, c.stats?.replied || 0) },
      { key: 'created', label: 'Created', render: c => el('span', { class: 'cell-muted ifz-nowrap' }, fmt.ago(c.created_at)) },
    ],
    rows: campaigns,
    onRowClick: c => ctx.navigate(`/app/outreach/campaigns/${c.id}`),
    empty: emptyState({ icon: 'mail', title: 'No campaigns yet', hint: 'Create a campaign from scanned leads.', action: button('New campaign', { kind: 'primary', icon: 'plus', onClick: () => openCampaignModal({ onCreated: c => ctx.navigate(`/app/outreach/campaigns/${c.id}`) }) }) }),
  });

  const sent = messages.filter(m => ['sent', 'replied'].includes(m.status)).length;
  const replies = messages.filter(m => m.status === 'replied').length;
  return el('div', {},
    el('div', { class: 'ifz-grid cols-4 ifz-mb-4' },
      statCard({ label: 'Campaigns', value: String(campaigns.length), delta: `${campaigns.filter(c => !['completed', 'cancelled'].includes(c.status)).length} active`, deltaDir: 'flat' }),
      statCard({ label: 'Awaiting approval', value: String(messages.filter(m => m.status === 'draft_generated').length), delta: 'review before send', deltaDir: 'flat' }),
      statCard({ label: 'Sent or drafted', value: String(sent + messages.filter(m => m.status === 'draft_created').length), delta: 'mailbox activity', deltaDir: 'up' }),
      statCard({ label: 'Replies', value: String(replies), delta: sent ? `${Math.round((replies / sent) * 100)}% reply rate` : 'no sent email', deltaDir: replies ? 'up' : 'flat' })),
    card({ flush: true, body: table }));
}

async function messagesView() {
  const res = await call('messages.list');
  let statusFilter = '';
  let channelFilter = '';
  let q = '';
  const host = el('div', {});

  function renderMessages() {
    let rows = res.items.slice();
    if (statusFilter) rows = rows.filter(m => m.status === statusFilter);
    if (channelFilter) rows = rows.filter(m => m.channel === channelFilter);
    if (q) {
      const needle = q.toLowerCase();
      rows = rows.filter(m => (m.subject || '').toLowerCase().includes(needle) || (leadFor(m.lead_id)?.company_name || '').toLowerCase().includes(needle));
    }
    const chip = (label, on, fn) => el('button', { class: `ifz-filter-chip${on ? ' on' : ''}`, onclick: fn }, label);
    const search = input({ type: 'search', placeholder: 'Search subject or company', value: q });
    search.classList.add('ifz-filter-search');
    search.addEventListener('input', () => { q = search.value; renderMessages(); });
    const filters = el('div', { class: 'ifz-filters' },
      search,
      chip('All', !statusFilter && !channelFilter, () => { statusFilter = ''; channelFilter = ''; renderMessages(); }),
      ['draft_generated', 'approved', 'draft_created', 'sent', 'replied'].map(s => chip(s.replace(/_/g, ' '), statusFilter === s, () => { statusFilter = statusFilter === s ? '' : s; renderMessages(); })),
      el('span', { class: 'ifz-filter-spacer', 'aria-hidden': 'true' }),
      ['email', 'whatsapp'].map(ch => chip(ch, channelFilter === ch, () => { channelFilter = channelFilter === ch ? '' : ch; renderMessages(); })));

    const table = dataTable({
      columns: [
        { key: 'subject', label: 'Message', render: m => {
          const lead = leadFor(m.lead_id);
          return recordTitle(m.subject || '(WhatsApp message)', lead ? `${lead.company_name} / ${countryName(lead.country)}` : 'Manual outreach');
        } },
        { key: 'contact', label: 'Contact', render: m => {
          const c = contactFor(m.contact_id);
          return c ? recordTitle(c.name, c.email) : el('span', { class: 'cell-muted' }, 'No contact');
        } },
        { key: 'status', label: 'Status', render: m => badge(m.status) },
        { key: 'channel', label: 'Channel', render: m => el('span', { class: 'ifz-tag' }, m.channel) },
        { key: 'cc', label: 'CC', render: m => el('span', { class: 'cell-muted' }, (m.cc || []).length ? m.cc.join(', ') : '-') },
        { key: 'created', label: 'Created', render: m => el('span', { class: 'cell-muted ifz-nowrap' }, fmt.ago(m.created_at)) },
      ],
      rows: rows.sort((a, b) => new Date(b.created_at) - new Date(a.created_at)),
      onRowClick: m => openMessageReviewModal(m, { onUpdated: renderMessages }),
      empty: emptyState({ icon: 'mail', title: 'No messages match', hint: 'Generate messages from a campaign, lead detail, or custom outreach.' }),
    });
    host.replaceChildren(filters, card({ flush: true, body: table }),
      el('div', { class: 'ifz-hint ifz-mt-2' }, `${rows.length} message${rows.length === 1 ? '' : 's'}`));
  }
  renderMessages();
  return host;
}

async function ccRulesView() {
  const res = await call('ccRules.list');
  const host = el('div', {});

  function openRuleModal(rule = null) {
    const nameInput = input({ value: rule?.name || 'New market rule' });
    const marketSelect = select(countryOptions({ includeAll: true }), { value: rule?.market_country || '' });
    const regionInput = input({ value: rule?.market_region || '', placeholder: 'GCC, EU, Benelux...' });
    const productSelect = select(productOptions({ includeAll: true }), { value: rule?.product_id || '' });
    const industryInput = input({ value: rule?.industry || '', placeholder: 'Hotel equipment supplier' });
    const ccInput = input({ value: (rule?.cc_emails || []).join(', '), placeholder: 'export@company.com, region@company.com' });
    const saveBtn = button(rule ? 'Save rule' : 'Create rule', { kind: 'primary', icon: 'check' });
    const m = modal({
      title: rule ? 'Edit CC rule' : 'Create CC rule',
      body: el('div', {},
        field('Name', nameInput),
        el('div', { class: 'ifz-form-row' },
          field('Market country', marketSelect),
          field('Market region', regionInput)),
        field('Product', productSelect),
        field('Industry', industryInput),
        field('CC emails', ccInput, { hint: 'Comma-separated email addresses.' })),
      actions: [button('Cancel', { onClick: () => m.close() }), saveBtn],
    });
    saveBtn.addEventListener('click', async () => {
      const body = {
        name: nameInput.value.trim(),
        market_country: marketSelect.value || null,
        market_region: regionInput.value.trim() || null,
        product_id: productSelect.value || null,
        industry: industryInput.value.trim() || null,
        cc_emails: ccInput.value.split(',').map(x => x.trim()).filter(Boolean),
      };
      if (rule) await call('ccRules.update', { params: { ruleId: rule.id }, body });
      else await call('ccRules.create', { body });
      toast('CC rule saved', 'success');
      m.close();
      location.hash = '#/app/outreach?tab=cc';
    });
  }

  const table = dataTable({
    columns: [
      { key: 'name', label: 'Rule', render: r => recordTitle(r.name, r.is_default ? 'Default rule' : [r.market_region, countryName(r.market_country), productFor(r.product_id)?.name, r.industry].filter(Boolean).join(' / ')) },
      { key: 'emails', label: 'CC emails', render: r => el('span', { class: 'cell-muted' }, r.cc_emails.join(', ') || '-') },
      { key: 'scope', label: 'Scope', render: r => el('span', {}, r.market_country ? countryName(r.market_country) : r.market_region || 'All') },
      { key: 'actions', label: '', width: '120px', render: r => el('div', { class: 'ifz-row' },
        button('Edit', { size: 'sm', icon: 'edit', onClick: () => openRuleModal(r) }),
        r.is_default ? null : button('Delete', { size: 'sm', kind: 'danger', icon: 'trash', onClick: async () => {
          await call('ccRules.delete', { params: { ruleId: r.id } });
          toast('CC rule deleted', 'warning');
          location.hash = '#/app/outreach?tab=cc';
        } })) },
    ],
    rows: res.items,
    empty: emptyState({ icon: 'mail', title: 'No CC rules yet' }),
  });

  host.replaceChildren(
    card({
      title: 'Market-specific CC rules',
      actions: button('Add rule', { kind: 'primary', icon: 'plus', onClick: () => openRuleModal() }),
      body: table,
      flush: true,
    }));
  return host;
}

export async function mountDetail(root, ctx) {
  let disposed = false;
  const campaignId = ctx.params.campaignId;
  const host = el('div', {});
  root.append(host);

  await Promise.all([
    call('leads.list'), call('contacts.list'), call('products.list'), call('ccRules.list'),
    call('company.getSalesPreferences'),
  ]);

  async function render() {
    let campaign;
    try {
      campaign = await call('campaigns.get', { params: { campaignId } });
    } catch {
      host.replaceChildren(emptyState({
        icon: 'mail',
        title: 'Campaign not found',
        action: button('All campaigns', { kind: 'primary', onClick: () => ctx.navigate('/app/outreach') }),
      }));
      return;
    }
    const [messagesRes, activityRes] = await Promise.all([
      call('messages.list', { query: { campaign_id: campaignId } }),
      call('activity.forCampaign', { params: { campaignId } }),
    ]);
    if (disposed) return;
    const messages = messagesRes.items.slice().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    const ready = messages.filter(m => ['draft_generated', 'approved'].includes(m.status)).length;
    const product = productFor(campaign.product_id);
    const ccRule = ccRuleFor(campaign.cc_rule_id);

    const generateBtn = button('Generate messages', { icon: 'sparkle', onClick: async () => {
      const res = await call('campaigns.generateMessages', { params: { campaignId } });
      if (res.items?.some(item => item.type)) {
        await Promise.all(res.items.map(item => waitForRun(item)));
      }
      toast(`Generated ${res.total} messages`, 'success');
      render();
    } });
    const approveBtn = button('Approve queue', { icon: 'check', onClick: async () => {
      await call('campaigns.approve', { params: { campaignId } });
      toast('Campaign approved', 'success');
      render();
    } });
    const sendBtn = button(campaign.send_mode === 'create_draft' ? 'Create drafts' : 'Send approved', { kind: 'primary', icon: campaign.send_mode === 'create_draft' ? 'mail' : 'send', onClick: async () => {
      await call('campaigns.send', {
        params: { campaignId },
        body: { mode: campaign.send_mode === 'approved_send' ? 'send' : 'draft' },
      });
      toast(campaign.send_mode === 'approved_send' ? 'Approved messages sent' : 'Mailbox drafts created', 'success');
      render();
    }, disabled: ready === 0 });
    const settingsBtn = button('Edit setup', { icon: 'edit', onClick: () => openCampaignSettings(campaign, render) });

    const table = dataTable({
      columns: [
        { key: 'subject', label: 'Message', render: m => {
          const lead = leadFor(m.lead_id);
          return recordTitle(m.subject || '(WhatsApp message)', lead ? `${lead.company_name} / ${countryName(lead.country)}` : 'Manual');
        } },
        { key: 'contact', label: 'Contact', render: m => {
          const c = contactFor(m.contact_id);
          return c ? recordTitle(c.name, c.email) : el('span', { class: 'cell-muted' }, 'No contact');
        } },
        { key: 'status', label: 'Status', render: m => badge(m.status) },
        { key: 'cc', label: 'CC', render: m => el('span', { class: 'cell-muted' }, (m.cc || []).join(', ') || '-') },
        { key: 'actions', label: '', width: '90px', render: m => button('Review', { size: 'sm', icon: 'eye', onClick: () => openMessageReviewModal(m, { onUpdated: render }) }) },
      ],
      rows: messages,
      onRowClick: m => openMessageReviewModal(m, { onUpdated: render }),
      empty: emptyState({ icon: 'mail', title: 'No messages generated', hint: 'Generate campaign messages from qualified leads in this market.' }),
    });

    host.replaceChildren(
      pageHead({
        title: campaign.name,
        sub: `${countryName(campaign.country)} / ${product?.name || 'All products'} / ${campaign.language.toUpperCase()}`,
        actions: [button('Back', { icon: 'arrowLeft', onClick: () => ctx.navigate('/app/outreach') }), settingsBtn, generateBtn, approveBtn, sendBtn],
      }),
      el('div', { class: 'ifz-grid cols-4 ifz-mb-4' },
        statCard({ label: 'Status', value: campaign.status.replace(/_/g, ' '), delta: campaign.send_mode === 'create_draft' ? 'draft mode' : 'approved send', deltaDir: 'flat' }),
        statCard({ label: 'Messages', value: String(messages.length), delta: `${ready} ready`, deltaDir: ready ? 'up' : 'flat' }),
        statCard({ label: 'Replies', value: String(messages.filter(m => m.status === 'replied').length), delta: `${messages.filter(m => ['sent', 'replied'].includes(m.status)).length} sent`, deltaDir: 'flat' }),
        statCard({ label: 'CC rule', value: ccRule?.name || '-', delta: ccRule?.cc_emails?.join(', ') || 'No CC', deltaDir: 'flat' })),
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        card({ title: 'Campaign setup', body: kv([
          ['Market', countryName(campaign.country)],
          ['Product', product?.name],
          ['Language', campaign.language.toUpperCase()],
          ['Send mode', campaign.send_mode === 'create_draft' ? 'Create drafts' : 'Approved send'],
          ['CC rule', ccRule?.name],
        ]) }),
        card({ title: 'Activity', body: activityRes.items.length
          ? timeline(activityRes.items.map(a => ({ label: a.label, time: fmt.ago(a.at), tone: a.kind === 'agent' ? 'accent' : 'success' })))
          : emptyState({ icon: 'clock', title: 'No campaign activity yet' }) }),
        card({ title: 'Approval posture', body: el('div', { class: 'ifz-col' },
          badge(campaign.send_mode === 'create_draft' ? 'draft_created' : 'approved', campaign.send_mode === 'create_draft' ? 'Drafts first' : 'Approved send'),
          el('p', { class: 'ifz-muted', style: { lineHeight: 1.55 } }, 'Every message remains revision-bound and requires approval before the configured provider creates a draft or sends it.')) })),
      card({ title: 'Message queue', flush: true, body: table }),
      messages[0] ? el('div', { class: 'ifz-mt-4' }, card({ title: 'Latest preview', body: emailPreview(messages[0]) })) : null);
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function openCampaignSettings(campaign, onSaved) {
  const nameInput = input({ value: campaign.name });
  const countrySelect = select(countryOptions({ includeAll: true }), { value: campaign.country || '' });
  const productSelect = select(productOptions(), { value: campaign.product_id || db.products[0]?.id });
  const languageSelect = select(languageOptions(), { value: campaign.language || 'en' });
  const modeSelect = select(SEND_MODES, { value: campaign.send_mode || 'create_draft' });
  const ccSelect = select(ccRuleOptions(), { value: campaign.cc_rule_id || db.company.sales_preferences.default_cc_rule_id });
  const saveBtn = button('Save campaign', { kind: 'primary', icon: 'check' });
  const m = modal({
    title: 'Campaign setup',
    body: el('div', {},
      field('Name', nameInput),
      el('div', { class: 'ifz-form-row' },
        field('Market', countrySelect),
        field('Language', languageSelect)),
      field('Product', productSelect),
      el('div', { class: 'ifz-form-row' },
        field('Send mode', modeSelect),
        field('CC rule', ccSelect))),
    actions: [button('Cancel', { onClick: () => m.close() }), saveBtn],
  });
  saveBtn.addEventListener('click', async () => {
    setBusy(saveBtn, true, 'Saving...');
    await call('campaigns.update', { params: { campaignId: campaign.id }, body: {
      name: nameInput.value.trim() || campaign.name,
      country: countrySelect.value || null,
      product_id: productSelect.value,
      language: languageSelect.value,
      send_mode: modeSelect.value,
      cc_rule_id: ccSelect.value,
    } });
    toast('Campaign updated', 'success');
    m.close();
    if (onSaved) onSaved();
  });
}
