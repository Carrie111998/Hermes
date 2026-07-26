/* Setup — one progressive surface for company context, markets and sending. */

import {
  el, button, input, select, textarea, field, pageHead, badge, emptyState,
  toast, setBusy, modal, icon, fmt, tabs, chipSelect, passwordField, runSentence,
} from '../ui.js';
import { call } from '../api.js';
import { db } from '../mocks/db.js';
import { COUNTRY_NAMES } from '../catalog.js';
import {
  countryOptions, countryName, languageOptions, SEND_MODES, waitForRun,
} from './_page-utils.js';
import { renderMiniMap } from './lead-map.js';
import { getSession } from '../session.js';

const REQUIRED_STEPS = Object.freeze([
  'company-identity',
  'positioning',
  'products',
  'internal-sales-data',
  'target-markets',
]);

const REQUIRED_META = Object.freeze([
  {
    key: 'company-identity',
    section: 'company',
    title: 'Your company',
    description: 'The business identity used in research and every email.',
  },
  {
    key: 'positioning',
    section: 'positioning',
    title: 'How you win',
    description: 'Why a buyer should choose you over another supplier.',
  },
  {
    key: 'products',
    section: 'products',
    title: 'What you sell',
    description: 'The products the agent should match to each market.',
  },
  {
    key: 'internal-sales-data',
    section: 'documents',
    title: 'What the agent can learn from',
    description: 'Catalogs, sales material and the files you already trust.',
  },
  {
    key: 'target-markets',
    section: 'markets',
    title: 'Where you sell',
    description: 'Priority markets and the places the agent must avoid.',
  },
]);

const DOC_TYPES = Object.freeze([
  { value: 'product_catalog', label: 'Product catalog' },
  { value: 'technical_sheet', label: 'Technical sheet' },
  { value: 'price_list', label: 'Price list' },
  { value: 'past_sales', label: 'Past sales or won/lost history' },
  { value: 'past_customers', label: 'Past customers' },
  { value: 'current_contacts', label: 'Current contacts' },
  { value: 'distributor_list', label: 'Distributor list' },
  { value: 'certificate', label: 'Certificate' },
  { value: 'other', label: 'Other useful file' },
]);

const EMAIL_PROVIDERS = Object.freeze([
  { key: 'google', title: 'Google Workspace', route: 'emailIntegrations.connectGoogle', mark: 'G' },
  { key: 'microsoft', title: 'Microsoft 365', route: 'emailIntegrations.connectMicrosoft', mark: 'M' },
  { key: 'smtp', title: 'Any email service', route: 'emailIntegrations.connectSmtp', mark: '@', credentials: 'smtp' },
  { key: 'browser', title: 'Webmail in a browser', route: 'emailIntegrations.connectBrowser', mark: 'W', credentials: 'browser' },
]);

const SMTP_PRESETS = Object.freeze({
  gmail: { label: 'Gmail with an app password', smtp_host: 'smtp.gmail.com', smtp_port: 587, imap_host: 'imap.gmail.com' },
  outlook: { label: 'Outlook or Microsoft', smtp_host: 'smtp.office365.com', smtp_port: 587, imap_host: 'outlook.office365.com' },
  yahoo: { label: 'Yahoo Mail', smtp_host: 'smtp.mail.yahoo.com', smtp_port: 465, imap_host: 'imap.mail.yahoo.com' },
  zoho: { label: 'Zoho Mail', smtp_host: 'smtp.zoho.com', smtp_port: 465, imap_host: 'imap.zoho.com' },
  custom: { label: 'Another service', smtp_host: '', smtp_port: 587, imap_host: '' },
});

const PLACEHOLDERS = Object.freeze([
  '{{company_name}}', '{{contact_name}}', '{{contact_title}}', '{{country}}',
]);

function itemsOf(value) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.items) ? value.items : [];
}

function arrayOf(value) {
  return Array.isArray(value) ? value : [];
}

function completedSteps(status) {
  if (Array.isArray(status?.completed_steps)) return new Set(status.completed_steps);
  return new Set(arrayOf(status?.steps)
    .filter(step => step.status === 'done')
    .map(step => step.key));
}

function isSetupComplete(status) {
  return status?.status === 'completed' || status?.status === 'complete';
}

function allRequiredComplete(status) {
  const completed = completedSteps(status);
  return REQUIRED_STEPS.every(step => completed.has(step));
}

function phrase(items) {
  const clean = items.filter(Boolean);
  if (!clean.length) return '';
  if (clean.length === 1) return clean[0];
  if (clean.length === 2) return `${clean[0]} and ${clean[1]}`;
  return `${clean.slice(0, -1).join(', ')}, and ${clean.at(-1)}`;
}

function languageLabel(code) {
  return languageOptions().find(language => language.value === code)?.label || code || 'Not chosen';
}

function sendModeLabel(mode) {
  return mode === 'approved_send' ? 'Send after approval' : 'Drafts first';
}

function providerLabel(provider) {
  return EMAIL_PROVIDERS.find(item => item.key === provider)?.title
    || (provider === 'stub' ? 'Local test mailbox' : 'Connected mailbox');
}

