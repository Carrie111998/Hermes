/* Shared, job-shaped page components for the collapsed customer experience. */

import {
  el, badge, button, input, textarea, QA_LABELS, labelFor, scoreBadge, fmt, icon,
  modal, field, select, toast, setBusy,
} from '../ui.js';
import { call } from '../api.js';
import { COUNTRY_NAMES, BUYER_INDUSTRIES } from '../catalog.js';
import { countryName } from './_page-utils.js';

const SOURCE_LABELS = {
  web: 'Public company sources',
  web_search: 'Public company sources',
  trade_data: 'Trade data',
  exhibitor_lists: 'Trade fair listings',
  company_registries: 'Company registry',
  linkedin_reference: 'Public professional profile',
  uploaded_internal_data: 'Your company records',
  manual: 'Added by your team',
};

function qaFailures(message) {
  const verdict = message?.qa_verdict
    || message?.data?.qa_verdict
    || message?.content?.qa_verdict
    || {};
  return Array.isArray(verdict.failures) ? verdict.failures : [];
}

function actionButton(label, {
  key: _key, kind = '', icon, onClick, disabled = false, title,
} = {}) {
  return button(label, { kind, icon, onClick, disabled, title });
}

function reviewProgress(position, total) {
  const visible = Math.min(total, 12);
  return el('div', {
    class: 'ifz-review-progress',
    'aria-hidden': 'true',
  }, Array.from({ length: visible }, (_, idx) =>
    el('span', {
      class: idx < position ? 'done' : '',
      'aria-hidden': 'true',
    })));
}

function recipientDetails(message, contact) {
  const to = message.to || message.content?.to || contact?.email || 'No email address on file';
  const cc = message.cc || message.content?.cc || [];
  return el('dl', { class: 'ifz-review-recipient' },
    el('dt', {}, 'To'),
    el('dd', {},
      el('strong', {}, contact?.name || to),
      contact?.title ? el('span', {}, contact.title) : null,
      contact?.name && to ? el('span', {}, to) : null),
    el('dt', {}, 'CC'),
    el('dd', {}, cc.length ? cc.join(', ') : 'No one'));
}

function emailEditor(message, handlers) {
  const fieldKey = String(message.id || 'message').replace(/[^a-zA-Z0-9_-]/g, '-');
  const subjectId = `ifz-review-subject-${fieldKey}`;
  const bodyId = `ifz-review-body-${fieldKey}`;
  const subject = input({
    id: subjectId,
    value: message.subject || '',
    placeholder: 'Email subject',
    autocomplete: 'off',
    'aria-invalid': handlers.validationField === 'subject' ? 'true' : null,
    'aria-describedby': handlers.validationField === 'subject' ? `${subjectId}-error` : null,
  });
  subject.classList.add('ifz-review-subject-input');
  const body = textarea({
    id: bodyId,
    value: message.body || '',
    rows: 18,
    'aria-invalid': handlers.validationField === 'body' ? 'true' : null,
    'aria-describedby': handlers.validationField === 'body' ? `${bodyId}-error` : null,
  });
  body.classList.add('ifz-review-body-input');
  return el('div', { class: 'ifz-review-editor' },
    el('label', { class: 'ifz-label', for: subjectId }, 'Subject'),
    subject,
    handlers.validationField === 'subject'
      ? el('p', { class: 'ifz-field-error', id: `${subjectId}-error` }, 'Add a subject.')
      : null,
    el('label', { class: 'ifz-label', for: bodyId }, 'Email'),
    body,
    handlers.validationField === 'body'
      ? el('p', { class: 'ifz-field-error', id: `${bodyId}-error` }, 'Add the email text.')
      : null,
    el('p', { class: 'ifz-hint' },
      handlers.blocked
        ? 'Saving approves your changes. Delivery stays paused until the address issue is resolved.'
        : 'Saving approves your changes and continues with the delivery choice shown on the main button.'),
    el('div', { class: 'ifz-review-edit-actions' },
      actionButton(handlers.busy ? 'Saving…' : 'Save and approve', {
        kind: 'primary',
        icon: 'check',
        key: 'A',
        disabled: handlers.busy,
        onClick: () => handlers.onSaveEdit?.({
          subject: subject.value.trim(),
          body: body.value,
        }),
      }),
      actionButton('Cancel edit', {
        onClick: handlers.onCancelEdit,
        disabled: handlers.busy,
      })));
}

