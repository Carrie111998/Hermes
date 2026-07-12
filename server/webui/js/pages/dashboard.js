/* Dashboard home — status conversation with a compact operational snapshot. */

import { el, icon, card, hbarList, timeline, fmt, button, badge, input } from '../ui.js';
import { call, config } from '../api.js';
import { subscribe, db } from '../mocks/db.js';
import { renderMiniMap } from './lead-map.js';
import { COUNTRY_NAMES } from '../catalog.js';
import { isHermesAvailable, askHermes } from '../hermes-client.js';

function workspaceContext(data) {
  const sales = data.sales;
  const running = db.agentRuns.filter(run => run.status === 'running');
  const awaiting = db.messages.filter(message => message.status === 'draft_generated').length;
  const bestMarket = data.market.best_countries[0];
  const marketLine = bestMarket
    ? `Strongest market signal: ${COUNTRY_NAMES[bestMarket.country] || bestMarket.country} (score ${bestMarket.score}).`
    : '';
  const runLine = running.length
    ? `Active run: ${running[0].label} at ${running[0].progress}%.`
    : 'No active agent runs.';
  return [
    '[Workspace snapshot for context]',
    `Leads: ${sales.leads_found}`,
    `Contacts: ${sales.contacts_found}`,
    `Replies: ${sales.replies}`,
    `Approvals pending: ${awaiting}`,
    runLine,
    marketLine,
  ].filter(Boolean).join('\n');
}

function statusReply(question, data) {
  const text = question.toLowerCase();
  const sales = data.sales;
  const running = db.agentRuns.filter(run => run.status === 'running');
  const awaiting = db.messages.filter(message => message.status === 'draft_generated').length;
  const bestMarket = data.market.best_countries[0];

  if (/approv|email|draft/.test(text)) {
    return awaiting
      ? `${awaiting} message${awaiting === 1 ? '' : 's'} are awaiting approval. Review the message queue before anything is sent.`
      : 'There are no messages waiting for approval right now.';
  }
  if (/run|scan|agent|work/.test(text)) {
    return running.length
      ? `${running[0].label} is running now at ${running[0].progress}%. It will remain visible in Agent Runs until it completes.`
      : 'There are no active runs right now. The next useful step is to review market opportunities or start a scan.';
  }
  if (/lead|contact|pipeline/.test(text)) {
    return `The workspace has ${fmt.num(sales.leads_found)} leads and ${fmt.num(sales.contacts_found)} contacts. ${fmt.num(sales.replies)} replies have been recorded so far.`;
  }
  if (/market|countr|where|opportun/.test(text)) {
    const country = bestMarket ? COUNTRY_NAMES[bestMarket.country] || bestMarket.country : 'your selected markets';
    return `${country} is currently the strongest market signal${bestMarket ? `, with an opportunity score of ${bestMarket.score}` : ''}.`;
  }
  return running.length
    ? `Here is the current picture: ${running[0].label} is running, ${awaiting} message${awaiting === 1 ? '' : 's'} await approval, and ${fmt.num(sales.leads_found)} leads are in the workspace. Ask about runs, approvals, leads, or markets.`
    : `Here is the current picture: ${fmt.num(sales.leads_found)} leads are in the workspace, ${awaiting} message${awaiting === 1 ? '' : 's'} await approval, and no agent run is active. Ask about approvals, leads, or markets.`;
}