function missingCopy(value) {
  return String(value || '')
    .replace(/\s*\(onboarding step \d+\)\.?/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function normalizeBrainRecord(value) {
  const defaults = {
    product_understanding: [],
    ideal_customer_profile: [],
    buyer_roles: [],
    market_assumptions: [],
    sales_arguments: [],
    business_rules_digest: [],
    missing_data: [],
  };
  if (!value) {
    return {
      id: null,
      status: 'not_built',
      approved_at: null,
      sections: defaults,
    };
  }
  const sections = {
    ...defaults,
    ...(value.sections || value.content || {}),
  };
  return {
    ...value,
    ...sections,
    sections,
    approved_at: value.approved_at || null,
  };
}

function setupLoading() {
  return el('div', {
    class: 'ifz-setup-loading',
    role: 'status',
    'aria-label': 'Loading setup',
    'aria-busy': 'true',
  },
  el('span', { class: 'ifz-setup-loading-title' }),
  Array.from({ length: 5 }, () => el('span', { class: 'ifz-setup-loading-row' })),
  el('span', { class: 'ifz-setup-loading-band' }),
  el('span', { class: 'ifz-setup-loading-band' }));
}

function errorLine() {
  return el('p', {
    class: 'ifz-setup-form-error',
    role: 'alert',
    tabindex: '-1',
    hidden: true,
  });
}

function showError(node, copy) {
  node.textContent = copy;
  node.hidden = false;
  node.focus?.({ preventScroll: true });
}

function marketSelector(selected = [], { max = 0 } = {}) {
  const values = new Set(selected.filter(Boolean));
  const picker = select([
    { value: '', label: 'Choose a country' },
    ...countryOptions(),
  ], { value: '' });
  const add = button('Add market', { icon: 'plus' });
  const chips = el('div', {
    class: 'ifz-setup-market-chips',
    role: 'list',
    'aria-label': 'Selected countries',
  });
  const note = el('p', {
    class: 'ifz-setup-market-note',
    role: 'status',
    'aria-live': 'polite',
  });

  function render() {
    chips.replaceChildren(...[...values].map(code =>
      el('span', { class: 'ifz-setup-market-chip', role: 'listitem' },
        el('span', {}, countryName(code)),
        el('button', {
          type: 'button',
          'aria-label': `Remove ${countryName(code)}`,
          onclick: () => {
            values.delete(code);
            render();
          },
        }, '×'))));
    note.textContent = max ? `${values.size} of ${max} markets selected` : '';
    add.disabled = !picker.value || values.has(picker.value) || Boolean(max && values.size >= max);
  }

  picker.addEventListener('change', render);
  add.addEventListener('click', () => {
    if (!picker.value || values.has(picker.value)) return;
    if (max && values.size >= max) {
      note.textContent = `Choose up to ${max} target markets.`;
      return;
    }
    values.add(picker.value);
    picker.value = '';
    render();
  });
  render();

  return {
    node: el('div', { class: 'ifz-setup-market-picker' },
      el('div', { class: 'ifz-setup-market-add' }, picker, add),
      chips,
      note),
    getSelected: () => [...values],
  };
}

function confirmAction({ title, copy, label, kind = 'danger', onConfirm }) {
  const confirm = button(label, { kind });
  const dialog = modal({
    title,
    body: el('p', { class: 'ifz-confirm-copy' }, copy),
    actions: [
      button('Cancel', { onClick: () => dialog.close() }),
      confirm,
    ],
  });
  confirm.addEventListener('click', async () => {
    setBusy(confirm, true, 'Saving…');
    try {
      await onConfirm();
      dialog.close();
    } catch {
      setBusy(confirm, false);
      toast("We couldn't save that change. Try again.", 'error');
    }
  });
}

export async function mount(root, ctx) {
  let disposed = false;
  let initialOpenChosen = false;
  const pageEl = root.closest('.ifz-page') || root;
  pageEl.classList.add('ifz-page--setup');

  const state = {
    onboarding: null,
    profile: {},
    positioning: {},
    preferences: {},
    templates: {},
    products: [],
    documents: [],
    contacts: [],
    leads: [],
    brain: null,
    emailIntegrations: [],
    whatsapp: null,
    ccRules: [],
    selectedCountries: [],
    countryStates: [],
    runtime: undefined,
    known: {
      brain: true,
      mailbox: true,
      whatsapp: true,
      templates: true,
    },
    openSection: ctx.query.section || '',
    loaded: false,
    loadError: false,
    workSentence: '',
    brainSentence: '',
  };

  const page = el('div', { class: 'ifz-setup' }, setupLoading());
  root.append(page);

  function syncUrl() {
    const query = new URLSearchParams();
    if (state.openSection) query.set('section', state.openSection);
    const suffix = query.toString() ? `?${query}` : '';
    history.replaceState(
      null,
      '',
      `${location.pathname}${location.search}#/app/setup${suffix}`,
    );
  }

  function setOpen(section, { focus = false } = {}) {
    state.openSection = state.openSection === section ? '' : section;
    syncUrl();
    render();
    if (focus && state.openSection) {
      requestAnimationFrame(() =>
        page.querySelector(`[data-setup-section="${state.openSection}"]`)?.focus({ preventScroll: true }));
    }
  }

  async function load({ soft = false } = {}) {
    if (!soft) page.replaceChildren(setupLoading());
    state.loadError = false;
    try {
      let [
        onboarding,
        profile,
        positioning,
        preferences,
        products,
        documents,
      ] = await Promise.all([
        call('onboarding.status'),
        call('company.getProfile'),
        call('company.getPositioning'),
        call('company.getSalesPreferences'),
        call('products.list'),
        call('documents.list'),
      ]);

      if (onboarding?.status === 'not_started') {
        onboarding = await call('onboarding.start');
      }

      const optional = await Promise.allSettled([
        call('company.getEmailTemplates'),
        call('contacts.list'),
        call('leads.list'),
        call('brain.get'),
        call('brain.snapshots'),
        call('emailIntegrations.list'),
        call('whatsapp.integrations'),
        call('ccRules.list'),
        call('leadMap.selectedCountries'),
        call('leadMap.countries'),
        fetch('/health').then(response => response.ok ? response.json() : undefined),
      ]);
      if (disposed) return;

      const optionalValue = (index, fallback) =>
        optional[index].status === 'fulfilled' ? optional[index].value : fallback;
      const templateSection = optionalValue(0, null);
      const emailIntegrations = optionalValue(5, null);
      const whatsapp = optionalValue(6, null);
      const brain = optionalValue(3, null);
      const draftBrain = itemsOf(optionalValue(4, []))
        .find(snapshot => snapshot.status === 'draft');

      state.known.templates = optional[0].status === 'fulfilled';
      state.known.brain = optional[3].status === 'fulfilled';
      state.known.mailbox = optional[5].status === 'fulfilled';
      state.known.whatsapp = optional[6].status === 'fulfilled';
      state.onboarding = onboarding;
      state.profile = profile || {};
      state.positioning = positioning || {};
      state.preferences = preferences || {};
      state.products = itemsOf(products);
      state.documents = itemsOf(documents);
      state.templates = {
        ...(templateSection?.templates || templateSection?.data?.templates || {}),
      };
      state.contacts = itemsOf(optionalValue(1, []));
      state.leads = itemsOf(optionalValue(2, []));
      state.brain = draftBrain ? normalizeBrainRecord(draftBrain) : brain;
      state.emailIntegrations = itemsOf(emailIntegrations);
      state.whatsapp = itemsOf(whatsapp)[0] || null;
      state.ccRules = itemsOf(optionalValue(7, []));
      state.selectedCountries = itemsOf(optionalValue(8, []));
      state.countryStates = itemsOf(optionalValue(9, []));
      state.runtime = optionalValue(10, undefined);

      if (allRequiredComplete(state.onboarding) && !isSetupComplete(state.onboarding)) {
        state.onboarding = await call('onboarding.complete');
      }

      state.loaded = true;
      if (!initialOpenChosen) {
        const missing = firstMissingSection();
        if (!state.openSection && missing) state.openSection = missing;
        initialOpenChosen = true;
        syncUrl();
      }
      render();
    } catch {
      state.loaded = true;
      state.loadError = true;
      render();
    }
  }

  function firstMissingSection() {
    const completed = completedSteps(state.onboarding);
    return REQUIRED_META.find(item => !completed.has(item.key))?.section || '';
  }

  async function markRequired(key, body = {}) {
    const route = {
      'company-identity': 'onboarding.updateCompanyIdentity',
      positioning: 'onboarding.updatePositioning',
      products: 'onboarding.updateProducts',
      'internal-sales-data': 'onboarding.updateInternalSalesData',
      'target-markets': 'onboarding.updateTargetMarkets',
    }[key];
    state.onboarding = await call(route, { body });
    if (allRequiredComplete(state.onboarding) && !isSetupComplete(state.onboarding)) {
      state.onboarding = await call('onboarding.complete');
    }
  }

  function nextMissingAfter(key) {
    const completed = completedSteps(state.onboarding);
    const currentIndex = REQUIRED_META.findIndex(item => item.key === key);
    return REQUIRED_META.slice(currentIndex + 1).find(item => !completed.has(item.key))?.section
      || REQUIRED_META.find(item => !completed.has(item.key))?.section
      || '';
  }

  async function finishRequiredSave(key, successCopy) {
    const next = nextMissingAfter(key);
    state.openSection = next;
    await load({ soft: true });
    toast(isSetupComplete(state.onboarding) ? 'Setup is ready.' : successCopy, 'success');
    requestAnimationFrame(() => {
      const target = next
        ? page.querySelector(`[data-setup-section="${next}"]`)
        : page.querySelector('.ifz-setup-status');
      target?.focus?.({ preventScroll: true });
    });
  }

  function render() {
    if (disposed || !state.loaded) return;
    if (state.loadError) {
      page.replaceChildren(emptyState({
        icon: 'warning',
        title: "Setup couldn't be loaded",
        hint: 'Nothing was changed. Try loading it again.',
        action: button('Try again', {
          kind: 'primary',
          onClick: () => load(),
        }),
      }));
      return;
    }

    const completed = completedSteps(state.onboarding);
    const missingRequired = REQUIRED_META.filter(item => !completed.has(item.key));
    const complete = missingRequired.length === 0 && isSetupComplete(state.onboarding);
    const headline = complete
      ? 'Setup is ready.'
      : `${missingRequired.length} ${missingRequired.length === 1 ? 'thing' : 'things'} left before the agent has the basics.`;
    const sub = complete
      ? 'The agent has the context it needs. Change anything here when your business changes.'
      : 'Complete the essentials below. Useful additions can wait and will never block setup.';

    page.replaceChildren(
      pageHead({
        title: 'Setup',
        sub: 'Everything the agent needs to understand your business, find the right buyers, and prepare safe outreach.',
        actions: complete
          ? [button('Back to Today', { icon: 'arrowRight', onClick: () => ctx.navigate('/app/today') })]
          : [button('Continue setup', {
              kind: 'primary',
              icon: 'arrowRight',
              onClick: () => {
                const section = firstMissingSection();
                if (!section) return;
                state.openSection = section;
                syncUrl();
                render();
                requestAnimationFrame(() =>
                  page.querySelector(`[data-setup-section="${section}"]`)?.focus());
              },
            })],
      }),
      el('section', {
        class: `ifz-setup-status${complete ? ' is-complete' : ''}`,
        tabindex: '-1',
        'aria-live': 'polite',
      },
      el('span', { class: 'ifz-setup-kicker' }, complete ? 'Ready' : 'Essentials'),
      el('h2', {}, headline),
      el('p', {}, sub)),
      renderEssentials(completed),
      renderUsefulAdditions(),
      renderBrainBand(),
      renderSendingBand(),
    );
  }

  function renderEssentials(completed) {
    return el('section', {
      class: 'ifz-setup-section',
      'aria-labelledby': 'ifz-setup-essentials-title',
    },
    el('div', { class: 'ifz-setup-section-heading' },
      el('div', {},
        el('span', { class: 'ifz-setup-kicker' }, 'Required'),
        el('h2', { id: 'ifz-setup-essentials-title' }, 'The essentials')),
      el('p', {}, 'These five items are enough to complete Setup.')),
    el('ol', { class: 'ifz-setup-ledger' },
      REQUIRED_META.map(item => requiredRow(item, completed.has(item.key)))));
  }

  function requiredRow(item, done) {
    const open = state.openSection === item.section;
    const panelId = `ifz-setup-panel-${item.section}`;
    const summary = {
      company: phrase([state.profile.name || state.profile.legal_name, state.profile.city]),
      positioning: state.positioning.main_value_proposition || state.positioning.what_company_sells,
      products: state.products.length
        ? `${state.products.length} ${state.products.length === 1 ? 'product' : 'products'}`
        : '',
      documents: state.documents.length
        ? `${state.documents.length} ${state.documents.length === 1 ? 'file' : 'files'} saved`
        : '',
      markets: targetMarkets().length
        ? phrase(targetMarkets().map(countryName))
        : '',
    }[item.section] || '';

    const article = el('li', {
      class: `ifz-setup-row${done ? ' is-done' : ''}${open ? ' is-open' : ''}`,
    },
    el('button', {
      class: 'ifz-setup-row-toggle',
      type: 'button',
      dataset: { setupSection: item.section },
      'aria-expanded': open ? 'true' : 'false',
      'aria-controls': panelId,
      onclick: () => setOpen(item.section),
    },
    el('span', { class: 'ifz-setup-row-mark', 'aria-hidden': 'true' }, done ? '✓' : '○'),
    el('span', { class: 'ifz-setup-row-title' },
      el('strong', {}, item.title),
      el('span', {}, item.description)),
    el('span', { class: 'ifz-setup-row-summary' },
      summary || (done ? 'Saved' : 'Needs your input'),
      el('small', {}, done ? 'Ready' : 'Required')),
    el('span', { class: 'ifz-setup-row-chevron', 'aria-hidden': 'true' }, icon('arrowRight', 16))));

    if (open) {
      article.append(el('div', {
        class: 'ifz-setup-editor',
        id: panelId,
      }, editorFor(item.section, done)));
    }
    return article;
  }

  function editorFor(section, done) {
    if (section === 'company') return companyEditor();
    if (section === 'positioning') return positioningEditor();
    if (section === 'products') return productsEditor(done);
    if (section === 'documents') return documentsEditor(done);
    if (section === 'markets') return marketsEditor();
    return null;
  }

  function companyEditor() {
    const profile = state.profile;
    const name = input({ value: profile.name || '', required: true, autocomplete: 'organization' });
    const legal = input({ value: profile.legal_name || '' });
    const website = input({ value: profile.website || '', type: 'url', placeholder: 'https://example.com' });
    const country = select(countryOptions(), { value: profile.headquarters_country || 'TR' });
    const city = input({ value: profile.city || '' });
    const founded = input({ value: profile.founded_year || '', type: 'number', min: '1800', max: '2100' });
    const industry = input({ value: profile.industry || '' });
    const employees = input({ value: profile.employee_count || '' });
    const model = input({ value: profile.business_model || '' });
    const language = select(languageOptions(), { value: profile.main_language || 'en' });
    const save = button('Save and continue', { kind: 'primary', icon: 'check' });
    const error = errorLine();

    save.addEventListener('click', async () => {
      if (!name.value.trim()) {
        name.setAttribute('aria-invalid', 'true');
        name.focus();
        showError(error, 'Add your company name.');
        return;
      }
      setBusy(save, true, 'Saving…');
      error.hidden = true;
      const body = {
        name: name.value.trim(),
        legal_name: legal.value.trim(),
        website: website.value.trim(),
        headquarters_country: country.value,
        city: city.value.trim(),
        founded_year: founded.value ? Number(founded.value) : null,
        industry: industry.value.trim(),
        employee_count: employees.value.trim(),
        business_model: model.value.trim(),
        main_language: language.value,
      };
      try {
        await call('company.updateProfile', { body });
        await markRequired('company-identity', body);
        await finishRequiredSave('company-identity', 'Your company details were saved.');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save your company details. Nothing was lost; try again.");
      }
    });

    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Use the name and website a buyer would recognise. More detail improves company matching.'),
      el('div', { class: 'ifz-form-row' },
        field('Company name', name, { required: true }),
        field('Legal name', legal)),
      field('Website', website),
      el('div', { class: 'ifz-form-row' },
        field('Headquarters country', country),
        field('City', city)),
      el('details', { class: 'ifz-setup-more' },
        el('summary', {}, 'More company detail'),
        el('div', { class: 'ifz-setup-more-body' },
          el('div', { class: 'ifz-form-row' },
            field('Founded year', founded),
            field('Employee count', employees)),
          el('div', { class: 'ifz-form-row' },
            field('Industry', industry),
            field('Main language', language)),
          field('Business model', model))),
      error,
      el('div', { class: 'ifz-setup-editor-actions' }, save));
  }

  function positioningEditor() {
    const positioning = state.positioning;
    const sells = textarea({ value: positioning.what_company_sells || '', rows: 3 });
    const value = textarea({ value: positioning.main_value_proposition || '', rows: 3, required: true });
    const quality = input({ value: positioning.quality_position || '' });
    const price = input({ value: positioning.price_position || '' });
    const tier = input({ value: positioning.premium_or_mass_market || '' });
    const differentiators = textarea({
      value: arrayOf(positioning.main_differentiators).join('\n'),
      rows: 4,
    });
    const certifications = input({ value: arrayOf(positioning.certifications).join(', ') });
    const manufacturing = input({ value: positioning.manufacturing_capacity || '' });
    const exportCapacity = input({ value: positioning.export_capacity || '' });
    const delivery = textarea({ value: positioning.delivery_capabilities || '', rows: 2 });
    const support = textarea({ value: positioning.after_sales_support || '', rows: 2 });
    const save = button('Save and continue', { kind: 'primary', icon: 'check' });
    const error = errorLine();

    save.addEventListener('click', async () => {
      if (!value.value.trim() && !sells.value.trim()) {
        value.setAttribute('aria-invalid', 'true');
        value.focus();
        showError(error, 'Describe what you sell or why buyers choose you.');
        return;
      }
      setBusy(save, true, 'Saving…');
      error.hidden = true;
      const body = {
        what_company_sells: sells.value.trim(),
        main_value_proposition: value.value.trim(),
        quality_position: quality.value.trim(),
        price_position: price.value.trim(),
        premium_or_mass_market: tier.value.trim(),
        main_differentiators: differentiators.value.split('\n').map(item => item.trim()).filter(Boolean),
        certifications: certifications.value.split(',').map(item => item.trim()).filter(Boolean),
        manufacturing_capacity: manufacturing.value.trim(),
        export_capacity: exportCapacity.value.trim(),
        delivery_capabilities: delivery.value.trim(),
        after_sales_support: support.value.trim(),
      };
      try {
        await call('company.updatePositioning', { body });
        await markRequired('positioning', body);
        await finishRequiredSave('positioning', 'Your positioning was saved.');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save your positioning. Nothing was lost; try again.");
      }
    });

    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Write this as you would explain the business to a new export salesperson.'),
      field('What you sell', sells),
      field('Why buyers choose you', value, { required: true }),
      field('Main differentiators', differentiators, { hint: 'One useful sales point per line.' }),
      el('details', { class: 'ifz-setup-more' },
        el('summary', {}, 'More positioning detail'),
        el('div', { class: 'ifz-setup-more-body' },
          el('div', { class: 'ifz-grid cols-3' },
            field('Quality position', quality),
            field('Price position', price),
            field('Market tier', tier)),
          el('div', { class: 'ifz-form-row' },
            field('Certifications', certifications),
            field('Manufacturing capacity', manufacturing)),
          el('div', { class: 'ifz-form-row' },
            field('Export capacity', exportCapacity),
            field('After-sales support', support)),
          field('Delivery capabilities', delivery))),
      error,
      el('div', { class: 'ifz-setup-editor-actions' }, save));
  }

  function productsEditor(done) {
    const confirm = button(done ? 'Product list saved' : 'Confirm product list', {
      kind: done ? '' : 'primary',
      icon: 'check',
      disabled: done,
    });
    const error = errorLine();
    confirm.addEventListener('click', async () => {
      if (!state.products.length) {
        showError(error, 'Add at least one product before continuing.');
        return;
      }
      setBusy(confirm, true, 'Saving…');
      try {
        await markRequired('products', {
          product_ids: state.products.map(product => product.id),
          catalog_confirmed: true,
        });
        await finishRequiredSave('products', 'Your product list was saved.');
      } catch {
        setBusy(confirm, false);
        showError(error, "We couldn't confirm the product list. Try again.");
      }
    });

    const list = state.products.length
      ? el('ul', { class: 'ifz-setup-product-list' },
          state.products.map(product =>
            el('li', {},
              el('div', {},
                el('strong', {}, product.name || 'Unnamed product'),
                el('span', {}, [
                  product.category,
                  product.moq ? `MOQ ${product.moq}` : '',
                  product.price_band,
                ].filter(Boolean).join(' · ') || 'Add a short product description')),
              button('Edit', {
                size: 'sm',
                icon: 'edit',
                onClick: () => openProductEditor(product),
              }))))
      : emptyState({
          icon: 'file',
          title: 'No products yet',
          hint: 'Add the products you want the agent to match with buyers.',
        });

    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Keep this list focused on products you can actively export.'),
      list,
      error,
      el('div', { class: 'ifz-setup-editor-actions' },
        button('Add product', {
          icon: 'plus',
          onClick: () => openProductEditor(),
        }),
        confirm));
  }

  function openProductEditor(product = null) {
    const name = input({
      value: product?.name || '',
      placeholder: 'Built-in oven series',
      required: true,
    });
    const category = input({ value: product?.category || '', placeholder: 'Built-in appliances' });
    const description = textarea({ value: product?.description || '', rows: 3 });
    const moq = input({ value: product?.moq || '', placeholder: '50 units' });
    const price = input({ value: product?.price_band || '', placeholder: 'EUR 165–240 FOB' });
    const save = button(product ? 'Save product' : 'Add product', {
      kind: 'primary',
      icon: product ? 'check' : 'plus',
    });
    const error = errorLine();
    const dialog = modal({
      title: product ? 'Edit product' : 'Add product',
      body: el('div', {},
        field('Product name', name, { required: true }),
        field('Category', category),
        field('Description', description),
        el('div', { class: 'ifz-form-row' },
          field('Minimum order', moq),
          field('Price range', price)),
        error),
      actions: [
        button('Cancel', { onClick: () => dialog.close() }),
        save,
      ],
    });
    save.addEventListener('click', async () => {
      if (!name.value.trim()) {
        name.setAttribute('aria-invalid', 'true');
        name.focus();
        showError(error, 'Add the product name.');
        return;
      }
      setBusy(save, true, 'Saving…');
      const body = {
        name: name.value.trim(),
        category: category.value.trim(),
        description: description.value.trim(),
        moq: moq.value.trim(),
        price_band: price.value.trim(),
      };
      try {
        const completingProducts = !completedSteps(state.onboarding).has('products');
        const saved = product
          ? await call('products.update', { params: { productId: product.id }, body })
          : await call('products.create', { body });
        if (completingProducts) {
          await markRequired('products', {
            product_ids: [...state.products.map(item => item.id), saved.id],
            catalog_confirmed: true,
          });
        }
        dialog.close();
        if (completingProducts) {
          await finishRequiredSave('products', 'Your product list was saved.');
        } else {
          await load({ soft: true });
          toast(product ? 'Product updated.' : 'Product added.', 'success');
        }
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save this product. Try again.");
      }
    });
  }

  function documentsEditor(done) {
    const confirm = button(done ? 'Sales material reviewed' : 'Confirm current material', {
      kind: done ? '' : 'primary',
      icon: 'check',
      disabled: done,
    });
    const error = errorLine();
    confirm.addEventListener('click', async () => {
      setBusy(confirm, true, 'Saving…');
      try {
        await markRequired('internal-sales-data', {
          sources_reviewed: true,
          document_ids: state.documents.map(document => document.id),
        });
        await finishRequiredSave('internal-sales-data', 'Your sales material was confirmed.');
      } catch {
        setBusy(confirm, false);
        showError(error, "We couldn't confirm your sales material. Try again.");
      }
    });

    const list = state.documents.length
      ? el('ul', { class: 'ifz-setup-document-list' },
          state.documents.map(document => {
            const canProcess = !['processed', 'ready'].includes(document.status);
            return el('li', {},
              el('div', {},
                el('strong', {}, document.name || 'Saved file'),
                el('span', {}, `${DOC_TYPES.find(type => type.value === document.type)?.label || 'Company file'} · ${document.size_kb || 0} KB`)),
              badge(document.status || 'uploaded'),
              canProcess
                ? button('Read this file', {
                    size: 'sm',
                    icon: 'refresh',
                    disabled: state.runtime?.agent_runs_enabled !== true,
                    title: state.runtime?.agent_runs_enabled === true
                      ? null
                      : 'The agent is offline right now',
                    onClick: event => processDocument(document, event.currentTarget),
                  })
                : null);
          }))
      : emptyState({
          icon: 'upload',
          title: 'No files saved yet',
          hint: 'You can continue without files. Missing details will stay visible as useful additions.',
        });

    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Add the files your export team already uses. You can continue without uploading anything.'),
      list,
      state.runtime?.agent_runs_enabled === false
        ? el('p', { class: 'ifz-setup-offline', role: 'status' },
            'The agent is offline right now. Your files are safe; they can be read when it reconnects.')
        : null,
      state.workSentence
        ? el('p', { class: 'ifz-setup-work', role: 'status', 'aria-live': 'polite' }, state.workSentence)
        : null,
      error,
      el('div', { class: 'ifz-setup-editor-actions' },
        button('Upload a file', {
          icon: 'upload',
          onClick: () => openDocumentUpload('past_sales'),
        }),
        confirm));
  }

  async function processDocument(document, control) {
    setBusy(control, true, 'Reading…');
    state.workSentence = `Reading ${document.name}.`;
    render();
    try {
      const run = await call('documents.process', { params: { documentId: document.id } });
      const completed = await waitForRun(run, {
        timeoutMs: 120000,
        intervalMs: 500,
        onUpdate: current => {
          state.workSentence = runSentence(current);
          render();
        },
      });
      if (['failed', 'error', 'cancelled'].includes(completed?.status)) throw new Error('failed');
      state.workSentence = `${document.name} is ready to use.`;
      await load({ soft: true });
      toast(`${document.name} is ready to use.`, 'success');
    } catch {
      state.workSentence = "I couldn't finish reading that file. The upload is safe; try again later.";
      render();
    }
  }

  function openDocumentUpload(defaultType = 'other') {
    const file = input({
      type: 'file',
      accept: '.csv,.pdf,.doc,.docx,.xls,.xlsx,.txt,.json',
    });
    const type = select(DOC_TYPES, { value: defaultType });
    const save = button('Upload file', { kind: 'primary', icon: 'upload' });
    const error = errorLine();
    const maxBytes = Number(globalThis.window?.__HERMES_CONFIG__?.maxUploadBytes || 0);
    const maxMb = maxBytes ? Math.ceil(maxBytes / (1024 * 1024)) : null;
    const dialog = modal({
      title: 'Upload a company file',
      body: el('div', {},
        field('File', file, {
          required: true,
          hint: maxMb ? `Maximum file size: ${maxMb} MB.` : 'The server checks the file size.',
        }),
        field('What is in this file?', type),
        error),
      actions: [
        button('Cancel', { onClick: () => dialog.close() }),
        save,
      ],
    });
    save.addEventListener('click', async () => {
      const selected = file.files?.[0];
      if (!selected) {
        showError(error, 'Choose a file to upload.');
        file.focus();
        return;
      }
      if (maxBytes && selected.size > maxBytes) {
        showError(error, `Choose a file smaller than ${maxMb} MB.`);
        return;
      }
      setBusy(save, true, 'Uploading…');
      try {
        const form = new FormData();
        form.append('document_type', type.value);
        form.append('file', selected, selected.name);
        const uploaded = await call('documents.upload', { body: form });
        if (!completedSteps(state.onboarding).has('internal-sales-data')) {
          await markRequired('internal-sales-data', {
            sources_reviewed: true,
            document_ids: [...state.documents.map(document => document.id), uploaded.id],
          });
        }
        if (type.value === 'current_contacts') {
          await markOptional('onboarding.updateCurrentContacts', {
            document_id: uploaded.id,
            contact_list_added: true,
          });
        }
        dialog.close();
        await load({ soft: true });
        toast(`${selected.name} was uploaded.`, 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't upload this file. Nothing was saved; try again.");
      }
    });
  }

  function targetMarkets() {
    if (state.selectedCountries.length) return state.selectedCountries;
    const fromCountryState = state.countryStates.filter(country => country.target).map(country => country.code);
    return fromCountryState.length ? fromCountryState : arrayOf(state.profile.sales_regions_target);
  }

  function blockedMarkets(kind) {
    const property = kind === 'research' ? 'research_allowed' : 'outreach_allowed';
    return state.countryStates
      .filter(country => country[property] === false)
      .map(country => country.code);
  }

  function marketsEditor() {
    const targets = marketSelector(targetMarkets(), { max: 5 });
    const noResearch = marketSelector(blockedMarkets('research'));
    const noOutreach = marketSelector(blockedMarkets('outreach'));
    const mapHost = el('div', {
      class: 'ifz-setup-map ifz-minimap',
      'aria-label': `Target markets: ${targetMarkets().map(countryName).join(', ') || 'none selected'}`,
    });
    renderMiniMap(mapHost, {}, targetMarkets());
    const save = button('Save markets and continue', { kind: 'primary', icon: 'check' });
    const error = errorLine();

    save.addEventListener('click', async () => {
      const selected = targets.getSelected();
      const researchBlocked = noResearch.getSelected();
      const outreachBlocked = noOutreach.getSelected();
      if (!selected.length) {
        showError(error, 'Choose at least one target market.');
        return;
      }
      const overlap = selected.filter(code =>
        researchBlocked.includes(code) || outreachBlocked.includes(code));
      if (overlap.length) {
        showError(error, `${phrase(overlap.map(countryName))} cannot be both a target and an excluded market.`);
        return;
      }
      setBusy(save, true, 'Saving…');
      const body = {
        target_markets: selected,
        no_research_markets: researchBlocked,
        no_outreach_markets: outreachBlocked,
      };
      try {
        await Promise.all([
          call('company.updateProfile', { body: { sales_regions_target: selected } }),
          call('leadMap.selectCountry', { body: { countries: selected } }),
        ]);
        await markRequired('target-markets', body);
        await finishRequiredSave('target-markets', 'Your markets were saved.');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save your markets. Nothing was changed; try again.");
      }
    });

    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Choose up to five priority markets. Exclusions are enforced before research or outreach starts.'),
      el('div', { class: 'ifz-setup-market-layout' },
        el('div', {},
          field('Target markets', targets.node),
          el('details', { class: 'ifz-setup-more' },
            el('summary', {}, 'Markets to avoid'),
            el('div', { class: 'ifz-setup-more-body' },
              field('Do not research', noResearch.node, {
                hint: 'The agent will not look for buyers in these markets.',
              }),
              field('Research is allowed, but never contact', noOutreach.node, {
                hint: 'Buyers may remain visible, but outreach is blocked.',
              })))),
        mapHost),
      error,
      el('div', { class: 'ifz-setup-editor-actions' }, save));
  }

  function hasDocument(type) {
    return state.documents.some(document => document.type === type);
  }

  function emailMailbox(integration) {
    return integration?.mailbox
      || integration?.data?.mailbox
      || integration?.username
      || '';
  }

  function optionalState() {
    const completed = completedSteps(state.onboarding);
    return {
      contacts: state.contacts.length > 0
        || hasDocument('current_contacts')
        || completed.has('current-contacts'),
      mailbox: state.emailIntegrations.length > 0,
      brain: state.brain?.status === 'approved'
        || completed.has('brain-review'),
    };
  }

  async function markOptional(route, body) {
    state.onboarding = await call(route, { body });
    if (allRequiredComplete(state.onboarding)) {
      state.onboarding = await call('onboarding.complete');
    }
  }

  function openLowerSection(section, selector) {
    state.openSection = section;
    syncUrl();
    render();
    requestAnimationFrame(() => {
      page.querySelector(selector)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      page.querySelector(selector)?.focus?.({ preventScroll: true });
    });
  }

  function renderUsefulAdditions() {
    const ready = optionalState();
    return el('section', {
      class: 'ifz-setup-section ifz-setup-useful',
      'aria-labelledby': 'ifz-setup-useful-title',
    },
    el('div', { class: 'ifz-setup-section-heading' },
      el('div', {},
        el('span', { class: 'ifz-setup-kicker' }, 'Optional'),
        el('h2', { id: 'ifz-setup-useful-title' }, 'Useful additions')),
      el('p', {}, 'These improve the work, but never block Setup.')),
    el('ul', { class: 'ifz-setup-capability-list' },
      optionalRow({
        section: 'contacts',
        title: 'Current contacts',
        description: 'People and customer lists you already know.',
        ready: ready.contacts,
        summary: state.contacts.length
          ? `${state.contacts.length} ${state.contacts.length === 1 ? 'person' : 'people'} saved`
          : hasDocument('current_contacts') ? 'Contact list saved' : 'Add later',
        editor: contactsEditor,
      }),
      optionalRow({
        section: 'mailbox',
        title: 'Mailbox',
        description: 'Where approved emails are saved or sent.',
        ready: ready.mailbox,
        summary: !state.known.mailbox
          ? "Couldn't check right now"
          : ready.mailbox
            ? emailMailbox(state.emailIntegrations[0]) || providerLabel(state.emailIntegrations[0]?.provider)
            : 'Connect later',
        onOpen: () => openLowerSection(
          'mailbox',
          '.ifz-setup-sending [data-setup-section="mailbox"]',
        ),
      }),
      optionalRow({
        section: 'brain',
        title: 'Review what the agent knows',
        description: 'Check the assumptions used for buyer matching and email writing.',
        ready: ready.brain,
        summary: !state.known.brain
          ? "Couldn't check right now"
          : ready.brain ? 'Reviewed' : state.brain?.id ? 'Ready for review' : 'Build when ready',
        onOpen: () => openLowerSection(
          'brain',
          '.ifz-setup-brain [data-setup-section="brain"]',
        ),
      })));
  }

  function optionalRow({
    section,
    title,
    description,
    summary,
    ready,
    editor,
    onOpen,
  }) {
    const open = state.openSection === section;
    const panelId = `ifz-setup-optional-${section}`;
    const row = el('li', {
      class: `ifz-setup-capability-row${ready ? ' is-done' : ''}${open ? ' is-open' : ''}`,
    },
    el('button', {
      class: 'ifz-setup-row-toggle',
      type: 'button',
      dataset: { setupSection: section },
      'aria-expanded': editor ? (open ? 'true' : 'false') : null,
      'aria-controls': editor ? panelId : null,
      onclick: onOpen || (() => setOpen(section)),
    },
    el('span', { class: 'ifz-setup-row-mark', 'aria-hidden': 'true' }, ready ? '✓' : '+'),
    el('span', { class: 'ifz-setup-row-title' },
      el('strong', {}, title),
      el('span', {}, description)),
    el('span', { class: 'ifz-setup-row-summary' },
      summary,
      el('small', {}, ready ? 'Added' : 'Optional')),
    el('span', { class: 'ifz-setup-row-chevron', 'aria-hidden': 'true' }, icon('arrowRight', 16))));
    if (open && editor) {
      row.append(el('div', {
        class: 'ifz-setup-editor',
        id: panelId,
      }, editor()));
    }
    return row;
  }

  function contactsEditor() {
    const contactFiles = state.documents.filter(document => document.type === 'current_contacts');
    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Add a contact list if you already have one. Individual people can be managed in Buyers.'),
      contactFiles.length
        ? el('ul', { class: 'ifz-setup-document-list' },
            contactFiles.map(document =>
              el('li', {},
                el('div', {},
                  el('strong', {}, document.name || 'Contact list'),
                  el('span', {}, 'Saved for future matching')),
                badge(document.status || 'uploaded'))))
        : null,
      state.contacts.length
        ? el('p', { class: 'ifz-setup-inline-note' },
            `${state.contacts.length} ${state.contacts.length === 1 ? 'person is' : 'people are'} already in Buyers.`)
        : null,
      el('div', { class: 'ifz-setup-editor-actions' },
        button('Upload contact list', {
          icon: 'upload',
          onClick: () => openDocumentUpload('current_contacts'),
        }),
        button('Open Buyers', {
          icon: 'arrowRight',
          onClick: () => ctx.navigate('/app/buyers'),
        })));
  }

  function brainContent() {
    return state.brain?.sections || {
      product_understanding: [],
      ideal_customer_profile: [],
      buyer_roles: [],
      market_assumptions: [],
      sales_arguments: [],
      missing_data: [],
    };
  }

  function usefulMissingData() {
    const reported = arrayOf(brainContent().missing_data);
    const startingPoints = state.brain?.id
      ? reported
      : [
          !hasDocument('price_list') ? 'Current export price list' : '',
          !hasDocument('current_contacts') ? 'Current customer and prospect contacts' : '',
          !hasDocument('past_sales') ? 'Recent won and lost sales history' : '',
        ];
    return startingPoints
      .map(missingCopy)
      .filter(Boolean)
      .filter(item => {
        const lower = item.toLowerCase();
        if (hasDocument('price_list') && lower.includes('price')) return false;
        if (hasDocument('current_contacts') && lower.includes('contact')) return false;
        if (hasDocument('past_sales') && /(won|lost|sales history)/.test(lower)) return false;
        return true;
      });
  }

  function missingDataAction(item) {
    const lower = item.toLowerCase();
    const type = lower.includes('price')
      ? 'price_list'
      : lower.includes('contact')
        ? 'current_contacts'
        : /(won|lost|sales history|past sale)/.test(lower)
          ? 'past_sales'
          : 'other';
    return button('Add file', {
      size: 'sm',
      icon: 'upload',
      onClick: () => openDocumentUpload(type),
    });
  }

  function renderBrainBand() {
    const built = Boolean(state.brain?.id);
    const approved = state.brain?.status === 'approved';
    const draft = state.brain?.status === 'draft';
    const open = state.openSection === 'brain';
    const gaps = usefulMissingData();
    let summary = 'Build this after the essentials are in place.';
    if (!state.known.brain) summary = "We couldn't check this right now. The rest of Setup still works.";
    else if (approved) {
      summary = state.brain.approved_at
        ? `Reviewed ${fmt.ago(state.brain.approved_at)}. This context is used for buyer matching and email drafts.`
        : 'Reviewed and used for buyer matching and email drafts.';
    } else if (draft) {
      summary = 'A draft is ready for you to review.';
    } else if (built) {
      summary = 'Review the current assumptions before they are used.';
    }

    const actions = [];
    if (state.known.brain && !built) {
      actions.push(button('Build from my files', {
        kind: 'primary',
        icon: 'sparkle',
        disabled: state.runtime?.agent_runs_enabled !== true,
        title: state.runtime?.agent_runs_enabled === true ? null : 'The agent is offline right now',
        onClick: () => runBrainWork('brain.build'),
      }));
    } else if (state.known.brain && approved) {
      actions.push(button('Refresh from files', {
        icon: 'refresh',
        disabled: state.runtime?.agent_runs_enabled !== true,
        title: state.runtime?.agent_runs_enabled === true ? null : 'The agent is offline right now',
        onClick: () => runBrainWork('brain.rebuild'),
      }));
    }
    if (draft) {
      actions.push(
        button('Edit assumptions', { icon: 'edit', onClick: openBrainEditor }),
        button('Approve knowledge', { kind: 'primary', icon: 'check', onClick: approveBrain }),
      );
    }
    if (built) {
      actions.unshift(button(open ? 'Hide details' : 'Review details', {
        icon: 'arrowRight',
        onClick: () => setOpen('brain'),
      }));
    }

    return el('section', {
      class: `ifz-setup-band ifz-setup-brain${open ? ' is-open' : ''}`,
      'aria-labelledby': 'ifz-setup-brain-title',
    },
    el('div', { class: 'ifz-setup-band-heading' },
      el('div', {},
        el('span', { class: 'ifz-setup-kicker' }, 'Company knowledge'),
        el('h2', { id: 'ifz-setup-brain-title' }, 'What we know about you'),
        el('p', {}, summary)),
      el('div', {
        class: 'ifz-setup-band-actions',
        dataset: { setupSection: 'brain' },
        tabindex: '-1',
      }, actions)),
    !state.known.brain
      ? el('div', { class: 'ifz-setup-band-body' },
          el('p', { class: 'ifz-setup-offline', role: 'status' },
            "Company knowledge couldn't be checked. Your saved Setup information is unchanged."))
      : el('div', { class: 'ifz-setup-band-body' },
          el('div', { class: 'ifz-setup-gap-heading' },
            el('strong', {}, 'Missing information'),
            el('span', {}, gaps.length
              ? 'Add these when you have them. None blocks Setup.'
              : 'Nothing is missing right now.')),
          gaps.length
            ? el('ul', { class: 'ifz-setup-gap-list' },
                gaps.map(item =>
                  el('li', { class: 'ifz-setup-gap-row' },
                    el('span', {}, item),
                    missingDataAction(item))))
            : el('p', { class: 'ifz-setup-gap-empty' }, 'The agent has enough context for now.'),
          state.runtime?.agent_runs_enabled === false && (!built || approved)
            ? el('p', { class: 'ifz-setup-offline', role: 'status' },
                'The agent is offline right now. Your saved knowledge is safe and can be refreshed when it reconnects.')
            : null,
          state.brainSentence
            ? el('p', {
                class: 'ifz-setup-work',
                role: 'status',
                'aria-live': 'polite',
              }, state.brainSentence)
            : null,
          open && built ? brainDetail() : null));
  }

  function brainDetail() {
    const sections = brainContent();
    const groups = [
      ['Product understanding', sections.product_understanding],
      ['Ideal customer profile', sections.ideal_customer_profile],
      ['Buyer roles', sections.buyer_roles],
      ['Market assumptions', sections.market_assumptions],
      ['Sales arguments', sections.sales_arguments],
    ];
    return el('div', {
      class: 'ifz-setup-brain-detail',
      id: 'ifz-setup-brain-detail',
    },
    groups.map(([title, values]) =>
      el('section', { class: 'ifz-setup-brain-group' },
        el('h3', {}, title),
        arrayOf(values).length
          ? el('ul', {}, values.map(value => el('li', {}, value)))
          : el('p', {}, 'No notes yet.'))));
  }

  async function runBrainWork(route) {
    state.brainSentence = route === 'brain.build'
      ? 'Building company knowledge from your saved files.'
      : 'Refreshing company knowledge from your saved files.';
    render();
    try {
      const run = await call(route, {
        body: { source_document_ids: state.documents.map(document => document.id) },
      });
      const completed = await waitForRun(run, {
        timeoutMs: 120000,
        intervalMs: 500,
        onUpdate: current => {
          state.brainSentence = runSentence(current);
          render();
        },
      });
      if (['failed', 'error', 'cancelled'].includes(completed?.status)) throw new Error('failed');
      state.brainSentence = 'Company knowledge is ready for review.';
      state.openSection = 'brain';
      if (completed?.output_ref) {
        const snapshot = await call('brain.snapshot', {
          params: { snapshotId: completed.output_ref },
        });
        state.brain = normalizeBrainRecord(snapshot);
      } else {
        state.brain = normalizeBrainRecord(await call('brain.get'));
      }
      render();
      toast('Company knowledge is ready for review.', 'success');
    } catch {
      state.brainSentence = "We couldn't finish that update. Your saved knowledge is unchanged.";
      render();
    }
  }

  function openBrainEditor() {
    if (state.brain?.status !== 'draft') {
      toast('Refresh the knowledge first, then edit the new draft.', 'warning');
      return;
    }
    const sections = brainContent();
    const assumptions = textarea({
      value: arrayOf(sections.market_assumptions).join('\n'),
      rows: 7,
    });
    const argumentsBox = textarea({
      value: arrayOf(sections.sales_arguments).join('\n'),
      rows: 6,
    });
    const save = button('Save assumptions', { kind: 'primary', icon: 'check' });
    const error = errorLine();
    const dialog = modal({
      title: 'Edit company knowledge',
      wide: true,
      body: el('div', {},
        field('Market assumptions', assumptions, { hint: 'One useful assumption per line.' }),
        field('Sales arguments', argumentsBox, { hint: 'One reusable sales point per line.' }),
        error),
      actions: [
        button('Cancel', { onClick: () => dialog.close() }),
        save,
      ],
    });
    save.addEventListener('click', async () => {
      setBusy(save, true, 'Saving...');
      try {
        state.brain = await call('brain.update', {
          body: {
            market_assumptions: assumptions.value.split('\n').map(value => value.trim()).filter(Boolean),
            sales_arguments: argumentsBox.value.split('\n').map(value => value.trim()).filter(Boolean),
          },
        });
        dialog.close();
        render();
        toast('Company knowledge updated.', 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save those assumptions. Try again.");
      }
    });
  }

  async function approveBrain(event) {
    const control = event?.currentTarget;
    if (!state.brain?.id || state.brain.status !== 'draft') return;
    setBusy(control, true, 'Approving...');
    try {
      state.brain = await call('brain.approve', { body: { snapshot_id: state.brain.id } });
      await markOptional('onboarding.reviewBrain', {
        snapshot_id: state.brain.id,
        reviewed: true,
      });
      await load({ soft: true });
      toast('Company knowledge approved.', 'success');
    } catch {
      setBusy(control, false);
      toast("We couldn't approve that knowledge. Nothing was changed.", 'error');
    }
  }

  function blockedPeopleCount() {
    const blockedLeads = state.leads.filter(lead =>
      lead.do_not_contact || lead.status === 'do_not_contact').length;
    const blockedContacts = state.contacts.filter(contact => contact.do_not_contact).length;
    return { blockedLeads, blockedContacts, total: blockedLeads + blockedContacts };
  }

  function renderSendingBand() {
    const blocked = blockedPeopleCount();
    const mailbox = state.emailIntegrations[0];
    const templateCount = Object.values(state.templates)
      .filter(template => template?.subject || template?.body).length;
    const allowedLanguages = arrayOf(state.preferences.languages);
    const preferencesSummary = [
      sendModeLabel(state.preferences.default_send_mode),
      `max ${Number(state.preferences.daily_email_limit || 50)}/day`,
      allowedLanguages.length
        ? phrase(allowedLanguages.map(languageLabel))
        : languageLabel(state.preferences.default_language || 'en'),
    ].join(' · ');

    return el('section', {
      class: 'ifz-setup-band ifz-setup-sending',
      'aria-labelledby': 'ifz-setup-sending-title',
    },
    el('div', { class: 'ifz-setup-band-heading' },
      el('div', {},
        el('span', { class: 'ifz-setup-kicker' }, 'Outreach preferences'),
        el('h2', { id: 'ifz-setup-sending-title' }, 'How we send'),
        el('p', {}, 'Safe defaults, channel connections, and the people who must never be contacted.'))),
    el('div', { class: 'ifz-setup-band-body' },
      el('ul', { class: 'ifz-setup-capability-list' },
        settingsRow({
          section: 'sending',
          title: 'Email preferences',
          description: 'Approval mode, languages, sending windows and limits.',
          summary: preferencesSummary,
          editor: preferencesEditor,
        }),
        settingsRow({
          section: 'mailbox',
          title: 'Mailbox',
          description: 'Connect, check or remove the mailbox used for approved emails.',
          summary: !state.known.mailbox
            ? "Couldn't check right now"
            : mailbox
              ? emailMailbox(mailbox) || providerLabel(mailbox.provider)
              : 'Not connected',
          editor: mailboxEditor,
        }),
        settingsRow({
          section: 'email-style',
          title: 'How our emails sound',
          description: 'Subject and body guidance for each language.',
          summary: templateCount
            ? `${templateCount} ${templateCount === 1 ? 'language' : 'languages'} prepared`
            : 'Add your first email style',
          editor: emailStyleEditor,
        }),
        settingsRow({
          section: 'whatsapp',
          title: 'WhatsApp Business',
          description: 'Save the business profile; an administrator finishes the secure connection.',
          summary: !state.known.whatsapp
            ? "Couldn't check right now"
            : state.whatsapp
              ? state.whatsapp.profile_state === 'verified' ? 'Profile verified' : 'Profile saved'
              : 'Not added',
          editor: whatsappEditor,
        }),
        actionRow({
          title: 'LinkedIn',
          description: 'Notes only - you send them yourself.',
          summary: 'Manual',
          label: 'Open Buyers',
          onClick: () => ctx.navigate('/app/buyers'),
        }),
        actionRow({
          title: 'Who we never contact',
          description: blocked.total
            ? `${blocked.blockedLeads} ${blocked.blockedLeads === 1 ? 'company' : 'companies'} and ${blocked.blockedContacts} ${blocked.blockedContacts === 1 ? 'person' : 'people'} blocked.`
            : 'No companies or people are blocked.',
          summary: blocked.total ? `${blocked.total} blocked` : 'None',
          label: 'Review list',
          onClick: () => ctx.navigate('/app/buyers?state=blocked'),
        }),
        settingsRow({
          section: 'account',
          title: 'Account',
          description: 'Your workspace and access level.',
          summary: getSession()?.user?.email || db.user?.email || 'Signed in',
          editor: accountEditor,
        }))));
  }

  function settingsRow({ section, title, description, summary, editor }) {
    const open = state.openSection === section;
    const panelId = `ifz-setup-sending-${section}`;
    const row = el('li', {
      class: `ifz-setup-capability-row${open ? ' is-open' : ''}`,
    },
    el('button', {
      class: 'ifz-setup-row-toggle',
      type: 'button',
      dataset: { setupSection: section },
      'aria-expanded': open ? 'true' : 'false',
      'aria-controls': panelId,
      onclick: () => setOpen(section),
    },
    el('span', { class: 'ifz-setup-row-title' },
      el('strong', {}, title),
      el('span', {}, description)),
    el('span', { class: 'ifz-setup-row-summary' }, summary),
    el('span', { class: 'ifz-setup-row-chevron', 'aria-hidden': 'true' }, icon('arrowRight', 16))));
    if (open) {
      row.append(el('div', {
        class: 'ifz-setup-editor',
        id: panelId,
      }, editor()));
    }
    return row;
  }

  function actionRow({ title, description, summary, label, onClick }) {
    return el('li', { class: 'ifz-setup-capability-row ifz-setup-action-row' },
      el('div', { class: 'ifz-setup-row-toggle' },
        el('span', { class: 'ifz-setup-row-title' },
          el('strong', {}, title),
          el('span', {}, description)),
        el('span', { class: 'ifz-setup-row-summary' }, summary),
        button(label, { size: 'sm', icon: 'arrowRight', onClick })));
  }

  function preferencesEditor() {
    const preferences = state.preferences;
    const mailbox = input({
      value: preferences.connected_mailbox || emailMailbox(state.emailIntegrations[0]),
      placeholder: 'sales@example.com',
      autocomplete: 'email',
    });
    const mode = select(SEND_MODES, {
      value: preferences.default_send_mode || 'create_draft',
    });
    const defaultLanguage = select(languageOptions(), {
      value: preferences.default_language || 'en',
    });
    const languages = chipSelect(
      languageOptions(),
      arrayOf(preferences.languages).length
        ? preferences.languages
        : [preferences.default_language || 'en'],
    );
    const ccOptions = [
      { value: '', label: 'No default CC rule' },
      ...state.ccRules.map(rule => ({ value: rule.id, label: rule.name || 'CC rule' })),
    ];
    const ccRule = select(ccOptions, {
      value: preferences.default_cc_rule_id || '',
    });
    const dailyLimit = input({
      type: 'number',
      min: 1,
      max: 500,
      value: String(preferences.daily_email_limit || 50),
    });
    const windows = input({
      value: preferences.send_windows || '09:00-12:00,13:00-15:00',
      placeholder: '09:00-12:00,13:00-15:00',
    });
    const excluded = textarea({
      value: Array.isArray(preferences.excluded_industries)
        ? preferences.excluded_industries.join('\n')
        : String(preferences.excluded_industries || ''),
      rows: 4,
    });
    const save = button('Save email preferences', { kind: 'primary', icon: 'check' });
    const error = errorLine();
    save.addEventListener('click', async () => {
      const selectedLanguages = languages.getSelected();
      if (!selectedLanguages.includes(defaultLanguage.value)) {
        showError(error, 'The default language must also be in the allowed language list.');
        return;
      }
      const limit = Number(dailyLimit.value);
      if (!Number.isFinite(limit) || limit < 1 || limit > 500) {
        showError(error, 'Choose a daily limit between 1 and 500.');
        dailyLimit.focus();
        return;
      }
      setBusy(save, true, 'Saving...');
      try {
        state.preferences = await call('company.updateSalesPreferences', {
          body: {
            connected_mailbox: mailbox.value.trim(),
            default_send_mode: mode.value,
            default_language: defaultLanguage.value,
            languages: selectedLanguages,
            default_cc_rule_id: ccRule.value || null,
            daily_email_limit: limit,
            send_windows: windows.value.trim(),
            excluded_industries: excluded.value
              .split('\n')
              .map(value => value.trim())
              .filter(Boolean),
          },
        });
        render();
        toast('Email preferences saved.', 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save these preferences. Try again.");
      }
    });
    return el('div', {},
      el('div', { class: 'ifz-form-row' },
        field('Approval mode', mode),
        field('Preferred mailbox label', mailbox, {
          hint: 'Connection status comes from the mailbox row above.',
        })),
      el('div', { class: 'ifz-form-row' },
        field('Default language', defaultLanguage),
        field('Default CC rule', ccRule)),
      field('Allowed languages', languages),
      el('div', { class: 'ifz-form-row' },
        field('Daily email limit', dailyLimit),
        field('Sending windows', windows, {
          hint: 'Use 24-hour time, separated by commas.',
        })),
      field('Industries to avoid', excluded, { hint: 'One industry per line.' }),
      error,
      el('div', { class: 'ifz-setup-editor-actions' }, save));
  }

  function mailboxEditor() {
    if (!state.known.mailbox) {
      return el('div', {},
        el('p', { class: 'ifz-setup-offline', role: 'status' },
          "Mailbox connections couldn't be checked. The rest of Setup is still available."),
        el('div', { class: 'ifz-setup-editor-actions' },
          button('Try again', { onClick: () => load({ soft: true }) })));
    }
    const items = state.emailIntegrations;
    return el('div', {},
      el('p', { class: 'ifz-setup-editor-intro' },
        'Connect the mailbox used for approved outreach. Connection details stay encrypted on the server.'),
      items.length
        ? el('ul', { class: 'ifz-setup-provider-list' },
            items.map(integration =>
              el('li', { class: 'ifz-setup-provider-row' },
                el('span', { class: 'ifz-setup-provider-mark', 'aria-hidden': 'true' },
                  EMAIL_PROVIDERS.find(item => item.key === integration.provider)?.mark || '@'),
                el('span', { class: 'ifz-setup-provider-copy' },
                  el('strong', {}, providerLabel(integration.provider)),
                  el('span', {}, emailMailbox(integration) || 'Connected mailbox')),
                el('div', { class: 'ifz-setup-provider-actions' },
                  button('Check', {
                    size: 'sm',
                    onClick: event => testMailbox(integration, event.currentTarget),
                  }),
                  button('Disconnect', {
                    size: 'sm',
                    kind: 'danger',
                    onClick: () => disconnectMailbox(integration),
                  })))))
        : emptyState({
            icon: 'mail',
            title: 'No mailbox connected',
            hint: 'You can finish Setup now and connect one when you are ready to send.',
          }),
      el('div', { class: 'ifz-setup-editor-actions' },
        button('Connect a mailbox', {
          kind: items.length ? '' : 'primary',
          icon: 'plus',
          onClick: openMailboxPicker,
        })));
  }

  async function testMailbox(integration, control) {
    setBusy(control, true, 'Checking...');
    try {
      await call('emailIntegrations.test', {
        params: { integrationId: integration.id },
      });
      setBusy(control, false);
      toast('Mailbox connection works.', 'success');
    } catch {
      setBusy(control, false);
      toast("We couldn't reach this mailbox. Your connection details are unchanged.", 'error');
    }
  }

  function disconnectMailbox(integration) {
    confirmAction({
      title: 'Disconnect this mailbox?',
      copy: 'Saved buyers and email drafts will stay. New outreach cannot use this mailbox until another is connected.',
      label: 'Disconnect mailbox',
      onConfirm: async () => {
        await call('emailIntegrations.delete', {
          params: { integrationId: integration.id },
        });
        await load({ soft: true });
        toast('Mailbox disconnected.', 'warning');
      },
    });
  }

  function openMailboxPicker() {
    let dialog;
    const choices = EMAIL_PROVIDERS.map(provider =>
      button(provider.title, {
        onClick: event => {
          if (provider.credentials === 'smtp') {
            dialog.close();
            openSmtpConnection(provider);
            return;
          }
          if (provider.credentials === 'browser') {
            dialog.close();
            openBrowserConnection(provider);
            return;
          }
          connectDirectMailbox(provider, event.currentTarget, dialog);
        },
      }));
    dialog = modal({
      title: 'Connect a mailbox',
      body: el('div', { class: 'ifz-setup-provider-picker' },
        el('p', {}, 'Choose the service your team sends from.'),
        choices),
      actions: button('Cancel', { onClick: () => dialog.close() }),
    });
  }

  async function connectDirectMailbox(provider, control, dialog) {
    setBusy(control, true, 'Connecting...');
    try {
      await call(provider.route, { body: { credentials: {}, data: {} } });
      dialog.close();
      await markOptional('onboarding.updateIntegrations', {
        email_provider: provider.key,
        connected: true,
      });
      await load({ soft: true });
      toast(`${provider.title} connected.`, 'success');
    } catch {
      setBusy(control, false);
      toast(`We couldn't connect ${provider.title}. Try another option or ask an administrator for help.`, 'error');
    }
  }

  function openSmtpConnection(provider) {
    const presets = Object.entries(SMTP_PRESETS)
      .map(([value, preset]) => ({ value, label: preset.label }));
    const preset = select(presets, { value: 'gmail' });
    const username = input({
      type: 'email',
      placeholder: 'you@example.com',
      autocomplete: 'username',
    });
    const secret = passwordField({
      placeholder: 'App password or mailbox password',
      autocomplete: 'current-password',
    });
    const host = input({ value: SMTP_PRESETS.gmail.smtp_host });
    const port = input({ type: 'number', value: String(SMTP_PRESETS.gmail.smtp_port) });
    const imap = input({ value: SMTP_PRESETS.gmail.imap_host });
    const save = button('Connect mailbox', { kind: 'primary', icon: 'check' });
    const error = errorLine();
    preset.addEventListener('change', () => {
      const values = SMTP_PRESETS[preset.value];
      host.value = values.smtp_host;
      port.value = String(values.smtp_port);
      imap.value = values.imap_host;
    });
    const dialog = modal({
      title: 'Connect any email service',
      body: el('div', {},
        el('p', { class: 'ifz-setup-editor-intro' },
          'Use an app password when your provider supports one. The browser never stores these details.'),
        field('Email service', preset),
        el('div', { class: 'ifz-form-row' },
          field('Email address', username, { required: true }),
          field('Password', secret.wrap, { required: true })),
        el('div', { class: 'ifz-form-row' },
          field('SMTP host', host, { required: true }),
          field('SMTP port', port)),
        field('IMAP host', imap, { hint: 'Optional. Used for drafts and reply checks.' }),
        error),
      actions: [
        button('Cancel', { onClick: () => dialog.close() }),
        save,
      ],
    });
    save.addEventListener('click', async () => {
      if (!username.value.trim() || !secret.input.value || !host.value.trim()) {
        showError(error, 'Email address, password and SMTP host are required.');
        return;
      }
      setBusy(save, true, 'Connecting...');
      try {
        await call(provider.route, {
          body: {
            credentials: {
              username: username.value.trim(),
              password: secret.input.value,
              smtp_host: host.value.trim(),
              smtp_port: Number(port.value) || 587,
              imap_host: imap.value.trim() || undefined,
              from_addr: username.value.trim(),
            },
          },
        });
        dialog.close();
        await markOptional('onboarding.updateIntegrations', {
          email_provider: 'smtp',
          connected: true,
        });
        await load({ soft: true });
        toast('Mailbox connected.', 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't connect this mailbox. Check the details and try again.");
      }
    });
  }

  function openBrowserConnection(provider) {
    const url = input({ type: 'url', placeholder: 'https://mail.example.com' });
    const username = input({ placeholder: 'you@example.com', autocomplete: 'username' });
    const secret = passwordField({
      placeholder: 'Mailbox password',
      autocomplete: 'current-password',
    });
    const hint = input({ placeholder: 'Roundcube, Zimbra or another provider' });
    const save = button('Connect webmail', { kind: 'primary', icon: 'check' });
    const error = errorLine();
    const dialog = modal({
      title: 'Connect webmail in a browser',
      body: el('div', {},
        el('p', { class: 'ifz-setup-editor-intro' },
          'Use this for services without direct mailbox access. Accounts with a sign-in challenge may need administrator help.'),
        field('Webmail URL', url, { required: true }),
        el('div', { class: 'ifz-form-row' },
          field('Email or username', username, { required: true }),
          field('Password', secret.wrap, { required: true })),
        field('Provider hint', hint),
        error),
      actions: [
        button('Cancel', { onClick: () => dialog.close() }),
        save,
      ],
    });
    save.addEventListener('click', async () => {
      if (!url.value.trim() || !username.value.trim() || !secret.input.value) {
        showError(error, 'Webmail URL, username and password are required.');
        return;
      }
      setBusy(save, true, 'Connecting...');
      try {
        await call(provider.route, {
          body: {
            credentials: {
              webmail_url: url.value.trim(),
              username: username.value.trim(),
              password: secret.input.value,
              provider_hint: hint.value.trim() || undefined,
            },
          },
        });
        dialog.close();
        await markOptional('onboarding.updateIntegrations', {
          email_provider: 'browser',
          connected: true,
        });
        await load({ soft: true });
        toast('Webmail connected.', 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't connect this webmail. Check the details and try again.");
      }
    });
  }

  function emailStyleEditor() {
    if (!state.known.templates) {
      return el('div', {},
        el('p', { class: 'ifz-setup-offline', role: 'status' },
          "Email styles couldn't be checked. Your saved styles are unchanged."),
        el('div', { class: 'ifz-setup-editor-actions' },
          button('Try again', { onClick: () => load({ soft: true }) })));
    }
    const languages = languageOptions();
    let active = state.preferences.default_language || languages[0]?.value || 'en';
    if (!languages.some(language => language.value === active)) active = languages[0]?.value || 'en';
    const host = el('div', { class: 'ifz-setup-template-editor' });

    function draw() {
      const current = state.templates[active] || { subject: '', body: '' };
      const subject = input({
        value: current.subject || '',
        placeholder: 'A clear partnership opportunity',
      });
      const body = textarea({
        value: current.body || '',
        rows: 10,
        placeholder: 'Hello {{contact_name}}, ...',
      });
      const remember = () => {
        state.templates[active] = {
          subject: subject.value,
          body: body.value,
        };
      };
      subject.addEventListener('input', remember);
      body.addEventListener('input', remember);
      const save = button('Save email style', { kind: 'primary', icon: 'check' });
      const error = errorLine();
      save.addEventListener('click', async () => {
        remember();
        setBusy(save, true, 'Saving...');
        try {
          const saved = await call('company.updateEmailTemplates', {
            body: { templates: state.templates },
          });
          state.templates = { ...(saved?.templates || saved?.data?.templates || state.templates) };
          draw();
          toast(`${languageLabel(active)} email style saved.`, 'success');
        } catch {
          setBusy(save, false);
          showError(error, "We couldn't save this email style. Try again.");
        }
      });
      host.replaceChildren(
        tabs(
          languages.map(language => ({ key: language.value, label: language.label })),
          active,
          key => {
            remember();
            active = key;
            draw();
          },
        ),
        el('div', { class: 'ifz-setup-template-fields' },
          field('Subject guidance', subject, {
            hint: 'Use a fixed subject only when your company requires one.',
          }),
          field('Email body guidance', body)),
        el('div', { class: 'ifz-setup-placeholders' },
          el('span', {}, 'Available details'),
          PLACEHOLDERS.map(value => el('code', {}, value))),
        error,
        el('div', { class: 'ifz-setup-editor-actions' }, save));
    }
    draw();
    return host;
  }

  function whatsappVerificationCopy(verification) {
    if (verification?.status === 'verified') return 'WhatsApp details verified.';
    if (verification?.status === 'incomplete') {
      return 'Your WhatsApp profile is saved. An administrator still needs to finish the secure connection.';
    }
    if (verification?.status === 'failed') {
      return "We couldn't verify the connection. Your saved profile is unchanged; ask an administrator to check it.";
    }
    return 'Your WhatsApp profile is saved. The secure connection has not been checked yet.';
  }

  function whatsappEditor() {
    if (!state.known.whatsapp) {
      return el('div', {},
        el('p', { class: 'ifz-setup-offline', role: 'status' },
          "WhatsApp details couldn't be checked. The rest of Setup is still available."),
        el('div', { class: 'ifz-setup-editor-actions' },
          button('Try again', { onClick: () => load({ soft: true }) })));
    }
    const whatsapp = state.whatsapp;
    const business = input({
      value: whatsapp?.business_name || state.profile.name || '',
      autocomplete: 'organization',
    });
    const waba = input({
      value: whatsapp?.whatsapp_business_account_id || '',
      placeholder: 'Business Account ID',
    });
    const phoneId = input({
      value: whatsapp?.phone_number_id || '',
      placeholder: 'Phone number ID',
    });
    const display = input({
      value: whatsapp?.display_phone_number || '',
      type: 'tel',
      placeholder: '+90 212 555 0101',
    });
    const country = select(countryOptions(), {
      value: whatsapp?.business_country || state.profile.headquarters_country || 'TR',
    });
    const language = select(languageOptions(), {
      value: whatsapp?.default_language || state.preferences.default_language || 'en',
    });
    const save = button(whatsapp ? 'Save profile' : 'Add Business profile', {
      kind: 'primary',
      icon: 'check',
    });
    const verify = button('Check connection', {
      icon: 'check',
      disabled: !whatsapp,
    });
    const error = errorLine();
    save.addEventListener('click', async () => {
      if (!business.value.trim() || !waba.value.trim() || !phoneId.value.trim()) {
        showError(error, 'Business name, Account ID and Phone number ID are required.');
        return;
      }
      setBusy(save, true, 'Saving...');
      try {
        state.whatsapp = await call('whatsapp.saveProfile', {
          body: {
            business_name: business.value.trim(),
            whatsapp_business_account_id: waba.value.trim(),
            phone_number_id: phoneId.value.trim(),
            display_phone_number: display.value.trim(),
            business_country: country.value,
            default_language: language.value,
          },
        });
        render();
        toast('WhatsApp Business profile saved.', 'success');
      } catch {
        setBusy(save, false);
        showError(error, "We couldn't save the WhatsApp profile. Check the details and try again.");
      }
    });
    verify.addEventListener('click', async () => {
      setBusy(verify, true, 'Checking...');
      try {
        const result = await call('whatsapp.verifyProfile');
        await load({ soft: true });
        toast(
          whatsappVerificationCopy(result),
          result?.status === 'verified' ? 'success' : result?.status === 'failed' ? 'error' : 'warning',
        );
      } catch {
        setBusy(verify, false);
        showError(error, "We couldn't check the WhatsApp connection. Your saved profile is unchanged.");
      }
    });
    return el('div', {},
      whatsapp
        ? el('p', { class: 'ifz-setup-inline-note', role: 'status' },
            whatsappVerificationCopy(whatsapp.verification))
        : el('p', { class: 'ifz-setup-editor-intro' },
            'Add the public Business profile now. Private connection details are handled by an administrator.'),
      el('div', { class: 'ifz-form-row' },
        field('Business display name', business, { required: true }),
        field('Display phone number', display)),
      el('div', { class: 'ifz-form-row' },
        field('WhatsApp Business Account ID', waba, { required: true }),
        field('Phone number ID', phoneId, { required: true })),
      el('div', { class: 'ifz-form-row' },
        field('Business country', country),
        field('Default language', language)),
      error,
      el('div', { class: 'ifz-setup-editor-actions' }, save, verify));
  }

  function accountEditor() {
    const session = getSession();
    const user = session?.user || db.user || {};
    const company = session?.company || {};
    const role = user.role === 'admin'
      ? 'Administrator'
      : user.role === 'manager'
        ? 'Manager'
        : 'Workspace member';
    return el('dl', { class: 'ifz-setup-account' },
      el('div', {},
        el('dt', {}, 'Name'),
        el('dd', {}, user.name || 'Signed-in user')),
      el('div', {},
        el('dt', {}, 'Email'),
        el('dd', {}, user.email || 'Not available')),
      el('div', {},
        el('dt', {}, 'Access'),
        el('dd', {}, role)),
      el('div', {},
        el('dt', {}, 'Workspace'),
        el('dd', {}, company.name || state.profile.name || 'Current workspace')));
  }

  await load();
  return () => {
    disposed = true;
    pageEl.classList.remove('ifz-page--setup');
  };
}