function emailCopy(message) {
  return el('div', { class: 'ifz-review-copy' },
    el('h2', { class: 'ifz-review-subject' }, message.subject || 'No subject'),
    el('div', { class: 'ifz-review-email-body' }, message.body || 'No message body yet.'));
}

function qaNotice(message) {
  const failures = qaFailures(message);
  return el('section', { class: 'ifz-review-qa', 'aria-labelledby': `qa-${message.id}` },
    el('div', { class: 'ifz-review-qa-kicker' }, 'Needs a second look'),
    el('h2', { id: `qa-${message.id}` }, "I wrote this one, but I don't trust it yet."),
    failures.length
      ? el('ul', {}, failures.map(code =>
          el('li', {}, QA_LABELS[code] || 'A quality check needs attention.')))
      : el('p', {}, 'One of the quality checks did not pass. Rewrite it or edit it yourself before approval.'));
}

function contextPanel(message, handlers) {
  const { lead, contact, research } = handlers;
  const source = lead?.source ? (SOURCE_LABELS[lead.source] || 'A saved buyer source') : null;
  const location = [lead?.city, countryName(lead?.country)].filter(Boolean).join(', ');
  const reason = research?.summary
    || lead?.research_summary
    || (lead
      ? 'No research summary is saved for this buyer yet.'
      : 'No buyer research is attached to this email.');
  const canInspectEvidence = handlers.onEvidence
    && Number.isFinite(Number(lead?.fit_score))
    && Number.isFinite(Number(lead?.evidence_confidence));

  return el('aside', { class: 'ifz-review-context', 'aria-label': 'Why this email was written' },
    el('div', { class: 'ifz-review-company' },
      el('h2', {}, lead?.company_name || 'Buyer details unavailable'),
      location ? el('p', {}, location) : null,
      lead?.industry ? el('p', {}, lead.industry) : null),
    el('section', {},
      el('h3', {}, 'Why this company'),
      el('p', {}, reason)),
    el('section', {},
      el('h3', {}, 'Where this came from'),
      source || research?.insights?.[0]?.title
        ? el('ul', { class: 'ifz-review-source-list' },
            source ? el('li', {}, source) : null,
            research?.insights?.[0]?.title ? el('li', {}, research.insights[0].title) : null)
        : el('p', { class: 'ifz-muted' }, 'No source evidence is attached yet.'),
      canInspectEvidence
        ? actionButton('See source evidence', {
            kind: 'ghost', icon: 'search', onClick: handlers.onEvidence,
          })
        : null),
    el('section', {},
      el('h3', {}, 'Contact'),
      contact
        ? el('div', { class: 'ifz-review-contact' },
            badge(contact.email_status || 'unverified',
              contact.email_status === 'verified' ? 'Email verified' : labelFor(contact.email_status)),
            contact.phone
              ? el('span', {}, contact.phone)
              : el('span', { class: 'ifz-muted' }, 'No phone on file'))
        : el('p', { class: 'ifz-muted' },
            message.to || message.content?.to
              ? 'No named contact is on file for this address.'
              : 'No verified recipient is on file.')));
}