function homeChat(messages, data, ctx, onSend, hermesConnected) {
  const feed = el('div', {
    class: 'ifz-home-chat-feed',
    role: 'log',
    'aria-live': 'polite',
    'aria-label': 'Workspace status conversation',
  }, messages.map(message =>
    el('div', { class: `ifz-home-chat-message ${message.role}${message.streaming ? ' streaming' : ''}` },
      el('div', { class: 'ifz-home-chat-label' }, message.role === 'assistant' ? 'Hermes Agent' : 'You'),
      el('div', { class: 'ifz-home-chat-copy' }, message.text))));

  const question = input({
    type: 'text',
    placeholder: 'Ask what is happening, what needs approval, or where to focus next…',
    autocomplete: 'off',
  });
  const send = button('Ask', { kind: 'primary', icon: 'arrowRight' });
  const form = el('form', { class: 'ifz-home-chat-composer' }, question, send);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = question.value.trim();
    if (!text) return;
    question.value = '';
    onSend(text);
  });
  send.addEventListener('click', (event) => {
    event.preventDefault();
    const text = question.value.trim();
    if (!text) return;
    question.value = '';
    onSend(text);
  });

  const suggestions = el('div', { class: 'ifz-home-chat-suggestions' },
    button('What is happening?', { size: 'sm', onClick: () => onSend('What is happening?') }),
    button('What needs approval?', { size: 'sm', onClick: () => onSend('What needs approval?') }),
    button('Which market is strongest?', { size: 'sm', onClick: () => onSend('Which market is strongest?') }));

  return card({
    title: 'Ask about your workspace',
    actions: badge(hermesConnected ? 'active' : 'not_connected', hermesConnected ? 'Hermes Agent' : 'Local status assistant'),
    class: 'ifz-home-chat',
    body: el('div', {},
      el('p', { class: 'ifz-home-chat-intro' }, hermesConnected
        ? 'Ask Hermes about current work, approvals, leads, and market signals. Answers stream live from your agent workspace.'
        : 'Get a clear answer about current work, approvals, leads, and market signals.'),
      feed,
      suggestions,
      form,
      el('div', { class: 'ifz-hint ifz-mt-2' },
        hermesConnected
          ? 'Connected to the Hermes Agent backend. Questions use your configured model and workspace.'
          : 'This is local status guidance until the Hermes Agent backend is available.')),
  });
}

