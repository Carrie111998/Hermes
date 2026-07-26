/* Custom outreach - manual lead -> contact -> research -> email approval. */

import {
  el, card, button, pageHead, stepper, field, input, select, textarea, toast,
  setBusy, badge, emptyState, kv,
} from '../ui.js';
import { call, config } from '../api.js';
import { db } from '../mocks/db.js';
import {
  countryOptions, industryOptions, productOptions, languageOptions, ccRuleOptions,
  emailPreview, countryName, leadCompanySummary, waitForRun,
} from './_page-utils.js';

const STEPS = [
  { key: 'lead', label: 'Lead' },
  { key: 'contact', label: 'Contact' },
  { key: 'research', label: 'Research' },
  { key: 'email', label: 'Email' },
  { key: 'send', label: 'Approve' },
];

export async function mount(root, ctx) {
  let active = 0;
  const state = { lead: null, contact: null, research: null, researchRunId: null, message: null, result: null };
  const host = el('div', {});

  root.append(pageHead({
    title: 'Custom Outreach',
    sub: 'Create a one-off lead, add or find a contact, generate a cold email, then create a draft or send after approval.',
    actions: [button('All leads', { icon: 'leads', onClick: () => ctx.navigate('/app/leads') })],
  }), host);

  function go(i) { active = Math.max(0, Math.min(STEPS.length - 1, i)); render(); }

  function render() {
    const progress = stepper(
      STEPS.map((s, i) => ({
        label: s.label,
        done: i < active || (i === 0 && state.lead) || (i === 1 && state.contact) || (i === 3 && state.message),
      })),
      active,
      { onStep: go },
    );
    const leadSummary = state.lead
      ? el('div', { class: 'ifz-mt-4' }, card({
          title: 'Current lead',
          body: kv([
            ['Company', state.lead.company_name],
            ['Market', countryName(state.lead.country)],
            ['Industry', state.lead.industry],
            ['Status', state.lead.status],
          ]),
        }))
      : null;
    const side = el('aside', { class: 'ifz-wizard-side' }, card({ body: progress }), leadSummary);
    const main = el('main', { class: 'ifz-wizard-main' }, renderStep());
    host.replaceChildren(el('div', { class: 'ifz-wizard' }, side, main));
  }

  function renderStep() {
    if (active === 0) return leadStep(go);
    if (active === 1) return contactStep(go);
    if (active === 2) return researchStep(go);
    if (active === 3) return emailStep(go);
    return sendStep();
  }

  function leadStep(next) {
    const nameInput = input({ value: state.lead?.company_name || '', placeholder: 'Example Appliances GmbH' });
    const websiteInput = input({ value: state.lead?.website || '', placeholder: 'https://example.com' });
    const countrySelect = select(countryOptions(), { value: state.lead?.country || 'DE' });
    const cityInput = input({ value: state.lead?.city || '', placeholder: 'Berlin' });
    const industrySelect = select(industryOptions(), { value: state.lead?.industry || 'Kitchen appliance importer' });
    const createBtn = button(state.lead ? 'Update lead' : 'Create lead', { kind: 'primary', icon: 'plus' });

    createBtn.addEventListener('click', async () => {
      if (!nameInput.value.trim()) { toast('Company name is required', 'warning'); return; }
      setBusy(createBtn, true, 'Creating...');
      try {
        if (state.lead) {
          state.lead = await call('leads.update', { params: { leadId: state.lead.id }, body: {
            company_name: nameInput.value.trim(),
            website: websiteInput.value.trim(),
            country: countrySelect.value,
            city: cityInput.value.trim(),
            industry: industrySelect.value,
          } });
        } else {
          state.lead = await call('leads.create', { body: {
            company_name: nameInput.value.trim(),
            website: websiteInput.value.trim(),
            country: countrySelect.value,
            city: cityInput.value.trim(),
            industry: industrySelect.value,
          } });
        }
        toast('Lead saved', 'success');
        next(1);
      } catch (err) {
        toast(err.message || 'Could not save lead', 'error');
        setBusy(createBtn, false);
      }
    });

    return card({
      title: '1. Create the custom lead',
      body: el('div', {},
        el('div', { class: 'ifz-form-row' },
          field('Company name', nameInput, { required: true }),
          field('Website', websiteInput)),
        el('div', { class: 'ifz-form-row' },
          field('Country', countrySelect),
          field('City', cityInput)),
        field('Industry', industrySelect),
        el('div', { class: 'ifz-row' }, createBtn)),
    });
  }

  function contactStep(next) {
    if (!state.lead) return emptyState({ icon: 'leads', title: 'Create a lead first' });
    const existing = db.contacts.filter(c => c.lead_id === state.lead.id);
    const nameInput = input({ value: state.contact?.name || '', placeholder: 'Anna Muller' });
    const titleInput = input({ value: state.contact?.title || '', placeholder: 'Purchasing Manager' });
    const emailInput = input({ value: state.contact?.email || '', placeholder: 'anna@example.com' });
    const linkedinInput = input({ value: state.contact?.linkedin_url || '', placeholder: 'https://www.linkedin.com/in/...' });
    const saveBtn = button(state.contact ? 'Update contact' : 'Save contact', { kind: 'primary', icon: 'contact' });
    const skipBtn = button('Continue without contact', { icon: 'arrowRight', onClick: () => next(2) });
    const findBtn = button('Run contact discovery', { icon: 'search' });

    saveBtn.addEventListener('click', async () => {
      if (!nameInput.value.trim() && !emailInput.value.trim()) { toast('Add a name or email', 'warning'); return; }
      setBusy(saveBtn, true, 'Saving...');
      try {
        state.contact = await call('contacts.create', { body: {
          lead_id: state.lead.id,
          full_name: nameInput.value.trim(),
          title: titleInput.value.trim(),
          email: emailInput.value.trim(),
          linkedin_url: linkedinInput.value.trim(),
        } });
        toast('Contact saved', 'success');
        next(2);
      } catch (err) {
        toast(err.message || 'Could not save contact', 'error');
        setBusy(saveBtn, false);
      }
    });
    findBtn.addEventListener('click', async () => {
      setBusy(findBtn, true, 'Searching...');
      const res = await call('contacts.discover', { body: { lead_id: state.lead.id } });
      toast('Contact discovery started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
      setBusy(findBtn, false);
    });

    return card({
      title: '2. Add or discover the buyer contact',
      body: el('div', {},
        existing.length ? el('div', { class: 'ifz-mb-4' },
          el('div', { class: 'ifz-label ifz-mb-4' }, 'Existing contacts for this lead'),
          existing.map(c => el('div', { class: 'ifz-actionrow' },
            el('div', { class: 'ifz-actionrow-body' },
              el('div', { class: 'ifz-actionrow-title' }, c.name),
              el('div', { class: 'ifz-actionrow-sub' }, `${c.title || 'No title'} / ${c.email || 'no email'}`)),
            badge(c.email_status),
            button('Use', { size: 'sm', onClick: () => { state.contact = c; next(2); } })))) : null,
        el('div', { class: 'ifz-form-row' },
          field('Full name', nameInput),
          field('Title', titleInput)),
        field('Email', emailInput),
        field('LinkedIn URL', linkedinInput),
        el('div', { class: 'ifz-row wrap' }, saveBtn, findBtn, skipBtn)),
    });
  }

  function researchStep(next) {
    if (!state.lead) return emptyState({ icon: 'search', title: 'Create a lead first' });
    const research = state.research || db.research.find(r => r.lead_id === state.lead.id);
    const runBtn = button(research ? 'Re-research lead' : 'Research lead', { kind: 'primary', icon: 'search' });
    const continueBtn = button('Continue to email', { icon: 'arrowRight', onClick: () => next(3) });

    runBtn.addEventListener('click', async () => {
      setBusy(runBtn, true, 'Researching...');
      try {
        const res = await call('leads.research', { params: { leadId: state.lead.id } });
        state.researchRunId = res.run_id;
        toast('Research started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
        await waitForRun(res);
        state.research = await call('research.leadInsights', { params: { leadId: state.lead.id } });
        render();
      } catch (err) {
        toast(err.message || 'Could not research lead', 'error');
      } finally {
        setBusy(runBtn, false);
      }
    });

    return card({
      title: '3. Research company context',
      body: el('div', {},
        research ? el('div', { class: 'ifz-mb-4' },
          badge(research.status),
          el('p', { class: 'ifz-mt-4', style: { lineHeight: 1.6 } }, research.summary),
          research.insights.map(i => el('div', { class: 'ifz-actionrow' },
            el('div', { class: 'ifz-actionrow-body' },
              el('div', { class: 'ifz-actionrow-title' }, i.title),
              el('div', { class: 'ifz-actionrow-sub' }, i.body))))) :
          emptyState({ icon: 'search', title: 'Research has not run yet', hint: 'Hermes will produce company insights and update the lead.' }),
        el('div', { class: 'ifz-row wrap' }, runBtn, continueBtn,
          state.researchRunId ? button('Watch latest run', { icon: 'bolt', onClick: () => ctx.navigate(`/app/agent-runs/${state.researchRunId}`) }) : null)),
    });
  }

  function emailStep(next) {
    if (!state.lead) return emptyState({ icon: 'mail', title: 'Create a lead first' });
    const productSelect = select(productOptions(), { value: db.products[0]?.id });
    const languageSelect = select(languageOptions(), { value: state.lead.country === 'DE' ? 'de' : 'en' });
    const ccSelect = select(ccRuleOptions(), { value: db.company.sales_preferences.default_cc_rule_id });
    const generateBtn = button(state.message ? 'Regenerate email' : 'Generate email', { kind: 'primary', icon: 'sparkle' });
    const continueBtn = button('Review approval', { icon: 'arrowRight', disabled: !state.message, onClick: () => next(4) });

    generateBtn.addEventListener('click', async () => {
      setBusy(generateBtn, true, 'Generating...');
      try {
        if (config.mode === 'mock') {
          state.message = await call('customOutreach.generateEmail', { body: {
            lead_id: state.lead.id,
            contact_id: state.contact?.id,
            product_id: productSelect.value,
            language: languageSelect.value,
            cc_rule: ccSelect.value,
          } });
        } else {
          const run = await call('leads.generateOutreach', { params: { leadId: state.lead.id }, body: {} });
          const completed = await waitForRun(run);
          if (!completed.output_ref) throw new Error('Outreach run completed without a message.');
          state.message = await call('messages.get', { params: { messageId: completed.output_ref } });
        }
        toast('Email generated', 'success');
        render();
      } catch (err) {
        toast(err.message || 'Could not generate email', 'error');
        setBusy(generateBtn, false);
      }
    });

    return card({
      title: '4. Generate the cold email',
      body: el('div', {},
        el('div', { class: 'ifz-grid cols-3' },
          field('Product focus', productSelect),
          field('Language', languageSelect),
          field('CC rule', ccSelect)),
        el('div', { class: 'ifz-row wrap ifz-mb-4' }, generateBtn, continueBtn),
        state.message ? emailPreview(state.message) : emptyState({ icon: 'mail', title: 'No email yet', hint: 'Generate a draft using Company Brain, contact, and product context.' })),
    });
  }

  function sendStep() {
    if (!state.message) return emptyState({ icon: 'mail', title: 'Generate an email first' });
    const approveBtn = button('Approve', { icon: 'check' });
    const draftBtn = button('Create draft', { kind: 'primary', icon: 'mail' });
    const sendBtn = button('Approved send', { kind: 'primary', icon: 'send' });
    const openLeadBtn = button('Open lead', { icon: 'leads', onClick: () => ctx.navigate(`/app/leads/${state.lead.id}`) });

    approveBtn.addEventListener('click', async () => {
      state.message = await call('messages.approve', { params: { messageId: state.message.id } });
      toast('Email approved', 'success');
      render();
    });
    draftBtn.addEventListener('click', async () => {
      state.result = await call('messages.createDraft', { params: { messageId: state.message.id } });
      state.message = await call('messages.get', { params: { messageId: state.message.id } });
      toast('Draft created in mailbox', 'success');
      render();
    });
    sendBtn.addEventListener('click', async () => {
      state.result = await call('messages.send', { params: { messageId: state.message.id } });
      state.message = await call('messages.get', { params: { messageId: state.message.id } });
      toast('Message marked sent', 'success');
      render();
    });

    return card({
      title: '5. Approval and send action',
      body: el('div', {},
        el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
          summaryPanel('Context', kv([['Lead', leadCompanySummary(state.lead)], ['Contact', state.contact?.name || 'No contact'], ['Status', state.message.status]])),
          summaryPanel('Safety', el('div', { class: 'ifz-col' }, badge('draft_created', 'Draft mode default'), el('span', { class: 'ifz-muted ifz-small' }, 'No backend provider is connected.'))),
          summaryPanel('Result', el('div', { class: 'ifz-col' }, badge(state.message.status), el('span', { class: 'ifz-muted ifz-small' }, state.message.sent_at ? `Updated ${fmtAgo(state.message.sent_at)}` : 'Awaiting action')))),
        emailPreview(state.message),
        el('div', { class: 'ifz-row wrap ifz-mt-4' }, approveBtn, draftBtn, sendBtn, openLeadBtn, button('Start another', { icon: 'refresh', onClick: () => {
          Object.assign(state, { lead: null, contact: null, researchRunId: null, message: null, result: null });
          active = 0;
          render();
        } }))),
    });
  }

  await Promise.all([
    call('leads.list'), call('contacts.list'), call('products.list'), call('research.list'),
    call('ccRules.list'), call('company.getSalesPreferences'),
  ]);
  render();
}

function fmtAgo(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function summaryPanel(label, body) {
  return el('div', { class: 'ifz-panellet' },
    el('div', { class: 'ifz-overline ifz-mb-4' }, label),
    body);
}