function reviewActions(message, handlers) {
  const qaFailed = message.status === 'qa_failed';

  if (handlers.editing) return null;

  const primary = qaFailed
    ? actionButton(handlers.busy ? 'Rewriting…' : 'Rewrite', {
        kind: 'primary', icon: 'refresh', key: 'A',
        onClick: handlers.onRegenerate, disabled: handlers.busy,
      })
    : actionButton(handlers.busy ? 'Working…' : (handlers.primaryLabel || 'Approve'), {
        kind: 'primary', icon: handlers.primaryIcon || 'check', key: 'A',
        onClick: handlers.onPrimary, disabled: handlers.busy || handlers.blocked,
      });

  return el('footer', { class: 'ifz-review-actions' },
    el('div', { class: 'ifz-review-primary-action' },
      primary,
      el('kbd', {}, 'A')),
    el('div', { class: 'ifz-review-secondary-actions' },
      el('div', {},
        actionButton(qaFailed ? 'Edit myself' : 'Edit', {
          icon: 'edit', key: 'E', onClick: handlers.onEdit, disabled: handlers.busy,
        }),
        el('kbd', {}, 'E')),
      el('div', {},
        actionButton('Skip for now', {
          key: 'S', onClick: handlers.onSkip, disabled: handlers.busy,
        }),
        el('kbd', {}, 'S')),
      actionButton(handlers.contact ? 'Never contact this person' : 'Never contact this company', {
        kind: 'danger', icon: 'ban', onClick: handlers.onNeverContact, disabled: handlers.busy,
      })));
}

/**
 * Full-width, one-message review surface.
 * `handlers` carries view context and actions so the component remains usable
 * with both the real API adapter and the deterministic mock store.
 */
export function reviewCard(message, handlers = {}) {
  const position = Math.max(1, Number(handlers.position) || 1);
  const total = Math.max(1, Number(handlers.total) || 1);
  const qaFailed = message.status === 'qa_failed';

  const article = el('article', {
    class: `ifz-review-card${qaFailed ? ' is-qa-failed' : ''}`,
    tabindex: '0',
    dataset: { messageId: message.id },
    'aria-label': `Review email ${position} of ${total}`,
    'aria-keyshortcuts': 'A E S ArrowLeft ArrowRight Escape',
  },
    el('header', { class: 'ifz-review-card-head' },
      el('div', {},
        el('span', { class: 'ifz-overline' }, qaFailed ? 'Quality check' : 'Email review'),
        el('strong', {}, `${position} of ${total}`)),
      el('div', { class: 'ifz-review-card-nav' },
        reviewProgress(position, total),
        actionButton('Previous', {
          kind: 'ghost', icon: 'arrowLeft', onClick: handlers.onPrevious,
          disabled: handlers.busy || position <= 1,
          title: 'Previous email (Left arrow)',
        }),
        actionButton('Next', {
          kind: 'ghost', icon: 'arrowRight', onClick: handlers.onNext,
          disabled: handlers.busy || position >= total,
          title: 'Next email (Right arrow)',
        }))),
    handlers.blockedReason
      ? el('div', { class: 'ifz-review-block' }, handlers.blockedReason)
      : null,
    qaFailed ? qaNotice(message) : null,
    handlers.feedback
      ? el('div', {
          class: `ifz-review-feedback ${handlers.feedback.kind || 'info'}`,
        }, handlers.feedback.text)
      : null,
    el('div', { class: 'ifz-review-layout' },
      el('section', {
        class: 'ifz-review-email',
        'aria-label': 'Email content before the required opt-out footer is added',
      },
        recipientDetails(message, handlers.contact),
        handlers.editing ? emailEditor(message, handlers) : emailCopy(message),
        el('p', { class: 'ifz-review-delivery-note' },
          'Your required opt-out link is added automatically when this is saved or sent.')),
      contextPanel(message, handlers)),
    reviewActions(message, handlers));

  return article;
}

function buyerAction(key, label, action = {}, defaults = {}) {
  if (!action || action.hidden) return null;
  const node = button(action.busy ? (action.busyLabel || defaults.busyLabel || 'Working…') : label, {
    kind: action.kind ?? defaults.kind ?? '',
    size: action.size ?? defaults.size ?? 'sm',
    icon: action.icon ?? defaults.icon,
    onClick: action.onClick,
    disabled: action.disabled || action.busy,
    title: action.title,
  });
  node.dataset.action = key;
  return node;
}

