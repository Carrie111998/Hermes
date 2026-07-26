/* Today — an honest briefing and one manual way to start the next piece of work. */

import {
  el, icon, button, fmt, isApprovalActionable, runSentence,
} from '../ui.js';
import { call, config } from '../api.js';
import { db } from '../mocks/db.js';
import { COUNTRY_NAMES } from '../catalog.js';
import { hermesApi } from '../hermes-client.js';
import { renderMiniMap } from './lead-map.js';
import { waitForRun } from './_page-utils.js';

const MAX_EMAILS_PER_SEARCH = 6;
const TERMINAL_OK = new Set(['completed', 'succeeded']);

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning.';
  if (hour < 18) return 'Good afternoon.';
  return 'Good evening.';
}

function plural(count, singular, pluralForm = `${singular}s`) {
  return `${fmt.num(count)} ${count === 1 ? singular : pluralForm}`;
}

function listPhrase(items) {
  if (!items.length) return '';
  try {
    return new Intl.ListFormat('en', { style: 'long', type: 'conjunction' }).format(items);
  } catch {
    return items.join(', ');
  }
}

function selectedCountryNames(summary) {
  return (summary?.selected_countries || [])
    .map(country => COUNTRY_NAMES[country] || country);
}

function reviewCount() {
  return (db.messages || []).filter(message => isApprovalActionable(message, {
    lead: (db.leads || []).find(lead => lead.id === message.lead_id),
    contact: (db.contacts || []).find(contact => contact.id === message.contact_id),
  })).length;
}

function scoreValue(lead) {
  const value = lead?.score && typeof lead.score === 'object' ? lead.score.value : lead?.score;
  return Number.isFinite(Number(value)) ? Number(value) : -1;
}

function activitySentence(activity) {
  const action = activity?.action || '';
  const known = {
    email_reply_detected: 'A buyer replied to one of your emails.',
    email_bounce_observed: 'An email could not be delivered.',
    bounce_circuit_tripped: 'Email sending paused after repeated delivery problems.',
    outreach_message_approved: 'An email was approved.',
    lead_created: 'A buyer was added to the workspace.',
    document_uploaded: 'A company document was added.',
    company_brain_approved: 'Your company information was approved.',
    email_integration_connected: 'Your mailbox was connected.',
    integration_connected: 'A mailbox was connected.',
  };
  if (known[action]) return known[action];

  const label = String(activity?.label || '').trim();
  if (label && !label.includes('_')) {
    return label
      .replace(/^Agent\s+/i, 'I ')
      .replace(/\s+/g, ' ');
  }
  if (activity?.kind === 'reply') return 'A buyer replied.';
  if (activity?.kind === 'document') return 'Company information was updated.';
  return 'The workspace was updated.';
}

/* The daily-rhythm briefing, used only when the scheduler actually assembled
   one. Without it the copy would claim work happened overnight that never did,
   so the retrospective picture below stays the honest default. */
function digestBriefing(digest, waiting) {
  const report = digest?.report;
  const plan = digest?.plan;
  if (!report && !plan) return '';

  if (report) {
    const found = Number(report.buyers_found) || 0;
    const written = Number(report.emails_written) || 0;
    const replies = Number(report.replies) || 0;
    const done = [];
    if (found) done.push(`found ${plural(found, 'company')} worth approaching`);
    if (written) done.push(`wrote ${plural(written, 'email')}`);
    if (replies) done.push(`recorded ${plural(replies, 'reply', 'replies')}`);
    const opening = done.length
      ? `Since yesterday I ${listPhrase(done)}.`
      : 'Since yesterday there was nothing new to report.';
    const unfinished = Number(report.unfinished) || 0;
    const tail = unfinished
      ? ` ${unfinished === 1 ? 'One piece of work' : `${fmt.num(unfinished)} pieces of work`} didn't finish.`
      : '';
    const needs = waiting
      ? ` ${plural(waiting, 'email')} ${waiting === 1 ? 'is' : 'are'} waiting for you.`
      : '';
    return `${opening}${tail}${needs}`;
  }

  const markets = (plan.markets || []).map(code => COUNTRY_NAMES[code] || code);
  const scope = markets.length ? ` through ${listPhrase(markets)}` : '';
  const needs = waiting
    ? ` ${plural(waiting, 'email')} ${waiting === 1 ? 'is' : 'are'} waiting for you first.`
    : '';
  return `Today I plan to look${scope} for new buyers.${needs}`;
}

