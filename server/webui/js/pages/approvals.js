/* Approvals — one email at a time, with every outcome stated before action. */

import {
  el, pageHead, emptyState, button, toast, isApprovalActionable, QA_LABELS,
  markMessageSuperseded,
} from '../ui.js';
import { call } from '../api.js';
import { db } from '../mocks/db.js';
import {
  campaignFor, contactFor, leadFor, waitForRun,
} from './_page-utils.js';
import { reviewCard } from './_components.js';
import { openLeadEvidence } from './research-evidence.js';

function researchFor(leadId) {
  return db.research
    .filter(item => item.lead_id === leadId)
    .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))[0] || null;
}

function qaSentence(message) {
  const verdict = message?.qa_verdict || message?.data?.qa_verdict
    || message?.content?.qa_verdict || {};
  const failures = Array.isArray(verdict.failures) ? verdict.failures : [];
  return failures.length
    ? failures.map(code => QA_LABELS[code] || 'A quality check still needs attention.').join(' ')
    : 'A quality check still needs attention.';
}

function refusalSentence(error, { approved, mode }) {
  const prefix = approved ? 'Approved. ' : '';
  const detail = String(error?.message || '').toLowerCase();

  if (error?.status === 429 || detail.includes('daily outreach limit')) {
    return `${prefix}Today's email limit has been reached, so nothing was sent. Try again tomorrow.`;
  }
  if (detail.includes('outside recipient-local send window')) {
    return `${prefix}It wasn't sent because it is outside the recipient's working hours. Return during their sending window to try again.`;
  }
  if (detail.includes('timezone')) {
    return `${prefix}It wasn't sent because the buyer's timezone is missing. Add their location before trying again.`;
  }
  if (detail.includes('unsubscribed') || detail.includes('do-not-contact')
      || detail.includes('do not contact') || detail.includes('outreach is disabled')) {
    return 'This person cannot be contacted under your outreach rules. Nothing was sent.';
  }
  if (detail.includes('one-channel-per-customer')) {
    return `${prefix}This buyer was already contacted today. Nothing else was sent.`;
  }
  if (detail.includes('connected') && detail.includes('integration')) {
    return `${prefix}Your mailbox is not connected, so ${mode === 'draft' ? 'no draft was saved' : 'nothing was sent'}.`;
  }
  if (detail.includes('exact current message revision')) {
    return 'This email changed after it was approved. Review the current version before delivery.';
  }
  if (error?.status === 422) {
    return `${prefix}This email still fails a safety check. Review the highlighted issue before continuing.`;
  }
  return `${prefix}${mode === 'draft' ? 'The draft could not be saved' : 'The email was not sent'}. Try again in a moment.`;
}