export async function mount(root, ctx) {
  const host = el('div', {});
  root.append(host);
  await Promise.all([
    call('onboarding.status'),
    call('messages.list'),
    call('agentRuns.list'),
  ]);
  let disposed = false;
  let hermesConnected = false;
  let chatBusy = false;
  let chatAbort = null;
  let streamRenderPending = null;
  const messages = [{
    role: 'assistant',
    text: 'I can summarize current activity, pending approvals, lead progress, and market opportunities. What would you like to know?',
  }];

  // The server injects chatEnabled into index.html; /health confirms that the
  // same process has the bridge mounted before the widget opens a session.
  hermesConnected = config.chatEnabled ? await isHermesAvailable() : false;
  if (disposed) return () => {};
  if (hermesConnected) {
    messages[0].text = 'I am connected to your Hermes Agent workspace. Ask about current activity, pending approvals, lead progress, or market opportunities.';
  }

  async function render() {
    const data = await call('dashboard.summary');
    if (disposed) return;
    const composerDraft = host.querySelector('.ifz-home-chat-composer .ifz-input')?.value || '';
    const s = data.sales;
    const awaiting = db.messages.filter(m => m.status === 'draft_generated').length;

    const banner = db.onboarding.status !== 'complete'
      ? el('div', { class: 'ifz-dash-banner' },
          icon('sparkle', 18),
          el('div', { class: 'ifz-dash-banner-text' },
            el('b', {}, `Onboarding step ${db.onboarding.current_step + 1} of ${db.onboarding.steps.length}: `),
            `${db.onboarding.steps[db.onboarding.current_step]?.label || 'Review'} — a complete profile makes the agent noticeably sharper.`),
          button('Continue setup', { kind: 'primary', size: 'sm', onClick: () => ctx.navigate('/app/onboarding') }))
      : null;

    const mapHost = el('div', { class: 'ifz-minimap' });
    renderMiniMap(mapHost, data.country_scores, data.selected_countries);

    const running = db.agentRuns.filter(r => r.status === 'running');
    const liveStrip = running.length
      ? el('div', { class: 'ifz-card ifz-live-strip ifz-mb-4' },
          badge('running', `${running.length} agent run${running.length > 1 ? 's' : ''} in progress`),
          el('span', { class: 'ifz-muted ifz-small ifz-live-strip-main' }, running[0].label),
          button('Watch live', { size: 'sm', kind: 'primary', onClick: () => ctx.navigate(`/app/agent-runs/${running[0].id}`) }))
      : null;

    const sendQuestion = async (question) => {
      if (chatBusy) return;
      messages.push({ role: 'user', text: question });
      const assistantIdx = messages.length;
      messages.push({
        role: 'assistant',
        text: hermesConnected ? 'Thinking…' : statusReply(question, data),
        streaming: hermesConnected,
      });
      await render();
      if (!hermesConnected) return;

      chatBusy = true;
      chatAbort = new AbortController();
      try {
        const answer = await askHermes(question, {
          context: workspaceContext(data),
          signal: chatAbort.signal,
          onToken: (text) => {
            messages[assistantIdx].text = text || 'Thinking…';
            messages[assistantIdx].streaming = true;
            if (streamRenderPending) return;
            streamRenderPending = setTimeout(() => {
              streamRenderPending = null;
              render().catch(console.error);
            }, 80);
          },
        });
        messages[assistantIdx].text = answer || 'No response from Hermes Agent.';
        messages[assistantIdx].streaming = false;
      } catch (err) {
        if (err?.message !== 'aborted') {
          messages[assistantIdx].text = err?.message
            ? `Hermes Agent error: ${err.message}`
            : 'Hermes Agent is unavailable right now. Try again in a moment.';
        }
        messages[assistantIdx].streaming = false;
      } finally {
        if (streamRenderPending) {
          clearTimeout(streamRenderPending);
          streamRenderPending = null;
        }
        chatBusy = false;
        chatAbort = null;
        await render();
      }
    };
    const snapshot = card({
      title: 'Workspace snapshot',
      body: el('div', { class: 'ifz-home-snapshot' },
        el('div', { class: 'ifz-home-metrics' },
          metric('Leads', fmt.num(s.leads_found)),
          metric('Approvals', fmt.num(awaiting)),
          metric('Replies', fmt.num(s.replies)),
          metric('Active runs', fmt.num(running.length))),
        el('div', { class: 'ifz-home-section-label' }, 'Next best action'),
        el('div', {}, data.recommended_actions.slice(0, 2).map(action =>
          el('div', { class: 'ifz-actionrow' },
            el('span', { class: 'ifz-actionrow-icon' }, icon(action.icon, 15)),
            el('div', { class: 'ifz-actionrow-body' },
              el('div', { class: 'ifz-actionrow-title' }, action.title),
              el('div', { class: 'ifz-actionrow-sub' }, action.sub)),
            button('Open', { size: 'sm', onClick: () => ctx.navigate(action.href) })))),
      ),
    });
    const home = el('div', { class: 'ifz-home-layout' },
      homeChat(messages, data, ctx, sendQuestion, hermesConnected),
      snapshot);
    const lower = el('div', { class: 'ifz-home-lower' },
      card({
        title: 'Market signal',
        actions: button('Open map', { size: 'sm', onClick: () => ctx.navigate('/app/lead-map') }),
        body: el('div', { class: 'ifz-home-market' },
          mapHost,
          el('div', {}, hbarList(data.market.best_countries.slice(0, 4).map(c => ({
            label: COUNTRY_NAMES[c.country] || c.country,
            value: c.score,
          }))))),
      }),
      card({
        title: 'Recent agent activity',
        actions: button('All runs', { size: 'sm', onClick: () => ctx.navigate('/app/agent-runs') }),
        body: timeline(data.recent_activity.slice(0, 4).map(a => ({
          label: a.label,
          time: fmt.ago(a.at),
          tone: a.kind === 'reply' ? 'success' : a.kind === 'agent' ? 'accent' : '',
        }))),
      }));

    host.replaceChildren(banner || '', liveStrip || '', home, lower);
    if (composerDraft) {
      const composerInput = host.querySelector('.ifz-home-chat-composer .ifz-input');
      if (composerInput) composerInput.value = composerDraft;
    }
  }

  await render();
  // Throttled live refresh — run logs emit frequently during scans.
  let pending = null;
  const unsub = subscribe('*', () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; render().catch(console.error); }, 1500);
  });
  return () => {
    disposed = true;
    unsub();
    if (pending) clearTimeout(pending);
    if (streamRenderPending) clearTimeout(streamRenderPending);
    if (chatAbort) chatAbort.abort();
  };
}

function metric(label, value) {
  return el('div', { class: 'ifz-home-metric' },
    el('div', { class: 'ifz-overline' }, label),
    el('div', { class: 'ifz-home-metric-value' }, value));
}