function currentPicture(summary, waiting) {
  const sales = summary?.sales || {};
  const buyers = Number(sales.leads_found) || 0;
  const replies = Number(sales.replies) || 0;
  const sent = Number(sales.emails_sent) || 0;
  const parts = [];
  if (buyers) parts.push(`${plural(buyers, 'buyer')} ${buyers === 1 ? 'is' : 'are'} in your workspace`);
  if (sent) parts.push(`${plural(sent, 'email')} ${sent === 1 ? 'has' : 'have'} been sent`);
  if (replies) parts.push(`${plural(replies, 'reply', 'replies')} ${replies === 1 ? 'has' : 'have'} come back`);

  const base = parts.length
    ? `Here is the current picture: ${listPhrase(parts)}.`
    : "There isn't any buyer activity to summarize yet.";
  if (!waiting) return base;
  return `${base} ${plural(waiting, 'email')} ${waiting === 1 ? 'is' : 'are'} waiting for your review.`;
}

function latestActivityHeading(activities) {
  const latest = activities[0]?.at;
  if (!latest) return 'Latest recorded activity';
  const value = typeof latest === 'number' && latest < 100000000000 ? latest * 1000 : latest;
  const age = Date.now() - new Date(value).getTime();
  return age <= 36 * 60 * 60 * 1000 ? 'Since yesterday' : 'Latest recorded activity';
}

function workCapability(health) {
  if (config.mode === 'mock') return true;
  return health?.agent_runs_enabled === true;
}

