/* Buyers — companies, the people inside them, and their place in one pipeline. */

import {
  el, button, input, pageHead, pipelineRail, emptyState, toast, modal, setBusy,
  isApprovalActionable, isMessageSuperseded, runSentence, badge, fmt, icon,
} from '../ui.js';
import { call } from '../api.js';
import { COUNTRY_NAMES } from '../catalog.js';
import { companyRow, openBuyerCreateModal } from './_components.js';
import { exportCsv, openContactModal, waitForRun } from './_page-utils.js';
import { openLeadEvidence } from './research-evidence.js';
import { renderMiniMap } from './lead-map.js';

const SENT_STATUSES = new Set(['sent', 'sent_manually', 'replied']);
const WRITTEN_STATUSES = new Set([
  'pending_approval', 'draft_generated', 'qa_failed', 'approved',
  'draft', 'draft_created', 'sent', 'sent_manually', 'replied',
]);
const RESEARCHED_STATUSES = new Set(['researched', 'contacted', 'replied', 'interested']);

function itemsOf(value) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.items) ? value.items : [];
}

function hasNumber(value) {
  return value !== null && value !== undefined && value !== ''
    && Number.isFinite(Number(value));
}

function newest(items, field = 'created_at') {
  return items.slice().sort((a, b) =>
    new Date(b[field] || b.updated_at || b.sent_at || 0)
      - new Date(a[field] || a.updated_at || a.sent_at || 0))[0] || null;
}

function runLike(value) {
  return Boolean(value?.run_id || value?.type || value?.run_type)
    && !Object.prototype.hasOwnProperty.call(value || {}, 'subject');
}

function loadingView() {
  return el('div', { class: 'ifz-buyers-loading', role: 'status', 'aria-label': 'Loading buyers' },
    el('span', { class: 'ifz-buyers-loading-title' }),
    el('span', { class: 'ifz-buyers-loading-rail' }),
    Array.from({ length: 5 }, () => el('span', { class: 'ifz-buyers-loading-row' })));
}

function confirmDialog({
  title, copy, confirmLabel, onConfirm, kind = 'danger',
}) {
  const confirm = button(confirmLabel, { kind });
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
  return dialog;
}

function exportDialog() {
  let dialog;
  const exportButton = (label, route, hint) => {
    const action = button(label, { icon: 'download' });
    action.addEventListener('click', async () => {
      setBusy(action, true, 'Preparing…');
      try {
        await exportCsv(route);
        dialog.close();
      } catch {
        setBusy(action, false);
        toast("We couldn't prepare that export. Try again.", 'error');
      }
    });
    return el('div', { class: 'ifz-buyers-export-option' },
      el('div', {}, el('strong', {}, label), el('span', {}, hint)),
      action);
  };
  dialog = modal({
    title: 'Export Buyers',
    body: el('div', { class: 'ifz-buyers-export-options' },
      exportButton('Buyer companies CSV', 'exports.leads', 'Company, market, fit and current state'),
      exportButton('People CSV', 'exports.contacts', 'Names, roles, email status and company')),
  });
  return dialog;
}

