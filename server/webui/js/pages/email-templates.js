/* Email Templates - language-keyed subject/body used to seed outreach drafts. */

import {
  el, card, button, pageHead, field, input, textarea, toast, setBusy, tabs, badge,
} from '../ui.js';
import { call } from '../api.js';
import { languageOptions } from './_page-utils.js';

const PLACEHOLDERS = ['{{company_name}}', '{{contact_name}}', '{{contact_title}}', '{{country}}'];

export async function mount(root, ctx) {
  const host = el('div', {});
  root.append(pageHead({
    title: 'Email Templates',
    sub: 'Per-language subject and body for outreach. The agent fills placeholders and runs preflight QA before anything is approved or sent.',
    actions: [button('Integrations', { icon: 'plug', onClick: () => ctx.navigate('/app/integrations') })],
  }), host);

  const languages = languageOptions();
  const section = await call('company.getEmailTemplates');
  const templates = { ...(section?.data?.templates || {}) };
  let active = languages[0].value;

  function render() {
    const current = templates[active] || { subject: '', body: '' };
    const subject = input({ value: current.subject || '', placeholder: 'Partnership opportunity' });
    const body = textarea({ value: current.body || '', rows: 12,
      placeholder: 'Hello {{contact_name}}, we would like to explore a partnership with {{company_name}}...' });
    const save = button('Save template', { kind: 'primary', icon: 'check' });

    save.addEventListener('click', async () => {
      setBusy(save, true, 'Saving...');
      templates[active] = { subject: subject.value.trim(), body: body.value.trim() };
      try {
        await call('company.updateEmailTemplates', { body: { templates } });
        toast(`${labelFor(active)} template saved`, 'success');
        render();
      } catch (err) {
        setBusy(save, false);
        toast(err.message || 'Could not save template', 'error');
      }
    });

    const filled = languages.filter(l => templates[l.value]?.subject || templates[l.value]?.body).length;
    host.replaceChildren(card({
      title: 'Templates by language',
      actions: [badge('active', `${filled}/${languages.length} languages`)],
      body: el('div', {},
        tabs(languages.map(l => ({ key: l.value, label: l.label })), active, key => { active = key; render(); }),
        el('div', { class: 'ifz-mt-4' },
          field('Subject', subject, { hint: 'A fixed subject line, if your company enforces one, must match this exactly.' }),
          field('Body', body)),
        el('div', { class: 'ifz-mt-4' },
          el('div', { class: 'ifz-overline ifz-mb-4' }, 'Placeholders'),
          el('div', { class: 'ifz-row wrap' }, PLACEHOLDERS.map(p =>
            el('code', { class: 'ifz-tag' }, p)))),
        el('div', { class: 'ifz-row ifz-mt-4' }, save)),
    }));
  }

  function labelFor(value) {
    return languages.find(l => l.value === value)?.label || value;
  }

  render();
}
