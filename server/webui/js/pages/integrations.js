/* Integrations - provider setup and manual workflow status. */

import {
  el, card, button, pageHead, badge, dataTable, emptyState, toast, field,
  input, select, modal, setBusy, kv, fmt,
} from '../ui.js';
import { call } from '../api.js';
import { startEmailOAuth } from '../oauth-popup.js';
import { db, subscribe } from '../mocks/db.js';
import {
  providerLabel, integrationLogo, countryOptions, languageOptions, leadFor,
  contactFor, countryName,
} from './_page-utils.js';

const EMAIL_PROVIDERS = [
  { key: 'google', title: 'Google Workspace', oauth: true, logo: 'G' },
  { key: 'microsoft', title: 'Microsoft 365', oauth: true, logo: 'M' },
  { key: 'smtp', title: 'Any email (SMTP)', route: 'emailIntegrations.connectSmtp', logo: '@', credential: 'smtp' },
  { key: 'browser', title: 'Any webmail (agent browser)', route: 'emailIntegrations.connectBrowser', logo: 'W', credential: 'browser' },
];

// Common webmail SMTP/IMAP presets so the user only supplies username + password.
const SMTP_PRESETS = {
  custom: { label: 'Custom / other', smtp_host: '', smtp_port: 587, imap_host: '' },
  gmail: { label: 'Gmail (app password)', smtp_host: 'smtp.gmail.com', smtp_port: 587, imap_host: 'imap.gmail.com' },
  outlook: { label: 'Outlook / Microsoft', smtp_host: 'smtp.office365.com', smtp_port: 587, imap_host: 'outlook.office365.com' },
  yahoo: { label: 'Yahoo Mail', smtp_host: 'smtp.mail.yahoo.com', smtp_port: 465, imap_host: 'imap.mail.yahoo.com' },
  zoho: { label: 'Zoho Mail', smtp_host: 'smtp.zoho.com', smtp_port: 465, imap_host: 'imap.zoho.com' },
};

export async function mount(root, ctx) {
  let disposed = false;
  const oauthAttempts = new Map();
  const host = el('div', {});
  root.append(pageHead({
    title: 'Integrations',
    sub: 'Email, WhatsApp Business, LinkedIn actions, and lead data sources for this workspace.',
    actions: [button('Settings', { icon: 'gear', onClick: () => ctx.navigate('/app/settings') })],
  }), host);

  function connectOauth(provider) {
    oauthAttempts.get(provider.key)?.cancel();
    const attempt = startEmailOAuth({
      provider: provider.key,
      startOAuth: key => call('emailIntegrations.startOAuth', {
        params: { provider: key },
      }),
      listIntegrations: () => call('emailIntegrations.list'),
      onConnected: () => {
        oauthAttempts.delete(provider.key);
        toast(`${provider.title} connected`, 'success');
        render().catch(err => toast(err.message || 'Could not refresh integrations', 'error'));
      },
      onStatus: ({ status, error }) => {
        oauthAttempts.delete(provider.key);
        const messages = {
          blocked: ['Allow popups for this site, then try Connect again.', 'warning'],
          start_failed: [error?.message || `${provider.title} OAuth could not start`, 'error'],
          cancelled: [`${provider.title} authorization was cancelled`, 'warning'],
          failed: [`${provider.title} authorization failed. Read the popup, then try again.`, 'error'],
          expired: [`${provider.title} authorization expired. Start again.`, 'warning'],
        };
        const [message, kind] = messages[status] || [`${provider.title} authorization stopped`, 'warning'];
        toast(message, kind);
      },
    });
    if (attempt) oauthAttempts.set(provider.key, attempt);
  }

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
      emailSection(emailRes.items, render, connectOauth),
      el('div', { class: 'ifz-grid cols-2 ifz-mt-4' },
        whatsappSection(whatsapp, render),
        linkedInSection(linkedInRes.items, render)),
      el('div', { class: 'ifz-mt-4' }, dataSourceSection(sourcesRes.items)));
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => {
    disposed = true;
    unsub();
    for (const attempt of oauthAttempts.values()) attempt.cancel();
    oauthAttempts.clear();
  };
}

function emailSection(items, onChange, onOauth) {
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
            el('div', { class: 'ifz-integration-sub' }, connected ? (connected.mailbox || connected.data?.mailbox || 'Connected') : 'Not connected'),
            el('div', { class: 'ifz-mt-2' }, connected ? badge(connected.status) : badge('not_connected'))),
          el('div', { class: 'ifz-col' },
            connected ? button('Test', { size: 'sm', onClick: async () => {
              const res = await call('emailIntegrations.test', { params: { integrationId: connected.id } });
              toast(res.message, 'success');
              onChange();
            } }) : button('Connect', { size: 'sm', kind: 'primary', onClick: () => connectProvider(provider, onChange, onOauth) }),
            connected ? button('Disconnect', { size: 'sm', kind: 'danger', onClick: async () => {
              await call('emailIntegrations.delete', { params: { integrationId: connected.id } });
              toast(`${provider.title} disconnected`, 'warning');
              onChange();
            } }) : null));
      })),
  });
}