function fitSummary(lead, currentScore, hasResearch) {
  const hasValue = value => value !== null && value !== undefined && value !== ''
    && Number.isFinite(Number(value));
  const evidenceFit = Number(lead?.fit_score);
  const evidenceConfidence = Number(lead?.evidence_confidence);
  const hasEvidenceFit = hasValue(lead?.fit_score) && hasValue(lead?.evidence_confidence);
  const score = hasEvidenceFit ? { value: evidenceFit } : (currentScore || lead?.score);
  const rawScore = score && typeof score === 'object' ? score.value : score;
  const hasFitAssessment = hasValue(rawScore) && (hasEvidenceFit ? Number(rawScore) >= 0 : Number(rawScore) > 0);
  let basis = hasResearch
    ? (hasFitAssessment ? 'based on current research' : 'research available')
    : 'research needed';

  if (hasEvidenceFit && Number.isFinite(evidenceConfidence)) {
    const confidence = evidenceConfidence <= 1 ? evidenceConfidence * 100 : evidenceConfidence;
    const estimateShare = Number(lead?.confidence_factors?.estimate_share);
    const hasSources = (Array.isArray(lead?.source_ids) && lead.source_ids.length > 0)
      || Number(lead?.evidence_count) > 0;
    basis = Number.isFinite(estimateShare) && estimateShare > 0
      ? 'partly estimated'
      : hasSources
        ? 'based on verified sources'
        : confidence >= 75
          ? 'supported by strong sources'
          : 'evidence is still limited';
  }

  return el('div', { class: 'ifz-company-fit' },
    scoreBadge(score, {
      words: true,
      allowZero: hasEvidenceFit,
      emptyLabel: hasResearch ? 'Fit not assessed yet' : 'Not researched yet',
      title: hasResearch && !hasFitAssessment
        ? 'Research is ready, but the fit assessment has not been calculated yet'
        : undefined,
    }),
    el('span', {}, basis));
}

function contactRow(contact, actions = {}) {
  const unavailable = contact.do_not_contact;
  const linkedIn = actions.linkedIn || {};
  const profileUrl = contact.linkedin_url || linkedIn.profile_url;
  const verify = buyerAction(`verify-contact:${contact.id}`, 'Verify email', actions.verify, {
    icon: 'check',
    busyLabel: 'Checking…',
  });
  const findLinkedIn = buyerAction(`find-linkedin:${contact.id}`, 'Find LinkedIn', actions.findLinkedIn, {
    icon: 'linkedin',
    busyLabel: 'Looking…',
  });
  const neverContact = buyerAction(`never-contact:${contact.id}`, 'Never contact', actions.neverContact, {
    icon: 'ban',
  });
  const generateNote = buyerAction(`linkedin-note:${contact.id}`, 'Prepare LinkedIn note', actions.generateNote, {
    icon: 'linkedin',
    busyLabel: 'Writing…',
  });
  const copyNote = buyerAction(`copy-linkedin-note:${contact.id}`, 'Copy note', actions.copyNote, {
  });
  const markOpened = buyerAction(`linkedin-opened:${contact.id}`, 'Profile opened', actions.markOpened);
  const markSent = buyerAction(`linkedin-sent:${contact.id}`, 'Connection sent', actions.markSent);

  return el('li', {
    class: `ifz-company-contact${unavailable ? ' is-blocked' : ''}`,
    dataset: { contactId: contact.id },
  },
  el('div', { class: 'ifz-company-contact-person' },
    el('strong', {}, contact.name || contact.email || 'Unnamed person'),
    el('span', {}, contact.title || 'Role not known')),
  el('div', { class: 'ifz-company-contact-address' },
    contact.email
      ? el('span', { class: 'ifz-mono' }, contact.email)
      : el('span', { class: 'ifz-muted' }, 'Email not known'),
    badge(unavailable ? 'do_not_contact' : (contact.email_status || 'unverified'))),
  el('div', { class: 'ifz-company-contact-links' },
    profileUrl
      ? el('a', {
          href: profileUrl,
          target: '_blank',
          rel: 'noopener',
          class: 'ifz-company-link',
        }, icon('linkedin', 14), 'Open LinkedIn')
      : findLinkedIn,
    profileUrl && !linkedIn.note ? generateNote : null,
    !unavailable ? verify : null,
    !unavailable ? neverContact : null),
  linkedIn.note
    ? el('div', { class: 'ifz-company-contact-note' },
        el('p', {}, linkedIn.note),
        el('div', {},
          copyNote,
          linkedIn.status !== 'opened' ? markOpened : null,
          !['connection_sent', 'connected', 'replied'].includes(linkedIn.status) ? markSent : null))
    : null);
}