export async function mount(root, ctx) {
  root.classList.add('ifz-page--today');
  const host = el('div', { class: 'ifz-today' });
  root.append(host);
  host.append(el('div', {
    class: 'ifz-today-loading',
    role: 'status',
    'aria-label': 'Loading today',
  },
  el('span', { class: 'ifz-today-loading-line wide' }),
  el('span', { class: 'ifz-today-loading-line' }),
  el('span', { class: 'ifz-today-loading-block' })));

  let disposed = false;
  let summary = null;
  let activities = [];
  let health = null;
  let digest = null;
  let mailboxConnected = false;
  let mailboxKnown = false;
  let work = {
    busy: false,
    sentence: '',
    tone: 'info',
    runId: null,      // the run a Stop press should cancel
    stopping: false,  // Stop pressed, cancellation in flight
    stopped: false,   // the chain must unwind at its next checkpoint
  };

  async function refreshData() {
    const results = await Promise.all([
      call('dashboard.summary'),
      call('activity.list', { query: { limit: 12 } }),
      call('messages.list'),
      call('leads.list'),
      call('contacts.list'),
      call('agentRuns.list'),
      call('products.list'),
      call('onboarding.status'),
      call('emailIntegrations.list').catch(() => null),
      hermesApi('/health').catch(() => null),
      // Only read what the scheduler already wrote — never refresh=true here,
      // or the page would manufacture a briefing about work that never ran.
      call('activity.digest').catch(() => null),
    ]);
    if (disposed) return;
    summary = results[0];
    activities = (results[1]?.items || [])
      .slice()
      .sort((a, b) => new Date(b.at || 0) - new Date(a.at || 0))
      .slice(0, 4);
    const integrations = results[8];
    mailboxKnown = integrations !== null;
    mailboxConnected = Boolean(integrations?.items?.some(integration =>
      ['active', 'connected', 'verified'].includes(integration.status)));
    health = results[9];
    digest = results[10];
  }

  function updateWork(run) {
    if (disposed) return;
    const sentence = runSentence(run);
    if (sentence === work.sentence) return;
    work = { ...work, sentence, tone: run?.status === 'failed' ? 'error' : 'info' };
    render();
  }

  async function finishRun(run) {
    // Remember which run is in flight so Stop has something to cancel.
    work.runId = typeof run === 'string' ? run : run?.run_id || run?.id || null;
    try {
      const completed = await waitForRun(run, {
        timeoutMs: 120000,
        intervalMs: 800,
        onUpdate: updateWork,
      });
      if (completed?.status === 'cancelled' || work.stopped) throw new Error('work_stopped');
      if (!TERMINAL_OK.has(completed?.status)) throw new Error('work_failed');
      return completed;
    } finally {
      work.runId = null;
    }
  }

  /* Stop is a control, not a log: cancel the run that is actually in flight and
     let the chain unwind at its next checkpoint. Nothing is ever sent by this
     work, so stopping is always safe. */
  async function stopTodayWork() {
    if (!work.busy || work.stopping) return;
    const runId = work.runId;
    work = { ...work, stopping: true, stopped: true, sentence: 'Stopping…', tone: 'warning' };
    render();
    try {
      if (runId) await call('agentRuns.cancel', { params: { runId } });
    } catch {
      // Already finished or not cancellable — the chain still unwinds below.
    }
  }

  function checkStopped() {
    if (work.stopped) throw new Error('work_stopped');
  }

  async function startTodayWork() {
    if (work.busy || !summary) return;
    const countries = summary.selected_countries || [];
    if (!countries.length) {
      ctx.navigate('/app/setup?section=markets');
      return;
    }
    if (!workCapability(health)) return;

    work = {
      busy: true,
      tone: 'info',
      sentence: 'Getting the buyer search ready.',
      runId: null,
      stopping: false,
      stopped: false,
    };
    render();

    try {
      const scan = await call('leadScans.create', {
        body: {
          name: `Buyer search — ${listPhrase(selectedCountryNames(summary))}`,
          countries,
          depth: 'standard',
          sources: ['web'],
          products: (db.products || []).slice(0, 3).map(product => product.id),
          industries: [],
          leads_per_country: 4,
          contact_discovery_enabled: false,
          outreach_generation_enabled: false,
        },
      });
      const scanRun = await call('leadScans.start', { params: { scanId: scan.id } });
      await finishRun(scanRun);

      const scanResults = await call('leadScans.results', { params: { scanId: scan.id } });
      const buyers = (scanResults?.items || [])
        .filter(lead => !lead.do_not_contact && lead.status !== 'do_not_contact')
        .sort((a, b) => scoreValue(b) - scoreValue(a));

      if (!buyers.length) {
        await refreshData();
        work = {
          busy: false,
          tone: 'warning',
          sentence: "I finished the search but didn't find a buyer that matched your setup.",
        };
        render();
        return;
      }

      checkStopped();
      const shortlist = buyers.slice(0, MAX_EMAILS_PER_SEARCH);
      work = {
        ...work,
        busy: true,
        tone: 'info',
        sentence: `Learning enough about ${plural(shortlist.length, 'buyer')} to write useful emails.`,
      };
      render();
      await Promise.all(shortlist.map(async (lead) => {
        const run = await call('leads.research', { params: { leadId: lead.id } });
        await finishRun(run);
      }));

      checkStopped();
      work = {
        ...work,
        busy: true,
        tone: 'info',
        sentence: 'Looking for the right person at each company.',
      };
      render();
      await Promise.all(shortlist.map(async (lead) => {
        const run = await call('contacts.discover', {
          body: {
            lead_id: lead.id,
            buyer_roles: [],
            channels: ['email'],
            max_contacts_per_company: 1,
          },
        });
        await finishRun(run);
      }));

      await call('contacts.list');
      const reachable = shortlist.filter(lead => (db.contacts || []).some(contact =>
        contact.lead_id === lead.id && contact.email && !contact.do_not_contact));
      if (!reachable.length) {
        await refreshData();
        work = {
          busy: false,
          tone: 'warning',
          sentence: `I found ${plural(buyers.length, 'buyer')}, but I couldn't find a usable email contact yet.`,
        };
        render();
        return;
      }

      checkStopped();
      work = {
        ...work,
        busy: true,
        tone: 'info',
        sentence: `Writing ${plural(reachable.length, 'email')} for your review.`,
      };
      render();
      const campaign = await call('campaigns.create', {
        body: {
          name: `Buyer search — ${new Date().toLocaleDateString('en-GB', {
            day: 'numeric',
            month: 'short',
          })}`,
          channel: 'email',
          lead_ids: reachable.map(lead => lead.id),
          language: 'en',
          send_mode: 'create_draft',
        },
      });
      const generated = await call('campaigns.generateMessages', {
        params: { campaignId: campaign.id },
        body: {
          lead_ids: reachable.map(lead => lead.id),
          language: 'en',
        },
      });
      const generationRuns = (generated?.items || [])
        .filter(item => item?.type || item?.run_type || item?.run_id);
      await Promise.all(generationRuns.map(finishRun));

      await refreshData();
      const waiting = reviewCount();
      work = {
        busy: false,
        runId: null,
        stopping: false,
        stopped: false,
        tone: waiting ? 'success' : 'warning',
        sentence: waiting
          ? `${plural(waiting, 'email')} ${waiting === 1 ? 'is' : 'are'} ready for your review. Nothing was sent.`
          : 'The buyer search is complete, but no new email is ready for review yet.',
      };
      render();
    } catch (error) {
      if (disposed) return;
      await refreshData().catch(() => {});
      const wasStopped = work.stopped || error?.message === 'work_stopped';
      work = {
        busy: false,
        runId: null,
        stopping: false,
        stopped: false,
        tone: wasStopped ? 'warning' : 'error',
        sentence: wasStopped
          ? 'Stopped. Anything already found is saved, and nothing was sent.'
          : error?.status === 409
            ? 'This work is blocked by one of your Setup rules. Review your target markets and try again.'
            : "I couldn't finish this work. Anything already completed is still saved, and nothing was sent.",
      };
      render();
    }
  }

  function emptyWorkspace() {
    const countries = selectedCountryNames(summary);
    return el('section', { class: 'ifz-today-empty' },
      el('span', { class: 'ifz-today-kicker' }, 'Your first search'),
      el('h1', {}, "Let's find your first buyers."),
      el('p', {}, countries.length
        ? `Your target markets are ${listPhrase(countries)}. Start a search when you're ready.`
        : 'Tell me which countries you sell to first.'),
      countries.length
        ? actionControl(countries)
        : button('Choose target markets', {
            kind: 'primary',
            icon: 'arrowRight',
            onClick: () => ctx.navigate('/app/setup?section=markets'),
          }));
  }

  function actionControl(countries) {
    const canRun = workCapability(health);
    const action = button(work.busy ? 'Working on it…' : 'Find buyers and write to them', {
      kind: 'primary',
      icon: work.busy ? null : 'search',
      disabled: work.busy || !canRun,
      onClick: startTodayWork,
      title: canRun ? null : 'Buyer search is not available in this workspace yet',
    });
    action.setAttribute('aria-busy', work.busy ? 'true' : 'false');
    // While work is in flight the user keeps control: stopping is always safe
    // because this chain never sends anything.
    const stop = work.busy
      ? button(work.stopping ? 'Stopping…' : 'Stop', {
          icon: work.stopping ? null : 'ban',
          disabled: work.stopping,
          onClick: stopTodayWork,
        })
      : null;
    return el('div', { class: 'ifz-today-action-control' },
      el('div', { class: 'ifz-today-action-buttons' }, action, stop),
      el('span', {}, countries.length ? listPhrase(countries) : 'Choose target markets in Setup'),
      el('small', {}, canRun
        ? 'I will find buyers, research the strongest matches, and write up to six emails for review. Nothing will be sent.'
        : 'Buyer search is not available in this workspace yet. Your existing buyers and drafts are still available.'));
  }

  /* The durable answer to "is it working?". In-page state wins while the user is
     here; otherwise fall back to what the server recorded, so live work and a
     failed or stopped run are both still visible after a reload. */
  function lastOutcome() {
    if (work.sentence) return { sentence: '', retry: false };
    const runs = (db.agentRuns || []).slice().sort((a, b) =>
      new Date(b.finished_at || b.created_at || 0) - new Date(a.finished_at || a.created_at || 0));
    const live = runs.find(run => run.status === 'running' || run.status === 'queued');
    if (live) return { sentence: runSentence(live), retry: false, live: true };
    const latest = runs[0];
    if (latest && ['failed', 'cancelled'].includes(latest.status)) {
      return { sentence: runSentence(latest), retry: true, live: false };
    }
    return { sentence: '', retry: false, live: false };
  }

  function render() {
    if (disposed || !summary) return;
    const sales = summary.sales || {};
    const waiting = reviewCount();
    const countries = selectedCountryNames(summary);
    const hasActivity = Number(sales.leads_found) > 0
      || Number(sales.emails_sent) > 0
      || Number(sales.replies) > 0
      || (db.messages || []).length > 0;

    if (!hasActivity) {
      host.replaceChildren(emptyWorkspace());
      return;
    }

    const outcome = lastOutcome();
    const nextSentence = outcome.sentence
      || (workCapability(health)
        ? 'Nothing runs automatically yet. Start the next buyer search when you are ready.'
        : 'Nothing runs automatically in this workspace. Existing buyers and drafts remain available.');

    const mapHost = el('div', {
      class: 'ifz-today-map ifz-minimap',
      'aria-label': `Target markets: ${countries.join(', ') || 'none selected'}`,
    });
    renderMiniMap(mapHost, summary.country_scores || {}, summary.selected_countries || []);

    const mailboxWarning = !mailboxConnected
      ? el('div', { class: 'ifz-today-warning', role: mailboxKnown ? 'status' : 'alert' },
          icon('warning', 17),
          el('span', {}, mailboxKnown
            ? 'Your mailbox is not connected. Emails can be written, but they cannot be delivered.'
            : "I couldn't confirm your mailbox connection."),
          button('Check setup', {
            size: 'sm',
            onClick: () => ctx.navigate('/app/setup?section=mailbox'),
          }))
      : null;

    const reviewPrompt = waiting
      ? el('section', { class: 'ifz-today-review' },
          el('div', {},
            el('span', { class: 'ifz-today-kicker' }, 'Needs you'),
            el('strong', {}, `${plural(waiting, 'email')} ${waiting === 1 ? 'is' : 'are'} waiting for you.`)),
          button('Review', {
            kind: 'primary',
            icon: 'arrowRight',
            onClick: () => ctx.navigate('/app/approvals'),
          }))
      : el('section', { class: 'ifz-today-clear' },
          icon('check', 16),
          el('span', {}, 'No emails are waiting for review.'));

    const workNotice = work.sentence
      ? el('div', {
          class: `ifz-today-work ${work.tone}`,
          role: work.tone === 'error' ? 'alert' : 'status',
          'aria-live': 'polite',
        },
        work.busy ? el('span', { class: 'ifz-spin', 'aria-hidden': 'true' }) : icon(
          work.tone === 'success' ? 'check' : work.tone === 'error' ? 'warning' : 'bolt',
          16,
        ),
        el('span', {}, work.sentence))
      : null;

    const activityList = activities.length
      ? el('div', { class: 'ifz-today-activity-list' }, activities.slice(0, 3).map(activity =>
          el('article', { class: 'ifz-today-activity' },
            el('span', { class: 'ifz-today-activity-mark', 'aria-hidden': 'true' }),
            el('div', {},
              el('p', {}, activitySentence(activity)),
              el('time', { datetime: activity.at }, fmt.ago(activity.at))))))
      : el('p', { class: 'ifz-muted' }, 'No recorded activity yet.');

    const summaryLine = [
      Number(sales.leads_found) > 0 ? plural(Number(sales.leads_found), 'buyer') : null,
      Number(sales.replies) > 0 ? plural(Number(sales.replies), 'reply', 'replies') : null,
      Number(sales.emails_sent) > 0 ? plural(Number(sales.emails_sent), 'sent email') : null,
    ].filter(Boolean).join(' · ');

    host.replaceChildren(...[
      mailboxWarning,
      el('header', { class: 'ifz-today-hero' },
        el('span', { class: 'ifz-today-kicker' }, 'Today'),
        el('h1', {}, greeting()),
        el('p', {}, digestBriefing(digest, waiting) || currentPicture(summary, waiting))),
      reviewPrompt,
      workNotice,
      el('div', { class: 'ifz-today-grid' },
        el('section', { class: 'ifz-today-activity-section' },
          el('span', { class: 'ifz-today-kicker' }, latestActivityHeading(activities)),
          activityList),
        el('aside', { class: 'ifz-today-action' },
          el('span', { class: 'ifz-today-kicker' }, 'Start the next search'),
          el('h2', {}, 'Find buyers and write to them'),
          el('p', {}, countries.length
            ? `Look through ${listPhrase(countries)} using the company information already in Setup.`
            : 'Choose target markets in Setup before starting a buyer search.'),
          countries.length
            ? actionControl(countries)
            : button('Choose target markets', {
                kind: 'primary',
                icon: 'arrowRight',
                onClick: () => ctx.navigate('/app/setup?section=markets'),
              }),
          countries.length ? mapHost : null)),
      el('section', { class: `ifz-today-next${outcome.retry ? ' needs-attention' : ''}` },
        el('span', { class: 'ifz-today-kicker' }, outcome.live ? 'Happening now' : 'Next'),
        el('p', { role: outcome.retry ? 'status' : null }, nextSentence),
        // A run that failed or was stopped stays visible after a reload, with the
        // one action that resolves it.
        outcome.retry
          ? button('Try again', {
              icon: 'refresh',
              disabled: work.busy || !workCapability(health),
              onClick: startTodayWork,
            })
          : null),
      el('footer', { class: 'ifz-today-summary' },
        el('span', {}, summaryLine),
        button('See the numbers', {
          kind: 'ghost',
          icon: 'arrowRight',
          onClick: () => ctx.navigate('/app/analytics'),
        })),
    ].filter(Boolean));
  }

  await refreshData();
  if (!disposed) render();

  return () => {
    disposed = true;
  };
}
