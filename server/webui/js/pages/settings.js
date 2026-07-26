/* Settings - company profile, sales preferences, and account context. */

import {
  el, card, button, pageHead, field, input, select, textarea, toast, setBusy,
  chipSelect, kv, badge,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import { countryOptions, languageOptions, ccRuleOptions, SEND_MODES, countryName } from './_page-utils.js';
import { getSession } from '../session.js';

export async function mount(root, ctx) {
  let disposed = false;
  const host = el('div', {});
  root.append(pageHead({
    title: 'Settings',
    sub: 'Workspace profile, sales preferences, and account access.',
    actions: [button('Back to dashboard', { icon: 'dashboard', onClick: () => ctx.navigate('/app/dashboard') })],
  }), host);

  async function render() {
    const [profile, prefs] = await Promise.all([
      call('company.getProfile'),
      call('company.getSalesPreferences'),
    ]);
    if (disposed) return;
    host.replaceChildren(
      el('div', { class: 'ifz-grid cols-2' },
        companyCard(profile, render),
        salesPrefsCard(prefs, render),
        authCard(),
        guardrailsCard(ctx)));
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}

function companyCard(profile, onSaved) {
  const name = input({ value: profile.name });
  const legal = input({ value: profile.legal_name });
  const website = input({ value: profile.website });
  const country = select(countryOptions(), { value: profile.headquarters_country });
  const city = input({ value: profile.city });
  const industry = input({ value: profile.industry });
  const employees = input({ value: profile.employee_count });
  const save = button('Save profile', { kind: 'primary', icon: 'check' });
  save.addEventListener('click', async () => {
    setBusy(save, true, 'Saving...');
    await call('company.updateProfile', { body: {
      name: name.value,
      legal_name: legal.value,
      website: website.value,
      headquarters_country: country.value,
      city: city.value,
      industry: industry.value,
      employee_count: employees.value,
    } });
    toast('Company profile saved', 'success');
    setBusy(save, false);
    onSaved();
  });
  return card({
    title: 'Company profile',
    body: el('div', {},
      el('div', { class: 'ifz-form-row' }, field('Company name', name), field('Legal name', legal)),
      field('Website', website),
      el('div', { class: 'ifz-form-row' }, field('Country', country), field('City', city)),
      el('div', { class: 'ifz-form-row' }, field('Industry', industry), field('Employee count', employees)),
      save),
  });
}

function salesPrefsCard(prefs, onSaved) {
  const mailbox = input({ value: prefs.connected_mailbox });
  const sendMode = select(SEND_MODES, { value: prefs.default_send_mode });
  const language = select(languageOptions(), { value: prefs.default_language });
  const languages = chipSelect(languageOptions(), prefs.languages || []);
  const ccRule = select(ccRuleOptions(), { value: prefs.default_cc_rule_id });
  const save = button('Save sales preferences', { kind: 'primary', icon: 'check' });
  save.addEventListener('click', async () => {
    setBusy(save, true, 'Saving...');
    await call('company.updateSalesPreferences', { body: {
      connected_mailbox: mailbox.value,
      default_send_mode: sendMode.value,
      default_language: language.value,
      languages: languages.getSelected(),
      default_cc_rule_id: ccRule.value,
    } });
    toast('Sales preferences saved', 'success');
    setBusy(save, false);
    onSaved();
  });
  return card({
    title: 'Sales preferences',
    body: el('div', {},
      field('Connected salesperson mailbox', mailbox, { hint: 'Used as the preferred mailbox label for outreach.' }),
      el('div', { class: 'ifz-form-row' }, field('Default send mode', sendMode), field('Default language', language)),
      field('Allowed languages', languages),
      field('Default CC rule', ccRule),
      save),
  });
}

function authCard() {
  const session = getSession();
  return card({
    title: 'Account and access',
    body: el('div', {},
      kv([
        ['User', session?.user?.name || db.user.name],
        ['Email', session?.user?.email || db.user.email],
        ['Role', session?.user?.role || db.user.role],
        ['Company', session?.company?.name || db.company.name || 'Not selected'],
      ]),
      el('div', { class: 'ifz-mt-4' },
        badge('active', 'Bearer session'),
        el('p', { class: 'ifz-muted ifz-mt-2', style: { lineHeight: 1.55 } }, 'Access is authenticated by the interfaze-agent backend and scoped to the assigned company.'))),
  });
}

function guardrailsCard(ctx) {
  const notes = textarea({ value: [
    'Lead Map selects up to five markets.',
    'Draft mode is the safe default for email.',
    'WhatsApp uses Business Platform fields only.',
    'LinkedIn remains manual: profile open, note generation, manual status update.',
  ].join('\n'), rows: 6 });
  return card({
    title: 'MVP guardrails',
    body: el('div', {},
      field('MVP guardrails', notes),
      el('div', { class: 'ifz-row wrap' },
        button('Lead Map', { icon: 'map', onClick: () => ctx.navigate('/app/lead-map') }),
        button('Custom Outreach', { icon: 'send', onClick: () => ctx.navigate('/app/custom-outreach') }),
        getSession()?.user?.role === 'admin'
          ? button('Administration', { icon: 'building', onClick: () => ctx.navigate('/admin/dashboard') })
          : null),
      el('div', { class: 'ifz-mt-4 ifz-hint' }, `Target markets: ${db.company.sales_regions_target.map(countryName).join(', ')}`)),
  });
}