export async function mount(root, ctx) {
  let disposed = false;
  let renderTimer = null;
  let renderSequence = 0;
  const feedbackTimers = new Map();
  const pageEl = root.closest('.ifz-page') || root;
  pageEl.classList.add('ifz-page--buyers');

  const state = {
    leads: [],
    contacts: [],
    messages: [],
    research: [],
    linkedinActions: [],
    selectedCountries: [],
    q: ctx.query.q || '',
    country: ctx.query.country || '',
    stateFilter: ctx.query.state || '',
    stageFilter: ctx.query.stage || '',
    expandedLeadId: ctx.query.buyer || ctx.query.company || ctx.query.lead || '',
    highlightedContactId: ctx.query.contact || ctx.query.person || '',
    mapOpen: ctx.query.map === '1',
    busy: new Set(),
    feedback: new Map(),
    details: new Map(),
    loaded: false,
  };

  const page = el('div', { class: 'ifz-buyers' }, loadingView());
  root.append(page);

  function isBusy(key) {
    return state.busy.has(key);
  }

  function setBusyState(key, busy) {
    if (busy) state.busy.add(key);
    else state.busy.delete(key);
  }

  function setFeedback(leadId, text, tone = 'info', { clearAfter = 0 } = {}) {
    const previous = feedbackTimers.get(leadId);
    if (previous) clearTimeout(previous);
    state.feedback.set(leadId, { text, tone });
    if (clearAfter) {
      const timer = setTimeout(() => {
        state.feedback.delete(leadId);
        feedbackTimers.delete(leadId);
        render();
      }, clearAfter);
      feedbackTimers.set(leadId, timer);
    }
  }

  function syncUrl() {
    const query = new URLSearchParams();
    if (state.expandedLeadId) query.set('buyer', state.expandedLeadId);
    if (state.highlightedContactId) query.set('contact', state.highlightedContactId);
    if (state.mapOpen) query.set('map', '1');
    const suffix = query.toString() ? `?${query}` : '';
    history.replaceState(
      null,
      '',
      `${location.pathname}${location.search}#/app/buyers${suffix}`,
    );
  }

  async function refreshCollections() {
    const [leads, contacts, messages] = await Promise.all([
      call('leads.list'),
      call('contacts.list'),
      call('messages.list'),
    ]);
    if (disposed) return;
    state.leads = itemsOf(leads);
    state.contacts = itemsOf(contacts);
    state.messages = itemsOf(messages).filter(message =>
      (message.channel == null || message.channel === 'email')
      && !isMessageSuperseded(message));

    const optional = await Promise.allSettled([
      call('research.list'),
      call('linkedin.actions'),
      call('leadMap.selectedCountries'),
    ]);
    if (disposed) return;
    if (optional[0].status === 'fulfilled') state.research = itemsOf(optional[0].value);
    if (optional[1].status === 'fulfilled') state.linkedinActions = itemsOf(optional[1].value);
    if (optional[2].status === 'fulfilled') state.selectedCountries = itemsOf(optional[2].value);
  }

  function contactsFor(leadId) {
    return state.contacts.filter(contact => contact.lead_id === leadId);
  }

  function messagesFor(leadId) {
    return state.messages.filter(message => message.lead_id === leadId);
  }

  function researchFor(lead) {
    const detail = state.details.get(lead.id);
    if (detail?.research) return detail.research;
    return newest(state.research.filter(item => item.lead_id === lead.id));
  }

  function linkedinFor(contactId) {
    return newest(state.linkedinActions.filter(action => action.contact_id === contactId), 'updated_at')
      || newest(state.linkedinActions.filter(action => action.contact_id === contactId));
  }

  function leadHasResearch(lead, research) {
    return Boolean(research?.status === 'completed' || research?.summary)
      || (hasNumber(lead.fit_score) && hasNumber(lead.evidence_confidence))
      || RESEARCHED_STATUSES.has(lead.status);
  }

  function buyerModel(lead) {
    const contacts = contactsFor(lead.id);
    const messages = messagesFor(lead.id);
    const research = researchFor(lead);
    const hasResearch = leadHasResearch(lead, research);
    const detail = state.details.get(lead.id);
    const currentScore = detail?.score || (hasResearch ? lead.score : { value: 0 });
    const blocked = Boolean(lead.do_not_contact || lead.status === 'do_not_contact');
    const archived = lead.status === 'archived';
    const eligibleContacts = contacts.filter(contact =>
      !contact.do_not_contact
      && Boolean(contact.email)
      && !['not_found', 'invalid', 'blocked'].includes(contact.email_status));
    const actionable = messages.find(message =>
      isApprovalActionable(message, {
        lead,
        contact: contacts.find(contact => contact.id === message.contact_id),
      }));
    const repliedMessage = messages.find(message =>
      message.status === 'replied' || message.replied_at);
    const sentWaiting = messages.find(message =>
      (SENT_STATUSES.has(message.status) || message.sent_at)
      && message.status !== 'replied'
      && !message.replied_at);
    const written = messages.some(message => WRITTEN_STATUSES.has(message.status));
    const busy = [...state.busy].some(key => key.endsWith(`:${lead.id}`));

    let stateLabel = 'Ready for an email';
    let stateDetail = '';
    let nextKind = 'write';
    if (archived) {
      stateLabel = 'Set aside';
      nextKind = '';
    } else if (blocked) {
      stateLabel = 'Never contact';
      nextKind = '';
    } else if (repliedMessage) {
      stateLabel = 'They replied';
      stateDetail = fmt.ago(repliedMessage.replied_at || repliedMessage.sent_at || repliedMessage.created_at);
      nextKind = 'reply';
    } else if (actionable) {
      stateLabel = 'Email waiting for you';
      stateDetail = 'Review before anything is saved or sent';
      nextKind = 'review';
    } else if (sentWaiting) {
      stateLabel = 'Waiting for a reply';
      stateDetail = sentWaiting.sent_at ? `Sent ${fmt.ago(sentWaiting.sent_at)}` : '';
      nextKind = 'waiting';
    } else if (!hasResearch) {
      stateLabel = 'Needs research';
      nextKind = 'research';
    } else if (!eligibleContacts.length) {
      stateLabel = 'Needs a contact';
      nextKind = 'contact';
    } else if (!written) {
      stateLabel = 'Ready for an email';
      nextKind = 'write';
    }

    return {
      lead, contacts, messages, research, hasResearch, currentScore, blocked, archived,
      eligibleContacts, actionable, repliedMessage, sentWaiting, written, busy,
      stateLabel, stateDetail, nextKind,
      needsYou: !archived && !blocked && ['research', 'contact', 'write', 'review'].includes(nextKind),
      waiting: !archived && !blocked && (busy || nextKind === 'waiting'),
      replied: Boolean(repliedMessage),
    };
  }

  function activeModels() {
    return state.leads.map(buyerModel).filter(model => !model.archived);
  }

  function pipelineCounts(models) {
    const ids = new Set(models.map(model => model.lead.id));
    const messages = state.messages.filter(message => ids.has(message.lead_id));
    return {
      found: models.length,
      researched: models.filter(model => model.hasResearch).length,
      contacts: models.reduce((total, model) => total + model.contacts.length, 0),
      written: messages.filter(message => WRITTEN_STATUSES.has(message.status)).length,
      sent: messages.filter(message => SENT_STATUSES.has(message.status) || message.sent_at).length,
      replied: messages.filter(message => message.status === 'replied' || message.replied_at).length,
    };
  }

  function matchesStage(model) {
    if (!state.stageFilter || state.stageFilter === 'found') return true;
    if (state.stageFilter === 'researched') return model.hasResearch;
    if (state.stageFilter === 'contacts') return model.contacts.length > 0;
    if (state.stageFilter === 'written') return model.written;
    if (state.stageFilter === 'sent') return model.messages.some(message =>
      SENT_STATUSES.has(message.status) || message.sent_at);
    if (state.stageFilter === 'replied') return model.replied;
    return true;
  }

  function filteredModels() {
    let models = state.leads.map(buyerModel);
    if (state.stateFilter === 'archived') models = models.filter(model => model.archived);
    else models = models.filter(model => !model.archived);
    if (state.country) models = models.filter(model => model.lead.country === state.country);
    if (state.stateFilter === 'needs') models = models.filter(model => model.needsYou);
    if (state.stateFilter === 'waiting') models = models.filter(model => model.waiting);
    if (state.stateFilter === 'replied') models = models.filter(model => model.replied);
    if (state.stateFilter === 'blocked') models = models.filter(model => model.blocked);
    if (state.q) {
      const needle = state.q.toLocaleLowerCase();
      models = models.filter(model =>
        [
          model.lead.company_name,
          model.lead.city,
          model.lead.industry,
          model.lead.website,
          ...model.contacts.flatMap(contact => [contact.name, contact.title, contact.email]),
        ].some(value => String(value || '').toLocaleLowerCase().includes(needle)));
    }
    models = models.filter(matchesStage);

    const rank = model => {
      if (model.replied) return 0;
      if (model.actionable) return 1;
      if (model.needsYou) return 2;
      if (model.waiting) return 3;
      return 4;
    };
    return models.sort((a, b) =>
      rank(a) - rank(b)
      || Number(b.currentScore?.value || 0) - Number(a.currentScore?.value || 0)
      || String(a.lead.company_name).localeCompare(String(b.lead.company_name)));
  }

  function nextPipelineAction(models) {
    const unresearched = models.filter(model => !model.hasResearch && !model.blocked);
    const waitingReview = models.filter(model => model.actionable);
    const missingPeople = models.filter(model =>
      model.hasResearch && !model.eligibleContacts.length && !model.blocked);
    const readyToWrite = models.filter(model =>
      model.hasResearch && model.eligibleContacts.length && !model.written && !model.blocked);

    if (unresearched.length) {
      const count = Math.min(unresearched.length, 12);
      return {
        text: `${unresearched.length} buyer${unresearched.length === 1 ? '' : 's'} still need research`,
        detail: unresearched.length > count ? `Research the next ${count} now.` : 'Research them together.',
        label: 'Research them',
        icon: 'search',
        onClick: researchBatch,
        busy: isBusy('bulk-research'),
        busyLabel: 'Researching…',
      };
    }
    if (waitingReview.length) {
      return {
        text: `${waitingReview.length} email${waitingReview.length === 1 ? '' : 's'} need your review`,
        detail: 'Nothing is sent from this screen.',
        label: 'Open Approvals',
        icon: 'arrowRight',
        onClick: () => ctx.navigate('/app/approvals'),
      };
    }
    if (missingPeople.length) {
      return {
        text: `${missingPeople.length} researched buyer${missingPeople.length === 1 ? '' : 's'} need the right person`,
        detail: 'Open a company to find or add a contact.',
        label: 'Show them',
        onClick: () => {
          state.stateFilter = 'needs';
          state.stageFilter = 'researched';
          render();
        },
      };
    }
    if (readyToWrite.length) {
      return {
        text: `${readyToWrite.length} buyer${readyToWrite.length === 1 ? ' is' : 's are'} ready for an email`,
        detail: 'Open a company to write one for review.',
        label: 'Show them',
        onClick: () => {
          state.stateFilter = 'needs';
          state.stageFilter = 'contacts';
          render();
        },
      };
    }
    return {
      text: 'Your current buyers have a clear next step',
      detail: 'Open any company to see its research, people and outreach.',
    };
  }

  function filterButton(label, value, group, { count } = {}) {
    const active = state[group] === value;
    const node = el('button', {
      class: `ifz-filter-chip${active ? ' on' : ''}`,
      type: 'button',
      'aria-pressed': active ? 'true' : 'false',
      onclick: () => {
        state[group] = active ? '' : value;
        render();
      },
    }, label, count != null ? el('span', { class: 'ifz-filter-count' }, String(count)) : null);
    return node;
  }

  function scheduleSearchRender() {
    if (renderTimer) clearTimeout(renderTimer);
    renderTimer = setTimeout(() => {
      renderTimer = null;
      render({ restoreSearch: true });
    }, 140);
  }

  function renderFilters(models) {
    const countries = [...new Set(state.leads.map(lead => lead.country).filter(Boolean))]
      .sort((a, b) => String(COUNTRY_NAMES[a] || a).localeCompare(COUNTRY_NAMES[b] || b));
    const search = input({
      id: 'ifz-buyers-search',
      type: 'search',
      value: state.q,
      placeholder: 'Search a company, person or email',
      autocomplete: 'off',
    });
    search.classList.add('ifz-buyers-search');
    search.addEventListener('input', () => {
      state.q = search.value;
      scheduleSearchRender();
    });

    return el('section', { class: 'ifz-buyers-filters', 'aria-label': 'Filter buyers' },
      search,
      el('div', { class: 'ifz-buyers-filter-scroll', role: 'group', 'aria-label': 'Markets' },
        filterButton('All markets', '', 'country'),
        countries.slice(0, 5).map(country =>
          filterButton(COUNTRY_NAMES[country] || country, country, 'country'))),
      el('div', { class: 'ifz-buyers-filter-scroll', role: 'group', 'aria-label': 'Buyer state' },
        filterButton('All', '', 'stateFilter'),
        filterButton('Needs you', 'needs', 'stateFilter', {
          count: models.filter(model => model.needsYou).length,
        }),
        filterButton('Waiting', 'waiting', 'stateFilter', {
          count: models.filter(model => model.waiting).length,
        }),
        filterButton('Replied', 'replied', 'stateFilter', {
          count: models.filter(model => model.replied).length,
        }),
        filterButton('Never contact', 'blocked', 'stateFilter', {
          count: models.filter(model => model.blocked).length,
        }),
        filterButton('Set aside', 'archived', 'stateFilter', {
          count: state.leads.filter(lead => lead.status === 'archived').length,
        })));
  }

  function renderMap(models, sequence) {
    const marketCodes = [...new Set(state.leads.map(lead => lead.country).filter(Boolean))];
    const counts = Object.fromEntries(marketCodes.map(code => [
      code,
      Math.max(70, Math.min(100, state.leads.filter(lead => lead.country === code).length * 8)),
    ]));
    const mapHost = el('div', { class: 'ifz-buyers-map-canvas ifz-minimap' });
    const selected = state.selectedCountries.length ? state.selectedCountries : marketCodes;
    const details = el('details', {
      class: 'ifz-buyers-map',
      open: state.mapOpen,
      ontoggle: event => {
        state.mapOpen = event.currentTarget.open;
        syncUrl();
        if (state.mapOpen && !mapHost.childElementCount) {
          renderMiniMap(mapHost, counts, selected, {
            active: state.country,
            interactive: marketCodes,
            onSelect: country => {
              state.country = state.country === country ? '' : country;
              render();
            },
          });
        }
      },
    },
    el('summary', {},
      el('span', {}, icon('map', 16), 'Market map'),
      el('span', { class: 'ifz-muted' },
        state.country ? `Showing ${COUNTRY_NAMES[state.country] || state.country}` : `${marketCodes.length} active markets`)),
    mapHost);

    if (state.mapOpen) {
      renderMiniMap(mapHost, counts, selected, {
        active: state.country,
        interactive: marketCodes,
        onSelect: country => {
          if (disposed || sequence !== renderSequence) return;
          state.country = state.country === country ? '' : country;
          render();
        },
      });
    }
    return details;
  }

  function busyAction(key, defaults = {}) {
    return {
      ...defaults,
      busy: isBusy(key) || isBusy('bulk-research'),
    };
  }

  function contactActions(model, contact) {
    const linkedIn = linkedinFor(contact.id) || {};
    const key = suffix => `${suffix}:${contact.id}`;
    return {
      linkedIn,
      verify: busyAction(key('verify-contact'), {
        hidden: !contact.email || contact.email_status === 'verified' || contact.do_not_contact,
        onClick: () => verifyContact(model.lead, contact),
      }),
      neverContact: busyAction(key('block-contact'), {
        hidden: contact.do_not_contact,
        onClick: () => blockContact(model.lead, contact),
      }),
      findLinkedIn: busyAction(key('find-linkedin'), {
        hidden: Boolean(contact.linkedin_url || linkedIn.profile_url),
        onClick: () => findLinkedIn(model.lead, contact),
      }),
      generateNote: busyAction(key('linkedin-note'), {
        hidden: !contact.linkedin_url && !linkedIn.profile_url,
        onClick: () => generateLinkedInNote(model.lead, contact),
      }),
      copyNote: linkedIn.note ? {
        onClick: () => copyLinkedInNote(linkedIn.note),
      } : null,
      markOpened: linkedIn.id ? busyAction(key('linkedin-opened'), {
        onClick: () => updateLinkedInAction(linkedIn, 'linkedin.markOpened', 'Profile marked as opened.'),
      }) : null,
      markSent: linkedIn.id ? busyAction(key('linkedin-sent'), {
        onClick: () => updateLinkedInAction(linkedIn, 'linkedin.markConnectionSent', 'Connection marked as sent.'),
      }) : null,
    };
  }

  function rowActions(model) {
    const lead = model.lead;
    const perContact = Object.fromEntries(model.contacts.map(contact => [
      contact.id,
      contactActions(model, contact),
    ]));
    const waitingMessage = model.actionable;
    return {
      research: busyAction(`research:${lead.id}`, {
        onClick: () => researchLead(lead),
      }),
      discover: busyAction(`contacts:${lead.id}`, {
        disabled: model.blocked,
        onClick: () => discoverContacts(lead),
      }),
      write: busyAction(`write:${lead.id}`, {
        disabled: model.blocked || !model.eligibleContacts.length,
        title: !model.eligibleContacts.length ? 'Find a valid email contact first' : null,
        onClick: () => writeEmail(lead),
      }),
      review: waitingMessage ? {
        onClick: () => ctx.navigate(`/app/approvals?message=${encodeURIComponent(waitingMessage.id)}`),
      } : null,
      addContact: {
        onClick: () => openContactModal({
          leadId: lead.id,
          onCreated: async () => {
            await refreshCollections();
            render();
          },
        }),
      },
      evidence: {
        onClick: () => openLeadEvidence(lead),
      },
      recalculate: busyAction(`score:${lead.id}`, {
        disabled: !model.hasResearch,
        title: model.hasResearch ? null : 'Research this buyer first',
        onClick: () => recalculateFit(lead),
      }),
      block: {
        onClick: () => blockCompany(lead),
      },
      archive: {
        onClick: () => archiveCompany(lead),
      },
      contacts: perContact,
    };
  }

  function renderRows(models) {
    if (!models.length) {
      const hasAny = state.leads.length > 0;
      return el('div', { class: 'ifz-buyers-empty' },
        emptyState({
          icon: 'leads',
          title: hasAny ? 'No buyers match these filters' : 'No buyers yet',
          hint: hasAny
            ? 'Clear the filters to see every company.'
            : 'Add a company you already know, or find buyers from Today.',
          action: hasAny
            ? button('Clear filters', {
                kind: 'primary',
                onClick: () => {
                  Object.assign(state, {
                    q: '', country: '', stateFilter: '', stageFilter: '',
                  });
                  render();
                },
              })
            : button('Add a buyer', {
                kind: 'primary',
                icon: 'plus',
                onClick: openAddBuyer,
              }),
        }));
    }

    return el('section', {
      class: 'ifz-company-ledger',
      'aria-label': `${models.length} buyer companies`,
    }, models.map(model => {
      const feedback = state.feedback.get(model.lead.id);
      return companyRow(model.lead, model.contacts, {
        expanded: state.expandedLeadId === model.lead.id,
        highlightedContactId: state.highlightedContactId,
        research: model.research,
        currentScore: model.currentScore,
        messages: model.messages,
        stateLabel: model.stateLabel,
        stateDetail: model.stateDetail,
        loading: state.details.get(model.lead.id)?.loading,
        progressText: feedback?.text,
        progressTone: feedback?.tone,
        actions: rowActions(model),
        onToggle: () => toggleBuyer(model.lead.id),
      });
    }));
  }

  function renderUnlinkedPeople() {
    const leadIds = new Set(state.leads.map(lead => lead.id));
    const unlinked = state.contacts.filter(contact =>
      !contact.lead_id || !leadIds.has(contact.lead_id));
    if (!unlinked.length) return null;
    return el('section', { class: 'ifz-unlinked-people' },
      el('div', {},
        el('span', { class: 'ifz-company-section-kicker' }, 'Needs a company'),
        el('h2', {}, `${unlinked.length} ${unlinked.length === 1 ? 'person is' : 'people are'} not linked to a buyer`),
        el('p', {}, 'They remain visible here and in your people export.')),
      el('ul', {}, unlinked.map(contact =>
        el('li', {},
          el('div', {},
            el('strong', {}, contact.name || contact.email || 'Unnamed person'),
            el('span', {}, contact.title || contact.email || 'Details not known')),
          badge(contact.do_not_contact ? 'do_not_contact' : (contact.email_status || 'unverified')),
          !contact.do_not_contact
            ? button('Never contact', {
                size: 'sm',
                kind: 'danger',
                onClick: () => blockContact(null, contact),
              })
            : null))));
  }

  function render({ restoreSearch = false } = {}) {
    if (disposed || !state.loaded) return;
    const sequence = ++renderSequence;
    const models = activeModels();
    const filtered = filteredModels();
    const counts = pipelineCounts(models);
    const filters = renderFilters(models);
    const map = renderMap(models, sequence);
    const unlinked = renderUnlinkedPeople();

    page.replaceChildren(
      pageHead({
        title: 'Buyers',
        sub: 'Companies, the people inside them, and where each relationship stands.',
        actions: [
          button('Export', { icon: 'download', onClick: exportDialog }),
          button('Add a buyer', { kind: 'primary', icon: 'plus', onClick: openAddBuyer }),
        ],
      }),
      pipelineRail(counts, nextPipelineAction(models), {
        active: state.stageFilter,
        onSelect: stage => {
          state.stageFilter = state.stageFilter === stage ? '' : stage;
          render();
        },
      }),
      filters,
      map,
      el('div', { class: 'ifz-buyers-result-summary', role: 'status' },
        `${filtered.length} buyer${filtered.length === 1 ? '' : 's'} shown`),
      renderRows(filtered),
      unlinked,
    );

    if (restoreSearch) {
      const search = page.querySelector('#ifz-buyers-search');
      search?.focus({ preventScroll: true });
      search?.setSelectionRange(state.q.length, state.q.length);
    }
  }

  async function loadBuyerDetails(leadId, { force = false } = {}) {
    if (!leadId || (!force && state.details.has(leadId))) return;
    const existingResearch = newest(state.research.filter(item => item.lead_id === leadId));
    const lead = state.leads.find(item => item.id === leadId);
    state.details.set(leadId, {
      loading: true,
      research: existingResearch,
      score: lead?.score,
    });
    render();
    const [research, score] = await Promise.allSettled([
      call('research.leadInsights', { params: { leadId } }),
      call('leads.score', { params: { leadId } }),
    ]);
    if (disposed) return;
    state.details.set(leadId, {
      loading: false,
      research: research.status === 'fulfilled' ? research.value : existingResearch,
      score: score.status === 'fulfilled' ? score.value : lead?.score,
    });
    render();
  }

  function toggleBuyer(leadId) {
    const opening = state.expandedLeadId !== leadId;
    state.expandedLeadId = opening ? leadId : '';
    state.highlightedContactId = opening ? state.highlightedContactId : '';
    syncUrl();
    render();
    if (opening) loadBuyerDetails(leadId);
  }

  function openAddBuyer() {
    openBuyerCreateModal({
      onCreated: async lead => {
        await refreshCollections();
        state.expandedLeadId = lead.id;
        state.highlightedContactId = '';
        syncUrl();
        render();
        await loadBuyerDetails(lead.id, { force: true });
      },
    });
  }

  async function waitForCustomerRun(run, leadId) {
    if (!runLike(run)) return run;
    const completed = await waitForRun(run, {
      timeoutMs: 120000,
      intervalMs: 300,
      onUpdate: current => {
        setFeedback(leadId, runSentence(current), 'info');
        render();
      },
    });
    if (completed?.status === 'failed' || completed?.status === 'error') {
      throw new Error('failed');
    }
    if (completed?.status === 'cancelled') throw new Error('cancelled');
    return completed;
  }

  async function runBuyerWork(lead, key, startCopy, work, successCopy) {
    const busyKey = `${key}:${lead.id}`;
    if (isBusy(busyKey)) return null;
    setBusyState(busyKey, true);
    setFeedback(lead.id, startCopy, 'info');
    render();
    try {
      const result = await work();
      await waitForCustomerRun(result, lead.id);
      await refreshCollections();
      state.details.delete(lead.id);
      setFeedback(lead.id, successCopy, 'success', { clearAfter: 5500 });
      toast(successCopy, 'success');
      return result;
    } catch (error) {
      const copy = key === 'write' && error?.status === 409
        ? 'Find or add a valid email contact before writing this email.'
        : key === 'research'
          ? "I couldn't finish the research. Try again."
          : key === 'contacts'
            ? "I couldn't finish looking for a contact. Try again."
            : key === 'write'
              ? "I couldn't finish writing the email. Try again."
              : "I couldn't finish that work. Try again.";
      setFeedback(lead.id, copy, 'error');
      toast(copy, 'error');
      return null;
    } finally {
      setBusyState(busyKey, false);
      render();
      loadBuyerDetails(lead.id, { force: true });
    }
  }

  function researchLead(lead) {
    return runBuyerWork(
      lead,
      'research',
      `Learning about ${lead.company_name}.`,
      () => call('leads.research', { params: { leadId: lead.id } }),
      `Research is ready for ${lead.company_name}.`,
    );
  }

  function discoverContacts(lead) {
    return runBuyerWork(
      lead,
      'contacts',
      `Looking for the right person at ${lead.company_name}.`,
      () => call('leads.findContacts', { params: { leadId: lead.id } }),
      `The contact search is complete for ${lead.company_name}.`,
    );
  }

  async function writeEmail(lead) {
    const before = new Set(messagesFor(lead.id).map(message => message.id));
    const result = await runBuyerWork(
      lead,
      'write',
      `Writing an email for ${lead.company_name}.`,
      () => call('leads.generateOutreach', { params: { leadId: lead.id }, body: {} }),
      `An email for ${lead.company_name} is ready for review.`,
    );
    if (!result) return;
    const created = newest(messagesFor(lead.id).filter(message => !before.has(message.id)));
    if (created) {
      toast('The email is in Approvals. Nothing was sent.', 'success', {
        actionLabel: 'Review',
        onAction: () => ctx.navigate(`/app/approvals?message=${encodeURIComponent(created.id)}`),
      });
    }
  }

  async function researchBatch() {
    if (isBusy('bulk-research')) return;
    const targets = activeModels()
      .filter(model => !model.hasResearch && !model.blocked)
      .slice(0, 12);
    if (!targets.length) return;
    setBusyState('bulk-research', true);
    targets.forEach(model =>
      setFeedback(model.lead.id, `Getting research ready for ${model.lead.company_name}.`, 'info'));
    render();
    try {
      const bulk = await call('research.bulk', {
        body: { lead_ids: targets.map(model => model.lead.id) },
      });
      let runs = itemsOf(bulk);
      if (!runs.length) {
        runs = await Promise.all(targets.map(model =>
          call('leads.research', { params: { leadId: model.lead.id } })));
      }
      let completedCount = 0;
      const results = await Promise.allSettled(runs.map((run, index) =>
        waitForRun(run, {
          timeoutMs: 120000,
          intervalMs: 300,
          onUpdate: current => {
            const model = targets[index];
            if (model) setFeedback(model.lead.id, runSentence(current), 'info');
            render();
          },
        }).then(completed => {
          if (['failed', 'error', 'cancelled'].includes(completed?.status)) throw new Error('failed');
          completedCount += 1;
          return completed;
        })));
      await refreshCollections();
      state.details.clear();
      const failed = results.length - completedCount;
      targets.forEach(model => setFeedback(
        model.lead.id,
        failed
          ? 'Research finished for some buyers. The others can be tried again.'
          : 'Research is ready.',
        failed ? 'warning' : 'success',
        { clearAfter: 5500 },
      ));
      toast(
        failed
          ? `Research finished for ${completedCount} of ${targets.length} buyers.`
          : `Research is ready for ${completedCount} buyers.`,
        failed ? 'warning' : 'success',
      );
    } catch {
      targets.forEach(model =>
        setFeedback(model.lead.id, "I couldn't finish the research. Try again.", 'error'));
      toast("I couldn't finish the buyer research. Try again.", 'error');
    } finally {
      setBusyState('bulk-research', false);
      render();
    }
  }

  async function recalculateFit(lead) {
    const key = `score:${lead.id}`;
    if (isBusy(key)) return;
    setBusyState(key, true);
    setFeedback(lead.id, `Rechecking the fit for ${lead.company_name}.`, 'info');
    render();
    try {
      const score = await call('leads.scoreRecalculate', { params: { leadId: lead.id } });
      const detail = state.details.get(lead.id) || {};
      state.details.set(lead.id, { ...detail, score, loading: false });
      setFeedback(lead.id, 'The fit assessment is up to date.', 'success', { clearAfter: 4500 });
      toast('Fit assessment updated.', 'success');
    } catch {
      setFeedback(lead.id, "I couldn't recalculate the fit. Try again.", 'error');
    } finally {
      setBusyState(key, false);
      render();
    }
  }

  function blockCompany(lead) {
    confirmDialog({
      title: `Never contact ${lead.company_name}?`,
      copy: 'Email writing will stop for this company. Its research and history will stay visible.',
      confirmLabel: 'Never contact this company',
      onConfirm: async () => {
        await call('leads.markDoNotContact', { params: { leadId: lead.id } });
        await refreshCollections();
        toast(`${lead.company_name} will not be contacted.`, 'warning');
        render();
      },
    });
  }

  function archiveCompany(lead) {
    confirmDialog({
      title: `Set ${lead.company_name} aside?`,
      copy: 'The company will leave the main Buyers list and remain available under Set aside.',
      confirmLabel: 'Set aside',
      kind: '',
      onConfirm: async () => {
        await call('leads.archive', { params: { leadId: lead.id } });
        await refreshCollections();
        state.expandedLeadId = '';
        syncUrl();
        toast(`${lead.company_name} was set aside.`, 'success');
        render();
      },
    });
  }

  async function verifyContact(lead, contact) {
    const key = `verify-contact:${contact.id}`;
    if (isBusy(key)) return;
    setBusyState(key, true);
    render();
    try {
      await call('contacts.verify', { params: { contactId: contact.id } });
      await refreshCollections();
      toast(`Email check refreshed for ${contact.name}.`, 'success');
    } catch {
      toast("We couldn't check that email. Try again.", 'error');
    } finally {
      setBusyState(key, false);
      render();
    }
  }

  function blockContact(lead, contact) {
    confirmDialog({
      title: `Never contact ${contact.name || contact.email}?`,
      copy: `This person will be excluded from future outreach${lead ? ` for ${lead.company_name}` : ''}.`,
      confirmLabel: 'Never contact this person',
      onConfirm: async () => {
        await call('contacts.markDoNotContact', { params: { contactId: contact.id } });
        await refreshCollections();
        toast(`${contact.name || 'This person'} will not be contacted.`, 'warning');
        render();
      },
    });
  }

  async function findLinkedIn(lead, contact) {
    const key = `find-linkedin:${contact.id}`;
    if (isBusy(key)) return;
    setBusyState(key, true);
    render();
    try {
      const result = await call('linkedin.findProfile', {
        body: { lead_id: lead.id, contact_id: contact.id },
      });
      if (runLike(result)) await waitForCustomerRun(result, lead.id);
      await refreshCollections();
      toast(`LinkedIn search finished for ${contact.name}.`, 'success');
    } catch {
      toast("I couldn't finish the LinkedIn search. Try again.", 'error');
    } finally {
      setBusyState(key, false);
      render();
    }
  }

  async function generateLinkedInNote(lead, contact) {
    const key = `linkedin-note:${contact.id}`;
    if (isBusy(key)) return;
    setBusyState(key, true);
    render();
    try {
      const result = await call('linkedin.generateNote', {
        body: { lead_id: lead.id, contact_id: contact.id, language: 'en' },
      });
      if (runLike(result)) await waitForCustomerRun(result, lead.id);
      await refreshCollections();
      toast(`A LinkedIn note is ready for ${contact.name}.`, 'success');
    } catch {
      toast("I couldn't finish the LinkedIn note. Try again.", 'error');
    } finally {
      setBusyState(key, false);
      render();
    }
  }

  async function updateLinkedInAction(action, route, successCopy) {
    const suffix = route === 'linkedin.markOpened' ? 'linkedin-opened' : 'linkedin-sent';
    const key = `${suffix}:${action.contact_id}`;
    if (isBusy(key)) return;
    setBusyState(key, true);
    render();
    try {
      await call(route, { params: { actionId: action.id } });
      const response = await call('linkedin.actions');
      state.linkedinActions = itemsOf(response);
      toast(successCopy, 'success');
    } catch {
      toast("We couldn't update that LinkedIn step. Try again.", 'error');
    } finally {
      setBusyState(key, false);
      render();
    }
  }

  async function copyLinkedInNote(note) {
    try {
      await navigator.clipboard.writeText(note);
      toast('LinkedIn note copied.', 'success');
    } catch {
      toast('Select and copy the note manually.', 'warning');
    }
  }

  try {
    await refreshCollections();
    if (disposed) return () => {};
    state.loaded = true;
    render();
    if (state.expandedLeadId) loadBuyerDetails(state.expandedLeadId);
    if (ctx.query.add === '1') {
      state.expandedLeadId = '';
      syncUrl();
      setTimeout(() => { if (!disposed) openAddBuyer(); }, 0);
    }
  } catch {
    state.loaded = false;
    page.replaceChildren(emptyState({
      icon: 'warning',
      title: 'Buyers could not be loaded',
      hint: 'Your data is safe. Try loading this page again.',
      action: button('Try again', {
        kind: 'primary',
        onClick: () => {
          root.replaceChildren();
          mount(root, ctx);
        },
      }),
    }));
  }

  return () => {
    disposed = true;
    pageEl.classList.remove('ifz-page--buyers');
    if (renderTimer) clearTimeout(renderTimer);
    feedbackTimers.forEach(timer => clearTimeout(timer));
  };
}