export async function mount(root, ctx) {
  let disposed = false;
  let queue = [];
  let currentId = ctx.query.message || null;
  let currentIndex = 0;
  let editing = false;
  let busy = false;
  let feedback = null;
  let validationField = null;
  let announcement = '';
  let mailboxConnected = false;
  const skippedIds = new Set();

  root.classList.add('ifz-page--approvals');
  const host = el('div', { class: 'ifz-review-page', 'aria-busy': 'true' },
    pageHead({
      title: 'Emails waiting for you',
      sub: 'Loading the emails that need your review…',
    }),
    el('p', { class: 'ifz-review-loading', role: 'status' }, 'Loading review queue…'));
  root.append(host);

  const [
    , , , , , , emailIntegrations,
  ] = await Promise.all([
    call('messages.list'),
    call('leads.list'),
    call('contacts.list'),
    call('campaigns.list'),
    call('company.getSalesPreferences'),
    call('research.list'),
    call('emailIntegrations.list'),
  ]);
  mailboxConnected = (emailIntegrations?.items || [])
    .some(item => item.status === 'connected');
  if (disposed) return () => {};
  host.removeAttribute('aria-busy');

  function baseQueue() {
    return db.messages
      .filter(message => isApprovalActionable(message, {
        lead: leadFor(message.lead_id),
        contact: contactFor(message.contact_id),
      }))
      .sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
  }

  function publishCount() {
    document.dispatchEvent(new CustomEvent('ifz:approval-count', {
      detail: { count: baseQueue().length },
    }));
  }

  function syncLocation(messageId) {
    const hash = messageId
      ? `#/app/approvals?message=${encodeURIComponent(messageId)}`
      : '#/app/approvals';
    if (window.location.hash !== hash) {
      window.history.replaceState(window.history.state, '', hash);
    }
  }

  function rebuildQueue(preferredId = currentId) {
    const previousIndex = currentIndex;
    queue = baseQueue().filter(message => !skippedIds.has(message.id));
    const preferredIndex = queue.findIndex(message => message.id === preferredId);
    currentIndex = preferredIndex >= 0
      ? preferredIndex
      : Math.max(0, Math.min(previousIndex, queue.length - 1));
    currentId = queue[currentIndex]?.id || null;
    publishCount();
    syncLocation(currentId);
  }

  function focusAfterRender(target) {
    requestAnimationFrame(() => {
      if (disposed) return;
      if (target === 'editor') {
        host.querySelector('.ifz-review-subject-input')?.focus();
      } else if (target === 'body') {
        host.querySelector('.ifz-review-body-input')?.focus();
      } else if (target === 'edit') {
        host.querySelector('.ifz-review-secondary-actions .ifz-btn')?.focus();
      } else if (target === 'card') {
        host.querySelector('.ifz-review-card')?.focus({ preventScroll: true });
      } else if (target === 'empty') {
        host.querySelector('.ifz-review-empty')?.focus({ preventScroll: true });
      }
    });
  }

  function render({ focus = null } = {}) {
    if (disposed) return;
    rebuildQueue();
    const message = queue[currentIndex] || null;
    const waiting = baseQueue();

    const header = pageHead({
      title: 'Emails waiting for you',
      sub: message
        ? 'Review one at a time. The main button always says whether it will save a draft or send.'
        : 'Your review queue, without campaign setup or delivery logs.',
    });
    const help = el('p', {
      class: 'ifz-review-help',
      id: 'ifz-review-help',
    }, 'Shortcuts work while the review card is focused: A uses the main action, E edits, S skips for now, and the arrow keys move.');
    const live = el('p', {
      class: 'ifz-review-live',
      role: 'status',
      'aria-live': 'polite',
      'aria-atomic': 'true',
    }, announcement);

    const pausedCampaigns = db.campaigns.filter(campaign => campaign.status === 'paused_bounce_rate');
    const pauseBanner = pausedCampaigns.length
      ? el('div', { class: 'ifz-review-pause-banner' },
          el('strong', {}, `Sending is paused for ${pausedCampaigns.length === 1 ? 'one campaign' : `${pausedCampaigns.length} campaigns`}.`),
          ' Only emails in those campaigns are blocked while you check the addresses.')
      : null;

    if (!message) {
      const skipped = waiting.filter(item => skippedIds.has(item.id)).length;
      const empty = skipped
        ? emptyState({
            icon: 'clock',
            title: `You set aside ${skipped} email${skipped === 1 ? '' : 's'} for later`,
            hint: 'They are still waiting. Review them now or come back later.',
            action: button('Review skipped emails', {
              kind: 'primary',
              onClick: () => {
                skippedIds.clear();
                announcement = 'Skipped emails are back in the queue.';
                render({ focus: 'card' });
              },
            }),
          })
        : emptyState({
            icon: 'check',
            title: 'Nothing waiting',
            hint: 'There are no emails waiting for review.',
            action: button('Open buyers', {
              onClick: () => ctx.navigate('/app/buyers'),
            }),
          });
      const emptyWrap = el('div', {
        class: 'ifz-review-empty',
        tabindex: '-1',
        'aria-label': skipped ? 'Skipped emails are waiting' : 'Nothing waiting for review',
      }, empty);
      host.replaceChildren(...[
        header,
        help,
        live,
        pauseBanner,
        emptyWrap,
      ].filter(Boolean));
      if (focus) focusAfterRender(focus);
      return;
    }

    const lead = leadFor(message.lead_id);
    const contact = contactFor(message.contact_id);
    const campaign = campaignFor(message.campaign_id);
    const research = researchFor(message.lead_id);
    const sendMode = campaign?.send_mode
      || db.company.sales_preferences?.default_send_mode
      || 'create_draft';
    const campaignPaused = campaign?.status === 'paused_bounce_rate';
    const isApproved = message.status === 'approved';
    let primaryLabel;
    let primaryIcon;
    if (!mailboxConnected && isApproved) {
      primaryLabel = 'Connect mailbox to continue';
      primaryIcon = 'plug';
    } else if (!mailboxConnected) {
      primaryLabel = 'Approve for later — mailbox offline';
      primaryIcon = 'check';
    } else if (sendMode === 'approved_send') {
      primaryLabel = isApproved ? 'Send approved email' : 'Approve and send';
      primaryIcon = 'send';
    } else {
      primaryLabel = isApproved ? 'Save approved draft' : 'Approve — saves a draft in your mailbox';
      primaryIcon = 'mail';
    }

    const card = reviewCard(message, {
      position: currentIndex + 1,
      total: queue.length,
      lead,
      contact,
      campaign,
      research,
      editing,
      busy,
      feedback,
      validationField,
      blocked: campaignPaused,
      blockedReason: campaignPaused
        ? 'Delivery is paused for this campaign because too many recent addresses bounced. You can still rewrite, edit, approve, or skip this email.'
        : null,
      primaryLabel,
      primaryIcon,
      onPrimary: () => {
        if (!mailboxConnected && isApproved) {
          ctx.navigate('/app/setup?section=sending');
          return;
        }
        approveAndDeliver(message);
      },
      onEdit: () => {
        editing = true;
        feedback = null;
        validationField = null;
        render({ focus: 'editor' });
      },
      onCancelEdit: () => {
        editing = false;
        feedback = null;
        validationField = null;
        announcement = 'Editing cancelled.';
        render({ focus: 'edit' });
      },
      onSaveEdit: patch => saveAndApprove(message, patch),
      onRegenerate: () => regenerate(message),
      onSkip: () => skipCurrent(),
      onNeverContact: () => neverContact(message),
      onPrevious: () => move(-1),
      onNext: () => move(1),
      onEvidence: lead ? () => openLeadEvidence(lead) : null,
    });
    card.setAttribute('aria-describedby', 'ifz-review-help');
    host.replaceChildren(...[header, help, live, pauseBanner, card].filter(Boolean));
    if (focus) focusAfterRender(focus);
  }

  function move(delta) {
    if (busy || !queue.length) return;
    currentIndex = Math.max(0, Math.min(currentIndex + delta, queue.length - 1));
    currentId = queue[currentIndex]?.id || null;
    editing = false;
    feedback = null;
    validationField = null;
    announcement = currentId ? `Email ${currentIndex + 1} of ${queue.length}.` : '';
    render({ focus: 'card' });
  }

  function skipCurrent() {
    if (busy || !currentId) return;
    const skippedId = currentId;
    skippedIds.add(skippedId);
    editing = false;
    feedback = null;
    validationField = null;
    announcement = 'Set aside for later. Nothing was changed or sent.';
    rebuildQueue(null);
    render({ focus: queue.length ? 'card' : 'empty' });
  }

  async function refreshMessage(messageId) {
    try {
      await call('messages.get', { params: { messageId } });
    } catch { /* the unfiltered list below remains the source of truth */ }
    await call('messages.list');
  }

  async function approveAndDeliver(message) {
    if (busy || message == null) return;
    const campaign = campaignFor(message.campaign_id);
    if (campaign?.status === 'paused_bounce_rate') {
      feedback = {
        kind: 'warning',
        text: 'Delivery is still paused for this campaign. You can edit, rewrite, approve, or skip this email.',
      };
      announcement = feedback.text;
      render({ focus: 'card' });
      return;
    }

    busy = true;
    feedback = null;
    validationField = null;
    render({ focus: 'card' });
    let approved = message.status === 'approved';
    const sendMode = campaign?.send_mode
      || db.company.sales_preferences?.default_send_mode
      || 'create_draft';
    const deliveryMode = sendMode === 'approved_send' ? 'send' : 'draft';

    try {
      if (!approved) {
        await call('messages.approve', { params: { messageId: message.id } });
        approved = true;
      }

      if (!mailboxConnected) {
        await refreshMessage(message.id);
        busy = false;
        editing = false;
        feedback = {
          kind: 'warning',
          text: 'Approved. Your mailbox is offline, so nothing was delivered. Connect it when you are ready.',
        };
        announcement = feedback.text;
        rebuildQueue(message.id);
        render({ focus: 'card' });
        return;
      }

      const receipt = await call(
        deliveryMode === 'send' ? 'messages.send' : 'messages.createDraft',
        { params: { messageId: message.id } },
      );
      if (receipt?.status === 'failed') {
        const failed = new Error('The mailbox provider could not complete delivery.');
        failed.status = 409;
        throw failed;
      }
      await refreshMessage(message.id);
      busy = false;
      editing = false;
      validationField = null;
      feedback = null;
      const outcome = deliveryMode === 'send'
        ? 'Email sent.'
        : 'Draft saved in your mailbox.';
      toast(outcome, 'success');
      rebuildQueue();
      announcement = `${outcome} ${baseQueue().length} email${baseQueue().length === 1 ? '' : 's'} remain.`;
      render({ focus: queue.length ? 'card' : 'empty' });
    } catch (error) {
      await refreshMessage(message.id).catch(() => {});
      busy = false;
      editing = false;
      validationField = null;
      feedback = {
        kind: error?.status === 422 ? 'error' : 'warning',
        text: refusalSentence(error, { approved, mode: deliveryMode }),
      };
      announcement = feedback.text;
      rebuildQueue(message.id);
      render({ focus: 'card' });
    }
  }

  async function saveAndApprove(message, patch) {
    if (busy) return;
    if (!patch.subject || !patch.body.trim()) {
      validationField = !patch.subject ? 'subject' : 'body';
      feedback = {
        kind: 'error',
        text: validationField === 'subject'
          ? 'Add a subject before saving.'
          : 'Add the email text before saving.',
      };
      announcement = feedback.text;
      render({ focus: validationField === 'body' ? 'body' : 'editor' });
      return;
    }

    busy = true;
    feedback = null;
    validationField = null;
    render({ focus: 'editor' });
    try {
      const updated = await call('messages.update', {
        params: { messageId: message.id },
        body: patch,
      });
      if (updated.status === 'qa_failed') {
        busy = false;
        editing = true;
        validationField = null;
        feedback = { kind: 'error', text: qaSentence(updated) };
        announcement = `Not approved. ${feedback.text}`;
        await call('messages.list');
        rebuildQueue(updated.id);
        render({ focus: 'editor' });
        return;
      }

      const campaign = campaignFor(updated.campaign_id);
      if (campaign?.status === 'paused_bounce_rate') {
        const approved = updated.status === 'approved'
          ? updated
          : await call('messages.approve', { params: { messageId: updated.id } });
        await refreshMessage(approved.id);
        busy = false;
        editing = false;
        validationField = null;
        feedback = {
          kind: 'warning',
          text: 'Changes saved and approved. Delivery stays paused until the campaign’s address issue is resolved.',
        };
        announcement = feedback.text;
        rebuildQueue(approved.id);
        render({ focus: 'card' });
        return;
      }

      busy = false;
      editing = false;
      validationField = null;
      await approveAndDeliver(updated);
    } catch (error) {
      busy = false;
      editing = true;
      validationField = null;
      feedback = { kind: 'error', text: 'The edit could not be saved. Try again.' };
      announcement = feedback.text;
      render({ focus: 'editor' });
    }
  }

  async function regenerate(message) {
    if (busy) return;
    busy = true;
    feedback = null;
    validationField = null;
    render({ focus: 'card' });
    try {
      const result = await call('messages.regenerate', { params: { messageId: message.id } });
      let replacementId = message.id;
      if (result?.type) {
        const completed = await waitForRun(result);
        if (completed.status !== 'completed') {
          throw new Error('The rewrite did not finish.');
        }
        replacementId = completed.output_ref || message.id;
      }
      if (replacementId !== message.id) {
        markMessageSuperseded(message.id);
      }
      await call('messages.list');
      busy = false;
      editing = false;
      validationField = null;
      feedback = {
        kind: 'success',
        text: 'Rewritten. Review the new version before approving it.',
      };
      announcement = feedback.text;
      currentId = replacementId;
      rebuildQueue(replacementId);
      render({ focus: 'card' });
    } catch (error) {
      busy = false;
      feedback = {
        kind: 'error',
        text: error?.status === 422
          ? 'I need a named contact with an email address before I can rewrite this.'
          : 'The rewrite could not be completed. Try again.',
      };
      announcement = feedback.text;
      render({ focus: 'card' });
    }
  }

  async function neverContact(message) {
    if (busy) return;
    const contact = contactFor(message.contact_id);
    const lead = leadFor(message.lead_id);
    const target = contact
      ? `${contact.name || contact.email || 'this person'}`
      : `${lead?.company_name || 'this company'}`;
    const scope = contact ? 'person' : 'company';
    if (!window.confirm(`Never contact ${target} again? This blocks outreach to this ${scope}.`)) return;

    busy = true;
    feedback = null;
    validationField = null;
    render({ focus: 'card' });
    try {
      if (contact) {
        await call('contacts.markDoNotContact', { params: { contactId: contact.id } });
      } else if (lead) {
        await call('leads.markDoNotContact', { params: { leadId: lead.id } });
      } else {
        throw new Error('No buyer or contact is attached to this email.');
      }
      await Promise.all([call('leads.list'), call('contacts.list'), call('messages.list')]);
      busy = false;
      editing = false;
      validationField = null;
      announcement = `${target} will not be contacted.`;
      toast(announcement, 'warning');
      rebuildQueue();
      render({ focus: queue.length ? 'card' : 'empty' });
    } catch (error) {
      busy = false;
      feedback = { kind: 'error', text: 'The contact preference could not be saved. Try again.' };
      announcement = feedback.text;
      render({ focus: 'card' });
    }
  }

  function onKeydown(event) {
    if (event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (!event.target.closest?.('.ifz-review-card')) return;

    if (event.key === 'Escape' && editing) {
      event.preventDefault();
      editing = false;
      feedback = null;
      validationField = null;
      announcement = 'Editing cancelled.';
      render({ focus: 'edit' });
      return;
    }
    if (busy) return;

    const textEntry = event.target.closest?.('input, textarea, select, [contenteditable="true"]');
    if (textEntry) return;

    const key = event.key.toLowerCase();
    if (editing) {
      if (key === 'a') {
        event.preventDefault();
        host.querySelector('.ifz-review-edit-actions .ifz-btn.primary')?.click();
      }
      return;
    }

    if (key === 'a') {
      event.preventDefault();
      const message = queue[currentIndex];
      if (message?.status === 'qa_failed') regenerate(message);
      else if (!mailboxConnected && message?.status === 'approved') {
        ctx.navigate('/app/setup?section=sending');
      } else if (message) approveAndDeliver(message);
    } else if (key === 'e') {
      event.preventDefault();
      editing = true;
      feedback = null;
      validationField = null;
      render({ focus: 'editor' });
    } else if (key === 's') {
      event.preventDefault();
      skipCurrent();
    } else if (event.key === 'ArrowLeft') {
      if (event.target.closest?.('button, a')) return;
      event.preventDefault();
      move(-1);
    } else if (event.key === 'ArrowRight') {
      if (event.target.closest?.('button, a')) return;
      event.preventDefault();
      move(1);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      ctx.navigate('/app/today');
    }
  }

  host.addEventListener('keydown', onKeydown);
  rebuildQueue(ctx.query.message || null);
  announcement = queue.length
    ? `${queue.length} email${queue.length === 1 ? '' : 's'} waiting for review.`
    : 'Nothing waiting for review.';
  render({ focus: queue.length ? 'card' : 'empty' });

  return () => {
    disposed = true;
    host.removeEventListener('keydown', onKeydown);
    root.classList.remove('ifz-page--approvals');
  };
}