function researchPanel(lead, research, currentScore, actions = {}) {
  const hasResearch = research?.status === 'completed' || Boolean(research?.summary);
  const insights = Array.isArray(research?.insights) ? research.insights.slice(0, 3) : [];

  return el('section', { class: 'ifz-company-section ifz-company-research' },
    el('div', { class: 'ifz-company-section-head' },
      el('div', {},
        el('span', { class: 'ifz-company-section-kicker' }, 'Company fit'),
        el('h3', {}, hasResearch ? 'Why this buyer may fit' : 'Research still needed')),
      fitSummary(lead, currentScore, hasResearch)),
    hasResearch
      ? el('div', { class: 'ifz-company-research-copy' },
          el('p', {}, research.summary || 'Research is ready for this buyer.'),
          insights.length
            ? el('ul', {}, insights.map(item =>
                el('li', {},
                  el('strong', {}, item.title || 'Buyer signal'),
                  el('span', {}, item.body || 'No detail was saved.'))))
            : null)
      : el('p', { class: 'ifz-company-section-empty' },
          'Let the agent learn about this company before you decide whether to approach them.'),
    el('div', { class: 'ifz-company-section-actions' },
      buyerAction(`research:${lead.id}`, hasResearch ? 'Refresh research' : 'Research company', actions.research, {
        icon: 'search',
        busyLabel: 'Researching…',
      }),
      buyerAction(`evidence:${lead.id}`, 'See source evidence', actions.evidence, {
        icon: 'search',
      }),
      buyerAction(`score:${lead.id}`, 'Recalculate fit', actions.recalculate, {
        icon: 'refresh',
        busyLabel: 'Recalculating…',
      })));
}

function peoplePanel(lead, contacts, actions = {}) {
  return el('section', { class: 'ifz-company-section ifz-company-people' },
    el('div', { class: 'ifz-company-section-head' },
      el('div', {},
        el('span', { class: 'ifz-company-section-kicker' }, 'People'),
        el('h3', {}, contacts.length
          ? `${contacts.length} ${contacts.length === 1 ? 'person' : 'people'} at this company`
          : 'No contact yet')),
      el('div', { class: 'ifz-company-section-head-actions' },
        buyerAction(`discover:${lead.id}`, contacts.length ? 'Find another person' : 'Find the right person', actions.discover, {
          icon: 'contact',
          busyLabel: 'Looking…',
        }),
        buyerAction(`add-contact:${lead.id}`, 'Add person', actions.addContact, {
          icon: 'plus',
        }))),
    contacts.length
      ? el('ul', { class: 'ifz-company-contacts' },
          contacts.map(contact => contactRow(contact, actions.contacts?.[contact.id] || {})))
      : el('p', { class: 'ifz-company-section-empty' },
          'Find a purchasing or import contact, or add someone you already know.'));
}

