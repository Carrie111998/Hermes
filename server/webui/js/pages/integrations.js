/* Integrations - provider setup and manual workflow status. */

import {
  el, card, button, pageHead, badge, dataTable, emptyState, toast, field,
  input, select, modal, setBusy, kv, fmt,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import {
  providerLabel, integrationLogo, countryOptions, languageOptions, leadFor,
  contactFor, countryName,
} from './_page-utils.js';

const EMAIL_PROVIDERS = [
  { key: 'google', title: 'Google Workspace', route: 'emailIntegrations.connectGoogle', logo: 'G' },
  { key: 'microsoft', title: 'Microsoft 365', route: 'emailIntegrations.connectMicrosoft', logo: 'M' },
];

export async function mount(root, ctx) {
  let disposed = false;
  const host = el('div', {});
  root.append(pageHead({
    title: 'Integrations',
    sub: 'Email, WhatsApp Business, LinkedIn actions, and lead data sources for this workspace.',
    actions: [button('Settings', { icon: 'gear', onClick: () => ctx.navigate('/app/settings') })],
  }), host);

  async function render() {
    const [emailRes, whatsappRes, linkedInRes, sourcesRes, runtime] = await Promise.all([
      call('emailIntegrations.list'),
      call('whatsapp.integrations'),
      call('linkedin.actions'),
      call('dataSources.list'),
      fetch('/health').then(response => response.ok ? response.json() : null).catch(() => null),
    ]);
    if (disposed) return;
    const whatsapp = whatsappRes.items[0] || null;
    host.replaceChildren(
      el('div', { class: 'ifz-grid cols-4 ifz-mb-4' },
        card({ body: kv([
          ['Email integrations', emailRes.total],
          ['Connected mailbox', db.company.sales_preferences.connected_mailbox],
          ['Default send mode', db.company.sales_preferences.default_send_mode],
        ]) }),
        card({ body: kv([
          ['WhatsApp status', whatsapp ? (whatsapp.profile_state === 'verified' ? 'profile verified' : 'profile saved') : 'not connected'],
          ['Template status', whatsapp?.template_status || '-'],
          ['Messages', db.messages.filter(m => m.channel === 'whatsapp').length],
        ]) }),
        card({ body: kv([
          ['LinkedIn actions', linkedInRes.total],
          ['Automation policy', 'manual only'],
          ['Profiles opened', linkedInRes.items.filter(a => ['opened', 'connection_sent', 'connected', 'replied'].includes(a.status)).length],
        ]) }),
        agentAdapterSection(runtime)),
      emailSection(emailRes.items, render),
      el('div', { class: 'ifz-grid cols-2 ifz-mt-4' },
        whatsappSection(whatsapp, render),
        linkedInSection(linkedInRes.items, render)),
      el('div', { class: 'ifz-mt-4' }, dataSourceSection(sourcesRes.items)));
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function emailSection(items, onChange) {
  const providers = items.some(item => item.provider === 'stub')
    ? [{ key: 'stub', title: 'Local test mailbox', route: null, logo: 'T' }, ...EMAIL_PROVIDERS]
    : EMAIL_PROVIDERS;
  return card({
    title: 'Email providers',
    body: el('div', { class: 'ifz-grid cols-4' },
      providers.map(provider => {
        const connected = items.find(i => i.provider === provider.key);
        return el('div', { class: 'ifz-integration-card ifz-panellet' },
          integrationLogo(provider.logo),
          el('div', { class: 'ifz-integration-body' },
            el('div', { class: 'ifz-integration-name' }, provider.title),
            el('div', { class: 'ifz-integration-sub' }, connected ? connected.mailbox : 'Not connected'),
            el('div', { class: 'ifz-mt-2' }, connected ? badge(connected.status) : badge('not_connected'))),
          el('div', { class: 'ifz-col' },
            connected ? button('Test', { size: 'sm', onClick: async () => {
              const res = await call('emailIntegrations.test', { params: { integrationId: connected.id } });
              toast(res.message, 'success');
              onChange();
            } }) : button('Connect', { size: 'sm', kind: 'primary', onClick: () => connectProvider(provider, onChange) }),
            connected ? button('Disconnect', { size: 'sm', kind: 'danger', onClick: async () => {
              await call('emailIntegrations.delete', { params: { integrationId: connected.id } });
              toast(`${provider.title} disconnected`, 'warning');
              onChange();
            } }) : null));
      })),
  });
}

function connectProvider(provider, onChange) {
  call(provider.route).then(() => {
    toast(`${provider.title} connected`, 'success');
    onChange();
  }).catch(err => {
    toast(err.message || `${provider.title} is not available on this server`, 'error');
  });
}

function whatsappSection(whatsapp, onChange) {
  const business = input({ value: whatsapp?.business_name || db.company.name, autocomplete: 'organization' });
  const waba = input({ value: whatsapp?.whatsapp_business_account_id || '', placeholder: 'WhatsApp Business Account ID' });
  const phoneId = input({ value: whatsapp?.phone_number_id || '', placeholder: 'Phone number ID' });
  const display = input({ value: whatsapp?.display_phone_number || '', placeholder: '+90 212 555 0101', type: 'tel' });
  const country = select(countryOptions(), { value: whatsapp?.business_country || 'TR' });
  const language = select(languageOptions(), { value: whatsapp?.default_language || 'en' });
  const save = button(whatsapp ? 'Save profile' : 'Save Business profile', { kind: 'primary', icon: 'check' });
  const verify = button('Verify profile', { icon: 'check', disabled: !whatsapp });
  const edit = button(whatsapp ? 'Edit profile' : 'Add Business profile', { icon: whatsapp ? 'edit' : 'plus' });
  const editor = el('div', { class: 'ifz-wa-profile-editor', hidden: Boolean(whatsapp) },
    el('div', { class: 'ifz-form-row' },
      field('Business display name', business, { required: true }),
      field('Display phone number', display)),
    el('div', { class: 'ifz-form-row' },
      field('WhatsApp Business Account ID', waba, { required: true }),
      field('Phone number ID', phoneId, { required: true })),
    el('div', { class: 'ifz-form-row' },
      field('Business country', country),
      field('Default template language', language)),
    el('div', { class: 'ifz-row wrap ifz-mt-4' }, save, button('Cancel', {
      onClick: () => { editor.hidden = true; edit.focus(); },
    })));

  const profileState = whatsapp?.profile_state || 'not_connected';
  const profileSummary = whatsapp
    ? el('div', { class: 'ifz-wa-profile-summary' },
        el('div', { class: 'ifz-row wrap' },
          badge(profileState === 'verified' ? 'verified' : 'pending', profileState === 'verified' ? 'Profile verified' : 'Profile saved'),
          badge('not_connected', 'Server credentials required')),
        kv([
          ['Business', whatsapp.business_name],
          ['WABA ID', whatsapp.whatsapp_business_account_id || 'Not provided'],
          ['Phone number ID', whatsapp.phone_number_id || 'Not provided'],
          ['Display phone', whatsapp.display_phone_number || 'Not provided'],
          ['Template status', whatsapp.template_status || 'Not configured'],
        ]),
        whatsapp.verification
          ? el('div', { class: 'ifz-wa-verification', role: 'status' }, whatsapp.verification.message)
          : null)
    : emptyState({
      icon: 'whatsapp',
      title: 'No WhatsApp Business profile',
      hint: 'Save Business Account details now. Permanent tokens and webhook secrets are configured later on the server.',
    });

  save.addEventListener('click', async () => {
    if (!business.value.trim() || !waba.value.trim() || !phoneId.value.trim()) {
      toast('Business name, Account ID, and Phone number ID are required', 'warning');
      return;
    }
    setBusy(save, true, 'Saving...');
    try {
      await call('whatsapp.saveProfile', { body: {
        business_name: business.value,
        whatsapp_business_account_id: waba.value,
        phone_number_id: phoneId.value,
        display_phone_number: display.value,
        business_country: country.value,
        default_language: language.value,
      } });
      toast('WhatsApp Business profile saved', 'success');
      onChange();
    } catch (err) {
      setBusy(save, false);
      toast(err.message || 'Could not save profile', 'error');
    }
  });
  verify.addEventListener('click', async () => {
    setBusy(verify, true, 'Verifying...');
    try {
      const result = await call('whatsapp.verifyProfile');
      toast(result.message, 'success');
      onChange();
    } catch (err) {
      setBusy(verify, false);
      toast(err.message || 'Could not verify profile', 'error');
    }
  });
  edit.addEventListener('click', () => {
    editor.hidden = !editor.hidden;
    if (!editor.hidden) business.focus();
  });

  return card({
    title: 'WhatsApp Business profile',
    actions: [edit, verify],
    body: el('div', { class: 'ifz-wa-profile' },
      profileSummary,
      el('div', { class: 'ifz-wa-credential-note' },
        'Connection credentials, permanent access tokens, and webhook secrets are configured server-side. They are never entered or stored in this browser.'),
      editor),
  });
}

function agentAdapterSection(runtime) {
  const available = runtime?.agent_runs_enabled === true;
  return card({
    title: 'Agent connection',
    body: el('div', { class: 'ifz-agent-readiness' },
      badge(available ? 'active' : 'not_connected', available ? 'Hermes backend ready' : 'Hermes command unavailable'),
      el('div', { class: 'ifz-agent-readiness-copy' },
        available
          ? 'Agent-backed runs are available through the tenant-scoped workspace backend.'
          : 'Install or expose the hermes command before starting agent-backed work.'),
      el('div', { class: 'ifz-hint' }, available
        ? 'Model and provider readiness is validated when a run starts.'
        : 'Historical tenant data remains available while the runtime is offline.')),
  });
}

function linkedInSection(items, onChange) {
  return card({
    title: 'LinkedIn manual workflow',
    flush: items.length > 0,
    body: items.length ? dataTable({
      columns: [
        { key: 'contact', label: 'Contact', render: a => {
          const contact = contactFor(a.contact_id);
          const lead = leadFor(a.lead_id);
          return el('div', {}, el('div', { class: 'cell-strong' }, contact?.name || 'Unknown contact'), el('div', { class: 'cell-muted ifz-small' }, lead ? `${lead.company_name} / ${countryName(lead.country)}` : 'No lead'));
        } },
        { key: 'status', label: 'Status', render: a => badge(a.status) },
        { key: 'note', label: 'Note', render: a => el('span', { class: 'cell-muted' }, a.note || 'No note') },
        { key: 'actions', label: '', width: '170px', render: a => el('div', { class: 'ifz-row wrap' },
          a.profile_url ? el('a', { href: a.profile_url, target: '_blank', rel: 'noopener', onclick: e => e.stopPropagation() }, 'Open') : null,
          button('Sent', { size: 'sm', onClick: async () => { await call('linkedin.markConnectionSent', { params: { actionId: a.id } }); toast('LinkedIn status updated', 'success'); onChange(); } }),
          button('Connected', { size: 'sm', onClick: async () => { await call('linkedin.markConnected', { params: { actionId: a.id } }); toast('LinkedIn status updated', 'success'); onChange(); } })) },
      ],
      rows: items,
    }) : emptyState({ icon: 'linkedin', title: 'No LinkedIn actions yet', hint: 'Lead/contact pages can find profiles and generate manual notes.' }),
  });
}

function dataSourceSection(items) {
  return card({
    title: 'Lead discovery data sources',
    flush: true,
    body: dataTable({
      columns: [
        { key: 'label', label: 'Source', render: s => s.label },
        { key: 'status', label: 'Status', render: s => badge(s.status === 'enabled' ? 'active' : 'pending', s.status) },
        { key: 'type', label: 'Type', render: s => el('span', { class: 'ifz-tag' }, s.type.replace(/_/g, ' ')) },
        { key: 'test', label: '', width: '90px', render: s => button('Test', { size: 'sm', onClick: async () => {
          const res = await call('dataSources.test', { params: { sourceId: s.id } });
          toast(`Source test passed (${res.latency_ms}ms)`, 'success');
        } }) },
      ],
      rows: items,
    }),
  });
}