function connectProvider(provider, onChange, onOauth) {
  if (provider.oauth) return onOauth(provider);
  if (provider.credential === 'smtp') return connectSmtp(provider, onChange);
  if (provider.credential === 'browser') return connectBrowserWebmail(provider, onChange);
}

function connectSmtp(provider, onChange) {
  const presetSel = select(Object.entries(SMTP_PRESETS).map(([value, p]) => ({ value, label: p.label })),
    { value: 'gmail' });
  const username = input({ placeholder: 'you@example.com', autocomplete: 'username' });
  const password = input({ type: 'password', placeholder: 'App password or mailbox password', autocomplete: 'current-password' });
  const smtpHost = input({ value: SMTP_PRESETS.gmail.smtp_host, placeholder: 'smtp.example.com' });
  const smtpPort = input({ type: 'number', value: String(SMTP_PRESETS.gmail.smtp_port) });
  const imapHost = input({ value: SMTP_PRESETS.gmail.imap_host, placeholder: 'imap.example.com (optional, for drafts + replies)' });
  presetSel.addEventListener('change', () => {
    const p = SMTP_PRESETS[presetSel.value];
    smtpHost.value = p.smtp_host; smtpPort.value = String(p.smtp_port); imapHost.value = p.imap_host;
  });
  const save = button('Connect mailbox', { kind: 'primary', icon: 'check' });
  const dialog = modal({
    title: 'Connect any email service',
    body: el('div', {},
      el('p', { class: 'ifz-muted ifz-small ifz-mb-4' },
        'Send from any provider with your username and password. For Gmail/Outlook use an app password. Credentials are encrypted server-side and never stored in the browser.'),
      field('Email service', presetSel),
      el('div', { class: 'ifz-form-row' }, field('Username / email', username, { required: true }), field('Password', password, { required: true })),
      el('div', { class: 'ifz-form-row' }, field('SMTP host', smtpHost, { required: true }), field('SMTP port', smtpPort)),
      field('IMAP host', imapHost, { hint: 'Optional. Enables saved drafts and reply detection.' })),
    actions: save,
  });
  save.addEventListener('click', async () => {
    if (!username.value.trim() || !password.value || !smtpHost.value.trim()) {
      toast('Username, password, and SMTP host are required', 'warning');
      return;
    }
    setBusy(save, true, 'Connecting...');
    try {
      await call(provider.route, { body: { credentials: {
        username: username.value.trim(),
        password: password.value,
        smtp_host: smtpHost.value.trim(),
        smtp_port: Number(smtpPort.value) || 587,
        imap_host: imapHost.value.trim() || undefined,
        from_addr: username.value.trim(),
      } } });
      toast('Mailbox connected', 'success');
      dialog.close();
      onChange();
    } catch (err) {
      setBusy(save, false);
      toast(err.message || 'Could not connect mailbox', 'error');
    }
  });
}

function connectBrowserWebmail(provider, onChange) {
  const url = input({ placeholder: 'https://mail.example.com', type: 'url' });
  const username = input({ placeholder: 'you@example.com', autocomplete: 'username' });
  const password = input({ type: 'password', placeholder: 'Mailbox password', autocomplete: 'current-password' });
  const hint = input({ placeholder: 'e.g. Roundcube, Zimbra, Yandex (optional)' });
  const save = button('Connect webmail', { kind: 'primary', icon: 'check' });
  const dialog = modal({
    title: 'Connect webmail via agent browser',
    body: el('div', {},
      el('p', { class: 'ifz-muted ifz-small ifz-mb-4' },
        'For providers without SMTP access. The agent signs into this webmail in a browser and sends each approved message through the provider’s own UI. Sends take minutes, not seconds. Credentials are encrypted server-side. Accounts with CAPTCHA or two-factor login will fail — use SMTP or an API provider for those.'),
      field('Webmail URL', url, { required: true }),
      el('div', { class: 'ifz-form-row' },
        field('Username / email', username, { required: true }),
        field('Password', password, { required: true })),
      field('Provider hint', hint)),
    actions: save,
  });
  save.addEventListener('click', async () => {
    if (!url.value.trim() || !username.value.trim() || !password.value) {
      toast('Webmail URL, username, and password are required', 'warning');
      return;
    }
    setBusy(save, true, 'Connecting...');
    try {
      await call(provider.route, { body: { credentials: {
        webmail_url: url.value.trim(),
        username: username.value.trim(),
        password: password.value,
        provider_hint: hint.value.trim() || undefined,
      } } });
      toast('Webmail connected', 'success');
      dialog.close();
      onChange();
    } catch (err) {
      setBusy(save, false);
      toast(err.message || 'Could not connect webmail', 'error');
    }
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