function outreachPanel(lead, contacts, messages, actions = {}) {
  const visible = (messages || []).slice().sort((a, b) =>
    new Date(b.created_at || b.sent_at || 0) - new Date(a.created_at || a.sent_at || 0));
  const latest = visible[0];
  const waiting = visible.find(message =>
    ['pending_approval', 'draft_generated', 'qa_failed', 'approved'].includes(message.status));

  return el('section', { class: 'ifz-company-section ifz-company-outreach' },
    el('div', { class: 'ifz-company-section-head' },
      el('div', {},
        el('span', { class: 'ifz-company-section-kicker' }, 'Outreach'),
        el('h3', {}, latest ? 'Latest email' : 'No email written yet')),
      waiting
        ? buyerAction(`review:${lead.id}`, 'Review email', actions.review, {
            kind: 'primary',
            icon: 'arrowRight',
          })
        : buyerAction(`write:${lead.id}`, latest ? 'Write another email' : 'Write email', actions.write, {
            kind: 'primary',
            icon: 'mail',
            busyLabel: 'Writing…',
          })),
    latest
      ? el('div', { class: 'ifz-company-message' },
          el('div', {},
            el('strong', {}, latest.subject || 'Email without a subject'),
            el('span', {}, latest.sent_at
              ? `Updated ${fmt.ago(latest.sent_at)}`
              : `Written ${fmt.ago(latest.created_at)}`)),
          badge(latest.status))
      : el('p', { class: 'ifz-company-section-empty' },
          contacts.length
            ? 'Write a personal email using the company research and the contact above.'
            : 'Find a contact before writing an email.'));
}

/**
 * One buyer with the people, research and outreach nested inside it.
 * The optional handlers object keeps API work in the page and this component
 * focused on rendering the job in customer language.
 */
export function companyRow(lead, contacts = [], handlers = {}) {
  const panelId = `ifz-company-panel-${String(lead.id).replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  const location = [lead.city, countryName(lead.country)].filter(Boolean).join(', ');
  const summary = el('button', {
    class: 'ifz-company-summary',
    type: 'button',
    'aria-expanded': handlers.expanded ? 'true' : 'false',
    'aria-controls': panelId,
    onclick: handlers.onToggle,
  },
  el('span', { class: 'ifz-company-chevron', 'aria-hidden': 'true' },
    icon('arrowRight', 16)),
  el('span', { class: 'ifz-company-identity' },
    el('strong', {}, lead.company_name || 'Unnamed buyer'),
    el('span', {}, lead.industry || 'Buyer type not known')),
  el('span', { class: 'ifz-company-market' }, location || 'Market not known'),
  el('span', { class: 'ifz-company-state' },
    el('strong', {}, handlers.stateLabel || labelFor(lead.status)),
    handlers.stateDetail ? el('span', {}, handlers.stateDetail) : null),
  fitSummary(lead, handlers.currentScore, Boolean(handlers.research)));

  const article = el('article', {
    class: `ifz-company-row${handlers.expanded ? ' is-open' : ''}${lead.do_not_contact || lead.status === 'do_not_contact' ? ' is-blocked' : ''}`,
    dataset: { leadId: lead.id },
  }, summary);

  if (!handlers.expanded && contacts.length) {
    article.append(el('ul', {
      class: 'ifz-company-contact-preview',
      'aria-label': `People at ${lead.company_name}`,
    }, contacts.slice(0, 2).map(contact =>
      el('li', {},
        el('span', { class: 'ifz-company-preview-mark', 'aria-hidden': 'true' }),
        el('strong', {}, contact.name || contact.email || 'Unnamed person'),
        el('span', {}, contact.title || 'Role not known'),
        badge(contact.do_not_contact ? 'do_not_contact' : (contact.email_status || 'unverified')))),
    contacts.length > 2
      ? el('li', { class: 'ifz-company-contact-preview-more' },
          `and ${contacts.length - 2} more`)
      : null));
  }

  if (handlers.expanded) {
    const blocked = lead.do_not_contact || lead.status === 'do_not_contact';
    const panel = el('div', {
      class: 'ifz-company-panel',
      id: panelId,
    },
    handlers.loading
      ? el('div', { class: 'ifz-company-panel-loading', role: 'status' },
          el('span', {}, 'Loading buyer details…'))
      : [
          handlers.progressText
            ? el('div', {
                class: `ifz-company-progress ${handlers.progressTone || ''}`,
                role: 'status',
                'aria-live': 'polite',
              }, handlers.progressText)
            : null,
          blocked
            ? el('div', { class: 'ifz-company-blocked' },
                'This company is on your never-contact list. Research stays visible, but no email can be written.')
            : null,
          researchPanel(lead, handlers.research, handlers.currentScore, handlers.actions),
          peoplePanel(lead, contacts, handlers.actions),
          outreachPanel(lead, contacts, handlers.messages || [], handlers.actions),
          el('footer', { class: 'ifz-company-footer' },
            lead.website
              ? el('a', {
                  href: lead.website,
                  target: '_blank',
                  rel: 'noopener',
                  class: 'ifz-company-link',
                }, 'Open company website')
              : el('span', { class: 'ifz-muted' }, 'No website on file'),
            el('div', {},
              !blocked
                ? buyerAction(`block:${lead.id}`, 'Never contact this company', handlers.actions?.block, {
                    icon: 'ban',
                  })
                : null,
              lead.status !== 'archived'
                ? buyerAction(`archive:${lead.id}`, 'Set aside', handlers.actions?.archive, {
                  })
                : null)),
        ]);
    article.append(panel);
  }

  return article;
}

/**
 * Add a company directly to the normal buyer pipeline.
 */
export function openBuyerCreateModal({ onCreated } = {}) {
  const nameIn = input({ placeholder: 'Example Appliances GmbH', required: true });
  const websiteIn = input({ placeholder: 'https://example.com', type: 'url' });
  const countrySel = select(
    Object.entries(COUNTRY_NAMES).map(([value, label]) => ({ value, label })),
    { value: 'DE' },
  );
  const cityIn = input({ placeholder: 'Berlin' });
  const industrySel = select(BUYER_INDUSTRIES, { value: BUYER_INDUSTRIES[0] });
  const createBtn = button('Add buyer', { kind: 'primary', icon: 'plus' });

  const dialog = modal({
    title: 'Add a buyer',
    body: el('div', {},
      el('p', { class: 'ifz-muted ifz-mb-4' },
        'Add a company you already know. It will join the same research, contact and email pipeline as discovered buyers.'),
      field('Company name', nameIn, { required: true }),
      el('div', { class: 'ifz-form-row' },
        field('Country', countrySel),
        field('City', cityIn)),
      field('Website', websiteIn),
      field('Buyer type', industrySel)),
    actions: [
      button('Cancel', { onClick: () => dialog.close() }),
      createBtn,
    ],
  });

  createBtn.addEventListener('click', async () => {
    const companyName = nameIn.value.trim();
    const website = websiteIn.value.trim();
    if (!companyName) {
      nameIn.setAttribute('aria-invalid', 'true');
      nameIn.focus();
      toast('Add the company name.', 'warning');
      return;
    }
    if (website) {
      try {
        const parsed = new URL(website);
        if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('unsupported');
      } catch {
        websiteIn.setAttribute('aria-invalid', 'true');
        websiteIn.focus();
        toast('Use a full website address, such as https://example.com.', 'warning');
        return;
      }
    }

    setBusy(createBtn, true, 'Adding…');
    try {
      const lead = await call('leads.create', { body: {
        company_name: companyName,
        website,
        country: countrySel.value,
        city: cityIn.value.trim(),
        industry: industrySel.value,
      } });
      dialog.close();
      toast(`${lead.company_name} was added to Buyers.`, 'success');
      onCreated?.(lead);
    } catch {
      setBusy(createBtn, false);
      toast("We couldn't add this buyer. Try again.", 'error');
    }
  });

  return dialog;
}
